"""REST API routes for vault access management."""

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel

from app.api.deps import get_current_user
from app.services.auth_service import AuthenticatedUser
from app.services.access_service import (
    archive_vault,
    delete_user_account,
    delete_vault,
    get_vault_info,
    grant_access,
    list_accessible_vaults,
    list_all_users_admin,
    list_vault_members,
    revoke_access,
    search_users,
    transfer_ownership,
    unarchive_vault,
    update_vault_metadata,
)
from app.util.text import NFCModel


def _require_admin(user: AuthenticatedUser) -> None:
    if not user.is_admin:
        raise HTTPException(status_code=403, detail="Admin only")

router = APIRouter()


class GrantRequest(NFCModel):
    user: str
    role: str  # reader, writer, admin


class RevokeRequest(NFCModel):
    user: str


class TransferRequest(NFCModel):
    new_owner: str


class VaultPatchRequest(NFCModel):
    description: str | None = None
    public_access: str | None = None


@router.get("/my/vaults", summary="List vaults accessible to me")
async def my_vaults(user: AuthenticatedUser = Depends(get_current_user)):
    return {"vaults": await list_accessible_vaults(user.user_id)}


@router.get("/vaults/{vault}/info", summary="Get vault details")
async def vault_info(vault: str, user: AuthenticatedUser = Depends(get_current_user)):
    return await get_vault_info(user.user_id, vault)


@router.get("/vaults/{vault}/members", summary="List vault members")
async def vault_members(vault: str, user: AuthenticatedUser = Depends(get_current_user)):
    return {"members": await list_vault_members(user.user_id, vault)}


@router.post("/vaults/{vault}/grant", summary="Grant vault access to a user")
async def grant(vault: str, req: GrantRequest, user: AuthenticatedUser = Depends(get_current_user)):
    return await grant_access(user.user_id, vault, req.user, req.role)


@router.post("/vaults/{vault}/revoke", summary="Revoke vault access from a user")
async def revoke(vault: str, req: RevokeRequest, user: AuthenticatedUser = Depends(get_current_user)):
    return await revoke_access(user.user_id, vault, req.user)


@router.post("/vaults/{vault}/transfer", summary="Transfer vault ownership")
async def transfer(
    vault: str,
    req: TransferRequest | None = None,
    new_owner: str | None = Query(None, description="(deprecated) use JSON body"),
    user: AuthenticatedUser = Depends(get_current_user),
):
    target = (req.new_owner if req else None) or new_owner
    if not target:
        from fastapi import HTTPException
        raise HTTPException(status_code=422, detail="new_owner is required")
    return await transfer_ownership(user.user_id, vault, target)


@router.post("/vaults/{vault}/archive", summary="Archive vault (read-only)")
async def archive(vault: str, user: AuthenticatedUser = Depends(get_current_user)):
    return await archive_vault(user.user_id, vault)


@router.post("/vaults/{vault}/unarchive", summary="Restore archived vault to active")
async def unarchive(vault: str, user: AuthenticatedUser = Depends(get_current_user)):
    return await unarchive_vault(user.user_id, vault)


@router.patch("/vaults/{vault}", summary="Update vault metadata (description, public_access)")
async def patch_vault(
    vault: str,
    req: VaultPatchRequest,
    user: AuthenticatedUser = Depends(get_current_user),
):
    return await update_vault_metadata(
        user.user_id, vault, description=req.description, public_access=req.public_access,
    )


@router.delete("/vaults/{vault}", summary="Permanently delete a vault and all its data")
async def delete_vault_route(
    vault: str,
    user: AuthenticatedUser = Depends(get_current_user),
):
    """Cascades S3 files, edges, chunks, vault_tables, todos, sessions,
    documents, collections, vault_access, and the git bare repo. The
    underlying service requires admin role on the vault (i.e. owner)."""
    return await delete_vault(user.user_id, vault)


@router.delete("/my/account", summary="Delete my account and all owned vaults")
async def delete_my_account(user: AuthenticatedUser = Depends(get_current_user)):
    """Self-delete: removes all owned vaults (cascading to chunks, Git repo,
    the vector store, S3 files, etc.), detaches residual FK references in other
    users' vaults, then deletes the user row."""
    return await delete_user_account(user.user_id)


@router.get("/users/search", summary="Search users")
async def user_search(
    q: str | None = Query(None),
    limit: int = Query(20, ge=1, le=100),
    user: AuthenticatedUser = Depends(get_current_user),
):
    return {"users": await search_users(q, limit)}


@router.get("/admin/users", summary="[admin] List every user with stats")
async def admin_list_users(user: AuthenticatedUser = Depends(get_current_user)):
    _require_admin(user)
    return {"users": await list_all_users_admin()}


@router.delete("/admin/users/{user_id}", summary="[admin] Delete any user + owned vaults")
async def admin_delete_user(
    user_id: str,
    user: AuthenticatedUser = Depends(get_current_user),
):
    _require_admin(user)
    if user_id == user.user_id:
        raise HTTPException(status_code=400, detail="Use DELETE /my/account to delete your own account")
    return await delete_user_account(user_id)
