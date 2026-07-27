"""REST API routes for vault file storage (S3-backed)."""

from fastapi import APIRouter, Depends, Query, Request

from app.api.deps import get_current_user, require_delegated_actor
from app.services.access_service import (
    FILE_UPLOAD_WRITE_ACTION,
    check_delegated_vault_writer,
    check_vault_access,
)
from app.services.auth_service import AuthenticatedUser
from app.services.file_service import FileService
from app.util.text import to_nfc

router = APIRouter()
file_service = FileService()


async def _resolve_file_write_context(
    request: Request,
    vault: str,
    user: AuthenticatedUser,
) -> tuple[dict, str, dict[str, str] | None]:
    access = await check_vault_access(
        user.user_id,
        vault,
        required_role="writer",
        write_action=FILE_UPLOAD_WRITE_ACTION,
    )
    actions = frozenset(access.get("write_grant_actions") or [])
    if access.get("role_source") != "write_policy_grant" or "*" in actions:
        return access, user.username, None

    delegated_actor = await require_delegated_actor(request, user)
    await check_delegated_vault_writer(delegated_actor.user.user_id, vault)
    return access, delegated_actor.user.username, {
        "delegated_user_id": delegated_actor.user.user_id,
        "service_user_id": delegated_actor.service_user_id,
        "service_token_id": delegated_actor.service_token_id,
    }


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
