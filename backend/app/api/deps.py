"""Shared dependencies for API routes."""

from dataclasses import dataclass
import re
from typing import NoReturn
import uuid

from fastapi import HTTPException, Request, Security
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from app.config import settings
from app.exceptions import AuthenticationError
from app.models.vault_scope import (
    current_request_jwt_claims,
    parse_request_jwt_claims_header,
)
from app.services.auth_service import (
    AuthenticatedUser,
    resolve_delegated_human_authorization,
    resolve_rest_credential_change_authorization,
    resolve_rest_user_authorization,
    token_has_scope,
)
from app.services.app_identity_service import (
    AppPrincipal,
    record_app_audit,
    resolve_app_authorization,
)
from app.services.auth_policy import sso_browser_session_ready
from app.services.sso_browser_session_service import (
    SSO_BROWSER_CSRF_HEADER,
    resolve_sso_browser_session,
    sso_browser_csrf_cookie_name,
    sso_browser_session_cookie_name,
)

bearer_auth = HTTPBearer(auto_error=False, scheme_name="bearerAuth")

_CLAIMS_HEADER = "x-akb-claims"
_DELEGATED_AUTH_HEADER = "X-Akb-Delegated-Authorization"
_CORRELATION_HEADER = "x-correlation-id"
_SAFE_CORRELATION = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")


@dataclass(frozen=True)
class DelegatedActor:
    user: AuthenticatedUser
    service_user_id: str
    service_token_id: str


def request_correlation_id(request: Request) -> str:
    supplied = request.headers.get(_CORRELATION_HEADER)
    if supplied and _SAFE_CORRELATION.fullmatch(supplied):
        return supplied
    return str(uuid.uuid4())


def _required_scope_for_request(request: Request) -> str:
    return "read" if request.method.upper() in {"GET", "HEAD", "OPTIONS"} else "write"


def _claim_header(request: Request) -> str | None:
    return request.headers.get(_CLAIMS_HEADER)


def _reject_claims(message: str, code: str) -> NoReturn:
    raise HTTPException(
        status_code=403,
        detail={
            "message": message,
            "code": code,
        },
    )


def _reject_delegation(message: str, code: str) -> NoReturn:
    raise HTTPException(
        status_code=403,
        detail={
            "message": message,
            "code": code,
        },
    )


async def require_delegated_actor(
    request: Request,
    service_user: AuthenticatedUser,
) -> DelegatedActor:
    """Resolve the human principal paired with an action-limited service key."""
    if service_user.key_class != "service" or service_user.token_id is None:
        _reject_delegation(
            "Delegated authorization requires a service key",
            "delegation_requires_service_key",
        )
    authorization = request.headers.get(_DELEGATED_AUTH_HEADER)
    if authorization is None:
        _reject_delegation(
            f"{_DELEGATED_AUTH_HEADER} is required",
            "delegated_authorization_required",
        )
    delegated = await resolve_delegated_human_authorization(authorization)
    if delegated is None or delegated.auth_method not in {"jwt", "oauth"}:
        _reject_delegation(
            "Delegated authorization must be an active human credential",
            "invalid_delegated_authorization",
        )
    return DelegatedActor(
        user=delegated,
        service_user_id=service_user.user_id,
        service_token_id=service_user.token_id,
    )


def _apply_claim_header(request: Request, user: AuthenticatedUser | None) -> None:
    current_request_jwt_claims.set(None)
    raw_claims = _claim_header(request)
    if raw_claims is None:
        return
    if user is None or user.key_class != "service":
        _reject_claims(
            "X-Akb-Claims is only accepted with a service key",
            "claims_require_service_key",
        )
    try:
        claims = parse_request_jwt_claims_header(raw_claims)
    except ValueError as exc:
        _reject_claims(str(exc), "invalid_claims")
    current_request_jwt_claims.set(claims)


async def _resolve_cookie_user(
    request: Request,
    *,
    optional: bool,
) -> AuthenticatedUser | None:
    """Resolve the separate SSO browser-session request carrier.

    This helper is called only when the Authorization header is absent. An
    explicitly supplied bearer credential always owns the request and can
    never fall through to a cookie after rejection.
    """
    if settings.require_auth_mode() != "sso" or not sso_browser_session_ready():
        return None
    raw_token = request.cookies.get(sso_browser_session_cookie_name(), "")
    if not raw_token:
        return None
    require_csrf = _required_scope_for_request(request) == "write"
    try:
        return await resolve_sso_browser_session(
            raw_token,
            require_csrf=require_csrf,
            csrf_cookie=request.cookies.get(sso_browser_csrf_cookie_name(), ""),
            csrf_header=request.headers.get(SSO_BROWSER_CSRF_HEADER, ""),
        )
    except AuthenticationError:
        if optional:
            return None
        raise HTTPException(
            status_code=401,
            detail="Invalid or expired browser session",
        ) from None


async def get_current_user(
    request: Request,
    _credentials: HTTPAuthorizationCredentials | None = Security(bearer_auth),
) -> AuthenticatedUser:
    """Extract and validate user from Authorization header. Required."""
    return await _authenticated_user(request)


async def _authenticated_user(
    request: Request,
    *,
    for_credential_change: bool = False,
) -> AuthenticatedUser:
    current_request_jwt_claims.set(None)
    authorization = request.headers.get("authorization")
    if authorization:
        resolve = (
            resolve_rest_credential_change_authorization
            if for_credential_change
            else resolve_rest_user_authorization
        )
        user = await resolve(authorization)
    else:
        user = await _resolve_cookie_user(request, optional=False)
        if user is None:
            raise HTTPException(status_code=401, detail="Authentication required")
    if not user:
        raise HTTPException(status_code=401, detail="Invalid or expired token")
    required_scope = _required_scope_for_request(request)
    if not token_has_scope(user.token_scopes, required_scope):
        raise HTTPException(
            status_code=403,
            detail={
                "message": f"Token scope does not include '{required_scope}'",
                "code": "insufficient_scope",
                "required_scope": required_scope,
                "granted_scopes": sorted(user.token_scopes or []),
            },
        )
    _apply_claim_header(request, user)
    return user


async def get_credential_change_user(
    request: Request,
    _credentials: HTTPAuthorizationCredentials | None = Security(bearer_auth),
) -> AuthenticatedUser:
    """``get_current_user`` for the one route a forced change must reach.

    An account owing a credential change is refused everywhere else, which
    would include the route that clears the requirement — so exactly one
    dependency carries the exemption, and only the change-password route
    depends on it. Everything else about the resolution is unchanged: an
    invalid credential is still 401, and an insufficient token scope is
    still 403.
    """
    return await _authenticated_user(request, for_credential_change=True)


async def get_optional_user(
    request: Request,
    _credentials: HTTPAuthorizationCredentials | None = Security(bearer_auth),
) -> AuthenticatedUser | None:
    """Extract user if present, None otherwise. For optional auth."""
    current_request_jwt_claims.set(None)
    authorization = request.headers.get("authorization")
    if not authorization:
        user = await _resolve_cookie_user(request, optional=True)
        if user is not None:
            _apply_claim_header(request, user)
            return user
        if _claim_header(request) is not None:
            _apply_claim_header(request, None)
        return None
    user = await resolve_rest_user_authorization(authorization)
    if user is None:
        if _claim_header(request) is not None:
            _apply_claim_header(request, None)
        return None
    required_scope = _required_scope_for_request(request)
    if not token_has_scope(user.token_scopes, required_scope):
        return None
    _apply_claim_header(request, user)
    return user


async def get_current_app(
    request: Request,
    _credentials: HTTPAuthorizationCredentials | None = Security(bearer_auth),
) -> AppPrincipal:
    """Resolve only an AKB app token; user JWT/PAT/service keys are rejected."""
    authorization = request.headers.get("authorization")
    principal = await resolve_app_authorization(authorization or "")
    if principal is None:
        record_app_audit(
            "app.token.denied",
            correlation_id=request_correlation_id(request),
            outcome="error",
            reason="invalid_or_stale_app_token",
        )
        raise HTTPException(status_code=401, detail="Invalid or expired app token")
    return principal
