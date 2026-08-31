"""REST API routes for search."""

from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query

from app.api.deps import get_current_user
from app.models.document import DrillDownResponse, GrepResponse, SearchResponse
from app.services.access_service import check_vault_access
from app.services.auth_service import AuthenticatedUser
from app.services.search_service import SearchService
from app.services.uri_service import parse_uri

router = APIRouter()
search_service = SearchService()


@router.get(
    "/search",
    response_model=SearchResponse,
    summary="Search documents",
    operation_id="searchSearchDocuments",
)
async def search_documents(
    q: str = Query(..., description="Search query"),
    mode: Literal["hybrid"] = Query(
        "hybrid",
        description="Search execution mode. Currently only hybrid dense+sparse search is supported.",
    ),
    rerank: bool | None = Query(
        None,
        description="Optional rerank preference for clients; server configuration controls whether rerank is available.",
    ),
    vault: list[str] | None = Query(None, description="Limit to one or more vaults (repeat the param); omit for all accessible vaults."),
    collection: str | None = Query(None),
    type: str | None = Query(None),
    tags: list[str] | None = Query(None),
    limit: int = Query(10, ge=1, le=100),
    include_archived: bool = Query(False, description="Include archived documents (hidden from search by default)."),
    source_uris: list[str] | None = Query(
        None,
        description="Restrict search to this set of resource akb:// URIs (intersected with the other filters + ACL).",
    ),
    user: AuthenticatedUser = Depends(get_current_user),
):
    return await search_service.search(
        query=q, vault=vault, collection=collection,
        mode=mode, rerank=rerank,
        doc_type=type, tags=tags, limit=limit,
        user_id=user.user_id, include_archived=include_archived,
        source_uris=source_uris,
    )


@router.get(
    "/drill-down",
    response_model=DrillDownResponse,
    summary="Drill down to document sections",
    operation_id="searchDrillDown",
)
async def drill_down(
    uri: str = Query(..., description="Document URI"),
    section: str | None = Query(None),
    user: AuthenticatedUser = Depends(get_current_user),
):
    parsed = parse_uri(uri)
    if parsed is None or parsed.kind != "doc" or parsed.identifier is None:
        raise HTTPException(status_code=400, detail=f"Expected a doc URI, got {uri!r}")
    vault, doc_path = parsed.vault, parsed.identifier
    # MCP `akb_drill_down` enforces vault ACL via check_vault_access; the
    # REST entry-point used to skip it, letting any authenticated user
    # read chunk content from any vault they don't belong to.
    await check_vault_access(user.user_id, vault, required_role="reader")
    sections = await search_service.drill_down(vault, doc_path, section)
    return {"kind": "drill_down", "uri": uri, "sections": sections}


@router.get(
    "/grep",
    response_model=GrepResponse,
    response_model_exclude_none=True,
    summary="Literal substring / regex search across documents",
    operation_id="searchGrepDocuments",
)
async def grep_documents(
    q: str = Query(..., description="Pattern to search for"),
    vault: list[str] | None = Query(None, description="Limit to one or more vaults (repeat the param); omit for all accessible vaults."),
    collection: str | None = Query(None),
    regex: bool = Query(False),
    case_sensitive: bool = Query(False),
    limit: int = Query(20, ge=1, le=100),
    count_only: bool = Query(False, description="grep -c — per-doc counts + total"),
    files_with_matches: bool = Query(False, description="grep -l — URIs with matches"),
    measurement_include_text_files: bool = Query(
        False,
        description="Native mode: include admitted searchable text Files.",
    ),
    user: AuthenticatedUser = Depends(get_current_user),
):
    response = await search_service.grep(
        pattern=q, vault=vault, collection=collection,
        regex=regex, case_sensitive=case_sensitive, limit=limit,
        count_only=count_only, files_with_matches=files_with_matches,
        measurement_include_text_files=measurement_include_text_files,
        user_id=user.user_id,
    )
    response.setdefault("regex", regex)
    return {"kind": "grep", **response}
