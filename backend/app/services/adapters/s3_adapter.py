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

_STREAM_CHUNK_SIZE = 64 * 1024
_PRESIGN_CLOCK_MARGIN = 60


# botocore's default is 10, which was ample while every byte moved directly
# between the client and the object store. Both transfer directions now go
# through this service: a download holds one connection for the whole object,
# and concurrent uploads each take one per part. The bounded slots in the file
# routes can reach twelve between them before any ordinary head/get/put asks
# for one, and exceeding the pool does not fail — urllib3 quietly opens and
# discards a connection per call instead, which is worse than sizing it.
_MAX_POOL_CONNECTIONS = 32


def _boto_config(*, endpoint_url: str = ""):
    return BotoConfig(
        signature_version="s3v4",
        max_pool_connections=_MAX_POOL_CONNECTIONS,
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


_internal_presign_client = None


def internal_presign_client():
    """A signer bound to the INTERNAL endpoint.

    There used to be a second signer here, bound to `s3_public_url`, for URLs
    handed to a browser. It was retired along with the browser-facing presign,
    and the internal endpoint is the only one left for a reason: the byte
    gateway sits inside the cluster, so a URL naming the public host would
    send it back out through the ingress the gateway exists to replace.

    It registers the `before-sign` hook that pins a frozen credential to one
    signature. Registering it is only half the wiring — the hook reads a
    ContextVar that `presign_internal_get` sets, and returns immediately when
    that is unset. Both halves or neither.
    """
    global _internal_presign_client
    if _internal_presign_client is None:
        with _client_lock:
            if _internal_presign_client is None:
                _internal_presign_client = session_client(
                    settings.s3_endpoint_url, settings.s3_region,
                )
                _internal_presign_client.meta.events.register(
                    "before-sign.s3", _bind_signing_snapshot,
                )
    return _internal_presign_client


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


def iter_chunks(
    key: str,
    chunk_size: int = _STREAM_CHUNK_SIZE,
    *,
    byte_range: str | None = None,
) -> Iterator[bytes]:
    """Sync generator yielding S3 object bytes. FastAPI's
    StreamingResponse iterates it in a thread pool so the boto3 blocking
    I/O doesn't stall the event loop. A failure mid-stream truncates
    the response (headers are already sent), so we log and re-raise as
    StorageError to surface the cause.

    ``byte_range`` is passed through verbatim as the S3 ``Range`` parameter
    (``bytes=start-end``). Serving a range through this process is what keeps
    a resumable download resumable once the client no longer talks to the
    store directly."""
    params: dict[str, Any] = {"Bucket": settings.s3_bucket, "Key": key}
    if byte_range:
        params["Range"] = byte_range
    try:
        obj = client().get_object(**params)
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


# Multipart primitives. The upload route receives a request body it cannot
# size in advance and must not hold in memory — the largest stored File is
# multi-gigabyte. Each call is one blocking boto3 operation; the service layer
# sequences them off the event loop.
MULTIPART_MIN_PART_BYTES = 8 * 1024 * 1024


def multipart_create(key: str, content_type: str) -> str:
    try:
        r = client().create_multipart_upload(
            Bucket=settings.s3_bucket, Key=key, ContentType=content_type,
        )
    except ClientError as e:
        raise StorageError(wrap_error(e, f"begin write {key}").message) from e
    return str(r["UploadId"])


def multipart_part(key: str, upload_id: str, part_number: int, body: bytes) -> str:
    try:
        r = client().upload_part(
            Bucket=settings.s3_bucket, Key=key, UploadId=upload_id,
            PartNumber=part_number, Body=body,
        )
    except ClientError as e:
        raise StorageError(wrap_error(e, f"write part {part_number} of {key}").message) from e
    return str(r["ETag"])


def multipart_complete(key: str, upload_id: str, parts: list[dict[str, Any]]) -> None:
    """Publish the parts as one object.

    Returns nothing on purpose. Reading the object back afterwards was a
    round trip whose result no caller used, and a failure in it would have
    reported the completed upload as missing — answering an error, and
    aborting an upload that no longer exists, for bytes that are stored.
    """
    try:
        client().complete_multipart_upload(
            Bucket=settings.s3_bucket, Key=key, UploadId=upload_id,
            MultipartUpload={"Parts": parts},
        )
    except ClientError as e:
        raise StorageError(wrap_error(e, f"finish write {key}").message) from e


def multipart_abort(key: str, upload_id: str) -> None:
    """Best effort. An abandoned multipart upload is billable storage that no
    listing shows, so it is worth attempting even while handling another
    failure — but never worth masking that failure with this one."""
    try:
        client().abort_multipart_upload(
            Bucket=settings.s3_bucket, Key=key, UploadId=upload_id,
        )
    except Exception as e:  # noqa: BLE001 — abort runs on an error path
        logger.warning("abandoned multipart upload %s (%s): %s", key, upload_id, e)


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


def presign_internal_get(
    key: str,
    *,
    ttl: int,
    content_type: str,
    content_disposition: str,
    cache_control: str,
) -> PresignedURL:
    """Sign one object read for the byte gateway, policy included.

    The response headers travel in the signature rather than being re-applied
    downstream. The object store honours `response-content-*` on a signed
    request (measured against the deployed RGW), so the gateway needs no
    `add_header` of its own for them — and that matters more than it sounds:
    a single `add_header` inside an nginx location silently drops every header
    inherited from the server block, which is where the unconditional security
    headers live.
    """
    if isinstance(ttl, bool) or not isinstance(ttl, int) or ttl < 1:
        raise StorageError("Presign lifetime must be a positive integer")

    # A temporary session's remaining life bounds the signature: a URL that
    # outlives the credential which signed it is refused at the moment it is
    # used, which here means mid-transfer. Freeze the credential and clamp the
    # lifetime to it, less a margin for clock skew.
    credentials, expiry = frozen_snapshot(_credential_session())
    if expiry is not None:
        remaining = int(
            (expiry - datetime.now(timezone.utc)).total_seconds()
        ) - _PRESIGN_CLOCK_MARGIN
        ttl = min(ttl, remaining)
    if ttl < 1:
        raise StorageError("S3 session has insufficient lifetime for a presigned URL")

    params = {
        "Bucket": settings.s3_bucket,
        "Key": key,
        "ResponseContentType": content_type,
        "ResponseContentDisposition": content_disposition,
        "ResponseCacheControl": cache_control,
    }
    # The frozen credential reaches the signer through this variable and the
    # `before-sign` hook the client registers. Setting it is what makes that
    # hook do anything: it returns immediately when the variable is unset, so
    # a signer that registers the hook and skips this step silently loses the
    # clamp — which is exactly what the first version of this function did.
    context_token = _signing_snapshot.set((credentials, expiry, ttl))
    try:
        url = internal_presign_client().generate_presigned_url(
            "get_object", Params=params, ExpiresIn=ttl,
        )
        # The lifetime actually signed, not the one asked for. A clamp nobody
        # can observe is a clamp that can quietly stop working.
        return PresignedURL(url, ttl)
    except ClientError as e:
        raise StorageError(wrap_error(e, f"sign read {key}").message) from e
    finally:
        _signing_snapshot.reset(context_token)


