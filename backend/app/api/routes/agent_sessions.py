"""Agent lifecycle endpoints.

The plugin (akb-claude-code, akb-cursor, akb-codex, …) calls these
endpoints from inside agent lifecycle hooks. The agent itself never
calls them — they are out of the tool-use loop on purpose, which is
why they are REST and not MCP.

API shape was derived from the 2026 cross-harness hook audit
(``deep-research`` workflow run 2026-06-02). Key invariants:

* **session_id lives in the path** — repeat starts (Claude Code's
  SessionStart with source=resume, Codex's compact replay, Cursor's
  re-attach) are naturally idempotent.
* **Fire-and-forget at the start/end boundary** — hooks cannot block
  the action they bracket, so the plugin does not depend on the
  response. We still return useful state so the plugin CAN consume
  it (injected context) when it's useful.
* **Synchronous recall** at ``GET /context`` — UserPromptSubmit-style
  hooks need the body to fold into the prompt before the model runs.
* **Bearer auth** — same PAT the agent uses for MCP, supplied to the
  hook via env var (``$AKB_PAT`` etc.) and interpolated by Claude
  Code / Cursor / Codex hook HTTP support.

The same surface is reused by every harness so the plugin packages
share the API client.
"""

from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from app.api.deps import get_current_user
from app.exceptions import NotFoundError, ValidationError
from app.services.agent_memory_service import (
    AGENT_ID_MAX_LEN,
    DEFAULT_RECALL_LIMIT,
    EndBody,
    OUTCOME_VALUES,
    REASON_VALUES,
    SESSION_ID_MAX_LEN,
    SOURCE_VALUES,
    SnapshotBody,
    StartBody,
    AgentMemoryService,
    sanitise_session_id,
)
from app.services.auth_service import AuthenticatedUser

router = APIRouter()
service = AgentMemoryService()


# ── Request / response models ────────────────────────────────


class StartRequest(BaseModel):
    agent_id: str = Field(
        ..., max_length=AGENT_ID_MAX_LEN,
        description=(
            "Agent harness identifier — `claude-code`, `cursor`, `codex`, "
            "`aider`, …. Free-form kebab-case; the server normalises."
        ),
    )
    source: Literal["startup", "resume", "clear", "compact", "first_use"] = "startup"
    transcript_path: str | None = None
    cwd: str | None = None
    workspace_roots: list[str] | None = None
    model: str | None = None
    permission_mode: str | None = None
    goal: str | None = None
    parent_session_id: str | None = None
    extras: dict | None = None


class EndRequest(BaseModel):
    reason: Literal[
        "completed", "aborted", "error",
        "window_close", "user_close", "stop",
    ]
    summary: str = ""
    outcome: Literal["success", "partial", "abandoned"] = "success"
    touched_uris: list[str] | None = None
    decisions: list[str] | None = None
    next_actions: list[str] | None = None
    duration_seconds: int | None = None
    metrics: dict | None = None
    error_message: str | None = None


class SnapshotRequest(BaseModel):
    partial_summary: str
    progress: dict | None = None
    cause: Literal["pre_compact", "manual"] = "manual"


# ── Routes ───────────────────────────────────────────────────


@router.post(
    "/agent-sessions/{session_id}",
    summary="Start (or re-attach to) an agent session",
)
async def start_session(
    session_id: str,
    req: StartRequest,
    user: AuthenticatedUser = Depends(get_current_user),
):
    """Idempotent on ``session_id``. Re-calls with the same id return
    the existing collection — the plugin should not deduplicate
    SessionStart events client-side."""
    _validate_session_id(session_id)
    body = StartBody(
        agent_id=req.agent_id,
        source=req.source,
        transcript_path=req.transcript_path,
        cwd=req.cwd,
        workspace_roots=req.workspace_roots,
        model=req.model,
        permission_mode=req.permission_mode,
        goal=req.goal,
        parent_session_id=req.parent_session_id,
        extras=req.extras,
    )
    try:
        return await service.start_session(
            user.user_id, user.username, session_id, body,
        )
    except ValidationError as e:
        raise HTTPException(status_code=422, detail=str(e))


@router.post(
    "/agent-sessions/{session_id}/end",
    summary="End the session and write its recap",
)
async def end_session(
    session_id: str,
    req: EndRequest,
    user: AuthenticatedUser = Depends(get_current_user),
):
    _validate_session_id(session_id)
    body = EndBody(
        reason=req.reason,
        summary=req.summary,
        outcome=req.outcome,
        touched_uris=req.touched_uris,
        decisions=req.decisions,
        next_actions=req.next_actions,
        duration_seconds=req.duration_seconds,
        metrics=req.metrics,
        error_message=req.error_message,
    )
    try:
        return await service.end_session(
            user.user_id, user.username, session_id, body,
        )
    except NotFoundError:
        raise HTTPException(status_code=404, detail="agent session not found")
    except ValidationError as e:
        raise HTTPException(status_code=422, detail=str(e))


@router.post(
    "/agent-sessions/{session_id}/snapshot",
    summary="Capture an in-flight partial summary (e.g. PreCompact)",
)
async def snapshot_session(
    session_id: str,
    req: SnapshotRequest,
    user: AuthenticatedUser = Depends(get_current_user),
):
    _validate_session_id(session_id)
    body = SnapshotBody(
        partial_summary=req.partial_summary,
        progress=req.progress,
        cause=req.cause,
    )
    try:
        return await service.snapshot_session(
            user.user_id, user.username, session_id, body,
        )
    except NotFoundError:
        raise HTTPException(status_code=404, detail="agent session not found")
    except ValidationError as e:
        raise HTTPException(status_code=422, detail=str(e))


@router.get(
    "/agent-sessions/{session_id}/context",
    summary="Recall preferences + learnings + parent recap for prompt injection",
)
async def session_context(
    session_id: str,
    query: str | None = Query(None, description="Optional semantic-search query"),
    scopes: str | None = Query(
        None,
        description=(
            "Comma-separated scope list. Defaults to `preferences,learnings`. "
            "Valid: preferences, learnings, context, general, sessions."
        ),
    ),
    limit: int = Query(DEFAULT_RECALL_LIMIT, ge=1, le=20),
    user: AuthenticatedUser = Depends(get_current_user),
):
    _validate_session_id(session_id)
    scope_list = [s.strip() for s in scopes.split(",")] if scopes else None
    try:
        return await service.get_context(
            user.user_id, user.username, session_id, query, scope_list, limit,
        )
    except ValidationError as e:
        raise HTTPException(status_code=422, detail=str(e))


@router.get(
    "/agent-sessions/{session_id}",
    summary="Session status (started / ended / recap pointer)",
)
async def session_status(
    session_id: str,
    user: AuthenticatedUser = Depends(get_current_user),
):
    _validate_session_id(session_id)
    try:
        return await service.get_session_status(
            user.user_id, user.username, session_id,
        )
    except NotFoundError:
        raise HTTPException(status_code=404, detail="agent session not found")


@router.get(
    "/agent-sessions",
    summary="List the caller's agent sessions (newest first)",
)
async def list_sessions(
    agent_id: str | None = Query(None),
    limit: int = Query(20, ge=1, le=100),
    offset: int = Query(0, ge=0),
    user: AuthenticatedUser = Depends(get_current_user),
):
    return await service.list_sessions(
        user.user_id, user.username, agent_id, limit, offset,
    )


# ── Helpers ──────────────────────────────────────────────────


def _validate_session_id(session_id: str) -> None:
    if not session_id or len(session_id) > SESSION_ID_MAX_LEN:
        raise HTTPException(
            status_code=422,
            detail=(
                f"session_id must be 1–{SESSION_ID_MAX_LEN} characters; "
                f"got length {len(session_id) if session_id else 0}"
            ),
        )
    # The sanitiser raises on un-slugifiable input — surface as 422 so
    # the plugin gets a clean schema error.
    try:
        sanitise_session_id(session_id)
    except ValidationError as e:
        raise HTTPException(status_code=422, detail=str(e))
