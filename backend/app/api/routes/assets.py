"""Authenticated upload and stable byte delivery for document images."""

from __future__ import annotations

import asyncio
import logging
import uuid

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import Response, StreamingResponse

from app.api.deps import get_current_user
from app.api.file_write_context import resolve_file_write_context
from app.db.postgres import get_pool
from app.exceptions import AKBError, ForbiddenError, NotFoundError
from app.config import settings
from app.repositories import vault_files_repo
from app.services import asset_service, file_service
from app.services.access_service import check_vault_access
from app.services.auth_service import AuthenticatedUser
from app.services.s3_delete_worker import enqueue_delete
from app.util.text import to_nfc


router = APIRouter()
stable_router = APIRouter()
logger = logging.getLogger("akb.assets")
_asset_body_slots = asyncio.Semaphore(8)
_asset_transfer_slots = asyncio.Semaphore(4)


async def _read_bounded_image(request: Request) -> bytes:
    declared = request.headers.get("content-length")
    if declared is not None:
        try:
            declared_size = int(declared)
        except ValueError as exc:
            raise AKBError("Invalid Content-Length", status_code=400) from exc
        if declared_size < 0:
            raise AKBError("Invalid Content-Length", status_code=400)
        if declared_size > asset_service.IMAGE_ASSET_MAX_BYTES:
            raise AKBError("Image exceeds the 10 MB limit", status_code=413)

    body = bytearray()
    async for chunk in request.stream():
        body.extend(chunk)
        if len(body) > asset_service.IMAGE_ASSET_MAX_BYTES:
            raise AKBError("Image exceeds the 10 MB limit", status_code=413)
    return bytes(body)


async def _asset_write_actor(
    request: Request,
    vault: str,
    user: AuthenticatedUser,
) -> tuple[dict, str]:
    access, actor_id, _delegated_actor = await resolve_file_write_context(
        request,
        vault,
        user,
    )
    return access, actor_id


@router.post("/assets/{vault}", status_code=201, summary="Upload a document image")
async def upload_document_image(
    request: Request,
    vault: str,
    filename: str = Query(..., min_length=1, max_length=255),
    user: AuthenticatedUser = Depends(get_current_user),
):
    """Proxy one bounded raster image to object storage.

    This is intentionally a raw-body endpoint rather than multipart or a
    browser-to-S3 presigned PUT.  The route can enforce the real transfer size,
    sniff bytes before publication, and works on deployments that have no
    browser-facing object-store/CORS configuration.
    """
    access, actor_id = await _asset_write_actor(request, vault, user)
    # Slow request bodies have a separate, bounded admission pool. They cannot
    # occupy the scarcer Pillow/S3 slots, and the deadline prevents a writer
    # from keeping a body slot forever with a trickle upload. Holding the body
    # slot until transfer completion also caps resident upload buffers at 8.
    body_slot_acquired = False
    try:
        try:
            async with asyncio.timeout(settings.document_asset_upload_body_timeout_secs):
                await _asset_body_slots.acquire()
                body_slot_acquired = True
                body = await _read_bounded_image(request)
        except TimeoutError as exc:
            raise AKBError("Image upload body timed out", status_code=408) from exc

        async with _asset_transfer_slots:
            return await asset_service.create_image_asset(
                vault_id=access["vault_id"],
                vault_name=vault,
                filename=to_nfc(filename),
                declared_mime=request.headers.get("content-type", ""),
                body=body,
                actor_id=actor_id,
            )
    finally:
        if body_slot_acquired:
            _asset_body_slots.release()


async def load_asset_row(file_id: str, vault_id: uuid.UUID) -> dict:
    try:
        fid = uuid.UUID(file_id)
    except (ValueError, AttributeError) as exc:
        raise NotFoundError("Asset", file_id) from exc
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await vault_files_repo.find_attachment_by_id(conn, vault_id, fid)
    if row is None:
        raise NotFoundError("Asset", file_id)
    return row


async def image_asset_response(row: dict, *, public: bool = False) -> Response:
    size = row.get("size_bytes")
    if size is None or size < 1 or size > asset_service.IMAGE_ASSET_MAX_BYTES:
        raise NotFoundError("Asset", str(row.get("id", "")))
    try:
        # Fail before committing a 200 response when the immutable object is
        # missing or disagrees with its verified metadata. The body itself is
        # streamed so a document with many images does not multiply 10 MiB
        # buffers inside the API worker.
        metadata = await asyncio.to_thread(file_service.head_object, row["s3_key"])
        stored_size = metadata.get("ContentLength")
        if stored_size != size or stored_size > asset_service.IMAGE_ASSET_MAX_BYTES:
            raise ValueError("asset object size does not match verified metadata")
    except Exception as exc:  # noqa: BLE001 — storage drivers surface several error types
        logger.warning("asset storage read failed for %s: %s", row.get("id"), exc)
        raise AKBError("Image content is temporarily unavailable", status_code=502) from exc

    headers = {
        "Content-Disposition": "inline",
        "X-Content-Type-Options": "nosniff",
        "Cache-Control": "no-store" if public else "private, no-store",
    }
    if not public:
        headers["Vary"] = "Authorization"
    return StreamingResponse(
        file_service.iter_object_chunks(
            row["s3_key"], max_bytes=asset_service.IMAGE_ASSET_MAX_BYTES,
        ),
        media_type=row["mime_type"],
        headers=headers,
    )


@stable_router.get("/assets/{file_id}", response_class=Response, summary="Read a document image")
async def read_document_image(
    file_id: str,
    vault: str = Query(..., min_length=1),
    document: str | None = Query(None, min_length=1, max_length=1024),
    commit: str | None = Query(None, min_length=7, max_length=64, pattern=r"^[0-9a-fA-F]+$"),
    user: AuthenticatedUser = Depends(get_current_user),
):
    try:
        access = await check_vault_access(user.user_id, vault, required_role="reader")
    except (ForbiddenError, NotFoundError) as exc:
        # Normalize access failures so the stable UUID is not an existence
        # oracle across vaults.
        raise NotFoundError("Asset", file_id) from exc
    try:
        fid = uuid.UUID(file_id)
    except (ValueError, AttributeError) as exc:
        raise NotFoundError("Asset", file_id) from exc
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await vault_files_repo.find_authorized_attachment(
            conn,
            vault_id=access["vault_id"],
            file_id=fid,
            created_by=user.username,
            document_path=document,
            commit_prefix=commit,
        )
    if row is None:
        raise NotFoundError("Asset", file_id)
    return await image_asset_response(row)


@router.delete(
    "/assets/{vault}/{file_id}",
    status_code=204,
    summary="Discard an uncommitted document image",
)
async def discard_document_image(
    request: Request,
    vault: str,
    file_id: str,
    user: AuthenticatedUser = Depends(get_current_user),
):
    """Remove only an upload that has never reached a document commit.

    Images already claimed by a document are intentionally retained: Git can
    still address older revisions after the current Markdown link is removed.
    Missing, foreign, claimed, and other users' uploads share the same 404.
    """
    access, actor_id = await _asset_write_actor(request, vault, user)
    try:
        fid = uuid.UUID(file_id)
    except (ValueError, AttributeError) as exc:
        raise NotFoundError("Asset", file_id) from exc

    pool = await get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            row = await vault_files_repo.delete_unclaimed_attachment(
                conn,
                vault_id=access["vault_id"],
                file_id=fid,
                created_by=actor_id,
            )
            if row is None:
                raise NotFoundError("Asset", file_id)
            await enqueue_delete(conn, row["s3_key"])
    return Response(status_code=204)
