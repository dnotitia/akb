"""Shared dependencies for API routes."""

from fastapi import Header, HTTPException

from app.services.auth_service import AuthenticatedUser, resolve_token


async def get_current_user(authorization: str = Header(None)) -> AuthenticatedUser:
    """Extract and validate user from Authorization header. Required."""
    if not authorization:
        raise HTTPException(status_code=401, detail="Authorization header required")
    user = await resolve_token(authorization)
    if not user:
        raise HTTPException(status_code=401, detail="Invalid or expired token")
    return user


async def get_optional_user(authorization: str = Header(None)) -> AuthenticatedUser | None:
    """Extract user if present, None otherwise. For optional auth."""
    if not authorization:
        return None
    return await resolve_token(authorization)
