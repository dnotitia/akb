"""REST auth routes: mode-gated humans plus mode-independent PAT management."""

import uuid
from typing import NoReturn

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import ConfigDict, Field, field_validator

from app.api.deps import get_current_user
from app.config import settings
from app.exceptions import (
    AuthenticationError,
    BrowserSessionNotReadyError,
    ForbiddenError,
    NotFoundError,
)
from app.services.access_service import VALID_WRITE_ACTIONS, check_vault_access
from app.services.auth_service import (
    AuthenticatedUser,
    register,
    login,
    create_pat,
    list_pats,
    revoke_pat,
    revoke_all_sessions,
    token_has_scope,
    update_profile,
)
from app.services.auth_policy import (
    SSO_BROWSER_SESSION_READY,
    require_local_auth_enabled,
)
from app.util.text import NFCModel

router = APIRouter()


class RegisterRequest(NFCModel):
    username: str
    email: str
    password: str
    display_name: str | None = None


class LoginRequest(NFCModel):
    username: str
    password: str


class CreatePATRequest(NFCModel):
    name: str
    expires_days: int | None = None
    scopes: list[str] | None = None
    key_class: str = "pat"
    # Per-PAT vault scope (Option B). Optional ``{prefixes, extra_vaults}``;
    # ``None`` = unscoped. Validated (well-formedness) + enforced (mutating
    # access ∩ scope). Self-minting any scope is safe by construction
    # (effective = user-ACL ∩ scope — only ever narrows). ``scopes`` are
    # coarse API gates; omitting them keeps read+write.
    vault_scope: dict[str, list[str]] | None = None


class AuthorityVaultScopeRequest(NFCModel):
    """Exact PAT vault-scope shape accepted by authority verification."""

    model_config = ConfigDict(extra="forbid")

    prefixes: list[str]
    extra_vaults: list[str]


class WriterAuthorityRequest(NFCModel):
    """One concrete writer action the authenticating PAT must possess."""

    model_config = ConfigDict(extra="forbid")

    vault: str
    action: str

    @field_validator("action")
    @classmethod
    def validate_action(cls, value: str) -> str:
        value = value.strip()
        if value not in VALID_WRITE_ACTIONS:
            raise ValueError("unknown write action")
        return value


class VerifyAuthorityRequest(NFCModel):
    """Authority a caller expects the authenticating PAT to possess."""

    model_config = ConfigDict(extra="forbid")

    vault_scope: AuthorityVaultScopeRequest
    # Keep the non-mutating check bounded: every distinct item performs one
    # database-backed effective-authority lookup.
    writer_authorities: list[WriterAuthorityRequest] = Field(min_length=1, max_length=32)


class VerifiedWriterAuthority(NFCModel):
    model_config = ConfigDict(extra="forbid")

    vault: str
    role: str
    action: str


class VerifyAuthorityResponse(NFCModel):
    model_config = ConfigDict(extra="forbid")

    token_id: uuid.UUID
    vault_scope: AuthorityVaultScopeRequest
    authorities: list[VerifiedWriterAuthority]


class ChangePasswordRequest(NFCModel):
    current_password: str
    new_password: str


class UpdateProfileRequest(NFCModel):
    display_name: str | None = None
    email: str | None = None


@router.post("/auth/register", summary="Register a local user")
async def register_user(req: RegisterRequest):
    require_local_auth_enabled()
    return await register(req.username, req.email, req.password, req.display_name)


@router.post("/auth/login", summary="Local login and session token")
async def login_user(req: LoginRequest):
    require_local_auth_enabled()
    return await login(req.username, req.password)


# ── Public auth config (lets the SPA decide which login options to show) ──


@router.get("/auth/config", summary="Public auth configuration")
async def auth_config():
    """Unauthenticated versioned capabilities; reveals no secrets."""
    mode = settings.require_auth_mode()
    human_sso_enabled = mode == "sso" and settings.keycloak_enabled
    browser_session_ready = human_sso_enabled and SSO_BROWSER_SESSION_READY
    return {
        "schema_version": 1,
        "auth_mode": mode,
        "local_auth": {
            "enabled": mode == "local",
        },
        "keycloak": {
            "enabled": human_sso_enabled,
            "browser_session_ready": browser_session_ready,
            "login_url": (
                "/api/v1/auth/keycloak/login"
                if browser_session_ready
                else None
            ),
        },
        "mcp_oauth": {
            "enabled": settings.mcp_oauth_enabled,
        },
    }


# ── Staged Keycloak browser surface ───────────────────────────────────
#
# Each handler 404s outside SSO mode and returns the stable staging error in
# SSO mode. No handler reaches OIDC, projection, or credential issuance.


def _reject_staged_keycloak_browser_route() -> NoReturn:
    """Hide human SSO in local mode and fail closed until Phase 4 custody."""
    if settings.require_auth_mode() != "sso" or not settings.keycloak_enabled:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND,
            "Human Keycloak SSO is not enabled",
        )
    raise BrowserSessionNotReadyError()


@router.get(
    "/auth/keycloak/login",
    status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
    summary="Keycloak SSO login (staged unavailable)",
)
async def keycloak_login(redirect: str = "/"):
    del redirect
    _reject_staged_keycloak_browser_route()


@router.get(
    "/auth/keycloak/callback",
    status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
    summary="Keycloak SSO callback (staged unavailable)",
)
async def keycloak_callback(request: Request):
    del request
    _reject_staged_keycloak_browser_route()


@router.get(
    "/auth/keycloak/logout",
    status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
    summary="Keycloak SSO logout (staged unavailable)",
)
async def keycloak_logout(id_token_hint: str | None = None):
    del id_token_hint
    _reject_staged_keycloak_browser_route()


@router.post(
    "/auth/keycloak/exchange",
    status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
    summary="Legacy SSO exchange (staged unavailable)",
)
async def keycloak_exchange():
    _reject_staged_keycloak_browser_route()


@router.get("/auth/me", summary="Get current user info")
async def me(user: AuthenticatedUser = Depends(get_current_user)):
    return {
        "user_id": user.user_id,
        "username": user.username,
        "email": user.email,
        "display_name": user.display_name,
        "is_admin": user.is_admin,
        "auth_method": user.auth_method,
        "key_class": user.key_class,
    }


@router.patch("/auth/me", summary="Update own profile (display_name / email)")
async def update_my_profile(
    req: UpdateProfileRequest,
    user: AuthenticatedUser = Depends(get_current_user),
):
    return await update_profile(
        user.user_id,
        display_name=req.display_name,
        email=req.email,
    )


@router.post("/auth/tokens", summary="Create a Personal Access Token")
async def create_token(req: CreatePATRequest, user: AuthenticatedUser = Depends(get_current_user)):
    from app.models.vault_scope import VaultScope

    if req.key_class != "pat" and not user.is_admin:
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            "Only admins can issue service keys",
        )
    scope = VaultScope.parse_input(req.vault_scope)
    return await create_pat(
        user.user_id,
        req.name,
        expires_days=req.expires_days,
        vault_scope=scope,
        scopes=req.scopes,
        key_class=req.key_class,
    )


@router.get("/auth/tokens", summary="List your PATs")
async def list_tokens(user: AuthenticatedUser = Depends(get_current_user)):
    return {"tokens": await list_pats(user.user_id)}


@router.post(
    "/auth/authority/verify",
    summary="Verify the current PAT's exact write authority",
    response_model=VerifyAuthorityResponse,
)
async def verify_authority(
    req: VerifyAuthorityRequest,
    user: AuthenticatedUser = Depends(get_current_user),
) -> VerifyAuthorityResponse:
    """Verify the authenticating token without performing a write.

    The caller supplies the exact scope it expects and the concrete Vaults on
    which it requires writer authority.  The response deliberately contains
    only the current token id, its canonical scope, and the verified roles;
    it never exposes token material, ACLs, or write-policy provenance.
    """
    from app.models.vault_scope import VaultScope

    if user.auth_method != "pat" or user.token_id is None:
        raise AuthenticationError("Authority verification requires a PAT")
    try:
        uuid.UUID(user.token_id)
    except (TypeError, ValueError, AttributeError) as exc:
        raise AuthenticationError("Authority verification requires a valid PAT") from exc
    if not token_has_scope(user.token_scopes, "write"):
        raise ForbiddenError("Authenticated token lacks the coarse write scope")

    expected_scope = VaultScope.parse_input(req.vault_scope.model_dump())
    requested_authorities = sorted({(item.vault, item.action) for item in req.writer_authorities})
    writer_scope = VaultScope.parse_input(
        {
            "prefixes": [],
            "extra_vaults": [vault for vault, _action in requested_authorities],
        }
    )
    assert expected_scope is not None
    assert writer_scope is not None
    if user.vault_scope is None or user.vault_scope.to_db_json() != expected_scope.to_db_json():
        raise ForbiddenError("Authenticated token Vault scope does not match")

    writer_vaults = sorted(writer_scope.extra_vaults)
    if any(not expected_scope.permits(vault) for vault in writer_vaults):
        raise ForbiddenError("Requested writer authority exceeds the PAT Vault scope")

    for vault, action in requested_authorities:
        try:
            await check_vault_access(
                user.user_id,
                vault,
                required_role="writer",
                write_action=action,
            )
        except (ForbiddenError, NotFoundError) as exc:
            raise ForbiddenError("Authenticated token lacks required writer authority") from exc

    return VerifyAuthorityResponse(
        token_id=uuid.UUID(user.token_id),
        vault_scope=AuthorityVaultScopeRequest(**expected_scope.to_db_json()),
        authorities=[
            VerifiedWriterAuthority(vault=vault, role="writer", action=action)
            for vault, action in requested_authorities
        ],
    )


@router.delete("/auth/tokens/{token_id}", summary="Revoke a PAT")
async def delete_token(token_id: str, user: AuthenticatedUser = Depends(get_current_user)):
    success = await revoke_pat(user.user_id, token_id)
    if not success:
        raise NotFoundError("Token", token_id)
    return {"revoked": True}


@router.post("/auth/change-password", summary="Change own password")
async def change_password_route(
    req: ChangePasswordRequest,
    user: AuthenticatedUser = Depends(get_current_user),
):
    require_local_auth_enabled()
    from app.services.auth_service import change_password, BadPasswordChange

    try:
        await change_password(user.user_id, req.current_password, req.new_password)
    except BadPasswordChange as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc))
    return {"ok": True}


@router.post(
    "/auth/revoke-all-sessions",
    summary="Invalidate every JWT issued to me before now",
)
async def revoke_my_sessions(user: AuthenticatedUser = Depends(get_current_user)):
    """End every JWT-backed session for the calling user, including this one.

    The next request with the JWT used here will return 401. Other devices
    that have the same user's JWT (mobile client, second browser, agent
    runners) will all fail on their next call and must re-login.

    Personal Access Tokens are NOT affected — manage those individually
    via DELETE /auth/tokens/{token_id}.
    """
    require_local_auth_enabled()
    revoked_at = await revoke_all_sessions(user.user_id)
    return {"revoked_before": revoked_at.isoformat()}
