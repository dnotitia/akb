"""REST API routes for vault access management."""

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import SecretStr

from app.api.deps import get_current_user
from app.services.auth_service import (
    AuthenticatedUser,
    REVOKE_REASON_ADMIN,
    revoke_all_sessions,
)
from app.services.account_service import (
    activate_user,
    adopt_current_admin_as_service,
    ensure_human_external_identity,
    ensure_service_user,
    get_user,
    get_user_by_external_identity,
    identify_user_token,
    revoke_user_token,
    set_user_admin,
    suspend_user,
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


class EnsureExternalIdentityRequest(NFCModel):
    issuer: str
    subject: str
    email: str
    display_name: str | None = None
    existing_user_id: str | None = None


class EnsureServiceUserRequest(NFCModel):
    username: str
    email: str
    display_name: str | None = None


class AdoptCurrentServiceUserRequest(NFCModel):
    expected_username: str
    expected_email: str


class SetUserRoleRequest(NFCModel):
    is_admin: bool


class IdentifyTokenRequest(NFCModel):
    token: SecretStr


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


@router.post(
    "/admin/users/ensure-external-identity",
    summary="[admin] Ensure a human user and stable OIDC binding",
)
async def admin_ensure_external_identity(
    req: EnsureExternalIdentityRequest,
    user: AuthenticatedUser = Depends(get_current_user),
):
    _require_admin(user)
    return await ensure_human_external_identity(
        issuer=req.issuer,
        subject=req.subject,
        email=req.email,
        display_name=req.display_name,
        existing_user_id=req.existing_user_id,
        actor_id=user.user_id,
    )


@router.post(
    "/admin/users/prepare-external-identity",
    summary="[admin] Prepare a suspended human user and stable OIDC binding",
)
async def admin_prepare_external_identity(
    req: EnsureExternalIdentityRequest,
    user: AuthenticatedUser = Depends(get_current_user),
):
    _require_admin(user)
    return await ensure_human_external_identity(
        issuer=req.issuer,
        subject=req.subject,
        email=req.email,
        display_name=req.display_name,
        existing_user_id=req.existing_user_id,
        prepare_suspended=True,
        actor_id=user.user_id,
    )


@router.get(
    "/admin/users/by-external-identity",
    summary="[admin] Resolve a user by exact OIDC issuer and subject",
)
async def admin_get_user_by_external_identity(
    issuer: str = Query(...),
    subject: str = Query(...),
    user: AuthenticatedUser = Depends(get_current_user),
):
    _require_admin(user)
    return await get_user_by_external_identity(issuer, subject)


@router.get(
    "/admin/users/{user_id}/governance",
    summary="[admin] Read account governance state by AKB user ID",
)
async def admin_get_governed_user(
    user_id: str,
    user: AuthenticatedUser = Depends(get_current_user),
):
    _require_admin(user)
    return await get_user(user_id)


@router.post(
    "/admin/service-users/ensure",
    summary="[admin] Ensure a non-interactive service user",
)
async def admin_ensure_service_user(
    req: EnsureServiceUserRequest,
    user: AuthenticatedUser = Depends(get_current_user),
):
    _require_admin(user)
    return await ensure_service_user(
        username=req.username,
        email=req.email,
        display_name=req.display_name,
        actor_id=user.user_id,
    )


@router.post(
    "/admin/service-users/adopt-current",
    summary="[admin] Adopt the current bootstrap administrator as a service identity",
)
async def admin_adopt_current_service_user(
    req: AdoptCurrentServiceUserRequest,
    user: AuthenticatedUser = Depends(get_current_user),
):
    _require_admin(user)
    if user.auth_method != "pat" or user.token_id is None:
        raise HTTPException(
            status_code=409,
            detail={
                "message": "Service identity adoption requires the current administrator PAT",
                "code": "service_identity_adoption_requires_pat",
            },
        )
    return await adopt_current_admin_as_service(
        user_id=user.user_id,
        token_id=user.token_id,
        expected_username=req.expected_username,
        expected_email=req.expected_email,
        actor_id=user.user_id,
    )


@router.put(
    "/admin/users/{user_id}/role",
    summary="[admin] Project a human user's AKB administrator role",
)
async def admin_set_user_role(
    user_id: str,
    req: SetUserRoleRequest,
    user: AuthenticatedUser = Depends(get_current_user),
):
    _require_admin(user)
    return await set_user_admin(
        user_id,
        is_admin=req.is_admin,
        actor_id=user.user_id,
    )


@router.post(
    "/admin/users/{user_id}/suspend",
    summary="[admin] Suspend an account and revoke all credentials",
)
async def admin_suspend_user(
    user_id: str,
    user: AuthenticatedUser = Depends(get_current_user),
):
    _require_admin(user)
    return await suspend_user(user_id, actor_id=user.user_id)


@router.post(
    "/admin/users/{user_id}/activate",
    summary="[admin] Reactivate an account without restoring credentials",
)
async def admin_activate_user(
    user_id: str,
    user: AuthenticatedUser = Depends(get_current_user),
):
    _require_admin(user)
    return await activate_user(user_id, actor_id=user.user_id)


@router.delete(
    "/admin/users/{user_id}/tokens/{token_id}",
    summary="[admin] Revoke one exact user-owned token",
)
async def admin_revoke_user_token(
    user_id: str,
    token_id: str,
    user: AuthenticatedUser = Depends(get_current_user),
):
    _require_admin(user)
    return await revoke_user_token(
        user_id,
        token_id,
        actor_id=user.user_id,
    )


@router.post(
    "/admin/tokens/identify",
    summary="[admin] Identify one presented token for exact migration revocation",
)
async def admin_identify_token(
    req: IdentifyTokenRequest,
    user: AuthenticatedUser = Depends(get_current_user),
):
    """Return only the owner and token IDs for an admin-presented raw token.

    This migration endpoint deliberately ignores expiry and account status so
    disabled legacy credentials can still be mapped to the exact owned revoke
    route. The raw token and its fingerprint are never returned or audited.
    """
    _require_admin(user)
    return await identify_user_token(
        req.token.get_secret_value(),
        actor_id=user.user_id,
    )


@router.delete("/admin/users/{user_id}", summary="[admin] Delete a user + owned vaults")
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
    scopes: list[str] | None = None
    key_class: str = "pat"
    # Per-PAT vault scope (Option B). Optional ``{prefixes, extra_vaults}``;
    # ``None`` = unscoped. The admin-mint path provisions a managed agent's
    # scoped PAT (e.g. a gardener token scoped to ``gdn-*`` ∪ an operator
    # whitelist) without the member's password.
    vault_scope: dict[str, list[str]] | None = None


class AdminManagedMintTokenRequest(AdminMintTokenRequest):
    token_id: str


@router.post(
    "/admin/users/{user_ref}/tokens",
    summary="[admin] Mint a PAT for a user (by id or email)",
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
    return await _mint_admin_user_token(user_ref, req)


@router.post(
    "/admin/users/{user_ref}/managed-tokens",
    summary="[admin] Mint a PAT with a caller-selected durable token ID",
)
async def admin_mint_managed_user_token(
    user_ref: str,
    req: AdminManagedMintTokenRequest,
    user: AuthenticatedUser = Depends(get_current_user),
):
    _require_admin(user)
    return await _mint_admin_user_token(user_ref, req, token_id=req.token_id)


async def _mint_admin_user_token(
    user_ref: str,
    req: AdminMintTokenRequest,
    *,
    token_id: str | None = None,
):
    import uuid as _uuid
    from app.db.postgres import get_pool
    from app.models.vault_scope import VaultScope
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
    return await create_pat(
        str(row["id"]),
        req.name,
        token_id=token_id,
        expires_days=req.expires_days,
        vault_scope=VaultScope.parse_input(req.vault_scope),
        scopes=req.scopes,
        key_class=req.key_class,
    )


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
        "missing_token_roles": diff.missing_token_roles,
        "orphan_token_roles": diff.orphan_token_roles,
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
        "token_roles_created": report.token_roles_created,
        "token_roles_dropped": report.token_roles_dropped,
        "grants_added": report.grants_added,
        "table_grants_applied": report.table_grants_applied,
        "public_grants_applied": report.public_grants_applied,
        "errors": report.errors,
    }
