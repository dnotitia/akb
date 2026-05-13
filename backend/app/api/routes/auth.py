"""REST API routes for auth — register, login, PAT management."""

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel

from app.api.deps import get_current_user
from app.exceptions import NotFoundError
from app.services.auth_service import (
    AuthenticatedUser,
    register,
    login,
    create_pat,
    list_pats,
    revoke_pat,
)
from app.util.text import NFCModel

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
    scopes: list[str] | None = None
    expires_days: int | None = None


class ChangePasswordRequest(NFCModel):
    current_password: str
    new_password: str


@router.post("/auth/register", summary="Register a new user")
async def register_user(req: RegisterRequest):
    return await register(req.username, req.email, req.password, req.display_name)


@router.post("/auth/login", summary="Login and get JWT")
async def login_user(req: LoginRequest):
    return await login(req.username, req.password)


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


@router.post("/auth/tokens", summary="Create a Personal Access Token")
async def create_token(req: CreatePATRequest, user: AuthenticatedUser = Depends(get_current_user)):
    return await create_pat(user.user_id, req.name, req.scopes, req.expires_days)


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
