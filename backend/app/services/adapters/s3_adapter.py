"""S3 adapter — boto3 client lifecycle + low-level S3 primitives.

Owns:

- Internal-endpoint client (server-side ops: head, get, put, delete,
  bucket lifecycle).
- Public-endpoint client (signing presigned URLs that clients reach
  from outside the cluster).
- Storage error mapping (`StorageError`) so callers don't import
  botocore exception types.

Domain logic (key naming, vault metadata, etc.) lives in `file_service`
and uses these primitives. Anything S3-shaped — but no domain concepts
— belongs here.
"""

from __future__ import annotations

import logging
from typing import Any, Iterator

import boto3
from botocore.config import Config as BotoConfig
from botocore.exceptions import ClientError

from app.config import settings
from app.exceptions import AKBError, NotFoundError

logger = logging.getLogger("akb.s3")


# ── Errors ───────────────────────────────────────────────────────


class StorageError(AKBError):
    """S3 storage error wrapper. Inherits AKBError so it propagates as
    HTTP 502 by default through the route layer."""

    def __init__(self, message: str):
        super().__init__(f"Storage error: {message}", status_code=502)


def wrap_error(e: ClientError, context: str) -> AKBError:
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


# ── Client lifecycle ─────────────────────────────────────────────


_internal_client = None
_presign_client = None
_bucket_verified = False

_DEFAULT_PRESIGN_TTL = 3600
_STREAM_CHUNK_SIZE = 64 * 1024


def _config() -> dict:
    return {
        "aws_access_key_id": settings.s3_access_key,
        "aws_secret_access_key": settings.s3_secret_key,
        "config": BotoConfig(signature_version="s3v4"),
        **({"region_name": settings.s3_region} if settings.s3_region else {}),
    }


def client():
    """boto3 S3 client targeting the internal endpoint. Used for
    server-side operations (head, get, put, delete)."""
    global _internal_client
    if _internal_client is None:
        _internal_client = boto3.client(
            "s3", endpoint_url=settings.s3_endpoint_url, **_config(),
        )
    return _internal_client


def presign_client():
    """boto3 S3 client targeting the public endpoint. Used to sign URLs
    that clients reach from outside the cluster. Falls back to the
    internal endpoint when no public URL is configured."""
    global _presign_client
    if _presign_client is None:
        endpoint = settings.s3_public_url or settings.s3_endpoint_url
        _presign_client = boto3.client(
            "s3", endpoint_url=endpoint, **_config(),
        )
    return _presign_client


def ensure_bucket(bucket: str) -> None:
    """Verify the bucket exists; create it on first miss. Idempotent
    and cached after the first successful check."""
    global _bucket_verified
    if _bucket_verified:
        return
    s3 = client()
    try:
        s3.head_bucket(Bucket=bucket)
    except ClientError as e:
        code = e.response["Error"].get("Code", "")
        if code in ("404", "NoSuchBucket"):
            s3.create_bucket(Bucket=bucket)
            logger.info("Created S3 bucket: %s", bucket)
        else:
            raise wrap_error(e, "check bucket") from e
    _bucket_verified = True


# ── Primitives ───────────────────────────────────────────────────


def head(key: str) -> dict[str, Any]:
    try:
        return client().head_object(Bucket=settings.s3_bucket, Key=key)
    except ClientError as e:
        raise wrap_error(e, f"head {key}") from e


def get_bytes(key: str) -> bytes:
    try:
        obj = client().get_object(Bucket=settings.s3_bucket, Key=key)
        return obj["Body"].read()
    except ClientError as e:
        raise StorageError(wrap_error(e, f"read {key}").message) from e


def iter_chunks(key: str, chunk_size: int = _STREAM_CHUNK_SIZE) -> Iterator[bytes]:
    """Sync generator yielding S3 object bytes. FastAPI's
    StreamingResponse iterates it in a thread pool so the boto3 blocking
    I/O doesn't stall the event loop. A failure mid-stream truncates
    the response (headers are already sent), so we log and re-raise as
    StorageError to surface the cause."""
    try:
        obj = client().get_object(Bucket=settings.s3_bucket, Key=key)
    except ClientError as e:
        raise StorageError(wrap_error(e, f"read {key}").message) from e
    body = obj["Body"]
    try:
        for chunk in body.iter_chunks(chunk_size=chunk_size):
            yield chunk
    except Exception as e:  # noqa: BLE001 — boto3/urllib3 surface various stream errors
        logger.warning("S3 stream %s aborted: %s", key, e)
        raise StorageError(f"stream {key}: {e}") from e
    finally:
        body.close()


def put_bytes(
    key: str, body: bytes, content_type: str = "application/octet-stream",
) -> None:
    try:
        client().put_object(
            Bucket=settings.s3_bucket, Key=key, Body=body, ContentType=content_type,
        )
    except ClientError as e:
        raise StorageError(wrap_error(e, f"write {key}").message) from e


def delete(key: str) -> None:
    try:
        client().delete_object(Bucket=settings.s3_bucket, Key=key)
    except ClientError as e:
        raise wrap_error(e, f"delete {key}") from e


# ── Presign ──────────────────────────────────────────────────────


def presign_get(
    key: str,
    *,
    ttl: int = _DEFAULT_PRESIGN_TTL,
    response_content_type: str | None = None,
    response_content_disposition: str | None = None,
) -> str:
    """Presigned GET URL for direct download from S3."""
    try:
        params: dict[str, Any] = {"Bucket": settings.s3_bucket, "Key": key}
        if response_content_type:
            params["ResponseContentType"] = response_content_type
        if response_content_disposition:
            params["ResponseContentDisposition"] = response_content_disposition
        return presign_client().generate_presigned_url(
            "get_object", Params=params, ExpiresIn=ttl,
        )
    except ClientError as e:
        raise StorageError(wrap_error(e, f"presign download {key}").message) from e


def presign_put(
    key: str,
    *,
    content_type: str = "application/octet-stream",
    ttl: int = _DEFAULT_PRESIGN_TTL,
) -> str:
    """Presigned PUT URL for direct upload to S3 by an external client."""
    try:
        return presign_client().generate_presigned_url(
            "put_object",
            Params={"Bucket": settings.s3_bucket, "Key": key, "ContentType": content_type},
            ExpiresIn=ttl,
        )
    except ClientError as e:
        raise wrap_error(e, f"presign upload {key}") from e
