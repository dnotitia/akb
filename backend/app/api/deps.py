"""Shared dependencies for API routes."""

from dataclasses import dataclass
from typing import NoReturn

from fastapi import HTTPException, Request, Security
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from app.models.vault_scope import (
    current_request_jwt_claims,
    parse_request_jwt_claims_header,
)
from app.services.auth_service import (
    AuthenticatedUser,
    resolve_akb_session_authorization,
    resolve_token,
    token_has_scope,
)

bearer_auth = HTTPBearer(auto_error=False, scheme_name="bearerAuth")

_CLAIMS_HEADER = "x-akb-claims"
_DELEGATED_AUTH_HEADER = "X-Akb-Delegated-Authorization"


@dataclass(frozen=True)
class DelegatedActor:
    user: AuthenticatedUser
    service_user_id: str
    service_token_id: str


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
    delegated = await resolve_akb_session_authorization(authorization)
    if delegated is None or delegated.auth_method != "jwt":
        _reject_delegation(
            "Delegated authorization must be an active AKB user session",
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


async def get_current_user(
    request: Request,
    _credentials: HTTPAuthorizationCredentials | None = Security(bearer_auth),
) -> AuthenticatedUser:
    """Extract and validate user from Authorization header. Required."""
    current_request_jwt_claims.set(None)
    authorization = request.headers.get("authorization")
    if not authorization:
        raise HTTPException(status_code=401, detail="Authorization header required")
    user = await resolve_token(authorization)
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


async def get_optional_user(
    request: Request,
    _credentials: HTTPAuthorizationCredentials | None = Security(bearer_auth),
) -> AuthenticatedUser | None:
    """Extract user if present, None otherwise. For optional auth."""
    current_request_jwt_claims.set(None)
    authorization = request.headers.get("authorization")
    if not authorization:
        if _claim_header(request) is not None:
            _apply_claim_header(request, None)
        return None
    user = await resolve_token(authorization)
    if user is None:
        if _claim_header(request) is not None:
            _apply_claim_header(request, None)
        return None
    required_scope = _required_scope_for_request(request)
    if not token_has_scope(user.token_scopes, required_scope):
        return None
    _apply_claim_header(request, user)
    return user
