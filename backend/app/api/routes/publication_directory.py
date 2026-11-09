"""Authenticated inventory; separate from anonymous publication resolution."""

from typing import Literal

from fastapi import APIRouter, Depends, Query

from app.api.deps import get_current_user
from app.services.auth_service import AuthenticatedUser
from app.services.publication_directory import list_directory

router = APIRouter()


@router.get("/publications", summary="Browse publications across accessible Vaults")
async def public_link_directory(
    vault: str | None = Query(default=None, min_length=1, max_length=200),
    q: str = Query(default="", max_length=200),
    status: Literal["active", "all", "unavailable"] = "active",
    expiry: Literal["any", "none", "soon", "long-running"] = "any",
    resource_type: Literal["document", "table_query", "file"] | None = None,
    page_size: int = Query(default=30, ge=1, le=100),
    cursor: str | None = Query(default=None, max_length=2048),
    user: AuthenticatedUser = Depends(get_current_user),
):
    return await list_directory(user, vault=vault, query=q, status=status, expiry=expiry,
                                resource_type=resource_type, page_size=page_size, cursor=cursor)
