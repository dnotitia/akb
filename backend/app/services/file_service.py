"""File service — S3-backed binary file storage for vaults.

AKB never touches file bytes. It only:
1. Generates presigned URLs for direct client ↔ S3 transfer.
2. Manages file metadata in PostgreSQL (`vault_files_repo`).

Access control inherits from vault permissions.

S3 client lifecycle and low-level primitives (head/get/put/delete,
presigning, error mapping) live in `app.services.adapters.s3_adapter`.
This module is the file-domain layer over those primitives.
"""

from __future__ import annotations

import logging
import uuid
from typing import Iterator
from urllib.parse import quote

from app.config import settings
from app.db.postgres import get_pool
from app.exceptions import AKBError, NotFoundError
from app.repositories import vault_files_repo
from app.services.adapters import s3_adapter
from app.services.index_service import (
    build_file_chunk, delete_file_chunks, write_source_chunks,
)

# Re-export so existing callers (publication_service, public routes)
# don't break. New code should import directly from s3_adapter.
from app.services.adapters.s3_adapter import StorageError  # noqa: F401

logger = logging.getLogger("akb.files")

_PRESIGN_UPLOAD_TTL = 3600
_PRESIGN_DOWNLOAD_TTL = 3600
_S3_STREAM_CHUNK_SIZE = 64 * 1024


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


def get_presigned_download_url(
    s3_key: str,
    ttl: int = _PRESIGN_DOWNLOAD_TTL,
    response_content_type: str | None = None,
    attachment_filename: str | None = None,
) -> str:
    """Presigned GET URL for an arbitrary S3 key.

    Used by share_service to bypass the vault_files lookup when the caller
    already has the s3_key in hand. Raises StorageError on failure.

    `response_content_type` overrides the stored object's Content-Type in
    the response (needed when the object was uploaded with a generic
    application/octet-stream but the DB metadata has the correct value).

    `attachment_filename` sets Content-Disposition so the browser forces
    a download rather than rendering inline (the presigned URL is
    cross-origin, so the <a download> attribute is ignored).
    """
    cd = (
        content_disposition_attachment(attachment_filename)
        if attachment_filename
        else None
    )
    return s3_adapter.presign_get(
        s3_key,
        ttl=ttl,
        response_content_type=response_content_type,
        response_content_disposition=cd,
    )


def get_object_bytes(s3_key: str) -> bytes:
    return s3_adapter.get_bytes(s3_key)


def iter_object_chunks(
    s3_key: str, chunk_size: int = _S3_STREAM_CHUNK_SIZE
) -> Iterator[bytes]:
    return s3_adapter.iter_chunks(s3_key, chunk_size=chunk_size)


def put_object_bytes(
    s3_key: str, body: bytes, content_type: str = "application/octet-stream",
) -> None:
    s3_adapter.put_bytes(s3_key, body, content_type=content_type)


# Backward-compat alias for callers that imported the historical
# private name (e.g. access_service). New code should call
# `s3_adapter.client()` directly; this alias will be removed in a
# follow-up commit once those imports are migrated.
def _get_s3_client():
    return s3_adapter.client()


# ── File-key naming convention ───────────────────────────────────


def _s3_key(vault_name: str, collection: str, filename: str) -> str:
    safe_name = filename.replace("/", "_")
    uid = uuid.uuid4().hex[:8]
    if collection:
        return f"{vault_name}/{collection}/{uid}_{safe_name}"
    return f"{vault_name}/{uid}_{safe_name}"


# ── File domain service ──────────────────────────────────────────


class FileService:
    def __init__(self):
        self._bucket = settings.s3_bucket

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
    ) -> dict:
        """Create a file record and return a presigned PUT URL.

        Client (akb-mcp proxy) uploads directly to S3, then calls
        confirm_upload().
        """
        s3_adapter.ensure_bucket(self._bucket)
        s3_key = _s3_key(vault_name, collection, filename)
        file_id = uuid.uuid4()

        presigned_url = s3_adapter.presign_put(
            s3_key, content_type=mime_type, ttl=_PRESIGN_UPLOAD_TTL,
        )

        pool = await get_pool()
        async with pool.acquire() as conn:
            await vault_files_repo.insert(
                conn,
                file_id=file_id, vault_id=vault_id,
                collection=collection, name=filename,
                s3_key=s3_key, mime_type=mime_type,
                size_bytes=0, description=description,
                created_by=actor_id,
            )

        logger.info("Presigned upload URL for %s/%s (file_id=%s)", vault_name, s3_key, file_id)
        return {
            "id": str(file_id),
            "upload_url": presigned_url,
            "s3_key": s3_key,
            "expires_in": _PRESIGN_UPLOAD_TTL,
        }

    async def confirm_upload(
        self,
        vault_id: uuid.UUID,
        file_id: str,
        *,
        actor_id: str,
    ) -> dict:
        """Confirm upload completion. Updates size_bytes from S3 metadata.

        If the file doesn't exist in S3 (upload failed/abandoned),
        deletes the orphan DB record and returns an error.
        """
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

        async with pool.acquire() as conn:
            await vault_files_repo.update_size(conn, fid, size_bytes)
            vault_row = await conn.fetchrow(
                "SELECT name FROM vaults WHERE id = $1", vault_id,
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
        return {
            "id": file_id,
            "name": row["name"],
            "collection": row["collection"],
            "s3_key": row["s3_key"],
            "mime_type": row["mime_type"],
            "size_bytes": size_bytes,
        }

    async def get_download_url(self, vault_id: uuid.UUID, file_id: str) -> dict:
        """Return a presigned GET URL for direct download from S3."""
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

        return {
            "name": row["name"],
            "download_url": presigned_url,
            "mime_type": row["mime_type"],
            "size_bytes": row["size_bytes"],
            "expires_in": _PRESIGN_DOWNLOAD_TTL,
        }

    async def list_files(
        self,
        vault_id: uuid.UUID,
        collection: str | None = None,
        limit: int = 50,
    ) -> list[dict]:
        pool = await get_pool()
        async with pool.acquire() as conn:
            rows = await vault_files_repo.list_for_vault(
                conn, vault_id, collection=collection, limit=limit,
            )

        return [
            {
                "id": str(r["id"]),
                "collection": r["collection"],
                "name": r["name"],
                "mime_type": r["mime_type"],
                "size_bytes": r["size_bytes"],
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
    ) -> bool:
        fid = uuid.UUID(file_id)
        pool = await get_pool()
        async with pool.acquire() as conn:
            row = await vault_files_repo.find_by_id(conn, vault_id, fid)
            if not row:
                raise NotFoundError("File", file_id)

            # Look up vault name for edge cleanup
            vault_row = await conn.fetchrow("SELECT name FROM vaults WHERE id = $1", vault_id)
            vault_name = vault_row["name"] if vault_row else ""

            s3_adapter.delete(row["s3_key"])
            await vault_files_repo.delete(conn, fid)

            # Clean up edges referencing this file
            if vault_name:
                f_uri = f"akb://{vault_name}/file/{file_id}"
                await conn.execute(
                    "DELETE FROM edges WHERE source_uri = $1 OR target_uri = $1",
                    f_uri,
                )

            # Drop the metadata chunk (outbox-driven vector-store delete).
            try:
                await delete_file_chunks(conn, file_id)
            except Exception as e:  # noqa: BLE001
                logger.warning("file chunk delete failed for %s: %s", file_id, e)

        logger.info("Deleted file %s (s3://%s/%s)", file_id, self._bucket, row["s3_key"])
        return True


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
