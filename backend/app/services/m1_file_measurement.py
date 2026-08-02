"""Durable guarded public File protocol for the M1 W4 storage measurement."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import secrets
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path

from app.config import NATIVE_REVISION_M1_MEASUREMENT_DATABASE_NAME, settings
from app.db.postgres import get_pool
from app.exceptions import AKBError, NotFoundError, ValidationError
from app.repositories import vault_files_repo
from app.repositories.document_repo import CollectionRepository
from app.services.adapters import s3_adapter
from app.services.m1_binary_store import BinaryStore, FilesystemCAS, PreparedBinary, S3CAS
from app.services.resource_hash import HASH_ALGORITHM, is_sha256_hex
from app.services.uri_service import file_uri
from app.util.text import normalize_collection_path, validate_file_name


TRANSFER_TTL_SECONDS = 3600
TRANSFER_REAP_BATCH_SIZE = 256


@dataclass(frozen=True, slots=True)
class NativeTextPublication:
    resource_id: uuid.UUID
    revision_id: str
    digest: str
    size_bytes: int


@dataclass(frozen=True, slots=True)
class NativeTextPublishRequest:
    vault_id: uuid.UUID
    file_id: uuid.UUID
    logical_path: str
    mime_type: str
    data: bytes
    digest: str
    actor_id: str
    description: str


NativeTextPublisher = Callable[[object, NativeTextPublishRequest], Awaitable[NativeTextPublication]]
NativeTextOpener = Callable[[uuid.UUID, uuid.UUID, str], Awaitable[bytes]]


@dataclass(frozen=True, slots=True)
class NativeTextDeleteRequest:
    vault_id: uuid.UUID
    resource_id: uuid.UUID
    revision_id: str
    logical_path: str
    actor_id: str


NativeTextDeleter = Callable[[object, NativeTextDeleteRequest], Awaitable[None]]

_native_text_publisher: NativeTextPublisher | None = None
_native_text_opener: NativeTextOpener | None = None
_native_text_deleter: NativeTextDeleter | None = None


def register_native_text_file_services(
    *, publisher: NativeTextPublisher, opener: NativeTextOpener, deleter: NativeTextDeleter,
) -> None:
    global _native_text_publisher, _native_text_opener, _native_text_deleter
    _native_text_publisher = publisher
    _native_text_opener = opener
    _native_text_deleter = deleter


def reset_native_text_file_services_for_tests() -> None:
    global _native_text_publisher, _native_text_opener, _native_text_deleter
    _native_text_publisher = None
    _native_text_opener = None
    _native_text_deleter = None


async def _tombstone_native_text_file(
    conn: object,
    *,
    storage_driver: str | None,
    vault_id: uuid.UUID,
    native_resource_id: uuid.UUID | None,
    native_revision_id: str | None,
    collection: str | None,
    name: str,
    actor_id: str,
) -> bool:
    """Publish a native File tombstone on the caller-owned transaction."""
    if storage_driver != "native_text":
        return False
    if _native_text_deleter is None or native_resource_id is None or native_revision_id is None:
        raise AKBError("native text File delete service is not registered", status_code=503)
    logical_path = f"{collection}/{name}" if collection else name
    await _native_text_deleter(
        conn,
        NativeTextDeleteRequest(
            vault_id=vault_id,
            resource_id=native_resource_id,
            revision_id=native_revision_id,
            logical_path=logical_path,
            actor_id=actor_id,
        ),
    )
    return True


def _is_native_text(mime_type: str, data: bytes) -> bool:
    if not mime_type.lower().startswith("text/"):
        return False
    if b"\x00" in data:
        raise ValidationError("declared text must be valid UTF-8 text without NUL bytes")
    try:
        data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValidationError(
            "declared text must be valid UTF-8 text without NUL bytes"
        ) from exc
    return True


def _token_digest(token: str) -> str:
    return hashlib.sha256(token.encode("ascii")).hexdigest()


def _sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _new_token() -> str:
    return base64.urlsafe_b64encode(secrets.token_bytes(32)).rstrip(b"=").decode("ascii")


def _store() -> BinaryStore:
    if not settings.native_revision_m1_measurement_only or settings.db_name != NATIVE_REVISION_M1_MEASUREMENT_DATABASE_NAME:
        raise RuntimeError("M1 File CAS requires the dedicated measurement guard")
    if settings.native_revision_m1_file_driver == "fscas":
        return FilesystemCAS(Path(settings.native_revision_m1_file_fscas_root))
    if settings.native_revision_m1_file_driver == "s3cas":
        return S3CAS(settings.s3_bucket, s3_adapter.client())
    raise RuntimeError("M1 File CAS driver must be fscas or s3cas")


def measurement_enabled() -> bool:
    return settings.native_revision_m1_file_driver != "s3_current"


class MeasurementFileService:
    """Stateless service: PostgreSQL is the only transfer/logical authority."""

    def __init__(self) -> None:
        if not measurement_enabled():
            raise RuntimeError("M1 File measurement is not enabled")
        if not settings.public_base_url:
            raise RuntimeError("M1 File CAS requires public_base_url")
        self._base_url = settings.public_base_url.rstrip("/")

    @property
    def driver(self) -> str:
        return settings.native_revision_m1_file_driver

    def _url(self, token: str) -> str:
        return f"{self._base_url}/api/v1/files/transfer/{token}"

    async def reap_transfer_intents(self, conn=None) -> int:
        """Delete one bounded batch of expired tokens and staging bodies."""
        if conn is not None:
            result = await conn.execute(
                """
                WITH expired AS (
                    SELECT id
                      FROM m1_file_transfer_intents
                     WHERE expires_at <= NOW()
                     ORDER BY expires_at, id
                     LIMIT $1
                     FOR UPDATE SKIP LOCKED
                )
                DELETE FROM m1_file_transfer_intents AS intent
                 USING expired
                 WHERE intent.id = expired.id
                """,
                TRANSFER_REAP_BATCH_SIZE,
            )
            return int(result.rsplit(" ", 1)[-1])
        pool = await get_pool()
        async with pool.acquire() as acquired:
            return await self.reap_transfer_intents(acquired)

    async def initiate_upload(
        self, *, vault_name: str, vault_id: uuid.UUID, collection: str,
        filename: str, actor_id: str, mime_type: str, description: str,
        content_hash: str | None,
    ) -> dict:
        if content_hash is not None and not is_sha256_hex(content_hash):
            raise AKBError("content_hash must be a lowercase sha256 hex digest", status_code=400)
        try:
            validate_file_name(filename)
        except ValueError as exc:
            raise ValidationError(str(exc)) from exc
        token = _new_token()
        requested_file_id = uuid.uuid4()
        collection_path = normalize_collection_path(collection)
        pool = await get_pool()
        async with pool.acquire() as conn:
            async with conn.transaction():
                await self.reap_transfer_intents(conn)
                collection_id = None
                if collection_path:
                    collection_id = await CollectionRepository(pool).get_or_create(
                        vault_id, collection_path, conn=conn,
                    )
                existing = None
                if content_hash:
                    existing = await conn.fetchrow(
                        """
                        SELECT vf.id, c.path AS collection
                          FROM vault_files vf LEFT JOIN collections c ON c.id = vf.collection_id
                         WHERE vf.vault_id = $1
                           AND vf.collection_id IS NOT DISTINCT FROM $2
                           AND vf.name = $3 AND vf.content_hash = $4
                           AND vf.storage_driver IS NOT NULL
                         ORDER BY vf.created_at ASC LIMIT 1
                        """, vault_id, collection_id, filename, content_hash,
                    )
                file_id = existing["id"] if existing else requested_file_id
                actual_collection = existing["collection"] if existing else collection_path or None
                await conn.execute(
                    """
                    INSERT INTO m1_file_transfer_intents (
                        id, file_id, vault_id, collection_id, method, filename,
                        mime_type, description, actor_id, declared_content_hash,
                        token_digest, expires_at
                    ) VALUES (
                        $1, $2, $3, $4, 'PUT', $5, $6, $7, $8, $9, $10,
                        NOW() + ($11 * INTERVAL '1 second')
                    )
                    """,
                    uuid.uuid4(), file_id, vault_id, collection_id, filename,
                    mime_type, description, actor_id, content_hash,
                    _token_digest(token), TRANSFER_TTL_SECONDS,
                )
        return {
            "kind": "file",
            "uri": file_uri(vault_name, str(file_id), collection=actual_collection),
            "file_id": str(file_id),
            "vault": vault_name,
            "collection": actual_collection,
            "upload_url": self._url(token),
            "s3_key": f"m1-transfer/{file_id}",
            "expires_in": TRANSFER_TTL_SECONDS,
            "deduplicated": existing is not None,
        }

    async def transfer(self, token: str, *, method: str, body: bytes | None = None) -> bytes | None:
        if method not in {"PUT", "GET"}:
            raise AKBError("measurement transfer method is not allowed", status_code=405)
        if method == "PUT" and body is None:
            raise AKBError("measurement PUT body is required", status_code=400)
        if body is not None and len(body) > settings.native_revision_m1_file_transfer_max_bytes:
            raise AKBError("measurement transfer exceeds configured size limit", status_code=413)
        actual_digest = await asyncio.to_thread(_sha256_hex, body) if body is not None else None
        mismatch = False
        pool = await get_pool()
        async with pool.acquire() as conn:
            async with conn.transaction():
                intent = await conn.fetchrow(
                    """
                    SELECT * FROM m1_file_transfer_intents
                     WHERE token_digest = $1 AND method = $2
                       AND consumed_at IS NULL AND expires_at > NOW()
                     FOR UPDATE
                    """, _token_digest(token), method,
                )
                if not intent:
                    raise AKBError(
                        "measurement transfer capability is invalid, expired, or already used",
                        status_code=403,
                    )
                if method == "PUT":
                    declared = intent["declared_content_hash"]
                    if declared is not None and declared != actual_digest:
                        await conn.execute("DELETE FROM m1_file_transfer_intents WHERE id = $1", intent["id"])
                        mismatch = True
                    else:
                        await conn.execute(
                            """
                            UPDATE m1_file_transfer_intents
                               SET consumed_at = NOW(), state = 'transferred', body = $2,
                                   actual_content_hash = $3, actual_size = $4
                             WHERE id = $1
                            """, intent["id"], body, actual_digest, len(body or b""),
                        )
                else:
                    row = await vault_files_repo.find_measurement_by_id(
                        conn, intent["vault_id"], intent["file_id"],
                    )
                    if row is None:
                        raise NotFoundError("File", str(intent["file_id"]))
                    await conn.execute("DELETE FROM m1_file_transfer_intents WHERE id = $1", intent["id"])
        if mismatch:
            raise AKBError("Uploaded file hash mismatch; transfer intent was cleaned up", status_code=409)
        if method == "PUT":
            return None
        assert row is not None
        return await self._open_verified(row)

    async def confirm_upload(
        self, vault_id: uuid.UUID, file_id: str, *, content_hash: str | None,
    ) -> dict:
        if content_hash is not None and not is_sha256_hex(content_hash):
            raise AKBError("content_hash must be a lowercase sha256 hex digest", status_code=400)
        fid = uuid.UUID(file_id)
        pool = await get_pool()
        async with pool.acquire() as conn:
            intent = await conn.fetchrow(
                """
                SELECT * FROM m1_file_transfer_intents
                 WHERE file_id = $1 AND vault_id = $2 AND method = 'PUT'
                   AND state = 'transferred' AND expires_at > NOW()
                 ORDER BY created_at DESC LIMIT 1
                """, fid, vault_id,
            )
            existing = await vault_files_repo.find_measurement_by_id(conn, vault_id, fid)
        if intent is None:
            if existing is not None:
                if content_hash is not None and content_hash != existing["content_hash"]:
                    raise AKBError("Confirmed file hash mismatch", status_code=409)
                return await self._response(existing)
            raise NotFoundError("File", file_id)

        data = bytes(intent["body"])
        digest = await asyncio.to_thread(_sha256_hex, data)
        declared = intent["declared_content_hash"]
        if digest != intent["actual_content_hash"] or len(data) != intent["actual_size"]:
            await self._delete_intent(intent["id"])
            raise AKBError("Transferred file failed persisted digest/size verification", status_code=409)
        if (declared is not None and declared != digest) or (content_hash is not None and content_hash != digest):
            await self._delete_intent(intent["id"])
            raise AKBError("Uploaded file hash mismatch; transfer intent was cleaned up", status_code=409)

        try:
            is_text = _is_native_text(intent["mime_type"], data)
        except ValidationError:
            await self._delete_intent(intent["id"])
            raise
        prepared = None
        if not is_text:
            prepared = await asyncio.to_thread(
                lambda: _store().prepare_verified(str(vault_id), data, digest, len(data)),
            )
        async with pool.acquire() as conn:
            async with conn.transaction():
                locked = await conn.fetchrow(
                    "SELECT * FROM m1_file_transfer_intents WHERE id = $1 FOR UPDATE",
                    intent["id"],
                )
                if locked is None:
                    # Another confirm may have adopted this distinct, late
                    # intent into the canonical logical File and deleted the
                    # intent before we acquired its lock.  The preallocated
                    # file id is not canonical in that case; use the already
                    # verified intent identity instead.
                    peer = await vault_files_repo.find_measurement_exact(
                        conn, vault_id=intent["vault_id"], collection_id=intent["collection_id"],
                        name=intent["filename"], digest=digest,
                    )
                    if peer is None:
                        raise NotFoundError("File", file_id)
                    return await self._response(peer)

                # Serialize the exact logical identity before native publish.
                # Two preallocated intents may race before either is confirmed;
                # only the winner may create a native resource/revision.
                identity = "|".join((
                    str(vault_id), str(intent["collection_id"] or "root"),
                    intent["filename"], digest,
                ))
                await conn.execute(
                    "SELECT pg_advisory_xact_lock(hashtextextended($1, 0))", identity,
                )
                peer = await vault_files_repo.find_measurement_exact(
                    conn, vault_id=vault_id, collection_id=intent["collection_id"],
                    name=intent["filename"], digest=digest,
                )
                if peer is not None:
                    await conn.execute(
                        "DELETE FROM m1_file_transfer_intents WHERE id = $1", intent["id"],
                    )
                    return await self._response(peer)

                native = None
                if is_text:
                    if _native_text_publisher is None:
                        raise AKBError("native text File services are not registered", status_code=503)
                    collection_path = await conn.fetchval(
                        "SELECT path FROM collections WHERE id = $1", intent["collection_id"],
                    ) if intent["collection_id"] else None
                    logical_path = (
                        f"{collection_path}/{intent['filename']}"
                        if collection_path else intent["filename"]
                    )
                    native = await _native_text_publisher(
                        conn,
                        NativeTextPublishRequest(
                            vault_id=vault_id, file_id=fid, logical_path=logical_path,
                            mime_type=intent["mime_type"], data=data, digest=digest,
                            actor_id=intent["actor_id"], description=intent["description"] or "",
                        ),
                    )
                    _validate_native_publication(native, fid, digest, len(data))
                if native is not None:
                    driver = "native_text"
                    locator = f"native-text/{native.resource_id}/{native.revision_id}"
                else:
                    assert prepared is not None
                    driver = self.driver
                    locator = prepared.locator
                row = await vault_files_repo.insert_or_adopt_measurement_confirmed(
                    conn,
                    file_id=fid, vault_id=vault_id, collection_id=intent["collection_id"],
                    name=intent["filename"], mime_type=intent["mime_type"],
                    description=intent["description"] or "", created_by=intent["actor_id"],
                    driver=driver, locator=locator,
                    digest=digest, size_bytes=len(data),
                    native_resource_id=native.resource_id if native else None,
                    native_revision_id=native.revision_id if native else None,
                )
                await conn.execute("DELETE FROM m1_file_transfer_intents WHERE id = $1", intent["id"])
        return await self._response(row)

    async def _delete_intent(self, intent_id: uuid.UUID) -> None:
        pool = await get_pool()
        async with pool.acquire() as conn:
            await conn.execute("DELETE FROM m1_file_transfer_intents WHERE id = $1", intent_id)

    async def get_download_url(self, vault_id: uuid.UUID, file_id: str) -> dict:
        fid = uuid.UUID(file_id)
        token = _new_token()
        pool = await get_pool()
        async with pool.acquire() as conn:
            async with conn.transaction():
                row = await vault_files_repo.find_measurement_by_id(conn, vault_id, fid)
                if row is None:
                    raise NotFoundError("File", file_id)
                await conn.execute(
                    """
                    INSERT INTO m1_file_transfer_intents (
                        id, file_id, vault_id, method, token_digest, expires_at
                    ) VALUES ($1, $2, $3, 'GET', $4, NOW() + ($5 * INTERVAL '1 second'))
                    """, uuid.uuid4(), fid, vault_id, _token_digest(token), TRANSFER_TTL_SECONDS,
                )
        return {
            **await self._response(row),
            "download_url": self._url(token),
            "expires_in": TRANSFER_TTL_SECONDS,
        }

    async def list_files(
        self, vault_id: uuid.UUID, vault_name: str, collection: str | None, limit: int,
    ) -> list[dict]:
        pool = await get_pool()
        async with pool.acquire() as conn:
            rows = await vault_files_repo.list_measurement_confirmed(
                conn, vault_id, collection=collection, limit=limit,
            )
        return [await self._response(row, vault_name=vault_name) for row in rows]

    async def delete(self, vault_id: uuid.UUID, file_id: str, *, actor_id: str) -> dict:
        fid = uuid.UUID(file_id)
        pool = await get_pool()
        async with pool.acquire() as conn:
            async with conn.transaction():
                row = await vault_files_repo.find_measurement_by_id(conn, vault_id, fid)
                if row is None:
                    raise NotFoundError("File", file_id)
                await _tombstone_native_text_file(
                    conn,
                    storage_driver=row["storage_driver"],
                    vault_id=vault_id,
                    native_resource_id=row["native_resource_id"],
                    native_revision_id=row["native_revision_id"],
                    collection=row["collection"],
                    name=row["name"],
                    actor_id=actor_id,
                )
                canonical_uri = file_uri(row["vault_name"], file_id, collection=row["collection"])
                await conn.execute(
                    "DELETE FROM edges WHERE source_uri = $1 OR target_uri = $1", canonical_uri,
                )
                await conn.execute("DELETE FROM publications WHERE resource_uri = $1", canonical_uri)
                await conn.execute("DELETE FROM m1_file_transfer_intents WHERE file_id = $1", fid)
                await vault_files_repo.delete(conn, fid)
        # Binary CAS is conservatively retained: a digest may be shared by
        # other confirmed logical Files and no refcount guess is made here.
        return {
            "kind": "file",
            "uri": canonical_uri,
            "file_id": file_id, "vault": row["vault_name"],
            "collection": row["collection"], "name": row["name"], "deleted": True,
        }

    async def _open_verified(self, row: dict) -> bytes:
        if row["storage_driver"] == "native_text":
            if _native_text_opener is None or row["native_resource_id"] is None or row["native_revision_id"] is None:
                raise AKBError("native text File open service is not registered", status_code=503)
            data = await _native_text_opener(
                row["vault_id"], row["native_resource_id"], row["native_revision_id"],
            )
            digest = hashlib.sha256(data).hexdigest()
            if digest != row["content_hash"] or len(data) != row["size_bytes"]:
                raise AKBError("native text File failed digest/size verification", status_code=502)
            return data
        prepared = PreparedBinary(
            row["storage_locator"], row["content_hash"], row["size_bytes"],
            "s3" if row["storage_driver"] == "s3cas" else "fscas",
        )
        return await asyncio.to_thread(
            lambda: _store().open_verified(str(row["vault_id"]), prepared),
        )

    async def _response(self, row: dict, *, vault_name: str | None = None) -> dict:
        resolved_vault = vault_name or row.get("vault_name")
        if not resolved_vault:
            pool = await get_pool()
            async with pool.acquire() as conn:
                resolved_vault = await conn.fetchval("SELECT name FROM vaults WHERE id = $1", row["vault_id"])
        return {
            "kind": "file",
            "uri": file_uri(resolved_vault, str(row["id"]), collection=row.get("collection")),
            "file_id": str(row["id"]),
            "vault": resolved_vault,
            "collection": row.get("collection"),
            "name": row["name"],
            "mime_type": row["mime_type"],
            "size_bytes": row["size_bytes"],
            "content_hash": row["content_hash"],
            "hash_algorithm": HASH_ALGORITHM,
            "storage_driver": row["storage_driver"],
            "etag": None,
            "storage_version": None,
            "native_resource_id": str(row["native_resource_id"]) if row.get("native_resource_id") else None,
            "native_revision_id": row.get("native_revision_id"),
        }


def _validate_native_publication(
    result: NativeTextPublication, file_id: uuid.UUID, digest: str, size_bytes: int,
) -> None:
    if not isinstance(result, NativeTextPublication):
        raise AKBError("native text publisher returned no concrete publication", status_code=502)
    if result.resource_id != file_id:
        raise AKBError("native text publisher returned the wrong File identity", status_code=502)
    if result.digest != digest or result.size_bytes != size_bytes:
        raise AKBError("native text publisher returned mismatched digest/size", status_code=502)
    if len(result.revision_id) != 40 or any(char not in "0123456789abcdef" for char in result.revision_id):
        raise AKBError("native text publisher returned an invalid revision", status_code=502)
