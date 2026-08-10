"""REST API routes for vault file storage (S3-backed)."""

import asyncio

from fastapi import APIRouter, Depends, Query, Request, Response

from app.api.deps import get_current_user
from app.api.file_write_context import (
    resolve_file_write_context as _resolve_file_write_context,
)
from app.config import settings
from app.exceptions import AKBError
from app.models.file import BodyPlacementObservation
from app.services.access_service import check_vault_access
from app.services.auth_service import AuthenticatedUser
from app.services.file_service import FileService
from app.util.text import to_nfc

router = APIRouter()
file_service = FileService()
_measurement_transfer_slots = asyncio.Semaphore(2)


@router.api_route("/files/transfer/{token}", methods=["PUT", "GET"], include_in_schema=False)
async def measurement_file_transfer(token: str, request: Request):
    """Guarded opaque CAS transfer endpoint used by the existing file proxy.

    It has no user-token dependency because the short-lived, scoped transfer
    capability is its authorization.  The service only exposes this route
    while the dedicated M1 measurement guard is active.
    """
    async with _measurement_transfer_slots:
        if request.method == "PUT":
            declared = request.headers.get("content-length")
            if declared:
                try:
                    declared_size = int(declared)
                except ValueError as exc:
                    raise AKBError("invalid measurement transfer content-length", status_code=400) from exc
                if declared_size < 0:
                    raise AKBError("invalid measurement transfer content-length", status_code=400)
                if declared_size > settings.native_revision_m1_file_transfer_max_bytes:
                    raise AKBError("measurement transfer exceeds configured size limit", status_code=413)
            body = bytearray()
            async for chunk in request.stream():
                body.extend(chunk)
                if len(body) > settings.native_revision_m1_file_transfer_max_bytes:
                    raise AKBError("measurement transfer exceeds configured size limit", status_code=413)
            await file_service.transfer_measurement_capability(
                token, method="PUT", body=bytes(body),
            )
            return Response(status_code=200)
        data = await file_service.transfer_measurement_capability(token, method="GET")
        return Response(content=data or b"", media_type="application/octet-stream")


@router.post("/files/{vault}/upload", summary="Upload a file (presigned URL flow)")
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
    """Returns a presigned PUT URL. Client uploads directly to S3."""
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
    """Called after presigned URL upload. Updates size and byte hash from S3."""
    access, actor_id, delegated_actor = await _resolve_file_write_context(
        request, vault, user,
    )
    return await file_service.confirm_upload(
        access["vault_id"], file_id, actor_id=actor_id,
        delegated_actor=delegated_actor,
        content_hash=content_hash, hash_algorithm=hash_algorithm,
    )


@router.get("/files/{vault}/{file_id}/download", summary="Get download URL")
async def get_download_url(
    vault: str,
    file_id: str,
    user: AuthenticatedUser = Depends(get_current_user),
):
    access = await check_vault_access(user.user_id, vault, required_role="reader")
    return await file_service.get_download_url(access["vault_id"], file_id)


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
