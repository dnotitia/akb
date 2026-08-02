"""Durable, guarded public File storage protocol for the M1 W4 measurement.

The database, not a backend process, is the authority for pending transfers,
opaque capabilities and confirmed File visibility.  CAS is deliberately
conservative on logical delete: no shared digest is ever removed by a File
delete; a later run-owned reaper can retire proven-unreferenced objects.
"""

from __future__ import annotations

import base64
import hashlib
import secrets
import uuid
from collections.abc import Awaitable, Callable
from pathlib import Path

from app.config import NATIVE_REVISION_M1_MEASUREMENT_DATABASE_NAME, settings
from app.db.postgres import get_pool
from app.exceptions import AKBError, NotFoundError
from app.repositories import vault_files_repo
from app.repositories.document_repo import CollectionRepository
from app.services.adapters import s3_adapter
from app.services.m1_binary_store import BinaryStore, FilesystemCAS, S3CAS
from app.services.resource_hash import HASH_ALGORITHM, is_sha256_hex
from app.services.uri_service import file_uri
from app.util.text import normalize_collection_path


TRANSFER_TTL_SECONDS = 3600

# Migration 050 owns concrete native text publication tables.  The integrating
# native service registers this File-identity seam instead of 051 creating a
# competing text ledger.
NativeTextFilePublisher = Callable[[object, uuid.UUID, str, str, bytes, str], Awaitable[None]]
_native_text_file_publisher: NativeTextFilePublisher | None = None
NativeTextFileOpener = Callable[[uuid.UUID, str, str], Awaitable[bytes]]
_native_text_file_opener: NativeTextFileOpener | None = None


def register_native_text_file_publisher(publisher: NativeTextFilePublisher) -> None:
    global _native_text_file_publisher
    _native_text_file_publisher = publisher


def register_native_text_file_opener(opener: NativeTextFileOpener) -> None:
    global _native_text_file_opener
    _native_text_file_opener = opener


def _is_native_text(mime_type: str, data: bytes) -> bool:
    if not mime_type.lower().startswith("text/") or b"\x00" in data:
        return False
    try:
        data.decode("utf-8")
    except UnicodeDecodeError:
        return False
    return True


def _token_digest(token: str) -> str:
    return hashlib.sha256(token.encode("ascii")).hexdigest()


def _new_token() -> str:
    # Only the SHA-256 digest is stored.  The raw bearer capability is returned
    # once and never included in an event or measurement receipt.
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
    """DB-backed implementation behind the existing public File endpoints."""

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

    async def _cleanup_expired(self, conn) -> None:
        # An untransferred row has no CAS bytes, so this is safe, bounded
        # cleanup.  Confirmed rows never match and are never bulk-deleted.
        await conn.execute(
            """
            DELETE FROM vault_files vf
             USING m1_file_transfer_intents ti
             WHERE vf.id = ti.file_id
               AND vf.storage_state = 'pending'
               AND ti.expires_at <= NOW()
               AND ti.consumed_at IS NULL
            """
        )

    async def initiate_upload(
        self, *, vault_name: str, vault_id: uuid.UUID, collection: str,
        filename: str, actor_id: str, mime_type: str, description: str,
        content_hash: str | None,
    ) -> dict:
        if content_hash is not None and not is_sha256_hex(content_hash):
            raise AKBError("content_hash must be a lowercase sha256 hex digest", status_code=400)
        token = _new_token()
        file_id = uuid.uuid4()
        collection_path = normalize_collection_path(collection)
        pool = await get_pool()
        async with pool.acquire() as conn:
            async with conn.transaction():
                await self._cleanup_expired(conn)
                if content_hash:
                    existing = await conn.fetchrow(
                        """
                        SELECT id FROM vault_files
                         WHERE vault_id = $1 AND name = $2 AND content_hash = $3
                           AND storage_state = 'confirmed'
                         ORDER BY created_at ASC LIMIT 1
                        """, vault_id, filename, content_hash,
                    )
                    if existing:
                        # Preserve the historic proxy flow: it will still PUT
                        # then confirm.  This one-use intent accepts that PUT
                        # as a harmless no-op against the confirmed identity.
                        await conn.execute(
                            """
                            INSERT INTO m1_file_transfer_intents (id, file_id, vault_id, method, token_digest, expires_at)
                            VALUES ($1, $2, $3, 'PUT', $4, NOW() + ($5 * INTERVAL '1 second'))
                            """, uuid.uuid4(), existing["id"], vault_id, _token_digest(token), TRANSFER_TTL_SECONDS,
                        )
                        return {
                            "kind": "file", "uri": file_uri(vault_name, str(existing["id"]), collection=collection_path or None),
                            "vault": vault_name, "collection": collection_path or None,
                            "upload_url": self._url(token), "s3_key": None,
                            "expires_in": TRANSFER_TTL_SECONDS, "deduplicated": True,
                        }
                collection_id = None
                if collection_path:
                    collection_id = await CollectionRepository(pool).get_or_create(vault_id, collection_path, conn=conn)
                await vault_files_repo.insert_measurement_pending(
                    conn, file_id=file_id, vault_id=vault_id, collection_id=collection_id,
                    name=filename, mime_type=mime_type, description=description, created_by=actor_id,
                )
                await conn.execute(
                    """
                    INSERT INTO m1_file_transfer_intents (id, file_id, vault_id, method, token_digest, expires_at)
                    VALUES ($1, $2, $3, 'PUT', $4, NOW() + ($5 * INTERVAL '1 second'))
                    """, uuid.uuid4(), file_id, vault_id, _token_digest(token), TRANSFER_TTL_SECONDS,
                )
        return {
            "kind": "file", "uri": file_uri(vault_name, str(file_id), collection=collection_path or None),
            "vault": vault_name, "collection": collection_path or None,
            "upload_url": self._url(token), "s3_key": None,
            "expires_in": TRANSFER_TTL_SECONDS, "deduplicated": False,
        }

    async def transfer(self, token: str, *, method: str, body: bytes | None = None) -> bytes | None:
        if method not in {"PUT", "GET"}:
            raise AKBError("measurement transfer method is not allowed", status_code=405)
        pool = await get_pool()
        async with pool.acquire() as conn:
            async with conn.transaction():
                intent = await conn.fetchrow(
                    """
                    UPDATE m1_file_transfer_intents
                       SET consumed_at = NOW(), body = CASE WHEN method = 'PUT' THEN $2 ELSE body END
                     WHERE token_digest = $1 AND method = $3
                       AND consumed_at IS NULL AND expires_at > NOW()
                    RETURNING file_id, vault_id, method
                    """, _token_digest(token), body, method,
                )
                if not intent:
                    raise AKBError("measurement transfer capability is invalid, expired, or already used", status_code=403)
                if method == "PUT":
                    return None
                row = await vault_files_repo.find_measurement_by_id(conn, intent["vault_id"], intent["file_id"])
                if not row or row["storage_state"] != "confirmed":
                    raise NotFoundError("File", str(intent["file_id"]))
        if row["storage_driver"] == "native_text":
            if _native_text_file_opener is None:
                raise AKBError("native text File opener is not registered", status_code=503)
            return await _native_text_file_opener(row["vault_id"], str(row["id"]), row["content_hash"])
        prepared = _prepared_from_row(row)
        return _store().open_verified(str(row["vault_id"]), prepared)

    async def confirm_upload(self, vault_id: uuid.UUID, file_id: str, *, content_hash: str | None) -> dict:
        if content_hash is not None and not is_sha256_hex(content_hash):
            raise AKBError("content_hash must be a lowercase sha256 hex digest", status_code=400)
        fid = uuid.UUID(file_id)
        pool = await get_pool()
        async with pool.acquire() as conn:
            row = await vault_files_repo.find_measurement_by_id(conn, vault_id, fid)
            if not row:
                raise NotFoundError("File", file_id)
            if row["storage_state"] == "confirmed":
                return _confirmed_response(row)
            intent = await conn.fetchrow(
                "SELECT body FROM m1_file_transfer_intents WHERE file_id = $1 AND vault_id = $2 AND method = 'PUT'", fid, vault_id,
            )
            if not intent or intent["body"] is None:
                raise AKBError("Upload not found in storage", status_code=404)
            data = bytes(intent["body"])
        digest = hashlib.sha256(data).hexdigest()
        if content_hash is not None and content_hash != digest:
            raise AKBError("Uploaded file hash mismatch", status_code=409)
        is_text = _is_native_text(row["mime_type"], data)
        if is_text:
            if _native_text_file_publisher is None:
                raise AKBError("native text File publisher is not registered", status_code=503)
            prepared = None
        else:
            prepared = _store().prepare_verified(str(vault_id), data, digest, len(data))
        # CAS succeeds first.  If this durable logical publication fails, the
        # bytes stay an unreachable CAS object; no public File is exposed.
        async with pool.acquire() as conn:
            async with conn.transaction():
                if is_text:
                    assert _native_text_file_publisher is not None
                    await _native_text_file_publisher(conn, vault_id, file_id, row["mime_type"], data, digest)
                updated = await vault_files_repo.confirm_measurement_file(
                    conn, file_id=fid, vault_id=vault_id,
                    locator=prepared.locator if prepared else f"native-text/{fid}",
                    driver="native_text" if is_text else self.driver,
                    digest=digest, size_bytes=len(data),
                )
                if not updated:
                    raise NotFoundError("File", file_id)
                await conn.execute("UPDATE m1_file_transfer_intents SET body = NULL WHERE file_id = $1", fid)
                row = await vault_files_repo.find_measurement_by_id(conn, vault_id, fid)
        assert row is not None
        return _confirmed_response(row)

    async def get_download_url(self, vault_id: uuid.UUID, file_id: str) -> dict:
        fid = uuid.UUID(file_id)
        token = _new_token()
        pool = await get_pool()
        async with pool.acquire() as conn:
            async with conn.transaction():
                row = await vault_files_repo.find_measurement_by_id(conn, vault_id, fid)
                if not row or row["storage_state"] != "confirmed":
                    raise NotFoundError("File", file_id)
                await conn.execute(
                    """
                    INSERT INTO m1_file_transfer_intents (id, file_id, vault_id, method, token_digest, expires_at)
                    VALUES ($1, $2, $3, 'GET', $4, NOW() + ($5 * INTERVAL '1 second'))
                    """, uuid.uuid4(), fid, vault_id, _token_digest(token), TRANSFER_TTL_SECONDS,
                )
        return {**_confirmed_response(row), "download_url": self._url(token), "expires_in": TRANSFER_TTL_SECONDS}

    async def list_files(self, vault_id: uuid.UUID, vault_name: str, collection: str | None, limit: int) -> list[dict]:
        pool = await get_pool()
        async with pool.acquire() as conn:
            rows = await vault_files_repo.list_measurement_confirmed(conn, vault_id, collection=collection, limit=limit)
        return [{**_confirmed_response(row), "uri": file_uri(vault_name, str(row["id"]), collection=row["collection"])} for row in rows]

    async def delete(self, vault_id: uuid.UUID, file_id: str) -> dict:
        fid = uuid.UUID(file_id)
        pool = await get_pool()
        async with pool.acquire() as conn:
            async with conn.transaction():
                row = await vault_files_repo.find_measurement_by_id(conn, vault_id, fid)
                if not row or row["storage_state"] != "confirmed":
                    raise NotFoundError("File", file_id)
                await conn.execute("DELETE FROM m1_file_transfer_intents WHERE file_id = $1", fid)
                await vault_files_repo.delete(conn, fid)
        # Deliberately no CAS delete: digest ownership is shared and deletion
        # is conservative until a durable refcount/reaper owns it.
        return {"kind": "file", "vault": str(vault_id), "name": row["name"], "deleted": True}


def _prepared_from_row(row):
    from app.services.m1_binary_store import PreparedBinary
    return PreparedBinary(row["storage_locator"], row["content_hash"], row["size_bytes"], "s3" if row["storage_driver"] == "s3cas" else "fscas")


def _confirmed_response(row: dict) -> dict:
    return {
        "kind": "file", "name": row["name"], "collection": row.get("collection"), "mime_type": row["mime_type"],
        "size_bytes": row["size_bytes"], "content_hash": row["content_hash"], "hash_algorithm": HASH_ALGORITHM,
        "storage_driver": row["storage_driver"],
    }
