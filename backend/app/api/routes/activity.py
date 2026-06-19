"""REST API routes for vault activity history (git-based).

Was `sessions.py` until the memory-feature removal in v0.4.0 — what
remained were the activity / recent-changes / diff endpoints, all of
which are read-only views over git history rather than session
management. The file was renamed accordingly.
"""

import json

from fastapi import APIRouter, Depends, HTTPException, Query

from app.api.deps import get_current_user
from app.services.access_service import check_vault_access
from app.services.auth_service import AuthenticatedUser
from app.services.document_service import DocumentService
from app.services.git_service import GitService
from app.services.user_directory import resolve_display_names
from app.db.postgres import get_pool
from app.repositories.document_repo import DocumentRepository

router = APIRouter()
git = GitService()
doc_service = DocumentService()


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


@router.get("/activity/{vault}", summary="Get vault activity history (Git-based)")
async def vault_activity(
    vault: str,
    collection: str | None = Query(None),
    author: str | None = Query(None),
    since: str | None = Query(None),
    limit: int = Query(20, ge=1, le=100),
    user: AuthenticatedUser = Depends(get_current_user),
):
    await check_vault_access(user.user_id, vault, required_role="reader")
    entries = git.vault_log(vault, max_count=limit, since=since, path=collection)
    entries = await _resolve_activity_authors(entries)

    if author:
        needle = author.lower()
        entries = [
            e for e in entries
            if needle in e.get("agent", "").lower()
            or needle in e.get("author", "").lower()
            or needle in (e.get("author_name") or "").lower()
        ]

    return {"vault": vault, "total": len(entries), "activity": entries}


@router.get("/recent", summary="Recent document changes across vaults the user can access")
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
    pool = await get_pool()
    async with pool.acquire() as conn:
        if vault:
            await check_vault_access(user.user_id, vault, required_role="reader")
            rows = await conn.fetch(
                """
                SELECT d.id, d.title, d.path, d.doc_type, d.current_commit,
                       d.updated_at, v.name AS vault_name, d.metadata
                FROM documents d
                JOIN vaults v ON d.vault_id = v.id
                WHERE v.name = $1
                ORDER BY d.updated_at DESC
                LIMIT $2
                """,
                vault, limit,
            )
        else:
            # Include public-readable vaults so /recent is consistent with
            # /search and list_accessible_vaults — pre-fix this route only
            # surfaced owned/granted vaults, hiding docs from vaults the
            # user could still read via public_access (06-F5).
            rows = await conn.fetch(
                """
                SELECT d.id, d.title, d.path, d.doc_type, d.current_commit,
                       d.updated_at, v.name AS vault_name, d.metadata
                FROM documents d
                JOIN vaults v ON d.vault_id = v.id
                LEFT JOIN vault_access va
                    ON va.vault_id = v.id AND va.user_id = $1
                WHERE v.owner_id = $1
                   OR va.user_id = $1
                   OR v.public_access IN ('reader', 'writer')
                ORDER BY d.updated_at DESC
                LIMIT $2
                """,
                user.user_id, limit,
            )

    changes = []
    for r in rows:
        meta = r["metadata"] or {}
        if isinstance(meta, str):
            try:
                meta = json.loads(meta)
            except json.JSONDecodeError:
                meta = {}
        doc_id = meta.get("id") or str(r["id"])
        changes.append({
            "doc_id": doc_id,
            "vault": r["vault_name"],
            "path": r["path"],
            "title": r["title"],
            "type": r["doc_type"] or "note",
            "commit": r["current_commit"],
            "changed_at": r["updated_at"].isoformat() if r["updated_at"] else None,
        })
    return {"changes": changes}


@router.get("/diff/{vault}/{doc_id:path}", summary="Get document diff at a specific commit")
async def document_diff(
    vault: str,
    doc_id: str,
    commit: str = Query(..., description="Commit hash"),
    user: AuthenticatedUser = Depends(get_current_user),
):
    await check_vault_access(user.user_id, vault, required_role="reader")

    pool = await get_pool()
    doc_repo = DocumentRepository(pool)
    async with pool.acquire() as conn:
        v = await conn.fetchrow("SELECT id FROM vaults WHERE name = $1", vault)
        doc = await doc_repo.find_by_ref_with_conn(conn, v["id"], doc_id)
        if not doc:
            raise HTTPException(status_code=404, detail=f"Document not found: {doc_id}")

    return git.file_diff(vault, doc["path"], commit)


@router.get("/history/{vault}/{doc_id:path}", summary="Get document version history (Git-based)")
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
    return await doc_service.history(vault, doc_id, limit=limit)
