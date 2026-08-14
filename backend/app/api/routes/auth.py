"""REST auth routes: mode-gated humans plus mode-independent PAT management."""

import uuid
from collections.abc import Mapping
from datetime import datetime, timezone
from urllib.parse import quote, urlsplit

from fastapi import APIRouter, Depends, Form, HTTPException, Request, Response, status
from fastapi.responses import JSONResponse, RedirectResponse
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
    project_verified_principal,
)
from app.services.auth_policy import require_local_auth_enabled, sso_browser_session_ready
from app.services.keycloak_oidc import get_keycloak_oidc
from app.services.sso_browser_session_service import (
    IssuedSsoBrowserSession,
    SSO_BROWSER_CSRF_HEADER,
    create_sso_browser_session,
    revoke_sso_browser_session,
    revoke_sso_browser_sessions_from_logout_token,
    sso_browser_csrf_cookie_name,
    sso_browser_session_cookie_name,
)
from app.sso.keycloak_admin import (
    ProviderControlError,
    get_keycloak_provider_control,
)
from app.sso.providers.keycloak_oidc import ProviderDefinitionError, validate_alias
from app.util.text import NFCModel

router = APIRouter()

_SSO_OIDC_BINDING_COOKIE = "__Host-akb_sso_oidc_binding"
_SSO_OIDC_BINDING_COOKIE_DEV = "akb_dev_sso_oidc_binding"


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
async def auth_config(response: Response):
    """Return unauthenticated versioned capabilities without secrets."""
    response.headers["Cache-Control"] = "no-store"
    response.headers["Pragma"] = "no-cache"
    mode = settings.require_auth_mode()
    human_sso_enabled = mode == "sso" and settings.keycloak_enabled
    browser_session_ready = human_sso_enabled and sso_browser_session_ready()
    providers: list[dict[str, object]] = []
    if human_sso_enabled:
        control = get_keycloak_provider_control()
        if control.control_mode != "direct":
            raise HTTPException(
                status.HTTP_503_SERVICE_UNAVAILABLE,
                "SSO provider catalog is unavailable",
            )
        try:
            catalog = await control.list_providers(allow_stale=True)
        except ProviderControlError as exc:
            raise HTTPException(
                status.HTTP_503_SERVICE_UNAVAILABLE,
                "SSO provider catalog is unavailable",
            ) from exc
        providers = [
            provider.public_view(
                login_url=(
                    f"/api/v1/auth/sso/{quote(provider.alias, safe='')}/login" if browser_session_ready else None
                )
            )
            for provider in catalog
            if provider.state == "enabled"
        ]
    return {
        "schema_version": 2,
        "auth_mode": mode,
        "local_auth": {
            "enabled": mode == "local",
        },
        "keycloak": {
            "enabled": human_sso_enabled,
            "browser_session_ready": browser_session_ready,
        },
        "providers": providers,
        "mcp_oauth": {
            "enabled": settings.mcp_oauth_enabled,
        },
    }


@router.get("/auth/jwks", summary="Local-session public verification keys")
async def local_session_jwks():
    """Publish only the bounded public keyset for local-session-rs256-v2."""
    if settings.require_auth_mode() != "local":
        raise HTTPException(
            status.HTTP_404_NOT_FOUND,
            "Local human authentication is not enabled",
        )
    from app.services.local_session_keys import get_local_session_keyset

    return get_local_session_keyset().public_jwks


def _require_sso_mode() -> None:
    if settings.require_auth_mode() != "sso" or not settings.keycloak_enabled:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND,
            "Human Keycloak SSO is not enabled",
        )


def _require_sso_browser_session() -> None:
    _require_sso_mode()
    if not sso_browser_session_ready():
        raise BrowserSessionNotReadyError()


def _secure_cookie() -> bool:
    return urlsplit(settings.public_base_url).scheme == "https"


def _sso_oidc_binding_cookie_name() -> str:
    if _secure_cookie():
        return _SSO_OIDC_BINDING_COOKIE
    return _SSO_OIDC_BINDING_COOKIE_DEV


def _safe_redirect_path(value: object) -> str:
    if (
        not isinstance(value, str)
        or not 1 <= len(value) <= 2048
        or not value.startswith("/")
        or value.startswith("//")
        or "\\" in value
        or any(ord(character) < 0x20 or ord(character) == 0x7F for character in value)
    ):
        raise AuthenticationError("Invalid SSO redirect target")
    try:
        parsed = urlsplit(value)
    except ValueError:
        raise AuthenticationError("Invalid SSO redirect target") from None
    if parsed.scheme or parsed.netloc or parsed.fragment:
        raise AuthenticationError("Invalid SSO redirect target")
    return value


def _set_sso_oidc_binding_cookie(
    response: RedirectResponse,
    browser_binding: str,
) -> None:
    response.set_cookie(
        _sso_oidc_binding_cookie_name(),
        browser_binding,
        max_age=600,
        secure=_secure_cookie(),
        httponly=True,
        samesite="lax",
        path="/",
    )


def _clear_sso_oidc_binding_cookie(response: RedirectResponse) -> None:
    response.delete_cookie(
        _sso_oidc_binding_cookie_name(),
        secure=_secure_cookie(),
        httponly=True,
        samesite="lax",
        path="/",
    )


def _set_sso_session_cookies(
    response: RedirectResponse,
    issued: IssuedSsoBrowserSession,
) -> None:
    max_age = max(
        1,
        int((issued.expires_at - datetime.now(timezone.utc)).total_seconds()),
    )
    secure = _secure_cookie()
    response.set_cookie(
        sso_browser_session_cookie_name(),
        issued.token,
        max_age=max_age,
        secure=secure,
        httponly=True,
        samesite="lax",
        # Protected surfaces also exist at /api/assets and /health/vault.
        # The value is opaque + HttpOnly, so root scope does not expose token
        # material to the SPA while allowing one same-origin session carrier.
        path="/",
    )
    response.set_cookie(
        sso_browser_csrf_cookie_name(),
        issued.csrf_token,
        max_age=max_age,
        secure=secure,
        httponly=False,
        samesite="lax",
        path="/",
    )
    response.headers["Cache-Control"] = "no-store"
    response.headers["Pragma"] = "no-cache"


def _clear_sso_session_cookies(response: Response) -> None:
    secure = _secure_cookie()
    response.delete_cookie(
        sso_browser_session_cookie_name(),
        secure=secure,
        httponly=True,
        samesite="lax",
        path="/",
    )
    response.delete_cookie(
        sso_browser_csrf_cookie_name(),
        secure=secure,
        httponly=False,
        samesite="lax",
        path="/",
    )


async def _require_enabled_provider(alias: str) -> str:
    try:
        alias = validate_alias(alias)
    except ProviderDefinitionError:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND,
            "SSO provider is not enabled",
        ) from None
    control = get_keycloak_provider_control()
    if control.control_mode != "direct":
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "SSO provider catalog is unavailable",
        )
    try:
        catalog = await control.list_providers(allow_stale=False)
    except ProviderControlError as exc:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "SSO provider catalog is unavailable",
        ) from exc
    if not any(provider.alias == alias and provider.state == "enabled" for provider in catalog):
        raise HTTPException(
            status.HTTP_404_NOT_FOUND,
            "SSO provider is not enabled",
        )
    return alias


def _require_signed_provider(
    claims: Mapping[str, object],
    expected_alias: str,
) -> None:
    value = claims.get("identity_provider")
    if not isinstance(value, str):
        raise AuthenticationError("SSO sign-in failed")
    try:
        value = validate_alias(value)
    except ProviderDefinitionError:
        raise AuthenticationError("SSO sign-in failed") from None
    if value != expected_alias:
        raise AuthenticationError("SSO sign-in failed")


@router.get(
    "/auth/sso/{alias}/login",
    summary="Start an enabled SSO login",
    status_code=status.HTTP_303_SEE_OTHER,
    response_class=RedirectResponse,
)
async def sso_provider_login(alias: str, redirect: str = "/"):
    _require_sso_browser_session()
    redirect_path = _safe_redirect_path(redirect)
    provider_alias = await _require_enabled_provider(alias)
    issued = await get_keycloak_oidc().begin_browser_login(
        redirect_path,
        provider_alias=provider_alias,
    )
    response = RedirectResponse(issued.location, status_code=status.HTTP_303_SEE_OTHER)
    _set_sso_oidc_binding_cookie(response, issued.browser_binding)
    response.headers["Cache-Control"] = "no-store"
    response.headers["Pragma"] = "no-cache"
    return response


@router.get(
    "/auth/keycloak/login",
    summary="Retired provider-less Keycloak login",
    status_code=status.HTTP_410_GONE,
)
async def keycloak_login(redirect: str = "/"):
    del redirect
    _require_sso_mode()
    raise HTTPException(
        status.HTTP_410_GONE,
        "Select one of the enabled SSO providers from /api/v1/auth/config",
    )


@router.get(
    "/auth/keycloak/callback",
    summary="Complete an ordinary Keycloak SSO browser login",
    status_code=status.HTTP_303_SEE_OTHER,
    response_class=RedirectResponse,
)
async def keycloak_callback(
    request: Request,
    code: str = "",
    state: str = "",
    error: str | None = None,
):
    _require_sso_browser_session()
    if error is not None or not code or not state:
        raise AuthenticationError("SSO sign-in failed")
    oidc = get_keycloak_oidc()
    transient = await oidc.consume_browser_state(
        state,
        request.cookies.get(_sso_oidc_binding_cookie_name(), ""),
    )
    if not isinstance(transient, dict) or set(transient) != {
        "redirect_path",
        "provider_alias",
        "client_id",
        "code_verifier",
        "nonce",
    }:
        raise AuthenticationError("SSO sign-in failed")
    if transient.get("client_id") != settings.keycloak_client_id:
        raise AuthenticationError("SSO sign-in failed")
    provider_alias = transient.get("provider_alias")
    if not isinstance(provider_alias, str):
        raise AuthenticationError("SSO sign-in failed")
    try:
        validate_alias(provider_alias)
    except ProviderDefinitionError:
        raise AuthenticationError("SSO sign-in failed") from None
    redirect_path = _safe_redirect_path(transient.get("redirect_path"))
    verifier = transient.get("code_verifier")
    nonce = transient.get("nonce")
    if not isinstance(verifier, str) or not isinstance(nonce, str):
        raise AuthenticationError("SSO sign-in failed")
    tokens = await oidc.exchange_browser_code(code, verifier)
    access_token = tokens.get("access_token")
    id_token = tokens.get("id_token")
    if tokens.get("token_type") != "Bearer" or not isinstance(access_token, str) or not isinstance(id_token, str):
        raise AuthenticationError("SSO sign-in failed")
    principal = await oidc.verify_access_token(
        access_token,
        settings.api_oauth_audience_effective,
        route_profile="api",
    )
    if principal is None:
        raise AuthenticationError("SSO sign-in failed")
    _require_signed_provider(principal.claims, provider_alias)
    id_claims = await oidc.verify_browser_id_token(
        id_token,
        expected_nonce=nonce,
        access_token=access_token,
        expected_provider_alias=provider_alias,
    )
    _require_signed_provider(id_claims, provider_alias)
    user = await project_verified_principal(principal)
    if user is None:
        raise AuthenticationError("SSO sign-in failed")
    issued = await create_sso_browser_session(
        user,
        principal,
        id_claims,
        tokens,
    )
    response = RedirectResponse(redirect_path, status_code=status.HTTP_303_SEE_OTHER)
    _set_sso_session_cookies(response, issued)
    _clear_sso_oidc_binding_cookie(response)
    return response


@router.get(
    "/auth/keycloak/logout",
    summary="Retired token-bearing SSO logout",
    status_code=status.HTTP_410_GONE,
)
async def keycloak_logout(id_token_hint: str | None = None):
    del id_token_hint
    _require_sso_mode()
    raise HTTPException(
        status.HTTP_410_GONE,
        "Use POST /api/v1/auth/logout",
    )


@router.post(
    "/auth/keycloak/exchange",
    summary="Retired AKB JWT exchange",
    status_code=status.HTTP_410_GONE,
)
async def keycloak_exchange():
    _require_sso_mode()
    raise HTTPException(
        status.HTTP_410_GONE,
        "SSO mode does not issue an AKB user JWT",
    )


@router.post("/auth/logout", summary="End the current SSO browser session")
async def sso_browser_logout(request: Request):
    _require_sso_browser_session()
    revoked = await revoke_sso_browser_session(
        request.cookies.get(sso_browser_session_cookie_name(), ""),
        request.cookies.get(sso_browser_csrf_cookie_name(), ""),
        request.headers.get(SSO_BROWSER_CSRF_HEADER, ""),
    )
    oidc = get_keycloak_oidc()
    if revoked.refresh_token is not None:
        await oidc.revoke_browser_refresh_token(revoked.refresh_token)
    logout_url = oidc.ordinary_logout_url(
        # The service API cannot accept an ID-token hint, so custodied identity
        # material cannot be serialized into a SPA-visible navigation URL.
        f"{settings.public_base_url.rstrip('/')}/auth",
    )
    response = JSONResponse({"logout_url": logout_url})
    response.headers["Cache-Control"] = "no-store"
    response.headers["Pragma"] = "no-cache"
    _clear_sso_session_cookies(response)
    return response


@router.post(
    "/auth/keycloak/backchannel-logout",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Receive a Keycloak back-channel logout token",
)
async def keycloak_backchannel_logout(logout_token: str = Form(...)):
    _require_sso_mode()
    try:
        claims = await get_keycloak_oidc().verify_backchannel_logout_token(logout_token)
        await revoke_sso_browser_sessions_from_logout_token(
            issuer=claims["iss"],
            sid=claims["sid"],
            subject=claims.get("sub"),
            issued_at=claims["iat"],
            expires_at=claims["exp"],
        )
    except AuthenticationError:
        # OpenID Connect Back-Channel Logout requires invalid logout tokens to
        # receive 400 rather than an interactive-authentication 401 challenge.
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            {
                "message": "Invalid back-channel logout token",
                "code": "invalid_logout_token",
            },
        ) from None
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/auth/me", summary="Get current user info")
async def me(user: AuthenticatedUser = Depends(get_current_user)):
    return {
        "user_id": user.user_id,
        "username": user.username,
        "email": user.email,
        "display_name": user.display_name,
        "is_admin": user.is_admin,
        "auth_method": user.auth_method,
        "account_kind": user.account_kind,
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
