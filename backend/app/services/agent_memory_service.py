"""Agent memory service — per-user memory vault + per-session collections.

Replaces the legacy `memory_service` + `session_service` pair that was
removed in v0.4.0. The new model layers cleanly on top of the existing
vault / collection / document primitives:

  - **One memory vault per user**, named ``agent-memory-{username}``,
    auto-provisioned on first plugin call. Owned by the user, no other
    grants — naturally owner-only.
  - **One collection per agent session**, at
    ``sessions/{YYYY-MM-DD}/{agent_id}/{session_id}``. The composite
    ``(agent_id, session_id)`` key avoids collisions when the same user
    runs Claude Code, Cursor, and Codex concurrently (the three agents
    pick session ids independently — Cursor's `conversation_id` can
    structurally collide with Codex's `session_id`).
  - **One ``recap.md`` per session collection**, written by ``end_session``
    with ``type: session`` frontmatter. The recap is just an AKB
    document, so it gets git-versioned, chunked, indexed, and edge-
    extracted alongside everything else.
  - **Idempotent on session_id-in-path.** Claude Code's SessionStart
    hook fires again with ``source: resume|clear|compact`` for the
    same session_id; ``start_session`` returns the existing collection
    in that case rather than creating a duplicate.

The plugin-side contract is documented in
`product/akb/design-proposals/akb-agent-memory-claude-code-plugin-…`.
"""

from __future__ import annotations

import logging
import re
import unicodedata
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone

from app.db.postgres import get_pool
from app.exceptions import ConflictError, NotFoundError, ValidationError
from app.models.document import DocumentPutRequest
from app.services.document_service import DocumentService

logger = logging.getLogger("akb.agent_memory")

# ── Constants ────────────────────────────────────────────────

#: Vault name template. ``{username_safe}`` is the sanitised username —
#: see ``sanitise_username``.
MEMORY_VAULT_TEMPLATE = "agent-memory-{username_safe}"

#: Top-level collections inside a memory vault. Folders, not enforced
#: schema — the plugin / agent can write anywhere, these are the
#: conventional read+write locations.
TOP_COLLECTIONS = ("preferences", "learnings", "context", "general", "sessions")

#: Cap on injected content per memory in the recall response. Keeps
#: the prompt-injection payload bounded so plugins don't blow past
#: model context windows on a busy vault.
RECALL_CONTENT_CAP_BYTES = 4096

#: Default per-scope limit on /context recall.
DEFAULT_RECALL_LIMIT = 5

#: Session id length cap. session_id is agent-supplied (Cursor's
#: conversation_id, Claude Code session uuid, Codex session_id) — none
#: of them shipped a length limit, so we set a generous one.
SESSION_ID_MAX_LEN = 200

#: agent_id length cap — kebab-case identifier of the harness type
#: (``claude-code``, ``cursor``, ``codex``, …).
AGENT_ID_MAX_LEN = 40

#: SessionStart `source` enum — Claude Code 1.0.85+ and Codex April 2026
#: both fire SessionStart on resume/clear/compact with one of these
#: causes. Recorded for analytics; behaviour is identical (idempotent).
SOURCE_VALUES = ("startup", "resume", "clear", "compact", "first_use")

#: SessionEnd `reason` enum — Cursor sessionEnd's reason verbatim, plus
#: ``stop`` to cover Claude Code's Stop hook semantics.
REASON_VALUES = (
    "completed", "aborted", "error",
    "window_close", "user_close", "stop",
)

#: SessionEnd `outcome` enum — agent-level summary of whether the
#: session achieved its goal. Independent of `reason` (a session can
#: end with reason=window_close + outcome=success if the work was
#: already done).
OUTCOME_VALUES = ("success", "partial", "abandoned")


# ── Models ───────────────────────────────────────────────────


@dataclass
class StartBody:
    agent_id: str
    source: str = "startup"
    transcript_path: str | None = None
    cwd: str | None = None
    workspace_roots: list[str] | None = None
    model: str | None = None
    permission_mode: str | None = None
    goal: str | None = None
    parent_session_id: str | None = None
    extras: dict | None = None


@dataclass
class EndBody:
    reason: str
    summary: str = ""
    outcome: str = "success"
    touched_uris: list[str] | None = None
    decisions: list[str] | None = None
    next_actions: list[str] | None = None
    duration_seconds: int | None = None
    metrics: dict | None = None
    error_message: str | None = None


@dataclass
class SnapshotBody:
    partial_summary: str
    progress: dict | None = None
    cause: str = "manual"   # "pre_compact" | "manual"


# ── Sanitisation ─────────────────────────────────────────────


_SAFE_RE = re.compile(r"[^a-z0-9._-]+")
_DASH_COLLAPSE_RE = re.compile(r"-+")


def sanitise_username(raw: str) -> str:
    """Map an arbitrary username to a vault-name-safe slug.

    Vault names must match ``^[a-z0-9][a-z0-9-]*$`` (validated by
    ``DocumentService.create_vault``). Usernames in the user catalogue
    are not constrained that tightly — they may carry dots, underscores,
    capitals, Unicode. Slugify here and cap at 60 chars so the final
    vault name ``agent-memory-{slug}`` stays under PG's 63-byte
    role-name budget downstream (PG roles `akb_user_<vault_id>` use the
    uuid; the vault name itself only bounds the on-disk git path).
    """
    if not raw:
        raise ValidationError("username is required to derive a memory vault name")
    s = unicodedata.normalize("NFKD", raw).encode("ascii", "ignore").decode("ascii")
    s = s.lower().strip()
    s = _SAFE_RE.sub("-", s)
    s = _DASH_COLLAPSE_RE.sub("-", s).strip("-.")
    s = s[:60]
    if not s:
        raise ValidationError(f"username cannot be safely slugified: {raw!r}")
    return s


def sanitise_agent_id(raw: str) -> str:
    """Normalise an agent harness identifier to kebab-case ASCII.

    The convergent identifier finding from the 2026 hook-spec audit is
    that ``agent_id`` is free-form across harnesses (``claude-code``,
    ``cursor``, ``codex``, ``aider``, …) — we accept any humanist label
    and normalise. Empty after normalisation is rejected so collisions
    on the empty string can't manufacture a shared collection path
    across agents.
    """
    if not raw:
        raise ValidationError("agent_id is required")
    s = unicodedata.normalize("NFKD", raw).encode("ascii", "ignore").decode("ascii")
    s = s.lower().strip()
    s = _SAFE_RE.sub("-", s)
    s = _DASH_COLLAPSE_RE.sub("-", s).strip("-.")
    s = s[:AGENT_ID_MAX_LEN]
    if not s:
        raise ValidationError(f"agent_id cannot be safely slugified: {raw!r}")
    return s


def sanitise_session_id(raw: str) -> str:
    """Normalise an agent-supplied session id to a path-safe slug.

    Three coding-agent reference points:
      * Claude Code emits a uuidv4 — ASCII alphanumerics + dashes.
      * Codex emits a uuid in the same shape.
      * Cursor emits ``conversation_id`` of unspecified format; the
        spec only guarantees stability across turns. We assume it is
        printable text.

    The slug is folder-name-safe and shorter than 120 chars so the
    final document path stays inside conventional filesystem limits
    when stacked with date + agent_id + ``recap.md``.
    """
    if not raw:
        raise ValidationError("session_id is required")
    s = unicodedata.normalize("NFKD", raw).encode("ascii", "ignore").decode("ascii")
    s = s.lower().strip()
    s = _SAFE_RE.sub("-", s)
    s = _DASH_COLLAPSE_RE.sub("-", s).strip("-.")
    s = s[:SESSION_ID_MAX_LEN]
    if not s:
        raise ValidationError(f"session_id cannot be safely slugified: {raw!r}")
    return s


def session_collection_path(date_str: str, agent_id_safe: str, session_id_safe: str) -> str:
    return f"sessions/{date_str}/{agent_id_safe}/{session_id_safe}"


def memory_vault_name(username: str) -> str:
    return MEMORY_VAULT_TEMPLATE.format(username_safe=sanitise_username(username))


# ── Service ──────────────────────────────────────────────────


class AgentMemoryService:
    """Service for agent dedicated memory operations.

    The service owns no state; every method takes the authenticated
    user and operates against PG + the document service.
    """

    def __init__(self, doc_service: DocumentService | None = None):
        self.doc_service = doc_service or DocumentService()

    # ── Vault provisioning ─────────────────────────────────

    async def ensure_memory_vault(self, user_id: str, username: str) -> dict:
        """Return the user's memory vault, creating it if missing.

        Idempotent — repeated calls return the existing vault. The
        caller is the owner; no grants are added, so the vault is
        owner-only by default (mirrors AKB's RBAC convention for
        private vaults).
        """
        name = memory_vault_name(username)
        pool = await get_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow("SELECT id FROM vaults WHERE name = $1", name)
        if row:
            return {"name": name, "vault_id": str(row["id"]), "created": False}

        try:
            vault_id = await self.doc_service.create_vault(
                name=name,
                description=(
                    f"Agent dedicated memory for {username}. "
                    "Auto-provisioned by the AKB lifecycle plugin. "
                    "Owner-only — no shared access."
                ),
                owner_id=user_id,
                public_access="none",
            )
        except ConflictError:
            # Race: another concurrent start_session call provisioned
            # it between the SELECT and the create call. Re-read.
            async with pool.acquire() as conn:
                row = await conn.fetchrow("SELECT id FROM vaults WHERE name = $1", name)
            if not row:
                raise
            return {"name": name, "vault_id": str(row["id"]), "created": False}

        logger.info(
            "Auto-provisioned memory vault %s for user %s (%s)",
            name, username, user_id[:8],
        )
        return {"name": name, "vault_id": str(vault_id), "created": True}

    # ── Session lifecycle ──────────────────────────────────

    async def start_session(
        self,
        user_id: str,
        username: str,
        session_id: str,
        body: StartBody,
    ) -> dict:
        """Idempotently start (or re-attach to) an agent session.

        Returns the canonical collection URI plus a small block of
        injected context (preferences + learnings + parent recap)
        suitable for the plugin to fold into the agent's system or
        opening user prompt.

        Re-calls with the same ``session_id`` return the existing
        collection — Claude Code/Codex/Cursor all fire SessionStart on
        resume with the same id, and the plugin is not expected to
        suppress those re-firings client-side.
        """
        if body.source not in SOURCE_VALUES:
            raise ValidationError(
                f"source must be one of {SOURCE_VALUES}, got {body.source!r}"
            )

        agent_safe = sanitise_agent_id(body.agent_id)
        sid_safe = sanitise_session_id(session_id)
        vault_info = await self.ensure_memory_vault(user_id, username)
        memory_vault = vault_info["name"]

        # Discover or create the session collection. The path is
        # rooted at today (in UTC); a resume/compact later in the same
        # session keeps the original date — we look up by (agent, sid)
        # alone first, then create with today's date only if absent.
        existing = await self._find_session_collection(
            memory_vault, agent_safe, sid_safe,
        )
        if existing:
            is_new = False
            coll_path = existing["path"]
            started_at = existing["created_at"]
        else:
            is_new = True
            today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
            coll_path = session_collection_path(today, agent_safe, sid_safe)
            await self._create_session_collection(
                memory_vault, coll_path, body, session_id, agent_safe,
            )
            started_at = datetime.now(timezone.utc)

        injected = await self._build_injected_context(
            memory_vault, body.parent_session_id, limit=DEFAULT_RECALL_LIMIT,
        )

        collection_uri = f"akb://{memory_vault}/coll/{coll_path}"
        return {
            "session_id": session_id,
            "session_id_safe": sid_safe,
            "agent_id": agent_safe,
            "memory_vault": memory_vault,
            "collection_uri": collection_uri,
            "collection_path": coll_path,
            "started_at": started_at.isoformat() if started_at else None,
            "is_new": is_new,
            "source": body.source,
            "injected_context": injected,
        }

    async def end_session(
        self,
        user_id: str,
        username: str,
        session_id: str,
        body: EndBody,
    ) -> dict:
        """Write the session recap and close the bracket.

        The recap is an ordinary AKB document (``type: session``) so it
        gets git-versioned, chunked, indexed, and exposed via the same
        search/browse/get tools an agent already uses. The
        ``touched_uris`` list lands in frontmatter ``depends_on`` so the
        kg layer wires up provenance edges.
        """
        if body.reason not in REASON_VALUES:
            raise ValidationError(
                f"reason must be one of {REASON_VALUES}, got {body.reason!r}"
            )
        if body.outcome not in OUTCOME_VALUES:
            raise ValidationError(
                f"outcome must be one of {OUTCOME_VALUES}, got {body.outcome!r}"
            )

        sid_safe = sanitise_session_id(session_id)
        memory_vault = memory_vault_name(username)
        existing = await self._find_session_collection_by_sid(memory_vault, sid_safe)
        if not existing:
            raise NotFoundError("agent session", session_id)
        coll_path = existing["path"]

        content = self._render_recap(body)
        tags = _recap_tags(existing["agent_id"], body)
        req = DocumentPutRequest(
            vault=memory_vault,
            collection=coll_path,
            title=f"Session recap — {existing['agent_id']} {sid_safe[:12]}",
            content=content,
            type="session",
            tags=tags,
            domain="agent-memory",
            summary=(body.summary or "").splitlines()[0][:200] if body.summary else None,
            depends_on=list(body.touched_uris or []),
            slug="recap",
        )
        try:
            resp = await self.doc_service.put(req, agent_id=existing["agent_id"])
        except ConflictError:
            # recap.md already written (idempotent end). Resolve the
            # existing doc and return its URI rather than 409 the
            # plugin.
            resp = None

        ended_at = datetime.now(timezone.utc)
        duration = body.duration_seconds
        if duration is None and existing.get("created_at"):
            delta = ended_at - existing["created_at"]
            duration = int(delta.total_seconds())

        recap_uri = (resp.uri if resp else
                     f"akb://{memory_vault}/coll/{coll_path}/doc/recap.md")
        return {
            "session_id": session_id,
            "session_id_safe": sid_safe,
            "memory_vault": memory_vault,
            "collection_uri": f"akb://{memory_vault}/coll/{coll_path}",
            "recap_uri": recap_uri,
            "ended_at": ended_at.isoformat(),
            "duration_seconds": duration,
            "reason": body.reason,
            "outcome": body.outcome,
        }

    async def snapshot_session(
        self,
        user_id: str,
        username: str,
        session_id: str,
        body: SnapshotBody,
    ) -> dict:
        """Persist an in-flight partial summary (PreCompact safety net).

        Writes a `snapshot-{N}.md` document inside the session
        collection rather than mutating collection metadata — that
        keeps every snapshot durable and git-versioned, and avoids the
        need for a collection-update API that AKB does not yet expose.
        """
        sid_safe = sanitise_session_id(session_id)
        memory_vault = memory_vault_name(username)
        existing = await self._find_session_collection_by_sid(memory_vault, sid_safe)
        if not existing:
            raise NotFoundError("agent session", session_id)
        coll_path = existing["path"]

        now = datetime.now(timezone.utc)
        n = await self._count_snapshots(memory_vault, coll_path)
        slug = f"snapshot-{n + 1:03d}"
        cause = body.cause or "manual"

        progress_lines = []
        if body.progress:
            for k, v in body.progress.items():
                progress_lines.append(f"- **{k}**: {v}")
        content = (
            f"# {slug.replace('-', ' ').title()}\n\n"
            f"Captured at {now.isoformat()} (cause: {cause}).\n\n"
            f"## Partial summary\n\n{body.partial_summary.strip()}\n"
            + ("\n## Progress\n\n" + "\n".join(progress_lines) + "\n" if progress_lines else "")
        )

        req = DocumentPutRequest(
            vault=memory_vault,
            collection=coll_path,
            title=f"Snapshot {n + 1} — {sid_safe[:12]}",
            content=content,
            type="session",
            tags=["snapshot", f"cause:{cause}"],
            domain="agent-memory",
            slug=slug,
        )
        resp = await self.doc_service.put(req, agent_id=existing["agent_id"])
        return {
            "session_id": session_id,
            "snapshot_uri": resp.uri,
            "snapshot_at": now.isoformat(),
            "sequence": n + 1,
        }

    async def get_context(
        self,
        user_id: str,
        username: str,
        session_id: str,
        query: str | None,
        scopes: list[str] | None,
        limit: int,
    ) -> dict:
        """Recall preferences/learnings/parent-recap for prompt injection.

        Called from UserPromptSubmit (or equivalent) — synchronous by
        contract because the response is concatenated into the model
        context. Pagination here is a hard cap, not an offset pattern;
        plugins consume the top-N and never paginate further.
        """
        sanitise_session_id(session_id)  # validate shape (raises on bad id); result unused here
        memory_vault = memory_vault_name(username)

        if not scopes:
            scopes = ["preferences", "learnings"]
        invalid = [s for s in scopes if s not in TOP_COLLECTIONS]
        if invalid:
            raise ValidationError(f"unknown context scope(s): {invalid}")

        out: dict = {
            "session_id": session_id,
            "memory_vault": memory_vault,
            "scopes": scopes,
        }
        for scope in scopes:
            out[scope] = await self._fetch_scope(memory_vault, scope, query, limit)
        return out

    async def get_session_status(
        self,
        user_id: str,
        username: str,
        session_id: str,
    ) -> dict:
        sid_safe = sanitise_session_id(session_id)
        memory_vault = memory_vault_name(username)
        existing = await self._find_session_collection_by_sid(memory_vault, sid_safe)
        if not existing:
            raise NotFoundError("agent session", session_id)
        recap = await self._fetch_recap_summary(memory_vault, existing["path"])
        return {
            "session_id": session_id,
            "session_id_safe": sid_safe,
            "memory_vault": memory_vault,
            "agent_id": existing["agent_id"],
            "collection_uri": f"akb://{memory_vault}/coll/{existing['path']}",
            "started_at": existing["created_at"].isoformat() if existing.get("created_at") else None,
            "ended": bool(recap),
            "recap": recap,
        }

    async def list_sessions(
        self,
        user_id: str,
        username: str,
        agent_id: str | None,
        limit: int,
        offset: int,
    ) -> dict:
        memory_vault = memory_vault_name(username)
        pool = await get_pool()
        clauses = ["v.name = $1", "c.path LIKE 'sessions/%'"]
        args: list = [memory_vault]
        if agent_id:
            clauses.append(f"c.path LIKE ${len(args) + 1}")
            args.append(f"sessions/%/{sanitise_agent_id(agent_id)}/%")
        where = " AND ".join(clauses)
        async with pool.acquire() as conn:
            total = await conn.fetchval(
                f"SELECT COUNT(*) FROM collections c JOIN vaults v ON c.vault_id = v.id WHERE {where}",
                *args,
            )
            rows = await conn.fetch(
                f"""
                SELECT c.path, c.last_updated AS created_at, c.summary, v.name AS vault_name
                  FROM collections c JOIN vaults v ON c.vault_id = v.id
                 WHERE {where}
                 ORDER BY c.last_updated DESC
                 LIMIT ${len(args) + 1} OFFSET ${len(args) + 2}
                """,
                *args, limit, offset,
            )
        sessions = []
        for r in rows:
            parts = r["path"].split("/")
            # Expected shape: sessions/{date}/{agent}/{sid}
            sessions.append({
                "collection_uri": f"akb://{r['vault_name']}/coll/{r['path']}",
                "collection_path": r["path"],
                "date": parts[1] if len(parts) > 1 else None,
                "agent_id": parts[2] if len(parts) > 2 else None,
                "session_id_safe": parts[3] if len(parts) > 3 else None,
                "started_at": r["created_at"].isoformat() if r["created_at"] else None,
                "summary": r["summary"],
            })
        return {
            "sessions": sessions,
            "returned": len(sessions),
            "total": int(total or 0),
            "truncated": int(total or 0) > len(sessions),
        }

    # ── Internal helpers ───────────────────────────────────

    async def _create_session_collection(
        self,
        vault_name: str,
        coll_path: str,
        body: StartBody,
        original_session_id: str,
        agent_id_safe: str,
    ) -> None:
        pool = await get_pool()
        async with pool.acquire() as conn:
            v = await conn.fetchrow("SELECT id FROM vaults WHERE name = $1", vault_name)
            if not v:
                raise NotFoundError("Vault", vault_name)

            # Compose a single-line collection summary that's machine
            # parseable on resume (the plugin pulls it out of
            # akb_browse responses). Keep under ~500 chars to fit
            # PG's TOAST inline boundary.
            summary_parts = [f"agent={agent_id_safe}", f"sid={original_session_id}"]
            if body.source:
                summary_parts.append(f"source={body.source}")
            if body.goal:
                summary_parts.append(f"goal={body.goal[:120]}")
            if body.cwd:
                summary_parts.append(f"cwd={body.cwd}")
            if body.model:
                summary_parts.append(f"model={body.model}")
            if body.parent_session_id:
                summary_parts.append(f"parent={body.parent_session_id}")
            summary = " | ".join(summary_parts)[:500]

            # `collections` rows are flat by path within a vault — the
            # repo enforces uniqueness on (vault_id, path). `name` is
            # the path basename; `last_updated` doubles as creation
            # time on first write.
            name = coll_path.rsplit("/", 1)[-1]
            await conn.execute(
                """
                INSERT INTO collections (id, vault_id, path, name, summary, last_updated)
                VALUES ($1, $2, $3, $4, $5, NOW())
                ON CONFLICT (vault_id, path) DO NOTHING
                """,
                uuid.uuid4(), v["id"], coll_path, name, summary,
            )

    async def _find_session_collection(
        self,
        vault_name: str,
        agent_id_safe: str,
        sid_safe: str,
    ) -> dict | None:
        pool = await get_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT c.path, c.last_updated AS created_at, c.summary
                  FROM collections c JOIN vaults v ON c.vault_id = v.id
                 WHERE v.name = $1
                   AND c.path LIKE $2
                """,
                vault_name, f"sessions/%/{agent_id_safe}/{sid_safe}",
            )
        return dict(row) if row else None

    async def _find_session_collection_by_sid(
        self,
        vault_name: str,
        sid_safe: str,
    ) -> dict | None:
        """Find a session collection knowing only the session id.

        The agent_id segment is implicitly recovered from the matched
        path. Used by end/snapshot/status where the plugin replays the
        same session_id from the SessionStart payload.
        """
        pool = await get_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT c.path, c.last_updated AS created_at, c.summary
                  FROM collections c JOIN vaults v ON c.vault_id = v.id
                 WHERE v.name = $1
                   AND c.path LIKE $2
                 ORDER BY c.last_updated DESC
                 LIMIT 1
                """,
                vault_name, f"sessions/%/%/{sid_safe}",
            )
        if not row:
            return None
        parts = row["path"].split("/")
        agent_id = parts[2] if len(parts) > 2 else "unknown"
        return {**dict(row), "agent_id": agent_id}

    async def _count_snapshots(self, vault_name: str, coll_path: str) -> int:
        pool = await get_pool()
        async with pool.acquire() as conn:
            n = await conn.fetchval(
                """
                SELECT COUNT(*) FROM documents d JOIN vaults v ON d.vault_id = v.id
                 WHERE v.name = $1
                   AND d.path LIKE $2
                """,
                vault_name, f"{coll_path}/snapshot-%.md",
            )
        return int(n or 0)

    async def _build_injected_context(
        self,
        vault_name: str,
        parent_session_id: str | None,
        *,
        limit: int,
    ) -> dict:
        out: dict = {
            "preferences": await self._fetch_scope(vault_name, "preferences", None, limit),
            "learnings": await self._fetch_scope(vault_name, "learnings", None, limit),
        }
        if parent_session_id:
            try:
                parent_safe = sanitise_session_id(parent_session_id)
            except ValidationError:
                parent_safe = None
            if parent_safe:
                existing = await self._find_session_collection_by_sid(vault_name, parent_safe)
                if existing:
                    recap = await self._fetch_recap_summary(vault_name, existing["path"])
                    if recap:
                        out["parent_recap"] = recap
        return out

    async def _fetch_scope(
        self,
        vault_name: str,
        scope: str,
        query: str | None,
        limit: int,
    ) -> list[dict]:
        """Fetch top-N docs under a memory scope.

        Without `query`, returns the most recently updated docs under
        the scope folder. With `query`, falls back to the same recency
        list — semantic search wiring inside scope-restricted vaults
        is a follow-up (uses ``search_service.search`` with
        ``vault=vault_name`` + collection filter).
        """
        pool = await get_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT d.id, d.title, d.path, d.summary, d.updated_at,
                       d.current_commit, d.doc_type, d.tags
                  FROM documents d JOIN vaults v ON d.vault_id = v.id
                 WHERE v.name = $1
                   AND d.path LIKE $2
                 ORDER BY d.updated_at DESC
                 LIMIT $3
                """,
                vault_name, f"{scope}/%", limit,
            )
        out = []
        for r in rows:
            out.append({
                "uri": f"akb://{vault_name}/coll/{_parent_path(r['path'])}/doc/{_basename(r['path'])}",
                "title": r["title"],
                "summary": r["summary"],
                "type": r["doc_type"],
                "tags": list(r["tags"] or []),
                "updated_at": r["updated_at"].isoformat() if r["updated_at"] else None,
            })
        return out

    async def _fetch_recap_summary(self, vault_name: str, coll_path: str) -> dict | None:
        pool = await get_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT d.id, d.title, d.path, d.summary, d.tags, d.updated_at, d.current_commit
                  FROM documents d JOIN vaults v ON d.vault_id = v.id
                 WHERE v.name = $1
                   AND d.path = $2
                """,
                vault_name, f"{coll_path}/recap.md",
            )
        if not row:
            return None
        return {
            "uri": f"akb://{vault_name}/coll/{coll_path}/doc/recap.md",
            "title": row["title"],
            "summary": row["summary"],
            "tags": list(row["tags"] or []),
            "ended_at": row["updated_at"].isoformat() if row["updated_at"] else None,
        }

    # ── Rendering ──────────────────────────────────────────

    def _render_recap(self, body: EndBody) -> str:
        body_md = (body.summary or "").strip()
        if not body_md:
            body_md = "_No summary provided._"

        sections: list[str] = [f"# Session recap\n\n{body_md}\n"]
        if body.decisions:
            sections.append(
                "## Decisions\n\n"
                + "\n".join(f"- {d}" for d in body.decisions)
                + "\n"
            )
        if body.next_actions:
            sections.append(
                "## Next actions\n\n"
                + "\n".join(f"- {a}" for a in body.next_actions)
                + "\n"
            )
        if body.touched_uris:
            sections.append(
                "## Touched documents\n\n"
                + "\n".join(f"- {u}" for u in body.touched_uris)
                + "\n"
            )
        meta_lines = [
            f"- **outcome**: {body.outcome}",
            f"- **reason**: {body.reason}",
        ]
        if body.duration_seconds is not None:
            meta_lines.append(f"- **duration_seconds**: {body.duration_seconds}")
        if body.metrics:
            for k, v in body.metrics.items():
                meta_lines.append(f"- **{k}**: {v}")
        if body.error_message:
            meta_lines.append(f"- **error**: {body.error_message}")
        sections.append("## Metadata\n\n" + "\n".join(meta_lines) + "\n")

        return "\n".join(sections)


# ── Small utilities ──────────────────────────────────────────


def _parent_path(full_path: str) -> str:
    parts = full_path.rsplit("/", 1)
    return parts[0] if len(parts) > 1 else ""


def _basename(full_path: str) -> str:
    return full_path.rsplit("/", 1)[-1]


def _recap_tags(agent_id: str, body: EndBody) -> list[str]:
    tags = ["recap", f"agent:{agent_id}", f"outcome:{body.outcome}", f"reason:{body.reason}"]
    if body.next_actions:
        tags.append("has-next-actions")
    return tags
