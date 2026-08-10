"""Authenticated upload and stable byte delivery for document images."""

from __future__ import annotations

import asyncio
import logging
import uuid

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import Response

from app.api.deps import get_current_user, require_delegated_actor
from app.db.postgres import get_pool
from app.exceptions import AKBError, ForbiddenError, NotFoundError
from app.repositories import vault_files_repo
from app.services import asset_service, file_service
from app.services.access_service import (
    FILE_UPLOAD_WRITE_ACTION,
    check_delegated_vault_writer,
    check_vault_access,
)
from app.services.auth_service import AuthenticatedUser
from app.services.s3_delete_worker import enqueue_delete
from app.util.text import to_nfc


router = APIRouter()
stable_router = APIRouter()
logger = logging.getLogger("akb.assets")
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
    access = await check_vault_access(
        user.user_id,
        vault,
        required_role="writer",
        write_action=FILE_UPLOAD_WRITE_ACTION,
    )
    actions = frozenset(access.get("write_grant_actions") or [])
    if access.get("role_source") != "write_policy_grant" or "*" in actions:
        return access, user.username

    delegated = await require_delegated_actor(request, user)
    await check_delegated_vault_writer(delegated.user.user_id, vault)
    return access, delegated.user.username


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
    # Each request is bounded to 10 MiB; the semaphore also caps aggregate
    # in-process buffering during an upload burst.
    async with _asset_transfer_slots:
        body = await _read_bounded_image(request)
        return await asset_service.create_image_asset(
            vault_id=access["vault_id"],
            vault_name=vault,
            filename=to_nfc(filename),
            declared_mime=request.headers.get("content-type", ""),
            body=body,
            actor_id=actor_id,
        )


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
        body = await asyncio.to_thread(
            file_service.get_object_bytes,
            row["s3_key"],
            asset_service.IMAGE_ASSET_MAX_BYTES,
        )
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
    return Response(content=body, media_type=row["mime_type"], headers=headers)


@stable_router.get("/assets/{file_id}", response_class=Response, summary="Read a document image")
async def read_document_image(
    file_id: str,
    vault: str = Query(..., min_length=1),
    user: AuthenticatedUser = Depends(get_current_user),
):
    try:
        access = await check_vault_access(user.user_id, vault, required_role="reader")
    except (ForbiddenError, NotFoundError) as exc:
        # Normalize access failures so the stable UUID is not an existence
        # oracle across vaults.
        raise NotFoundError("Asset", file_id) from exc
    row = await load_asset_row(file_id, access["vault_id"])
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
