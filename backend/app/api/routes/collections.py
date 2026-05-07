"""REST API routes for browsing collections."""

from fastapi import APIRouter, Depends, Query

from app.api.deps import get_current_user
from app.models.document import BrowseResponse
from app.services.access_service import check_vault_access
from app.services.auth_service import AuthenticatedUser
from app.services.document_service import DocumentService

router = APIRouter()
doc_service = DocumentService()


@router.get("/browse/{vault}", response_model=BrowseResponse, summary="Browse vault collections and documents")
async def browse_vault(
    vault: str,
    collection: str | None = Query(None),
    depth: int = Query(1, ge=1, le=2),
    user: AuthenticatedUser = Depends(get_current_user),
):
    await check_vault_access(user.user_id, vault, required_role="reader")
    return await doc_service.browse(vault, collection=collection, depth=depth)
