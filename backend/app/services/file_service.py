"""File service — S3-backed binary file storage for vaults.

AKB never touches file bytes. It only:
1. Generates presigned URLs for direct client ↔ S3 transfer
2. Manages file metadata in PostgreSQL

Access control inherits from vault permissions.
"""

from __future__ import annotations

import logging
import uuid
from typing import Iterator
from urllib.parse import quote

import boto3
from botocore.config import Config as BotoConfig
from botocore.exceptions import ClientError

from app.config import settings
from app.db.postgres import get_pool
from app.exceptions import AKBError, NotFoundError
from app.services.index_service import (
    build_file_chunk, delete_file_chunks, write_source_chunks,
)

logger = logging.getLogger("akb.files")

_s3_client = None         # For server-side operations (internal endpoint)
_s3_presign_client = None  # For generating presigned URLs (public endpoint)
_bucket_verified = False

_PRESIGN_UPLOAD_TTL = 3600
_PRESIGN_DOWNLOAD_TTL = 3600
_S3_STREAM_CHUNK_SIZE = 64 * 1024


class StorageError(AKBError):
    """Generic S3 storage error wrapper used by share_service / public routes."""
    def __init__(self, message: str):
        super().__init__(f"Storage error: {message}", status_code=502)


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


def get_presigned_download_url(
    s3_key: str,
    ttl: int = _PRESIGN_DOWNLOAD_TTL,
    response_content_type: str | None = None,
    attachment_filename: str | None = None,
) -> str:
    """Generate a presigned GET URL for an arbitrary S3 key.

    Used by share_service to bypass the vault_files lookup when the caller
    already has the s3_key in hand. Raises StorageError on failure.

    If ``response_content_type`` is provided, S3 overrides the stored object's
    Content-Type in the response. Needed when the object was uploaded with a
    generic Content-Type but DB metadata has the correct one (e.g. legacy
    uploads from proxy versions before mime_type propagation).

    If ``attachment_filename`` is provided, S3 sets Content-Disposition so the
    browser forces a download rather than rendering inline (needed because the
    presigned URL is cross-origin, so the <a download> attribute is ignored).
    """
    try:
        s3 = _get_presign_client()
        params = {"Bucket": settings.s3_bucket, "Key": s3_key}
        if response_content_type:
            params["ResponseContentType"] = response_content_type
        if attachment_filename:
            params["ResponseContentDisposition"] = content_disposition_attachment(
                attachment_filename
            )
        return s3.generate_presigned_url(
            "get_object",
            Params=params,
            ExpiresIn=ttl,
        )
    except ClientError as e:
        raise StorageError(_wrap_s3_error(e, f"presign download {s3_key}").message) from e


def get_object_bytes(s3_key: str) -> bytes:
    """Read an S3 object's bytes. Raises StorageError on failure."""
    try:
        s3 = _get_s3_client()
        obj = s3.get_object(Bucket=settings.s3_bucket, Key=s3_key)
        return obj["Body"].read()
    except ClientError as e:
        raise StorageError(_wrap_s3_error(e, f"read {s3_key}").message) from e


def iter_object_chunks(
    s3_key: str, chunk_size: int = _S3_STREAM_CHUNK_SIZE
) -> Iterator[bytes]:
    """Yield an S3 object as chunks. Sync generator — FastAPI's
    StreamingResponse iterates it in a thread pool so the underlying boto3
    blocking I/O doesn't stall the event loop.

    Used for downloads we proxy through the backend instead of redirecting
    to a presigned S3 URL, e.g. when the page is HTTPS but S3 is HTTP and
    browsers would block the redirect as a mixed-content download.

    A failure mid-stream truncates the response (headers are already sent
    when bytes start flowing), so we log and re-raise as StorageError to
    surface the cause to operators.
    """
    try:
        s3 = _get_s3_client()
        obj = s3.get_object(Bucket=settings.s3_bucket, Key=s3_key)
    except ClientError as e:
        raise StorageError(_wrap_s3_error(e, f"read {s3_key}").message) from e
    body = obj["Body"]
    try:
        for chunk in body.iter_chunks(chunk_size=chunk_size):
            yield chunk
    except Exception as e:  # noqa: BLE001 — boto3/urllib3 surface various stream errors
        logger.warning("S3 stream %s aborted: %s", s3_key, e)
        raise StorageError(f"stream {s3_key}: {e}") from e
    finally:
        body.close()


def put_object_bytes(s3_key: str, body: bytes, content_type: str = "application/octet-stream") -> None:
    """Write bytes to S3. Raises StorageError on failure."""
    try:
        s3 = _get_s3_client()
        s3.put_object(Bucket=settings.s3_bucket, Key=s3_key, Body=body, ContentType=content_type)
    except ClientError as e:
        raise StorageError(_wrap_s3_error(e, f"write {s3_key}").message) from e


def _s3_config():
    return {
        "aws_access_key_id": settings.s3_access_key,
        "aws_secret_access_key": settings.s3_secret_key,
        "config": BotoConfig(signature_version="s3v4"),
        **({"region_name": settings.s3_region} if settings.s3_region else {}),
    }


def _get_s3_client():
    """S3 client for server-side operations (head, delete, etc.)."""
    global _s3_client
    if _s3_client is not None:
        return _s3_client
    _s3_client = boto3.client("s3", endpoint_url=settings.s3_endpoint_url, **_s3_config())
    return _s3_client


def _get_presign_client():
    """S3 client for generating presigned URLs.

    Uses s3_public_url so clients can reach S3 from outside the cluster.
    Falls back to s3_endpoint_url if s3_public_url is not set.
    """
    global _s3_presign_client
    if _s3_presign_client is not None:
        return _s3_presign_client
    endpoint = settings.s3_public_url or settings.s3_endpoint_url
    _s3_presign_client = boto3.client("s3", endpoint_url=endpoint, **_s3_config())
    return _s3_presign_client


def _s3_key(vault_name: str, collection: str, filename: str) -> str:
    safe_name = filename.replace("/", "_")
    uid = uuid.uuid4().hex[:8]
    if collection:
        return f"{vault_name}/{collection}/{uid}_{safe_name}"
    return f"{vault_name}/{uid}_{safe_name}"


class FileService:
    def __init__(self):
        self._bucket = settings.s3_bucket

    def _ensure_bucket(self):
        global _bucket_verified
        if _bucket_verified:
            return
        s3 = _get_s3_client()
        try:
            s3.head_bucket(Bucket=self._bucket)
        except ClientError as e:
            code = e.response["Error"].get("Code", "")
            if code in ("404", "NoSuchBucket"):
                s3.create_bucket(Bucket=self._bucket)
                logger.info("Created S3 bucket: %s", self._bucket)
            else:
                raise _wrap_s3_error(e, "check bucket")
        _bucket_verified = True

    async def initiate_upload(
        self,
        vault_name: str,
        vault_id: uuid.UUID,
        collection: str,
        filename: str,
        mime_type: str = "application/octet-stream",
        description: str = "",
        created_by: str = "",
    ) -> dict:
        """Create a file record and return a presigned PUT URL.

        Client (akb-mcp proxy) uploads directly to S3, then calls confirm_upload().
        """
        self._ensure_bucket()
        s3_key = _s3_key(vault_name, collection, filename)
        file_id = uuid.uuid4()

        s3 = _get_presign_client()
        try:
            presigned_url = s3.generate_presigned_url(
                "put_object",
                Params={
                    "Bucket": self._bucket,
                    "Key": s3_key,
                    "ContentType": mime_type,
                },
                ExpiresIn=_PRESIGN_UPLOAD_TTL,
            )
        except ClientError as e:
            raise _wrap_s3_error(e, f"presign upload {filename}")

        pool = await get_pool()
        async with pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO vault_files (id, vault_id, collection, name, s3_key, mime_type, size_bytes, description, created_by)
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)
                """,
                file_id, vault_id, collection, filename, s3_key,
                mime_type, 0, description, created_by,
            )

        logger.info("Presigned upload URL for %s/%s (file_id=%s)", vault_name, s3_key, file_id)
        return {
            "id": str(file_id),
            "upload_url": presigned_url,
            "s3_key": s3_key,
            "expires_in": _PRESIGN_UPLOAD_TTL,
        }

    async def confirm_upload(self, vault_id: uuid.UUID, file_id: str) -> dict:
        """Confirm upload completion. Updates size_bytes from S3 metadata.

        If the file doesn't exist in S3 (upload failed/abandoned),
        deletes the orphan DB record and returns an error.
        """
        fid = uuid.UUID(file_id)
        pool = await get_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT name, s3_key, mime_type, collection, description FROM vault_files WHERE id = $1 AND vault_id = $2",
                fid, vault_id,
            )
            if not row:
                raise NotFoundError("File", file_id)

        # Get actual size from S3
        s3 = _get_s3_client()
        try:
            head = s3.head_object(Bucket=self._bucket, Key=row["s3_key"])
            size_bytes = head["ContentLength"]
        except ClientError as e:
            code = e.response["Error"].get("Code", "")
            if code in ("404", "NoSuchKey"):
                # Upload never completed — clean up orphan record
                async with pool.acquire() as conn:
                    await conn.execute("DELETE FROM vault_files WHERE id = $1", fid)
                logger.warning("Orphan file record deleted: %s (S3 object missing)", file_id)
                raise AKBError(f"Upload not found in storage — file record cleaned up: {file_id}", status_code=404)
            raise _wrap_s3_error(e, f"confirm upload {file_id}")

        async with pool.acquire() as conn:
            await conn.execute(
                "UPDATE vault_files SET size_bytes = $1, updated_at = NOW() WHERE id = $2",
                size_bytes, fid,
            )
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
            row = await conn.fetchrow(
                "SELECT name, s3_key, size_bytes, mime_type FROM vault_files WHERE id = $1 AND vault_id = $2",
                uuid.UUID(file_id), vault_id,
            )
            if not row:
                raise NotFoundError("File", file_id)

        s3 = _get_presign_client()
        try:
            params = {"Bucket": self._bucket, "Key": row["s3_key"]}
            # Override stored Content-Type with DB value so browsers inline
            # render correctly even when the object was uploaded with a
            # generic octet-stream (legacy proxy versions < 0.5.1).
            if row["mime_type"] and row["mime_type"] != "application/octet-stream":
                params["ResponseContentType"] = row["mime_type"]
            presigned_url = s3.generate_presigned_url(
                "get_object",
                Params=params,
                ExpiresIn=_PRESIGN_DOWNLOAD_TTL,
            )
        except ClientError as e:
            raise _wrap_s3_error(e, f"presign download {file_id}")

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
            if collection:
                rows = await conn.fetch(
                    """
                    SELECT id, collection, name, mime_type, size_bytes, description, created_by, created_at
                    FROM vault_files WHERE vault_id = $1 AND collection = $2
                    ORDER BY created_at DESC LIMIT $3
                    """,
                    vault_id, collection, limit,
                )
            else:
                rows = await conn.fetch(
                    """
                    SELECT id, collection, name, mime_type, size_bytes, description, created_by, created_at
                    FROM vault_files WHERE vault_id = $1
                    ORDER BY created_at DESC LIMIT $2
                    """,
                    vault_id, limit,
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

    async def delete(self, vault_id: uuid.UUID, file_id: str) -> bool:
        pool = await get_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT s3_key FROM vault_files WHERE id = $1 AND vault_id = $2",
                uuid.UUID(file_id), vault_id,
            )
            if not row:
                raise NotFoundError("File", file_id)

            # Look up vault name for edge cleanup
            vault_row = await conn.fetchrow("SELECT name FROM vaults WHERE id = $1", vault_id)
            vault_name = vault_row["name"] if vault_row else ""

            s3 = _get_s3_client()
            try:
                s3.delete_object(Bucket=self._bucket, Key=row["s3_key"])
            except ClientError as e:
                raise _wrap_s3_error(e, f"delete {file_id}")
            await conn.execute("DELETE FROM vault_files WHERE id = $1", uuid.UUID(file_id))

            # Clean up edges referencing this file
            if vault_name:
                f_uri = f"akb://{vault_name}/file/{file_id}"
                await conn.execute("DELETE FROM edges WHERE source_uri = $1 OR target_uri = $1", f_uri)

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


def _wrap_s3_error(e: ClientError, context: str) -> AKBError:
    code = e.response["Error"].get("Code", "Unknown")
    msg = e.response["Error"].get("Message", str(e))
    logger.error("S3 error during %s: [%s] %s", context, code, msg)

    if code in ("AccessDenied", "403"):
        return AKBError(f"Storage access denied: {context}", status_code=502)
    if code in ("NoSuchKey", "404"):
        return NotFoundError("File in storage", context)
    if code == "EntityTooLarge":
        return AKBError(f"File too large: {context}", status_code=413)
    return AKBError(f"Storage error during {context}: {msg}", status_code=502)
