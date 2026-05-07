"""Session service — agent work session tracking.

Manages session lifecycle and recent changes queries.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

from app.db.postgres import get_pool


class SessionService:

    async def start_session(self, vault: str, agent_id: str, context: str | None = None) -> dict:
        """Start a new agent work session."""
        pool = await get_pool()
        async with pool.acquire() as conn:
            vault_row = await conn.fetchrow("SELECT id FROM vaults WHERE name = $1", vault)
            if not vault_row:
                return {"error": f"Vault not found: {vault}"}

            session_id = uuid.uuid4()
            now = datetime.now(timezone.utc)
            await conn.execute(
                """
                INSERT INTO sessions (id, vault_id, agent_id, started_at, context)
                VALUES ($1, $2, $3, $4, $5)
                """,
                session_id,
                vault_row["id"],
                agent_id,
                now,
                context,
            )

            return {
                "session_id": str(session_id),
                "vault": vault,
                "agent_id": agent_id,
                "started_at": now.isoformat(),
            }

    async def end_session(self, session_id: str, summary: str | None = None, user_id: str | None = None) -> dict:
        """End a session, record summary, and auto-store work memory."""
        pool = await get_pool()
        sid = uuid.UUID(session_id)
        now = datetime.now(timezone.utc)

        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT s.id, s.agent_id, s.started_at, s.ended_at, s.doc_ids, v.name as vault_name FROM sessions s JOIN vaults v ON s.vault_id = v.id WHERE s.id = $1",
                sid,
            )
            if not row:
                return {"error": "Session not found"}

            if row["ended_at"] is not None:
                return {
                    "error": "Session already ended",
                    "session_id": session_id,
                    "ended_at": row["ended_at"].isoformat(),
                }

            await conn.execute(
                "UPDATE sessions SET ended_at = $1, summary = $2 WHERE id = $3",
                now, summary, sid,
            )

            # Auto-store work memory
            if user_id and summary:
                from app.services.memory_service import auto_summarize_session
                doc_titles = []
                if row["doc_ids"]:
                    title_rows = await conn.fetch(
                        "SELECT title FROM documents WHERE id = ANY($1)",
                        row["doc_ids"],
                    )
                    doc_titles = [r["title"] for r in title_rows]

                await auto_summarize_session(
                    user_id=user_id,
                    session_id=session_id,
                    vault=row["vault_name"],
                    doc_titles=doc_titles,
                    summary=summary,
                )

            return {
                "session_id": session_id,
                "agent_id": row["agent_id"],
                "started_at": row["started_at"].isoformat(),
                "ended_at": now.isoformat(),
                "summary": summary,
            }

    async def add_doc_to_session(self, session_id: str, doc_id: str) -> None:
        """Record that a document was created/modified during this session."""
        pool = await get_pool()
        await pool.execute(
            """
            UPDATE sessions
            SET doc_ids = array_append(doc_ids, $1)
            WHERE id = $2 AND NOT ($1 = ANY(doc_ids))
            """,
            uuid.UUID(doc_id),
            uuid.UUID(session_id),
        )
