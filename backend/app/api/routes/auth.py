"""REST API routes for auth — register, login, PAT management, Keycloak SSO."""

import logging
import urllib.parse

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import RedirectResponse

from app.api.deps import get_current_user
from app.config import settings
from app.exceptions import AKBError, NotFoundError
from app.services.auth_service import (
    AuthenticatedUser,
    register,
    login,
    login_with_keycloak_claims,
    create_pat,
    list_pats,
    revoke_pat,
    revoke_all_sessions,
    update_profile,
)
from app.util.text import NFCModel

logger = logging.getLogger("akb.auth.routes")

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
    # NOTE: scopes are stored on the `tokens` row but not enforced
    # anywhere in the backend yet; accepting them as input would lie
    # to the caller about a restriction that doesn't exist. When
    # scope enforcement lands, re-expose this field with the matching
    # check in the request handlers.


class ChangePasswordRequest(NFCModel):
    current_password: str
    new_password: str


class UpdateProfileRequest(NFCModel):
    display_name: str | None = None
    email: str | None = None


@router.post("/auth/register", summary="Register a new user")
async def register_user(req: RegisterRequest):
    return await register(req.username, req.email, req.password, req.display_name)


@router.post("/auth/login", summary="Login and get JWT")
async def login_user(req: LoginRequest):
    return await login(req.username, req.password)


# ── Public auth config (lets the SPA decide which login options to show) ──

@router.get("/auth/config", summary="Public auth configuration")
async def auth_config():
    """Unauthenticated. Tells the frontend whether the optional Keycloak
    SSO button should be shown and where it points. Reveals no secrets."""
    return {
        "keycloak": {
            "enabled": settings.keycloak_enabled,
            # SPA appends ?redirect=<path> when navigating here.
            "login_url": "/api/v1/auth/keycloak/login" if settings.keycloak_enabled else None,
        }
    }


# ── Keycloak OIDC (optional) ──────────────────────────────────────────
#
# Only mounted-effective when keycloak_enabled. Each handler 404s when
# SSO is off so a disabled deployment exposes no live SSO surface.

class KeycloakExchangeRequest(NFCModel):
    code: str


def _require_keycloak() -> None:
    if not settings.keycloak_enabled:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Keycloak SSO is not enabled")


def _safe_redirect_path(raw: str | None) -> str:
    """Only allow same-site path redirects (must start with a single '/').

    Blocks open-redirects: '//evil.com', 'https://evil.com', backslash and
    scheme-relative tricks all collapse to '/'.
    """
    if not raw or not raw.startswith("/") or raw.startswith("//") or "\\" in raw:
        return "/"
    return raw


@router.get("/auth/keycloak/login", summary="Begin Keycloak SSO login")
async def keycloak_login(redirect: str = "/"):
    _require_keycloak()
    from app.services.keycloak_oidc import get_keycloak_oidc
    url = await get_keycloak_oidc().begin_login(_safe_redirect_path(redirect))
    return RedirectResponse(url, status_code=status.HTTP_302_FOUND)


@router.get("/auth/keycloak/callback", summary="Keycloak SSO redirect callback")
async def keycloak_callback(request: Request):
    _require_keycloak()
    from app.services.keycloak_oidc import get_keycloak_oidc, issue_exchange_code

    params = request.query_params
    # Keycloak signals user-side errors (e.g. access_denied) as query params.
    if err := params.get("error"):
        return _sso_error_redirect(err)
    code = params.get("code")
    state = params.get("state")
    if not code or not state:
        return _sso_error_redirect("missing_code_or_state")

    svc = get_keycloak_oidc()
    flow = await svc.consume_state(state)
    if flow is None:
        # Unknown/expired/replayed state — CSRF guard.
        return _sso_error_redirect("invalid_state")

    try:
        tokens = await svc.exchange_code_for_tokens(code, flow.get("code_verifier"))
        id_token = tokens.get("id_token")
        if not id_token:
            return _sso_error_redirect("no_id_token")
        claims = await svc.verify_id_token(id_token)
        login_response = await login_with_keycloak_claims(claims)
    except AKBError as e:
        # Don't leak detail into the URL; log server-side, show a code.
        logger.warning("Keycloak SSO callback failed: %s", e)
        return _sso_error_redirect("auth_failed")

    # Hand the SPA a one-time code; the token is delivered via POST /exchange.
    one_time = await issue_exchange_code(login_response)
    dest = _safe_redirect_path(flow.get("redirect_path", "/"))
    target = (
        f"{settings.keycloak_post_login_path}"
        f"?code={urllib.parse.quote(one_time)}"
        f"&redirect={urllib.parse.quote(dest, safe='')}"
    )
    return RedirectResponse(target, status_code=status.HTTP_302_FOUND)


@router.post("/auth/keycloak/exchange", summary="Exchange one-time SSO code for a JWT")
async def keycloak_exchange(req: KeycloakExchangeRequest):
    _require_keycloak()
    from app.services.keycloak_oidc import redeem_exchange_code
    result = await redeem_exchange_code(req.code)
    if result is None:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST, "Invalid or expired exchange code"
        )
    # Same {token, user} shape as POST /auth/login.
    return result


def _sso_error_redirect(reason: str) -> RedirectResponse:
    """Bounce a failed SSO browser navigation back to the login page with a
    short reason code the SPA can surface (avoids dumping JSON at the user)."""
    return RedirectResponse(
        f"/auth?sso_error={urllib.parse.quote(reason, safe='')}",
        status_code=status.HTTP_302_FOUND,
    )


@router.get("/auth/me", summary="Get current user info")
async def me(user: AuthenticatedUser = Depends(get_current_user)):
    return {
        "user_id": user.user_id,
        "username": user.username,
        "email": user.email,
        "display_name": user.display_name,
        "is_admin": user.is_admin,
        "auth_method": user.auth_method,
    }


@router.patch("/auth/me", summary="Update own profile (display_name / email)")
async def update_my_profile(
    req: UpdateProfileRequest,
    user: AuthenticatedUser = Depends(get_current_user),
):
    return await update_profile(
        user.user_id, display_name=req.display_name, email=req.email,
    )


@router.post("/auth/tokens", summary="Create a Personal Access Token")
async def create_token(req: CreatePATRequest, user: AuthenticatedUser = Depends(get_current_user)):
    return await create_pat(user.user_id, req.name, expires_days=req.expires_days)


@router.get("/auth/tokens", summary="List your PATs")
async def list_tokens(user: AuthenticatedUser = Depends(get_current_user)):
    return {"tokens": await list_pats(user.user_id)}


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
    revoked_at = await revoke_all_sessions(user.user_id)
    return {"revoked_before": revoked_at.isoformat()}
