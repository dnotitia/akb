"""REST API routes for vault activity history (git-based).

Was `sessions.py` until the memory-feature removal in v0.4.0 — what
remained were the activity / recent-changes / diff endpoints, all of
which are read-only views over git history rather than session
management. The file was renamed accordingly.
"""

from fastapi import APIRouter, Depends, HTTPException, Query

from app.api.deps import get_current_user
from app.services.access_service import check_vault_access
from app.services.auth_service import AuthenticatedUser
from app.services.revision_backend import get_revision_backend
from app.services.user_directory import resolve_display_names
from app.models.activity import (
    AkbActivityEnvelope,
    AkbDocumentDiffEnvelope,
    AkbDocumentHistoryEnvelope,
    AkbRecentChangesEnvelope,
)

router = APIRouter()
revision_backend = get_revision_backend()


async def _resolve_activity_authors(entries: list[dict]) -> list[dict]:
    """Add a human `author_name` to each commit-log entry.

    The git author/agent token is the actor's username on the normal write
    path (older rows / some lifecycle ops carry the user UUID). Resolve either
    form to a display name so the UI shows a name instead of a raw token.
    Authors that match no user (external-git imports) are left as-is.
    """
    names = await resolve_display_names(
        v for e in entries for v in (e.get("agent"), e.get("author"))
    )
    if not names:
        return entries
    for e in entries:
        raw = e.get("agent") or e.get("author")
        if raw and raw in names:
            e["author_name"] = names[raw]
    return entries


@router.get(
    "/activity/{vault}",
    summary="Get vault activity history (Git-based)",
    operation_id="activityList",
    tags=["activity"],
    response_model=AkbActivityEnvelope,
    response_model_exclude_unset=True,
)
async def vault_activity(
    vault: str,
    collection: str | None = Query(None),
    author: str | None = Query(None),
    since: str | None = Query(None),
    limit: int = Query(20, ge=1, le=100),
    user: AuthenticatedUser = Depends(get_current_user),
):
    await check_vault_access(user.user_id, vault, required_role="reader")
    entries = await revision_backend.vault_activity(
        vault, max_count=limit, since=since, path=collection,
    )
    entries = await _resolve_activity_authors(entries)

    if author:
        needle = author.lower()
        entries = [
            e for e in entries
            if needle in e.get("agent", "").lower()
            or needle in e.get("author", "").lower()
            or needle in (e.get("author_name") or "").lower()
        ]

    return {"kind": "activity", "vault": vault, "total": len(entries), "activity": entries}


@router.get(
    "/recent",
    summary="Recent document changes across vaults the user can access",
    operation_id="activityRecent",
    tags=["activity"],
    response_model=AkbRecentChangesEnvelope,
    response_model_exclude_unset=True,
)
async def recent_changes(
    vault: str | None = Query(None, description="Limit to a single vault"),
    limit: int = Query(20, ge=1, le=100),
    user: AuthenticatedUser = Depends(get_current_user),
):
    """Return recent document updates for the user.

    When `vault` is given, returns docs from that vault only (after access
    check). Otherwise, returns docs from every vault the user owns or has
    been granted access to. Documents are sorted by `updated_at DESC`.
    """
    if vault:
        await check_vault_access(user.user_id, vault, required_role="reader")
    changes = await revision_backend.recent_changes(
        user.user_id, vault=vault, limit=limit,
    )
    return {"kind": "recent_changes", "changes": changes}


@router.get(
    "/diff/{vault}/{doc_id:path}",
    summary="Get document diff at a specific commit",
    operation_id="documentsDiff",
    tags=["documents"],
    response_model=AkbDocumentDiffEnvelope,
    response_model_exclude_unset=True,
)
async def document_diff(
    vault: str,
    doc_id: str,
    commit: str = Query(..., description="Commit hash"),
    user: AuthenticatedUser = Depends(get_current_user),
):
    await check_vault_access(user.user_id, vault, required_role="reader")

    result = await revision_backend.document_diff(vault, doc_id, commit)
    if result is None:
        raise HTTPException(status_code=404, detail=f"Document not found: {doc_id}")
    return {"kind": "document_diff", **result}


@router.get(
    "/history/{vault}/{doc_id:path}",
    summary="Get document version history (Git-based)",
    operation_id="documentsHistory",
    tags=["documents"],
    response_model=AkbDocumentHistoryEnvelope,
    response_model_exclude_unset=True,
)
async def document_history(
    vault: str,
    doc_id: str,
    limit: int = Query(20, ge=1, le=100),
    user: AuthenticatedUser = Depends(get_current_user),
):
    """REST mirror of the akb_history MCP tool.

    Lives under /history/... (not nested in /documents/...) so the greedy
    {doc_id:path} converter on GET /documents/{vault}/{doc_id} — registered
    first — can't swallow the /history suffix. Business logic (doc lookup,
    created_at lineage boundary, author_name annotation) is shared via
    DocumentService.history(); a missing vault/doc raises NotFoundError,
    which the global AKBError handler maps to 404.
    """
    await check_vault_access(user.user_id, vault, required_role="reader")
    result = await revision_backend.document_history(vault, doc_id, limit=limit)
    return {"kind": "document_history", **result}
