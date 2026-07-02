"""Shared dependencies for API routes."""

from fastapi import HTTPException, Request, Security
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from app.services.auth_service import AuthenticatedUser, resolve_token, token_has_scope

bearer_auth = HTTPBearer(auto_error=False, scheme_name="bearerAuth")


def _required_scope_for_request(request: Request) -> str:
    return "read" if request.method.upper() in {"GET", "HEAD", "OPTIONS"} else "write"


async def get_current_user(
    request: Request,
    _credentials: HTTPAuthorizationCredentials | None = Security(bearer_auth),
) -> AuthenticatedUser:
    """Extract and validate user from Authorization header. Required."""
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
    return user


async def get_optional_user(
    request: Request,
    _credentials: HTTPAuthorizationCredentials | None = Security(bearer_auth),
) -> AuthenticatedUser | None:
    """Extract user if present, None otherwise. For optional auth."""
    authorization = request.headers.get("authorization")
    if not authorization:
        return None
    user = await resolve_token(authorization)
    if user is None:
        return None
    required_scope = _required_scope_for_request(request)
    if not token_has_scope(user.token_scopes, required_scope):
        return None
    return user
