"""REST API routes for vault access management."""

from fastapi import APIRouter, Depends, HTTPException, Query, status

from app.api.deps import get_current_user
from app.services.auth_service import (
    AuthenticatedUser,
    REVOKE_REASON_ADMIN,
    revoke_all_sessions,
)
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


@router.post(
    "/admin/users/{user_id}/revoke-sessions",
    summary="[admin] Force-logout all JWT sessions for a user",
)
async def admin_revoke_user_sessions(
    user_id: str,
    user: AuthenticatedUser = Depends(get_current_user),
):
    """Invalidate every JWT issued to ``user_id`` before now.

    Useful for incident response (account compromise, employee
    offboarding) without needing the user's password and without
    rotating the global ``jwt_secret`` (which would log out everyone).
    Does not touch PATs — those have their own revoke flow.
    """
    _require_admin(user)
    revoked_at = await revoke_all_sessions(
        user_id, actor_id=user.user_id, reason=REVOKE_REASON_ADMIN,
    )
    return {"user_id": user_id, "revoked_before": revoked_at.isoformat()}


@router.post(
    "/admin/users/{user_id}/reset-password",
    summary="[admin] Reset a user's password to a generated temp",
)
async def admin_reset_user_password(
    user_id: str,
    user: AuthenticatedUser = Depends(get_current_user),
):
    if not user.is_admin:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Admin only")

    import uuid as _uuid
    from app.db.postgres import get_pool
    from app.services.password_service import reset_password

    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT username FROM users WHERE id = $1", _uuid.UUID(user_id),
        )
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "User not found")

    temp, username = await reset_password(
        username=row["username"],
        actor_id=user.user_id,
        method="admin_ui",
    )
    return {"temporary_password": temp, "username": username}


class AdminMintTokenRequest(NFCModel):
    name: str
    expires_days: int | None = None


@router.post(
    "/admin/users/{user_ref}/tokens",
    summary="[admin] Mint a PAT for any user (by id or email)",
)
async def admin_mint_user_token(
    user_ref: str,
    req: AdminMintTokenRequest,
    user: AuthenticatedUser = Depends(get_current_user),
):
    """Issue a Personal Access Token on behalf of another user.

    The normal ``POST /auth/tokens`` mints for the *caller*, which forces
    the caller to know that user's password. A managed control plane that
    provisions members (and especially after a member SSO-links, retiring
    their local password) has no password to log in with — so it needs an
    admin-authenticated way to mint a member's PAT by email. Returns the raw
    token once, same shape as ``/auth/tokens``.
    """
    _require_admin(user)
    import uuid as _uuid
    from app.db.postgres import get_pool
    from app.services.auth_service import create_pat

    pool = await get_pool()
    async with pool.acquire() as conn:
        try:
            uid = _uuid.UUID(user_ref)
            row = await conn.fetchrow("SELECT id FROM users WHERE id = $1", uid)
        except ValueError:
            # Not a UUID → treat as an email (how the platform keys members).
            row = await conn.fetchrow(
                "SELECT id FROM users WHERE email = $1", user_ref
            )
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "User not found")
    return await create_pat(str(row["id"]), req.name, expires_days=req.expires_days)


@router.get(
    "/admin/role-state",
    summary="[admin] Diff PG role state against the AKB catalog (read-only)",
)
async def admin_role_state(user: AuthenticatedUser = Depends(get_current_user)):
    """Inspect what ``reconcile_from_catalog`` WOULD change without
    mutating anything.

    Useful before deciding to call ``POST /admin/reconcile-roles`` —
    an operator can confirm the drift is what they expect (e.g. a
    user just registered + reconcile hasn't run yet, vs. genuine
    state corruption) instead of running the mutating endpoint
    blind.
    """
    _require_admin(user)
    from app.services.role_sync import get_role_sync
    diff = await get_role_sync().diff_against_catalog()
    return {
        "drift_count": diff.drift_count(),
        "is_clean": diff.is_clean(),
        "missing_user_roles": diff.missing_user_roles,
        "orphan_user_roles": diff.orphan_user_roles,
        "missing_vault_roles": diff.missing_vault_roles,
        "orphan_vault_roles": diff.orphan_vault_roles,
        "missing_memberships": diff.missing_memberships,
        "missing_public_grants": diff.missing_public_grants,
        "stale_public_grants": diff.stale_public_grants,
        "missing_table_grants": diff.missing_table_grants,
        "authenticated_role_missing": diff.authenticated_role_missing,
        "users_not_in_authenticated": diff.users_not_in_authenticated,
    }


@router.post(
    "/admin/reconcile-roles",
    summary="[admin] Reconcile PG roles with the AKB catalog",
)
async def admin_reconcile_roles(user: AuthenticatedUser = Depends(get_current_user)):
    """Reconcile PostgreSQL role + GRANT state with the AKB catalog.

    The reconciler runs automatically at backend startup. This endpoint
    is for drift recovery: an operator that suspects role state has
    diverged (manual edits, partial lifecycle hook failure, restore
    from snapshot, …) can force a reconciliation without restarting
    the backend. Inspect with ``GET /admin/role-state`` first to
    confirm the expected drift before running. Idempotent.
    """
    _require_admin(user)
    from app.services.role_sync import get_role_sync
    report = await get_role_sync().reconcile_from_catalog()
    return {
        "reconciled": True,
        "user_roles_created": report.user_roles_created,
        "user_roles_dropped": report.user_roles_dropped,
        "vault_roles_created": report.vault_roles_created,
        "vault_roles_dropped": report.vault_roles_dropped,
        "grants_added": report.grants_added,
        "table_grants_applied": report.table_grants_applied,
        "public_grants_applied": report.public_grants_applied,
        "errors": report.errors,
    }
