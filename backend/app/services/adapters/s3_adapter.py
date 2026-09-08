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
import threading
from contextvars import ContextVar
from datetime import datetime, timedelta, timezone
from typing import Any, Iterator, NamedTuple

import boto3
from botocore.config import Config as BotoConfig
from botocore.exceptions import ClientError

from app.config import settings
from app.exceptions import AKBError, NotFoundError
from app.services.adapters.s3_credentials import build_session, frozen_snapshot

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
_session = None
_client_lock = threading.RLock()
_bucket_verified: set[str] = set()


class PresignedURL(NamedTuple):
    url: str
    expires_in: int


# A request-local snapshot prevents one thread's refresh from changing another
# thread's presign keys after its TTL was calculated. No shared signer mutates.
_signing_snapshot: ContextVar[tuple[Any, datetime | None, int] | None] = ContextVar(
    "s3_signing_snapshot", default=None,
)

_DEFAULT_PRESIGN_TTL = 3600
_STREAM_CHUNK_SIZE = 64 * 1024
_PRESIGN_CLOCK_MARGIN = 60


def _boto_config(*, endpoint_url: str = ""):
    return BotoConfig(
        signature_version="s3v4",
        connect_timeout=settings.s3_connect_timeout_secs,
        read_timeout=settings.s3_read_timeout_secs,
        retries={"max_attempts": settings.s3_max_attempts, "mode": "standard"},
        s3={"addressing_style": "path" if endpoint_url else "auto"},
        ignore_configured_endpoint_urls=settings.model_api_governance_mode == "platform_hard",
    )


def make_client(endpoint_url: str, access_key: str, secret_key: str, region: str = ""):
    """Build a boto3 S3 client for an explicit endpoint + credential set.

    The single place that knows the boto config (sigv4, optional region),
    so the primary file store (`client`/`presign_client`) and a
    credential-isolated audit store (`audit_log`) all construct clients the
    same way instead of each re-deriving the boto kwargs."""
    if settings.model_api_governance_mode == "platform_hard":
        raise StorageError("Static S3 clients are forbidden in platform_hard mode")
    return boto3.client(
        "s3",
        endpoint_url=endpoint_url or None,
        aws_access_key_id=access_key,
        aws_secret_access_key=secret_key,
        config=_boto_config(endpoint_url=endpoint_url),
        **({"region_name": region} if region else {}),
    )


def _credential_session():
    global _session
    with _client_lock:
        if _session is None:
            _session = build_session(settings)
        return _session


def session_client(endpoint_url: str, region: str = ""):
    """Build an S3 client sharing the selected, refreshable runtime session."""
    with _client_lock:
        return _credential_session().client(
            "s3", endpoint_url=endpoint_url or None,
            config=_boto_config(endpoint_url=endpoint_url),
            **({"region_name": region} if region else {}),
        )


def client():
    """boto3 S3 client targeting the internal endpoint. Used for
    server-side operations (head, get, put, delete)."""
    global _internal_client
    with _client_lock:
        if _internal_client is None:
            _internal_client = session_client(settings.s3_endpoint_url, settings.s3_region)
    return _internal_client


def presign_client():
    """boto3 S3 client targeting the public endpoint. Used to sign URLs
    that clients reach from outside the cluster. Falls back to the
    internal endpoint when no public URL is configured."""
    global _presign_client
    with _client_lock:
        if _presign_client is None:
            _presign_client = session_client(
                settings.s3_public_url or settings.s3_endpoint_url, settings.s3_region,
            )
            _presign_client.meta.events.register("before-sign.s3", _bind_signing_snapshot)
    return _presign_client


def _bind_signing_snapshot(request, **_kwargs):
    snapshot = _signing_snapshot.get()
    if snapshot is None or not request.context.get("is_presign_request"):
        return
    credentials, expiry, ttl = snapshot
    if expiry is not None and datetime.now(timezone.utc) + timedelta(seconds=ttl) >= expiry:
        raise StorageError("S3 session expired before signing")
    # Botocore's request-specific credential hook avoids replacing the shared
    # client's credentials. The pinned-runtime tests exercise actual SigV4.
    request.context.setdefault("signing", {})["request_credentials"] = credentials


def _presign(operation: str, params: dict[str, Any], ttl: int) -> PresignedURL:
    if isinstance(ttl, bool) or not isinstance(ttl, int) or ttl < 1:
        raise StorageError("Presign lifetime must be a positive integer")
    signer = presign_client()
    credentials, expiry = frozen_snapshot(_credential_session())
    if expiry is not None:
        remaining = int((expiry - datetime.now(timezone.utc)).total_seconds()) - _PRESIGN_CLOCK_MARGIN
        ttl = min(ttl, remaining)
    if ttl < 1:
        raise StorageError("S3 session has insufficient lifetime for a presigned URL")
    context_token = _signing_snapshot.set((credentials, expiry, ttl))
    try:
        url = signer.generate_presigned_url(operation, Params=params, ExpiresIn=ttl)
        return PresignedURL(url, ttl)
    finally:
        _signing_snapshot.reset(context_token)


def ensure_bucket(bucket: str) -> None:
    """Head-only for managed/native-chain storage; standalone static can create."""
    with _client_lock:
        if bucket in _bucket_verified:
            return
        s3 = client()
        try:
            s3.head_bucket(Bucket=bucket)
        except ClientError as e:
            code = e.response["Error"].get("Code", "")
            if (code in ("404", "NoSuchBucket") and settings.s3_auth_mode == "static"
                    and settings.model_api_governance_mode != "platform_hard"):
                s3.create_bucket(Bucket=bucket)
                logger.info("Created S3 bucket: %s", bucket)
            else:
                raise wrap_error(e, "check bucket") from e
        _bucket_verified.add(bucket)


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


def copy(source_key: str, destination_key: str) -> dict[str, Any]:
    """Copy one object to another key and return the destination metadata.

    File replacement uploads land on a short-lived staging key. The server
    copies those bytes to a fresh, non-presigned key before publishing the
    metadata switch, so the still-valid upload URL cannot mutate the live
    object after confirmation.
    """
    try:
        # Managed copy automatically switches to multipart copy above the
        # single CopyObject limit, preserving the proxy's large-file contract.
        client().copy(
            {"Bucket": settings.s3_bucket, "Key": source_key},
            settings.s3_bucket,
            destination_key,
        )
    except ClientError as e:
        raise StorageError(
            wrap_error(e, f"copy {source_key} to {destination_key}").message
        ) from e
    except Exception as e:  # noqa: BLE001 — managed transfer wraps worker failures
        logger.error("S3 managed copy failed: %s", e)
        raise StorageError("copy failed") from e
    return head(destination_key)


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
) -> PresignedURL:
    """Presigned GET URL for direct download from S3."""
    try:
        params: dict[str, Any] = {"Bucket": settings.s3_bucket, "Key": key}
        if response_content_type:
            params["ResponseContentType"] = response_content_type
        if response_content_disposition:
            params["ResponseContentDisposition"] = response_content_disposition
        return _presign("get_object", params, ttl)
    except ClientError as e:
        raise StorageError(wrap_error(e, f"presign download {key}").message) from e


def presign_put(
    key: str,
    *,
    content_type: str = "application/octet-stream",
    ttl: int = _DEFAULT_PRESIGN_TTL,
) -> PresignedURL:
    """Presigned PUT URL for direct upload to S3 by an external client."""
    try:
        return _presign(
            "put_object",
            {"Bucket": settings.s3_bucket, "Key": key, "ContentType": content_type},
            ttl,
        )
    except ClientError as e:
        raise wrap_error(e, f"presign upload {key}") from e
