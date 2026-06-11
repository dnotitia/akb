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


def _normalize_origin(value: str | None) -> str | None:
    """Canonical ``scheme://host[:port]`` for an absolute http(s) URL, else None.

    Rejects anything that is not a plain absolute http/https URL: relative
    paths, scheme-relative ``//host``, non-web schemes, and — crucially —
    URLs carrying embedded credentials (``https://trusted@evil.com``, whose
    real host is ``evil.com``), a classic origin-spoofing trick. The host is
    lowercased so the comparison is case-insensitive.
    """
    if not value:
        return None
    parsed = urllib.parse.urlparse(value)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        return None
    if "@" in parsed.netloc:  # embedded userinfo — spoof guard
        return None
    origin = f"{parsed.scheme}://{parsed.hostname.lower()}"
    if parsed.port is not None:
        origin += f":{parsed.port}"
    return origin


def _allowed_companion_origin(raw: str | None) -> str | None:
    """Origin of ``raw`` iff it is an absolute URL on a configured companion
    origin (``keycloak_post_login_allowed_origins``); otherwise None.

    This allowlist is the ONLY gate that lets the post-login one-time code
    leave akb's own origin. An empty list ⇒ always None ⇒ the same-site
    behaviour that predates the option.
    """
    origin = _normalize_origin(raw)
    if origin is None:
        return None
    allowed = {
        _normalize_origin(o) for o in settings.keycloak_post_login_allowed_origins
    }
    return origin if origin in allowed else None


def _with_query_param(url: str, key: str, value: str) -> str:
    """Append ``key=value`` to ``url``'s query, preserving existing query
    and fragment."""
    parts = urllib.parse.urlsplit(url)
    query = urllib.parse.parse_qsl(parts.query, keep_blank_values=True)
    query.append((key, value))
    return urllib.parse.urlunsplit(
        (parts.scheme, parts.netloc, parts.path,
         urllib.parse.urlencode(query), parts.fragment)
    )


def _post_login_target(redirect: str | None, one_time_code: str) -> str:
    """Build the URL the SSO callback bounces the browser to, carrying the
    one-time code. Two shapes, chosen per request:

    - **Companion app** (cross-origin SSO delegation): ``redirect`` is an
      absolute URL on an allowlisted origin → deliver the code straight to
      that URL (origin + path + its own query preserved). This is how a
      first-party app like reef, riding akb's Keycloak client, gets the code
      back on *its* origin instead of akb's SPA.
    - **Default / akb's own SPA**: append the code to the same-site
      ``keycloak_post_login_path`` and carry the safe in-app path as
      ``redirect``. Any non-allowlisted absolute / scheme-relative value
      collapses here (open-redirect protection).

    The allowlist is re-checked here (not just at login time) so the
    delivery decision always reflects current config, never a value frozen
    into flow state minutes earlier.
    """
    if _allowed_companion_origin(redirect) is not None:
        return _with_query_param(redirect, "code", one_time_code)  # type: ignore[arg-type]
    safe = _safe_redirect_path(redirect)
    return (
        f"{settings.keycloak_post_login_path}"
        f"?code={urllib.parse.quote(one_time_code)}"
        f"&redirect={urllib.parse.quote(safe, safe='')}"
    )


@router.get("/auth/keycloak/login", summary="Begin Keycloak SSO login")
async def keycloak_login(redirect: str = "/"):
    _require_keycloak()
    from app.services.keycloak_oidc import get_keycloak_oidc
    # An allowlisted companion-app absolute URL rides through verbatim
    # (re-validated at callback time); everything else is reduced to a safe
    # same-site path before it ever enters the flow state.
    dest = (
        redirect
        if _allowed_companion_origin(redirect) is not None
        else _safe_redirect_path(redirect)
    )
    url = await get_keycloak_oidc().begin_login(dest)
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
    # Stash the Keycloak id_token too so the SPA can pass it back as
    # id_token_hint on logout (seamless RP-initiated logout, no KC prompt).
    one_time = await issue_exchange_code({**login_response, "kc_id_token": id_token})
    target = _post_login_target(flow.get("redirect_path", "/"), one_time)
    return RedirectResponse(target, status_code=status.HTTP_302_FOUND)


@router.get("/auth/keycloak/logout", summary="RP-initiated Keycloak logout")
async def keycloak_logout(id_token_hint: str | None = None):
    """End the Keycloak SSO session (so the next SSO login prompts again /
    can switch user). AKB already cleared its own JWT client-side; this
    redirects the browser to Keycloak's end_session_endpoint, which then
    redirects back to the AKB login page.

    `id_token_hint` (optional) makes the logout seamless (no Keycloak
    confirmation page). The SPA passes the Keycloak id_token it received at
    exchange time; without it, Keycloak may show a logout confirmation."""
    _require_keycloak()
    from app.services.keycloak_oidc import get_keycloak_oidc
    post_logout = settings.public_base_url.rstrip("/") + "/auth"
    url = get_keycloak_oidc().logout_url(
        id_token_hint=id_token_hint, post_logout_redirect=post_logout
    )
    return RedirectResponse(url, status_code=status.HTTP_302_FOUND)


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
