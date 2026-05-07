"""REST API routes for search."""

from fastapi import APIRouter, Depends, Query

from app.api.deps import get_current_user
from app.models.document import SearchResponse
from app.services.auth_service import AuthenticatedUser
from app.services.search_service import SearchService

router = APIRouter()
search_service = SearchService()


@router.get("/search", response_model=SearchResponse, summary="Search documents")
async def search_documents(
    q: str = Query(..., description="Search query"),
    vault: str | None = Query(None),
    collection: str | None = Query(None),
    type: str | None = Query(None),
    tags: list[str] | None = Query(None),
    limit: int = Query(10, ge=1, le=100),
    user: AuthenticatedUser = Depends(get_current_user),
):
    return await search_service.search(
        query=q, vault=vault, collection=collection,
        doc_type=type, tags=tags, limit=limit,
        user_id=user.user_id,
    )


@router.get("/drill-down/{vault}/{doc_id:path}", summary="Drill down to document sections")
async def drill_down(
    vault: str,
    doc_id: str,
    section: str | None = Query(None),
    user: AuthenticatedUser = Depends(get_current_user),
):
    sections = await search_service.drill_down(vault, doc_id, section)
    return {"doc_id": doc_id, "vault": vault, "sections": sections}


@router.get("/grep", summary="Literal substring / regex search across documents")
async def grep_documents(
    q: str = Query(..., description="Pattern to search for"),
    vault: str | None = Query(None),
    collection: str | None = Query(None),
    regex: bool = Query(False),
    case_sensitive: bool = Query(False),
    limit: int = Query(20, ge=1, le=100),
    user: AuthenticatedUser = Depends(get_current_user),
):
    return await search_service.grep(
        pattern=q, vault=vault, collection=collection,
        regex=regex, case_sensitive=case_sensitive, limit=limit,
        user_id=user.user_id,
    )
