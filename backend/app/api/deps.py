"""Shared dependencies for API routes."""

from fastapi import HTTPException, Request, Security
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from app.services.auth_service import AuthenticatedUser, resolve_token

bearer_auth = HTTPBearer(auto_error=False, scheme_name="bearerAuth")


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
    return user


async def get_optional_user(
    request: Request,
    _credentials: HTTPAuthorizationCredentials | None = Security(bearer_auth),
) -> AuthenticatedUser | None:
    """Extract user if present, None otherwise. For optional auth."""
    authorization = request.headers.get("authorization")
    if not authorization:
        return None
    return await resolve_token(authorization)
