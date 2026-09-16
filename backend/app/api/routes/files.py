"""REST API routes for vault file storage (S3-backed)."""

import asyncio
import hmac
import re

from fastapi import APIRouter, Depends, Query, Request, Response
from fastapi.responses import StreamingResponse

from app.api.bounded_body import read_bounded_body
from app.api.deps import get_current_user
from app.api.file_write_context import (
    resolve_file_write_context as _resolve_file_write_context,
)
from app.config import settings
from app.exceptions import AKBError, NotFoundError
from app.models.file import BodyPlacementObservation
from app.services.access_service import check_vault_access
from app.services.auth_service import AuthenticatedUser
from app.services.adapters.s3_adapter import presign_internal_get
from app.services.file_service import (
    FileService,
    content_disposition_inline,
    head_object,
    iter_object_chunks,
    store_object_stream,
)
from app.services.raw_mime_policy import is_inert_raw_mime
from app.util.text import normalize_content_type, to_nfc

router = APIRouter()
# Mounted under a prefix no ingress routes, so it is unreachable from outside
# even before the shared key is checked. See `authorize_gateway_download`.
internal_router = APIRouter()
file_service = FileService()
_measurement_transfer_slots = asyncio.Semaphore(2)
# Bounded like its sibling above. A download streams one GET response for the
# whole object, so it holds a botocore connection and an anyio thread from the
# first byte to the last.
#
# Shared with the public publication download, which streams the same way from
# the same pool. They are one budget because they are one resource, and that
# path answers the open internet with no credential at all.
_download_capability_slots = asyncio.Semaphore(4)
# An upload bounds something different, and the number reflects that. Each
# part is a discrete request, so the connection and the thread go back between
# parts; what is held for the whole transfer is the part buffer. Eight of them
# is a modest fixed cost, and the extra slots matter because here it is the
# *client* that sets the pace — a handful of slow senders must not be able to
# block every other upload.
_upload_capability_slots = asyncio.Semaphore(8)


@router.api_route("/files/transfer/{token}", methods=["PUT", "GET"], include_in_schema=False)
async def measurement_file_transfer(token: str, request: Request):
    """Guarded opaque CAS transfer endpoint used by the existing file proxy.

    It has no user-token dependency because the short-lived, scoped transfer
    capability is its authorization.  The service only exposes this route
    while the dedicated M1 measurement guard is active.
    """
    async with _measurement_transfer_slots:
        if request.method == "PUT":
            body = await read_bounded_body(
                request,
                max_bytes=settings.native_revision_m1_file_transfer_max_bytes,
                too_large_message="measurement transfer exceeds configured size limit",
                invalid_length_message="invalid measurement transfer content-length",
            )
            await file_service.transfer_measurement_capability(
                token, method="PUT", body=body,
            )
            return Response(status_code=200)
        data = await file_service.transfer_measurement_capability(token, method="GET")
        return Response(content=data or b"", media_type="application/octet-stream")


@router.api_route(
    "/files/upload/{token}", methods=["PUT"], include_in_schema=False,
)
async def upload_by_capability(token: str, request: Request):
    """Accept File bytes from a holder of an upload capability.

    No user-token dependency: the short-lived capability is the authorization,
    exactly as a presigned signature was, and it names the one key these bytes
    may land on. The public contract is the `upload_url` field, not this path,
    so it stays out of the schema.

    Registered ahead of every `/files/{vault}/...` route. Nothing can collide
    with it today — a capability token cannot spell `upload` — but the order
    is what makes that true regardless of what a vault is called.
    """
    grant = await file_service.resolve_write_capability(token)
    max_bytes = settings.file_upload_max_bytes

    # Reject an oversized upload before reading any of it. The streaming
    # counter inside `store_object_stream` is the authority — a header can be
    # absent or wrong — but honouring it here saves transferring bytes that
    # are going to be refused.
    declared = request.headers.get("content-length")
    declared_size: int | None = None
    if declared is not None:
        try:
            declared_size = int(declared)
        except ValueError as exc:
            raise AKBError("Invalid Content-Length", status_code=400) from exc
        if declared_size < 0:
            raise AKBError("Invalid Content-Length", status_code=400)
        if declared_size > max_bytes:
            raise AKBError(
                "Upload exceeds the maximum accepted size", status_code=413,
            )

    if grant["already_confirmed"]:
        # This reservation deduplicated onto a File whose bytes are final.
        # The key is content-addressed, so bytes that belong there are
        # already there and re-sending them changes nothing; bytes that do
        # not belong there must never reach it. Writing them would not merely
        # replace the File's content — `confirm` would then find the stored
        # digest disagreeing with the key it is stored under and DELETE the
        # row, its publications and the object. A caller who knows a File's
        # collection, name and hash, all of which a reader can see, could
        # destroy it.
        #
        # The body is read and discarded rather than refused, because two
        # shipped clients ignore `deduplicated` and PUT unconditionally. They
        # get the 200 they expect and the File is untouched either way.
        await _drain(request)
        return Response(status_code=200)

    # Held for the whole transfer. Unlike the download route this is a plain
    # `async with`: the response is sent after the body has been consumed, so
    # the slot's lifetime really is this function's.
    async with _upload_capability_slots:
        await store_object_stream(
            grant["object_key"],
            request.stream(),
            content_type=grant["mime_type"],
            max_bytes=max_bytes,
            declared_bytes=declared_size,
        )
    # No ETag header: the presigned PUT returned the object store's and no
    # caller read it. AKB certifies these bytes at `confirm`, from the stored
    # object rather than from anything the client was told here.
    return Response(status_code=200)


@router.post("/files/{vault}/upload", summary="Reserve an upload and get its URL")
async def upload_file(
    request: Request,
    vault: str,
    filename: str = Query(..., description="Original filename"),
    collection: str = Query("", description="Logical grouping"),
    description: str = Query("", description="File description"),
    mime_type: str = Query("application/octet-stream", description="MIME type"),
    content_hash: str | None = Query(
        None,
        description=(
            "Optional sha256 of the bytes about to be uploaded. Supplying it "
            "makes the upload idempotent: the same bytes under the same "
            "vault/collection/filename resolve to the existing file instead of "
            "creating a duplicate. Omit for the original one-row-per-call "
            "behaviour."
        ),
    ),
    user: AuthenticatedUser = Depends(get_current_user),
):
    """Reserve a file and return the absolute URL its bytes are PUT to."""
    access, actor_id, _delegated_actor = await _resolve_file_write_context(
        request, vault, user,
    )
    return await file_service.initiate_upload(
        vault_name=vault,
        vault_id=access["vault_id"],
        collection=to_nfc(collection),
        filename=to_nfc(filename),
        actor_id=actor_id,
        mime_type=mime_type,
        description=to_nfc(description),
        content_hash=content_hash,
    )


@router.post("/files/{vault}/{file_id}/confirm", summary="Confirm upload completion (recovery)")
async def confirm_upload(
    request: Request,
    vault: str,
    file_id: str,
    content_hash: str | None = Query(None, description="Optional client-computed sha256 of uploaded bytes"),
    hash_algorithm: str = Query("sha256", description="Hash algorithm for content_hash"),
    user: AuthenticatedUser = Depends(get_current_user),
):
    """Called once the bytes are uploaded. Certifies size and hash from storage."""
    access, actor_id, delegated_actor = await _resolve_file_write_context(
        request, vault, user,
    )
    return await file_service.confirm_upload(
        access["vault_id"], file_id, actor_id=actor_id,
        delegated_actor=delegated_actor,
        content_hash=content_hash, hash_algorithm=hash_algorithm,
    )


@router.post("/files/{vault}/{file_id}/replace", summary="Prepare an in-place file replacement")
async def replace_file(
    request: Request,
    vault: str,
    file_id: str,
    content_hash: str = Query(..., description="sha256 of the replacement bytes"),
    mime_type: str | None = Query(
        None,
        description="Replacement MIME type; preserves the existing type when omitted",
    ),
    expected_content_hash: str | None = Query(
        None,
        description="Reject with 409 unless the current file hash matches",
    ),
    expected_version: str | None = Query(
        None,
        description="Reject with 409 unless the current opaque file version matches",
    ),
    user: AuthenticatedUser = Depends(get_current_user),
):
    """Return an isolated upload URL for the replacement, or ``unchanged=true``."""
    access, actor_id, _delegated_actor = await _resolve_file_write_context(
        request, vault, user,
    )
    return await file_service.initiate_replace(
        vault_name=vault,
        vault_id=access["vault_id"],
        file_id=file_id,
        actor_id=actor_id,
        content_hash=content_hash,
        mime_type=mime_type,
        expected_content_hash=expected_content_hash,
        expected_version=expected_version,
    )


@router.post(
    "/files/{vault}/{file_id}/replace/{replacement_id}/confirm",
    summary="Confirm an in-place file replacement",
)
async def confirm_file_replace(
    request: Request,
    vault: str,
    file_id: str,
    replacement_id: str,
    content_hash: str = Query(..., description="sha256 of the replacement bytes"),
    expected_content_hash: str | None = Query(
        None,
        description="Reject with 409 unless the current file hash matches",
    ),
    expected_version: str | None = Query(
        None,
        description="Reject with 409 unless the current opaque file version matches",
    ),
    user: AuthenticatedUser = Depends(get_current_user),
):
    """Certify staged bytes and atomically publish them under the same URI."""
    access, actor_id, delegated_actor = await _resolve_file_write_context(
        request, vault, user,
    )
    return await file_service.confirm_replace(
        vault_name=vault,
        vault_id=access["vault_id"],
        file_id=file_id,
        replacement_id=replacement_id,
        actor_id=actor_id,
        delegated_actor=delegated_actor,
        content_hash=content_hash,
        expected_content_hash=expected_content_hash,
        expected_version=expected_version,
    )


async def _drain(request: Request) -> None:
    """Consume a request body without storing it.

    Not skipped: a client that is mid-`PUT` when the response arrives sees a
    connection error rather than its 200, and two shipped clients send the
    body unconditionally.
    """
    async for _chunk in request.stream():
        pass


_SINGLE_RANGE_RE = re.compile(r"^bytes=(\d*)-(\d*)$")


def _parse_single_range(header: str | None, size: int) -> tuple[int, int] | None:
    """Resolve one `bytes=` range against a known size, or None.

    Only the single-range form is honoured. Multipart ranges need a
    multipart/byteranges body, no browser asks for them here, and answering
    200 to a range we do not understand is the correct fallback."""
    if not header or size <= 0:
        return None
    m = _SINGLE_RANGE_RE.match(header.strip())
    if not m:
        return None
    raw_start, raw_end = m.group(1), m.group(2)
    if raw_start == "" and raw_end == "":
        return None
    if raw_start == "":
        # `bytes=-N` — the final N bytes.
        length = int(raw_end)
        if length <= 0:
            return None
        start, end = max(0, size - length), size - 1
    else:
        start = int(raw_start)
        end = int(raw_end) if raw_end else size - 1
        if start >= size or end < start:
            return None
        end = min(end, size - 1)
    return start, end


def _raw_download_policy(row: dict) -> dict[str, str]:
    """The response headers one File's bytes are served under.

    One function because there are now two ways those bytes reach a client —
    streamed by this process, or carried by the byte gateway — and a policy
    that exists in two copies is a policy that will disagree with itself. The
    gateway gets these same values through the signature it is handed.
    """
    mime = normalize_content_type(row["mime_type"])
    headers = {
        "Content-Type": mime,
        "X-Content-Type-Options": "nosniff",
        "Cache-Control": "private, no-store",
        "Content-Disposition": content_disposition_inline(
            row["name"] or "download",
        ),
    }
    if not is_inert_raw_mime(mime):
        # Fail CLOSED. These bytes were uploaded by someone; a type we cannot
        # prove inert must not run as a document in this origin.
        headers["Content-Security-Policy"] = "sandbox allow-same-origin"
    return headers


@router.api_route(
    "/files/download/{token}", methods=["GET"], include_in_schema=False,
)
async def download_by_capability(token: str, request: Request):
    """Serve File bytes to a holder of a download capability.

    No user-token dependency: the short-lived capability is the authorization,
    exactly as a presigned signature was. The public contract is the
    `download_url` field, not this path, so it stays out of the schema.

    Still the only byte path in a deployment without the gateway — the
    all-in-one compose file and the Helm chart both route this prefix
    straight here — so it keeps its own range handling and streaming.
    """
    row = await file_service.resolve_download_capability(token)
    headers = _raw_download_policy(row)
    mime = headers.pop("Content-Type")

    if request.method == "HEAD":
        # Defensive, and measured: FastAPI's APIRoute does NOT add HEAD to a
        # GET route (unlike Starlette's plain Route), so nothing reaches this
        # today. It matters if HEAD is ever added to `methods`, because
        # StreamingResponse has no HEAD branch — it would drain the whole
        # object to send no body at all. Answer from what we already know.
        headers["Content-Length"] = str(row["size_bytes"] or 0)
        headers["Accept-Ranges"] = "bytes"
        return Response(status_code=200, media_type=mime, headers=headers)

    # Serving bytes through this process replaces what used to be a direct
    # browser→store transfer with no application involvement. That had a
    # perfect implicit limit; this needs an explicit one, or a handful of
    # multi-gigabyte reads starve every other request sharing anyio's thread
    # limiter — the same tokens every sync route handler draws from.
    #
    # The slot is held for the transfer, not for this function: a
    # StreamingResponse has barely started when we return. Releasing in the
    # generator's `finally` covers a client that disconnects mid-stream too,
    # which a response-completion hook does not.
    loop = asyncio.get_running_loop()
    await _download_capability_slots.acquire()

    # HEAD the object before committing a 200: a missing object becomes a clean
    # 502 rather than a silently truncated body. It also gives us the real
    # size — the DB figure can drift from what is stored.
    try:
        meta = await asyncio.to_thread(head_object, row["s3_key"])
    except Exception as exc:  # noqa: BLE001 — any storage failure is a 502 here
        _download_capability_slots.release()
        raise AKBError("File content is temporarily unavailable", status_code=502) from exc
    stored_size = meta.get("ContentLength")
    size = stored_size if isinstance(stored_size, int) else 0

    # The object store answered ranges directly before this route existed, and
    # the largest stored File is multi-gigabyte: losing resume would mean a
    # download that fails at 90% starts over. Honour a single range here.
    span = _parse_single_range(request.headers.get("range"), size)
    status, byte_range = 200, None
    if span is not None:
        start, end = span
        status, byte_range = 206, f"bytes={start}-{end}"
        headers["Content-Range"] = f"bytes {start}-{end}/{size}"
        headers["Content-Length"] = str(end - start + 1)
    elif size:
        # Taken from the object, not the row, so a drift cannot truncate the
        # wire. Without it every response is chunked and the browser shows no
        # progress at all — which the presigned path did not do.
        headers["Content-Length"] = str(size)
    if size:
        headers["Accept-Ranges"] = "bytes"

    def _bounded_chunks():
        # Runs in anyio's threadpool, so the release must be marshalled back:
        # `asyncio.Semaphore.release` is not thread-safe.
        try:
            yield from iter_object_chunks(row["s3_key"], byte_range=byte_range)
        finally:
            loop.call_soon_threadsafe(_download_capability_slots.release)

    return StreamingResponse(
        _bounded_chunks(), status_code=status, media_type=mime, headers=headers,
    )


@internal_router.get(
    "/files/download/{token}", include_in_schema=False,
)
async def authorize_gateway_download(token: str, request: Request):
    """Answer the byte gateway's authorization subrequest.

    Returns no body. On success it hands back a signed URL the gateway may
    fetch the object with, plus the response headers that URL will produce —
    the policy is decided here, as it always was, and travels in the
    signature rather than being re-applied downstream.

    Reachability: no ingress maps this prefix, and `file_gateway_key` is the
    second lock. Unset, the route answers 404 — a deployment without a
    gateway has nothing here to find. A wrong key fails every download at
    once rather than leaking quietly, which is the failure worth having.

    Every refusal is 401 with no body. The gateway maps that to the 404 a
    client sees today. It must not be 403: the gateway reads 403 from its
    upstream as a storage failure and would answer 502.
    """
    expected = settings.file_gateway_key
    presented = request.headers.get("x-akb-gateway-key") or ""
    if not expected or not hmac.compare_digest(presented, expected):
        # 404, not 401: the gateway maps 401 to the client-facing 404 for a
        # refused capability, and a misconfigured key must not look like one.
        # This path becomes a 503 at the gateway — every download fails at
        # once, which is how a wrong shared secret should announce itself.
        return Response(status_code=404)

    try:
        row = await file_service.resolve_download_capability(token)
    except NotFoundError:
        # Shape, expiry, never-issued and since-deleted stay one answer.
        return Response(status_code=401)

    headers = _raw_download_policy(row)
    signed = await asyncio.to_thread(
        presign_internal_get,
        row["s3_key"],
        ttl=settings.file_gateway_presign_ttl,
        content_type=headers["Content-Type"],
        content_disposition=headers["Content-Disposition"],
        cache_control=headers["Cache-Control"],
    )
    # The gateway reads only this. The rest of `headers` is what the object
    # store will emit because it is what was signed.
    return Response(status_code=204, headers={"X-AKB-S3": signed})


@router.get("/files/{vault}/{file_id}/download", summary="Get download URL")
async def get_download_url(
    vault: str,
    file_id: str,
    user: AuthenticatedUser = Depends(get_current_user),
):
    access = await check_vault_access(user.user_id, vault, required_role="reader")
    return await file_service.get_download_url(
        access["vault_id"], file_id, actor_id=user.username,
    )


@router.get("/files/{vault}", summary="List files in vault storage")
async def list_files(
    vault: str,
    collection: str | None = Query(None),
    limit: int = Query(50, ge=1, le=200),
    user: AuthenticatedUser = Depends(get_current_user),
):
    access = await check_vault_access(user.user_id, vault, required_role="reader")
    files = await file_service.list_files(access["vault_id"], vault, collection, limit)
    return {"kind": "file", "vault": vault, "items": files, "total": len(files)}


@router.get(
    "/files/{vault}/body-placements",
    response_model=BodyPlacementObservation,
    operation_id="filesGetVaultBodyPlacements",
    summary="Body placement census for a vault (measurement builds only)",
)
async def get_body_placements(
    vault: str,
    user: AuthenticatedUser = Depends(get_current_user),
):
    """Report which native body placements a vault's bodies still use.

    Makes the M1 placement decision observable from outside without exposing
    any internal address: the response is an aggregate of counts and byte sums
    keyed by the closed placement identifiers, and carries no resource id, no
    payload id, no locator, and no digest values.

    Authorization matches the neighbouring measurement read `GET /files/{vault}`
    (vault reader): the census is strictly less specific than the file listing
    a reader can already fetch, so it introduces no new permission model.

    Deployments that keep the direct-S3 File driver have no measurement facade
    at all, so this route answers 404 there — same discipline as the guarded
    transfer capability. It stays in the published schema because, unlike that
    route, its path holds no secret and clients need a typed shape for it.
    """
    access = await check_vault_access(user.user_id, vault, required_role="reader")
    return await file_service.namespace_placement_observation(access["vault_id"], vault)


@router.get("/files/{vault}/{file_id}", summary="Get one file's metadata")
async def get_file(
    vault: str,
    file_id: str,
    user: AuthenticatedUser = Depends(get_current_user),
):
    """Resolve one File by id.

    Registered AFTER the literal `/files/{vault}/body-placements` route so that
    path keeps matching itself rather than being read as a file id."""
    access = await check_vault_access(user.user_id, vault, required_role="reader")
    return await file_service.get_file(access["vault_id"], vault, file_id)


@router.delete("/files/{vault}/{file_id}", summary="Delete a file")
async def delete_file(
    vault: str,
    file_id: str,
    user: AuthenticatedUser = Depends(get_current_user),
):
    access = await check_vault_access(user.user_id, vault, required_role="writer")
    return await file_service.delete(
        access["vault_id"], file_id, actor_id=user.username,
    )
