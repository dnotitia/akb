"""REST API routes for search."""

from fastapi import APIRouter, Depends, HTTPException, Query

from app.api.deps import get_current_user
from app.models.document import SearchResponse
from app.services.access_service import check_vault_access
from app.services.auth_service import AuthenticatedUser
from app.services.search_service import SearchService
from app.services.uri_service import parse_uri

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


@router.get("/drill-down", summary="Drill down to document sections")
async def drill_down(
    uri: str = Query(..., description="Document URI"),
    section: str | None = Query(None),
    user: AuthenticatedUser = Depends(get_current_user),
):
    parsed = parse_uri(uri)
    if parsed is None or parsed[1] != "doc":
        raise HTTPException(status_code=400, detail=f"Expected a doc URI, got {uri!r}")
    vault, _rtype, doc_path = parsed
    # MCP `akb_drill_down` enforces vault ACL via check_vault_access; the
    # REST entry-point used to skip it, letting any authenticated user
    # read chunk content from any vault they don't belong to.
    await check_vault_access(user.user_id, vault, required_role="reader")
    sections = await search_service.drill_down(vault, doc_path, section)
    return {"uri": uri, "sections": sections}


@router.get("/grep", summary="Literal substring / regex search across documents")
async def grep_documents(
    q: str = Query(..., description="Pattern to search for"),
    vault: str | None = Query(None),
    collection: str | None = Query(None),
    regex: bool = Query(False),
    case_sensitive: bool = Query(False),
    limit: int = Query(20, ge=1, le=100),
    count_only: bool = Query(False, description="grep -c — per-doc counts + total"),
    files_with_matches: bool = Query(False, description="grep -l — URIs with matches"),
    user: AuthenticatedUser = Depends(get_current_user),
):
    return await search_service.grep(
        pattern=q, vault=vault, collection=collection,
        regex=regex, case_sensitive=case_sensitive, limit=limit,
        count_only=count_only, files_with_matches=files_with_matches,
        user_id=user.user_id,
    )
