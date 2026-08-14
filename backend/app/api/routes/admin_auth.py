"""Mode-separated product-admin browser authentication routes."""

from __future__ import annotations

from datetime import datetime, timezone
from urllib.parse import urlsplit

from fastapi import APIRouter, Depends, Header, HTTPException, Request, status
from fastapi.responses import JSONResponse, RedirectResponse

from app.config import settings
from app.exceptions import AuthenticationError
from app.services.admin_auth_service import (
    ProductAdminIdentity,
    authenticate_local_product_admin,
    create_sso_admin_browser_session,
    resolve_local_product_admin,
    resolve_prebound_sso_product_admin,
    resolve_sso_admin_browser_session,
    revoke_sso_admin_browser_session,
    validate_sso_admin_browser_session_csrf,
)
from app.services.auth_service import AuthenticatedUser
from app.services.keycloak_oidc import get_keycloak_oidc
from app.util.text import NFCModel


router = APIRouter()

_ADMIN_SESSION_COOKIE = "__Host-akb_admin_session"
_ADMIN_CSRF_COOKIE = "__Host-akb_admin_csrf"
_ADMIN_OIDC_BINDING_COOKIE = "__Host-akb_admin_oidc_binding"
_ADMIN_SESSION_COOKIE_DEV = "akb_dev_admin_session"
_ADMIN_CSRF_COOKIE_DEV = "akb_dev_admin_csrf"
_ADMIN_OIDC_BINDING_COOKIE_DEV = "akb_dev_admin_oidc_binding"


class AdminLocalLoginRequest(NFCModel):
    username: str
    password: str


ProductAdminActor = ProductAdminIdentity | AuthenticatedUser


async def get_current_product_admin(
    request: Request,
    authorization: str | None = Header(default=None),
) -> ProductAdminActor:
    """Resolve the mode-selected product-admin credential."""
    if settings.require_auth_mode() == "local":
        if not authorization:
            raise AuthenticationError()
        return await resolve_local_product_admin(authorization)
    return await resolve_sso_admin_browser_session(request.cookies.get(_admin_session_cookie_name(), ""))


async def get_product_admin_mutation(
    request: Request,
    authorization: str | None = Header(default=None),
    csrf_header: str | None = Header(default=None, alias="X-AKB-Admin-CSRF"),
) -> ProductAdminActor:
    """Resolve admin authority and require CSRF for cookie-backed SSO."""
    if settings.require_auth_mode() == "local":
        if not authorization:
            raise AuthenticationError()
        return await resolve_local_product_admin(authorization)
    if csrf_header is None:
        raise AuthenticationError("Invalid admin CSRF token")
    return await validate_sso_admin_browser_session_csrf(
        request.cookies.get(_admin_session_cookie_name(), ""),
        request.cookies.get(_admin_csrf_cookie_name(), ""),
        csrf_header,
    )


def _require_mode(expected: str) -> None:
    if settings.require_auth_mode() != expected:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Admin login route is not enabled")


def _admin_sso_ready() -> bool:
    return bool(
        settings.keycloak_enabled
        and settings.keycloak_admin_client_id.strip()
        and settings.keycloak_admin_client_secret
    )


def _require_admin_sso() -> None:
    _require_mode("sso")
    if not _admin_sso_ready():
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "Product-admin SSO is not configured",
        )


def _secure_cookie() -> bool:
    return urlsplit(settings.public_base_url).scheme == "https"


def _admin_session_cookie_name() -> str:
    return _ADMIN_SESSION_COOKIE if _secure_cookie() else _ADMIN_SESSION_COOKIE_DEV


def _admin_csrf_cookie_name() -> str:
    return _ADMIN_CSRF_COOKIE if _secure_cookie() else _ADMIN_CSRF_COOKIE_DEV


def _admin_oidc_binding_cookie_name() -> str:
    if _secure_cookie():
        return _ADMIN_OIDC_BINDING_COOKIE
    return _ADMIN_OIDC_BINDING_COOKIE_DEV


def _identity_response(
    identity: ProductAdminIdentity | AuthenticatedUser,
) -> dict:
    return {
        "id": str(identity.user_id),
        "username": identity.username,
        "email": identity.email,
        "display_name": identity.display_name,
        "is_admin": True,
    }


def _set_admin_cookies(
    response: RedirectResponse,
    *,
    token: str,
    csrf_token: str,
    expires_at: datetime,
) -> None:
    max_age = max(
        1,
        int((expires_at - datetime.now(timezone.utc)).total_seconds()),
    )
    secure = _secure_cookie()
    response.set_cookie(
        _admin_session_cookie_name(),
        token,
        max_age=max_age,
        secure=secure,
        httponly=True,
        samesite="lax",
        path="/",
    )
    response.set_cookie(
        _admin_csrf_cookie_name(),
        csrf_token,
        max_age=max_age,
        secure=secure,
        httponly=False,
        samesite="lax",
        path="/",
    )
    response.headers["Cache-Control"] = "no-store"
    response.headers["Pragma"] = "no-cache"


def _clear_admin_cookies(response: JSONResponse) -> None:
    secure = _secure_cookie()
    response.delete_cookie(
        _admin_session_cookie_name(),
        secure=secure,
        httponly=True,
        samesite="lax",
        path="/",
    )
    response.delete_cookie(
        _admin_csrf_cookie_name(),
        secure=secure,
        httponly=False,
        samesite="lax",
        path="/",
    )


def _set_admin_oidc_binding_cookie(
    response: RedirectResponse,
    browser_binding: str,
) -> None:
    response.set_cookie(
        _admin_oidc_binding_cookie_name(),
        browser_binding,
        max_age=600,
        secure=_secure_cookie(),
        httponly=True,
        samesite="lax",
        path="/",
    )


def _clear_admin_oidc_binding_cookie(response: RedirectResponse) -> None:
    response.delete_cookie(
        _admin_oidc_binding_cookie_name(),
        secure=_secure_cookie(),
        httponly=True,
        samesite="lax",
        path="/",
    )


@router.get("/admin/auth/config", summary="Public product-admin login configuration")
async def admin_auth_config():
    mode = settings.require_auth_mode()
    local_enabled = mode == "local"
    keycloak_enabled = mode == "sso" and _admin_sso_ready()
    return {
        "schema_version": 1,
        "auth_mode": mode,
        "local": {
            "enabled": local_enabled,
            "login_url": "/api/v1/admin/auth/local/login" if local_enabled else None,
        },
        "keycloak": {
            "enabled": keycloak_enabled,
            "login_url": ("/api/v1/admin/auth/keycloak/login" if keycloak_enabled else None),
        },
    }


@router.post("/admin/auth/local/login", summary="Local product-admin login")
async def admin_local_login(request: AdminLocalLoginRequest):
    _require_mode("local")
    return await authenticate_local_product_admin(request.username, request.password)


@router.get(
    "/admin/auth/keycloak/login",
    summary="Keycloak product-admin login",
    status_code=status.HTTP_303_SEE_OTHER,
    response_class=RedirectResponse,
)
async def admin_keycloak_login():
    _require_admin_sso()
    issued = await get_keycloak_oidc().begin_admin_login()
    response = RedirectResponse(
        issued.location,
        status_code=status.HTTP_303_SEE_OTHER,
    )
    _set_admin_oidc_binding_cookie(response, issued.browser_binding)
    return response


@router.get(
    "/admin/auth/keycloak/callback",
    summary="Keycloak product-admin callback",
    status_code=status.HTTP_303_SEE_OTHER,
    response_class=RedirectResponse,
)
async def admin_keycloak_callback(
    request: Request,
    code: str = "",
    state: str = "",
    error: str | None = None,
):
    _require_admin_sso()
    if error is not None or not code or not state:
        raise AuthenticationError("Product-admin sign-in failed")
    oidc = get_keycloak_oidc()
    browser_binding = request.cookies.get(_admin_oidc_binding_cookie_name(), "")
    transient = await oidc.consume_admin_state(state, browser_binding)
    if not isinstance(transient, dict) or set(transient) != {
        "code_verifier",
        "nonce",
    }:
        raise AuthenticationError("Product-admin sign-in failed")
    verifier = transient.get("code_verifier")
    nonce = transient.get("nonce")
    if not isinstance(verifier, str) or not isinstance(nonce, str):
        raise AuthenticationError("Product-admin sign-in failed")
    tokens = await oidc.exchange_admin_code(code, verifier)
    id_token = tokens.get("id_token")
    if tokens.get("token_type") != "Bearer" or not isinstance(id_token, str):
        raise AuthenticationError("Product-admin sign-in failed")
    claims = await oidc.verify_admin_id_token(id_token, expected_nonce=nonce)
    identity = await resolve_prebound_sso_product_admin(claims)
    issued = await create_sso_admin_browser_session(identity, claims)
    response = RedirectResponse("/admin", status_code=status.HTTP_303_SEE_OTHER)
    _set_admin_cookies(
        response,
        token=issued.token,
        csrf_token=issued.csrf_token,
        expires_at=issued.expires_at,
    )
    _clear_admin_oidc_binding_cookie(response)
    return response


@router.get("/admin/auth/session", summary="Get current product-admin session")
async def admin_session(
    identity: ProductAdminActor = Depends(get_current_product_admin),
):
    mode = settings.require_auth_mode()
    return {
        "schema_version": 1,
        "auth_mode": mode,
        "user": _identity_response(identity),
    }


@router.post("/admin/auth/logout", summary="End current product-admin session")
async def admin_logout(
    request: Request,
    authorization: str | None = Header(default=None),
    csrf_header: str | None = Header(default=None, alias="X-AKB-Admin-CSRF"),
):
    mode = settings.require_auth_mode()
    if mode == "local":
        if not authorization:
            raise AuthenticationError()
        await resolve_local_product_admin(authorization)
        logout_url = "/admin"
    else:
        if csrf_header is None:
            raise AuthenticationError("Invalid admin CSRF token")
        raw_token = request.cookies.get(_admin_session_cookie_name(), "")
        csrf_cookie = request.cookies.get(_admin_csrf_cookie_name(), "")
        await revoke_sso_admin_browser_session(
            raw_token,
            csrf_cookie,
            csrf_header,
        )
        logout_url = get_keycloak_oidc().admin_logout_url()
    response = JSONResponse({"logout_url": logout_url})
    response.headers["Cache-Control"] = "no-store"
    response.headers["Pragma"] = "no-cache"
    if mode == "sso":
        _clear_admin_cookies(response)
    return response
