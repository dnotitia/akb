"""REST API routes for session management and activity history."""

import json

from fastapi import APIRouter, Depends, HTTPException, Query

from app.api.deps import get_current_user
from app.services.access_service import check_vault_access
from app.services.auth_service import AuthenticatedUser
from app.services.git_service import GitService
from app.services.session_service import SessionService
from app.db.postgres import get_pool
from app.repositories.document_repo import DocumentRepository

router = APIRouter()
session_service = SessionService()
git = GitService()


@router.post("/sessions/start", summary="Start an agent work session")
async def start_session(vault: str, agent_id: str, context: str | None = None, user: AuthenticatedUser = Depends(get_current_user)):
    return await session_service.start_session(vault, agent_id, context)


@router.post("/sessions/{session_id}/end", summary="End a session")
async def end_session(session_id: str, summary: str | None = None, user: AuthenticatedUser = Depends(get_current_user)):
    return await session_service.end_session(session_id, summary, user_id=user.user_id)


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

    if author:
        entries = [
            e for e in entries
            if author.lower() in e.get("agent", "").lower() or author.lower() in e.get("author", "").lower()
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
            rows = await conn.fetch(
                """
                SELECT d.id, d.title, d.path, d.doc_type, d.current_commit,
                       d.updated_at, v.name AS vault_name, d.metadata
                FROM documents d
                JOIN vaults v ON d.vault_id = v.id
                LEFT JOIN vault_access va
                    ON va.vault_id = v.id AND va.user_id = $1
                WHERE v.owner_id = $1 OR va.user_id = $1
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
