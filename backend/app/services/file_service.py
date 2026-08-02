"""File service — S3-backed binary file storage for vaults.

AKB does not store file bytes in the application database. It:
1. Generates presigned URLs for direct client ↔ S3 transfer.
2. Manages file metadata in PostgreSQL (`vault_files_repo`).
3. Streams uploaded object bytes once at confirmation to certify sha256.

Access control inherits from vault permissions.

S3 client lifecycle and low-level primitives (head/get/put/delete,
presigning, error mapping) live in `app.services.adapters.s3_adapter`.
This module is the file-domain layer over those primitives.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from typing import Iterator
from urllib.parse import quote

from app.config import settings
from app.db.postgres import get_pool
from app.exceptions import AKBError, NotFoundError
from app.repositories import vault_files_repo
from app.repositories.document_repo import CollectionRepository
from app.repositories.events_repo import emit_event
from app.services.adapters import s3_adapter
from app.services.index_service import (
    build_file_chunk, delete_file_chunks, write_source_chunks,
)
from app.services.resource_hash import (
    HASH_ALGORITHM,
    compute_stream_content_hash,
    is_sha256_hex,
)
from app.services.s3_delete_worker import enqueue_delete as _enqueue_s3_delete
from app.services.uri_service import file_uri
from app.services.m1_file_measurement import MeasurementFileService, measurement_enabled

# Re-export so existing callers (publication_service, public routes)
# don't break. New code should import directly from s3_adapter.
from app.services.adapters.s3_adapter import StorageError  # noqa: F401

logger = logging.getLogger("akb.files")

_PRESIGN_UPLOAD_TTL = 3600
_PRESIGN_DOWNLOAD_TTL = 3600
_S3_STREAM_CHUNK_SIZE = 64 * 1024

_DELEGATED_ACTOR_EVENT_KEYS = (
    "delegated_user_id",
    "service_user_id",
    "service_token_id",
)


def _delegated_actor_event_fields(
    delegated_actor: dict[str, str] | None,
) -> dict[str, str]:
    """Return the exact non-secret delegation fields allowed in file events."""
    if delegated_actor is None:
        return {}
    return {
        key: delegated_actor[key]
        for key in _DELEGATED_ACTOR_EVENT_KEYS
    }


# ── HTTP header helper (kept here — not S3-specific) ─────────────


def content_disposition_attachment(filename: str) -> str:
    """Build a safe RFC 5987 Content-Disposition: attachment header value.

    The non-ASCII ``filename*=UTF-8''...`` form is what modern browsers honor;
    the ASCII ``filename=...`` is a fallback. CR/LF/quote chars are stripped
    from the ASCII part to prevent header injection when ``filename`` is
    user-controlled.
    """
    ascii_safe = (
        filename.encode("ascii", "replace")
        .decode("ascii")
        .translate({ord(c): None for c in '"\r\n'})
    )
    utf8_encoded = quote(filename, safe="")
    return f'attachment; filename="{ascii_safe}"; filename*=UTF-8\'\'{utf8_encoded}'


# ── Top-level S3 helpers (thin wrappers around s3_adapter) ───────


def get_object_bytes(s3_key: str, max_bytes: int | None = None) -> bytes:
    """Read the whole object into memory. When ``max_bytes`` is given, abort with
    StorageError if the ACTUAL object exceeds it. Callers (e.g. /raw) pre-check a
    DB ``size_bytes`` gate, but that recorded size can drift from the stored
    object — the presigned PUT stays usable after confirm — so we also bound the
    READ itself and never buffer an over-cap body into the single process."""
    if max_bytes is None:
        return s3_adapter.get_bytes(s3_key)
    buf = bytearray()
    gen = s3_adapter.iter_chunks(s3_key)
    try:
        for chunk in gen:
            buf.extend(chunk)
            if len(buf) > max_bytes:
                raise StorageError(f"object {s3_key} exceeds {max_bytes} bytes")
    finally:
        # release the boto stream promptly on cap-abort or exhaustion; the
        # concrete iterator is a generator, but the annotation is Iterator[bytes]
        close = getattr(gen, "close", None)
        if callable(close):
            close()
    return bytes(buf)


def head_object(s3_key: str) -> dict:
    """Cheap existence/metadata probe (S3 HEAD, no body). Raises StorageError/
    AKBError if the object is missing or unreadable. Callers streaming a large
    object HEAD it first (off the event loop) so a missing object is a clean 502
    before the response is committed, without buffering the whole body."""
    return s3_adapter.head(s3_key)


def iter_object_chunks(
    s3_key: str, chunk_size: int = _S3_STREAM_CHUNK_SIZE
) -> Iterator[bytes]:
    return s3_adapter.iter_chunks(s3_key, chunk_size=chunk_size)


def put_object_bytes(
    s3_key: str, body: bytes, content_type: str = "application/octet-stream",
) -> None:
    s3_adapter.put_bytes(s3_key, body, content_type=content_type)


# ── File-key naming convention ───────────────────────────────────


# A key's leading path component is `{prefix}_{safe_filename}`. Two prefix
# flavours exist and are told apart by `_CONTENT_KEY_MARKER`:
#
#   `{8 random hex}_name`        — opaque. The historical default, still used
#                                  whenever the caller cannot state the bytes
#                                  up front. Never collides, so re-uploading
#                                  the same artifact always makes a new row.
#   `sha256-{16 hex}_name`       — content-addressed. The digest is the leading
#                                  16 hex chars of the sha256 of the object's
#                                  bytes, so the key is a function of *what the
#                                  object is* rather than of when it was
#                                  uploaded. Re-uploading the same artifact
#                                  lands on the same key and therefore on the
#                                  existing `UNIQUE(vault_id, s3_key)` row.
#
# The digest is deliberately a plain prefix of the content hash and not a
# derived value: an operator holding a `vault_files.content_hash` can pair it
# to a key by eye, which is what makes an acquired artifact traceable.
_CONTENT_KEY_MARKER = "sha256-"
_CONTENT_KEY_DIGEST_LEN = 16


def _content_key_prefix(content_hash: str) -> str:
    """Key prefix that makes an object's key a function of its bytes."""
    return f"{_CONTENT_KEY_MARKER}{content_hash[:_CONTENT_KEY_DIGEST_LEN]}"


def _s3_key(
    vault_name: str,
    collection: str,
    filename: str,
    *,
    content_hash: str | None = None,
) -> str:
    """Build the storage key for an uploaded file.

    With `content_hash`, the key is deterministic: the same bytes under the
    same vault/collection/filename always produce the same key. Without it,
    the key carries a random prefix and is unique per call — the pre-existing
    behaviour, preserved for callers that cannot hash before uploading.
    """
    safe_name = filename.replace("/", "_")
    prefix = (
        _content_key_prefix(content_hash) if content_hash
        else uuid.uuid4().hex[:8]
    )
    if collection:
        return f"{vault_name}/{collection}/{prefix}_{safe_name}"
    return f"{vault_name}/{prefix}_{safe_name}"


def _content_key_honors_hash(s3_key: str, content_hash: str) -> bool:
    """Whether `s3_key`'s content claim is borne out by `content_hash`.

    Only content-addressed keys make a claim about their bytes; a random
    (legacy) key asserts nothing and therefore always passes. This is what
    stops a caller from declaring one hash at `initiate_upload` — thereby
    reserving that content's key — and then storing unrelated bytes under it.
    """
    last_segment = s3_key.rsplit("/", 1)[-1]
    if not last_segment.startswith(_CONTENT_KEY_MARKER):
        return True
    return last_segment.startswith(f"{_content_key_prefix(content_hash)}_")


from app.util.text import normalize_collection_path as _normalize_collection_path  # noqa: E402


# ── File domain service ──────────────────────────────────────────


class FileService:
    def __init__(self):
        self._bucket = settings.s3_bucket
        # Normal deployments retain the existing direct-S3 File service.  The
        # facade can only exist under the config/database guard checked by its
        # factory, so there is no runtime fallback or dual write.
        self._measurement = MeasurementFileService() if measurement_enabled() else None

    async def initiate_upload(
        self,
        vault_name: str,
        vault_id: uuid.UUID,
        collection: str,
        filename: str,
        *,
        actor_id: str,
        mime_type: str = "application/octet-stream",
        description: str = "",
        content_hash: str | None = None,
    ) -> dict:
        """Create a file record and return a presigned PUT URL.

        Client (akb-mcp proxy) uploads directly to S3, then calls
        confirm_upload(). `collection` is a path string (empty / None
        for vault root); a matching `collections` row is auto-created
        if needed so files share the same FK-normalized hierarchy as
        documents and tables.

        `content_hash` is the caller's sha256 of the bytes it is about to
        upload. Supplying it makes the upload **idempotent**: the storage key
        becomes a function of the bytes, so re-uploading the same artifact to
        the same vault/collection/filename resolves to the file that is
        already there instead of creating a second row for the same content.
        The returned envelope is unchanged in shape and still carries a usable
        `upload_url` — an unaware client can re-PUT the identical bytes to the
        identical key and confirm as usual; the net effect is one row, not two.
        `deduplicated` says which happened, for clients that would rather skip
        the redundant transfer.

        The hash is a *claim* at this point — AKB certifies the real one in
        `confirm_upload`, which rejects bytes that do not match the key they
        were stored under. Omitting `content_hash` preserves the historical
        behaviour exactly: a random key, and one new row per call.
        """
        if content_hash is not None and not is_sha256_hex(content_hash):
            raise AKBError("content_hash must be a lowercase sha256 hex digest", status_code=400)
        if self._measurement is not None:
            return await self._measurement.initiate_upload(
                vault_name=vault_name, vault_id=vault_id, collection=collection,
                filename=filename, actor_id=actor_id, mime_type=mime_type,
                description=description, content_hash=content_hash,
            )

        s3_adapter.ensure_bucket(self._bucket)
        collection_path = _normalize_collection_path(collection)
        s3_key = _s3_key(
            vault_name, collection_path, filename, content_hash=content_hash,
        )
        file_id = uuid.uuid4()

        presigned_url = s3_adapter.presign_put(
            s3_key, content_type=mime_type, ttl=_PRESIGN_UPLOAD_TTL,
        )

        pool = await get_pool()
        async with pool.acquire() as conn:
            async with conn.transaction():
                collection_id = None
                if collection_path:
                    coll_repo = CollectionRepository(pool)
                    collection_id = await coll_repo.get_or_create(
                        vault_id, collection_path, conn=conn,
                    )
                stored_id = await vault_files_repo.insert_or_adopt(
                    conn,
                    file_id=file_id, vault_id=vault_id,
                    name=filename,
                    s3_key=s3_key, mime_type=mime_type,
                    size_bytes=0, description=description,
                    created_by=actor_id,
                    collection_id=collection_id,
                )

        deduplicated = stored_id != file_id
        file_id = stored_id

        logger.info(
            "Presigned upload URL for %s/%s (file_id=%s, collection=%s, deduplicated=%s)",
            vault_name, s3_key, file_id, collection_path or "<root>", deduplicated,
        )
        return {
            "kind": "file",
            "uri": file_uri(vault_name, str(file_id), collection=collection_path),
            "vault": vault_name,
            "collection": collection_path or None,
            "upload_url": presigned_url,
            "s3_key": s3_key,
            "expires_in": _PRESIGN_UPLOAD_TTL,
            "deduplicated": deduplicated,
        }

    async def confirm_upload(
        self,
        vault_id: uuid.UUID,
        file_id: str,
        *,
        actor_id: str,
        delegated_actor: dict[str, str] | None = None,
        content_hash: str | None = None,
        hash_algorithm: str = HASH_ALGORITHM,
    ) -> dict:
        """Confirm upload completion and persist AKB-certified byte hash.

        If the file doesn't exist in S3 (upload failed/abandoned),
        deletes the orphan DB record and returns an error.
        """
        if hash_algorithm != HASH_ALGORITHM:
            raise AKBError(
                f"Unsupported file hash algorithm: {hash_algorithm}",
                status_code=400,
            )
        if content_hash is not None and not is_sha256_hex(content_hash):
            raise AKBError("content_hash must be a lowercase sha256 hex digest", status_code=400)

        if self._measurement is not None:
            return await self._measurement.confirm_upload(vault_id, file_id, content_hash=content_hash)

        fid = uuid.UUID(file_id)
        pool = await get_pool()
        async with pool.acquire() as conn:
            row = await vault_files_repo.find_by_id(conn, vault_id, fid)
            if not row:
                raise NotFoundError("File", file_id)

        # Read object size. Treat NoSuchKey specially: that means the
        # client never finished its presigned upload; clean up the
        # orphan DB record so the same filename can be retried.
        try:
            meta = s3_adapter.head(row["s3_key"])
            size_bytes = meta["ContentLength"]
        except NotFoundError:
            async with pool.acquire() as conn:
                await vault_files_repo.delete(conn, fid)
            logger.warning("Orphan file record deleted: %s (S3 object missing)", file_id)
            raise AKBError(
                f"Upload not found in storage — file record cleaned up: {file_id}",
                status_code=404,
            )

        server_content_hash = await asyncio.to_thread(
            compute_stream_content_hash,
            s3_adapter.iter_chunks(row["s3_key"]),
        )
        # Two ways the stored bytes can fail to be what was declared: the
        # caller's `content_hash` argument disagrees with them, or the key they
        # were stored under is content-addressed and does not. The second check
        # is what keeps a content-addressed key honest end to end — without it a
        # caller could reserve some other content's key at `initiate_upload`,
        # omit `content_hash` here, and leave unrelated bytes sitting where the
        # next caller's idempotent upload would adopt them. Both are the same
        # fault ("these are not the bytes you said") and take the same
        # pre-existing 409 + cleanup path.
        stored_bytes_disowned = (
            (content_hash is not None and content_hash != server_content_hash)
            or not _content_key_honors_hash(row["s3_key"], server_content_hash)
        )
        if stored_bytes_disowned:
            async with pool.acquire() as conn:
                async with conn.transaction():
                    await vault_files_repo.delete(conn, fid)
                    await _enqueue_s3_delete(conn, row["s3_key"])
            raise AKBError(
                "Uploaded file hash mismatch; file record was cleaned up.",
                status_code=409,
            )

        etag = (meta.get("ETag") or "").strip('"') or None
        storage_version = meta.get("VersionId")

        async with pool.acquire() as conn:
            async with conn.transaction():
                await vault_files_repo.update_confirmed_metadata(
                    conn, fid,
                    size_bytes=size_bytes,
                    content_hash=server_content_hash,
                    hash_algorithm=HASH_ALGORITHM,
                    etag=etag,
                    storage_version=storage_version,
                )
                vault_row = await conn.fetchrow(
                    "SELECT name FROM vaults WHERE id = $1", vault_id,
                )
                vault_name_for_event = vault_row["name"] if vault_row else None
                await emit_event(
                    conn, "file.put",
                    vault_id=vault_id,
                    resource_uri=(
                        file_uri(
                            vault_name_for_event,
                            file_id,
                            collection=row["collection"],
                        )
                        if vault_name_for_event else None
                    ),
                    actor_id=actor_id,
                    payload={
                        "vault": vault_name_for_event,
                        "collection": row["collection"],
                        "name": row["name"],
                        "mime_type": row["mime_type"],
                        "size_bytes": size_bytes,
                        "content_hash": server_content_hash,
                        "hash_algorithm": HASH_ALGORITHM,
                        "etag": etag,
                        "storage_version": storage_version,
                        **_delegated_actor_event_fields(delegated_actor),
                    },
                )

        # Index file metadata for hybrid search.
        try:
            await index_file_metadata(
                file_id,
                vault_id=vault_id,
                vault_name=vault_row["name"] if vault_row else "",
                collection=row["collection"] or "",
                name=row["name"],
                mime_type=row["mime_type"],
                size_bytes=size_bytes,
                description=row["description"],
            )
        except Exception as e:  # noqa: BLE001
            logger.warning("file metadata indexing failed for %s: %s", file_id, e)

        logger.info("Upload confirmed: %s (%d bytes)", row["name"], size_bytes)
        vault_name = vault_row["name"] if vault_row else None
        return {
            "kind": "file",
            "uri": (
                file_uri(vault_name, file_id, collection=row["collection"])
                if vault_name else None
            ),
            "vault": vault_name,
            "name": row["name"],
            "collection": row["collection"],
            "mime_type": row["mime_type"],
            "size_bytes": size_bytes,
            "content_hash": server_content_hash,
            "hash_algorithm": HASH_ALGORITHM,
            "etag": etag,
            "storage_version": storage_version,
        }

    async def get_download_url(self, vault_id: uuid.UUID, file_id: str) -> dict:
        """Return a presigned GET URL for direct download from S3."""
        if self._measurement is not None:
            return await self._measurement.get_download_url(vault_id, file_id)
        pool = await get_pool()
        async with pool.acquire() as conn:
            row = await vault_files_repo.find_by_id(
                conn, vault_id, uuid.UUID(file_id),
            )
            if not row:
                raise NotFoundError("File", file_id)

        # Override stored Content-Type with DB value so browsers inline
        # render correctly even when the object was uploaded with a
        # generic octet-stream (legacy proxy versions < 0.5.1).
        ct = row["mime_type"] if (
            row["mime_type"] and row["mime_type"] != "application/octet-stream"
        ) else None
        presigned_url = s3_adapter.presign_get(
            row["s3_key"], ttl=_PRESIGN_DOWNLOAD_TTL,
            response_content_type=ct,
        )

        # `get_download_url` is called by the HTTP route that the proxy
        # invokes after parsing the URI client-side; the returned dict is
        # consumed by the proxy, not the end user. We still surface the
        # URI for symmetry with confirm_upload.
        return {
            "kind": "file",
            "name": row["name"],
            "download_url": presigned_url,
            "mime_type": row["mime_type"],
            "size_bytes": row["size_bytes"],
            "content_hash": row["content_hash"],
            "hash_algorithm": row["hash_algorithm"],
            "etag": row["etag"],
            "storage_version": row["storage_version"],
            "expires_in": _PRESIGN_DOWNLOAD_TTL,
        }

    async def list_files(
        self,
        vault_id: uuid.UUID,
        vault_name: str,
        collection: str | None = None,
        limit: int = 50,
    ) -> list[dict]:
        """List files in a vault. When `collection` is set (path string),
        only files in that exact collection (NULL collection_id for empty
        path = vault root) are returned. The path is resolved to a
        collection_id at query time. `vault_name` is required so each
        row's canonical `uri` can be built without a second join."""
        if self._measurement is not None:
            return await self._measurement.list_files(vault_id, vault_name, collection, limit)
        pool = await get_pool()
        async with pool.acquire() as conn:
            if collection is None:
                rows = await vault_files_repo.list_for_vault(
                    conn, vault_id, limit=limit,
                )
            else:
                collection_path = _normalize_collection_path(collection)
                if collection_path == "":
                    # Empty path => vault root scope.
                    rows = await vault_files_repo.list_for_vault(
                        conn, vault_id, collection_id=None, scoped=True, limit=limit,
                    )
                else:
                    cid_row = await conn.fetchrow(
                        "SELECT id FROM collections WHERE vault_id = $1 AND path = $2",
                        vault_id, collection_path,
                    )
                    if not cid_row:
                        return []  # collection doesn't exist → no files
                    rows = await vault_files_repo.list_for_vault(
                        conn, vault_id,
                        collection_id=cid_row["id"], scoped=True, limit=limit,
                    )

        return [
            {
                "kind": "file",
                "uri": file_uri(vault_name, str(r["id"]), collection=r["collection"]),
                "collection": r["collection"],
                "name": r["name"],
                "mime_type": r["mime_type"],
                "size_bytes": r["size_bytes"],
                "content_hash": r["content_hash"],
                "hash_algorithm": r["hash_algorithm"],
                "etag": r["etag"],
                "storage_version": r["storage_version"],
                "description": r["description"],
                "created_by": r["created_by"],
                "created_at": r["created_at"].isoformat() if r["created_at"] else None,
            }
            for r in rows
        ]

    async def delete(
        self,
        vault_id: uuid.UUID,
        file_id: str,
        *,
        actor_id: str,
    ) -> dict:
        if self._measurement is not None:
            return await self._measurement.delete(vault_id, file_id, actor_id=actor_id)
        fid = uuid.UUID(file_id)
        pool = await get_pool()

        # 1. Look up the file + vault name (read-only, no TX needed).
        async with pool.acquire() as conn:
            row = await vault_files_repo.find_by_id(conn, vault_id, fid)
            if not row:
                raise NotFoundError("File", file_id)
            vault_row = await conn.fetchrow(
                "SELECT name FROM vaults WHERE id = $1", vault_id,
            )
            vault_name = vault_row["name"] if vault_row else ""

        # 2. Atomic PG mutations: vault_files DELETE + edges cleanup +
        # chunk-delete outbox + s3-delete outbox under one TX. The
        # actual S3 delete is performed asynchronously by
        # s3_delete_worker after the TX commits, so a crash between
        # commit and S3 cannot leave us with an orphan blob (or a
        # missing-blob row). The outbox row carries the s3_key.
        async with pool.acquire() as conn:
            async with conn.transaction():
                await vault_files_repo.delete(conn, fid)

                if vault_name:
                    f_uri = file_uri(vault_name, file_id, collection=row["collection"])
                    await conn.execute(
                        "DELETE FROM edges WHERE source_uri = $1 OR target_uri = $1",
                        f_uri,
                    )
                    # App-level publication cascade — `publications.
                    # file_id` FK is gone after migration 022.
                    await conn.execute(
                        "DELETE FROM publications WHERE resource_uri = $1",
                        f_uri,
                    )

                # Drop the metadata chunk (outbox-driven vector-store delete).
                # delete_file_chunks → _drop_source_chunks_with_outbox is
                # designed to RAISE so a failed outbox enqueue rolls back the
                # whole file delete (03-F1). Do NOT swallow it: swallowing
                # commits the vault_files delete + s3 enqueue while leaving
                # the chunk's vector-store point orphaned.
                await delete_file_chunks(conn, file_id)

                await _enqueue_s3_delete(conn, row["s3_key"])

                await emit_event(
                    conn, "file.delete",
                    vault_id=vault_id,
                    resource_uri=(
                        file_uri(vault_name, file_id, collection=row["collection"])
                        if vault_name else None
                    ),
                    actor_id=actor_id,
                    payload={
                        "vault": vault_name,
                        "collection": row["collection"],
                        "name": row["name"],
                        "s3_key": row["s3_key"],
                        "size_bytes": row["size_bytes"],
                    },
                )

        logger.info("Deleted file %s (s3://%s/%s)", file_id, self._bucket, row["s3_key"])
        return {
            "kind": "file",
            "uri": (
                file_uri(vault_name, file_id, collection=row.get("collection"))
                if vault_name else None
            ),
            "vault": vault_name,
            "collection": row.get("collection"),
            "name": row["name"],
            "deleted": True,
        }

    async def transfer_measurement_capability(
        self, token: str, *, method: str, body: bytes | None = None,
    ) -> bytes | None:
        """Serve a guarded FS-CAS PUT/GET capability through the File route.

        This is deliberately unavailable outside M1 measurement mode.  The
        opaque token itself is process-local and is neither a database value
        nor part of the measurement receipt.
        """
        if self._measurement is None:
            raise NotFoundError("File transfer", token)
        return await self._measurement.transfer(token, body=body, method=method)


async def index_file_metadata(
    file_id: str,
    vault_id: uuid.UUID,
    vault_name: str,
    collection: str,
    name: str,
    mime_type: str | None,
    size_bytes: int | None,
    description: str | None,
) -> None:
    """Build + upsert the metadata chunk for a file so hybrid search
    can surface it. Safe to call repeatedly — write_source_chunks
    replaces all prior chunks for this file first."""
    chunk = build_file_chunk(
        vault_name=vault_name, collection=collection, name=name,
        mime_type=mime_type, size_bytes=size_bytes, description=description,
    )
    pool = await get_pool()
    async with pool.acquire() as conn:
        await write_source_chunks(
            conn, "file", file_id,
            vault_id=vault_id,
            chunks=[chunk],
        )
