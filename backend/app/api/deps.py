"""Shared dependencies for API routes."""

from typing import NoReturn

from fastapi import HTTPException, Request, Security
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from app.models.vault_scope import (
    current_request_jwt_claims,
    parse_request_jwt_claims_header,
)
from app.services.auth_service import AuthenticatedUser, resolve_token, token_has_scope

bearer_auth = HTTPBearer(auto_error=False, scheme_name="bearerAuth")

_CLAIMS_HEADER = "x-akb-claims"


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
