"""REST API routes for vault file storage (S3-backed)."""

from fastapi import APIRouter, Depends, Query

from app.api.deps import get_current_user
from app.services.access_service import check_vault_access
from app.services.auth_service import AuthenticatedUser
from app.services.file_service import FileService
from app.util.text import to_nfc

router = APIRouter()
file_service = FileService()


@router.post("/files/{vault}/upload", summary="Upload a file (presigned URL flow)")
async def upload_file(
    vault: str,
    filename: str = Query(..., description="Original filename"),
    collection: str = Query("", description="Logical grouping"),
    description: str = Query("", description="File description"),
    mime_type: str = Query("application/octet-stream", description="MIME type"),
    user: AuthenticatedUser = Depends(get_current_user),
):
    """Returns a presigned PUT URL. Client uploads directly to S3."""
    access = await check_vault_access(user.user_id, vault, required_role="writer")
    return await file_service.initiate_upload(
        vault_name=vault,
        vault_id=access["vault_id"],
        collection=to_nfc(collection),
        filename=to_nfc(filename),
        actor_id=user.username,
        mime_type=mime_type,
        description=to_nfc(description),
    )


@router.post("/files/{vault}/{file_id}/confirm", summary="Confirm upload completion (recovery)")
async def confirm_upload(
    vault: str,
    file_id: str,
    user: AuthenticatedUser = Depends(get_current_user),
):
    """Called after presigned URL upload. Updates file size from S3. Recovery use only."""
    access = await check_vault_access(user.user_id, vault, required_role="writer")
    return await file_service.confirm_upload(
        access["vault_id"], file_id, actor_id=user.username,
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
    files = await file_service.list_files(access["vault_id"], collection, limit)
    return {"vault": vault, "files": files, "total": len(files)}


@router.delete("/files/{vault}/{file_id}", summary="Delete a file")
async def delete_file(
    vault: str,
    file_id: str,
    user: AuthenticatedUser = Depends(get_current_user),
):
    access = await check_vault_access(user.user_id, vault, required_role="writer")
    deleted = await file_service.delete(
        access["vault_id"], file_id, actor_id=user.username,
    )
    return {"deleted": deleted}
