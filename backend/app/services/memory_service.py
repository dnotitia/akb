"""Agent memory service — per-user persistent memory.

Two modes:
- Explicit: agent calls akb_remember/akb_recall/akb_forget
- Auto: session_end auto-summarizes work into memory
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone

from app.db.postgres import get_pool

logger = logging.getLogger("akb.memory")

VALID_CATEGORIES = {"context", "preference", "learning", "work", "general"}


async def remember(
    user_id: str,
    content: str,
    category: str = "general",
    source: str = "manual",
    session_id: str | None = None,
) -> dict:
    """Store a memory for the user."""
    if category not in VALID_CATEGORIES:
        category = "general"

    pool = await get_pool()
    uid = uuid.UUID(user_id)
    mid = uuid.uuid4()
    now = datetime.now(timezone.utc)

    async with pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO memories (id, user_id, category, content, source, session_id, created_at, updated_at)
            VALUES ($1, $2, $3, $4, $5, $6, $7, $7)
            """,
            mid, uid, category, content, source,
            uuid.UUID(session_id) if session_id else None,
            now,
        )

    logger.info("Memory stored for user %s: [%s] %s", user_id[:8], category, content[:50])
    return {
        "memory_id": str(mid),
        "category": category,
        "content": content,
        "stored": True,
    }


async def recall(
    user_id: str,
    category: str | None = None,
    limit: int = 20,
) -> list[dict]:
    """Retrieve memories for the user."""
    pool = await get_pool()
    uid = uuid.UUID(user_id)

    async with pool.acquire() as conn:
        if category:
            rows = await conn.fetch(
                """
                SELECT id, category, content, source, created_at, updated_at
                FROM memories
                WHERE user_id = $1 AND category = $2
                ORDER BY updated_at DESC
                LIMIT $3
                """,
                uid, category, limit,
            )
        else:
            rows = await conn.fetch(
                """
                SELECT id, category, content, source, created_at, updated_at
                FROM memories
                WHERE user_id = $1
                ORDER BY updated_at DESC
                LIMIT $2
                """,
                uid, limit,
            )

    return [
        {
            "memory_id": str(r["id"]),
            "category": r["category"],
            "content": r["content"],
            "source": r["source"],
            "created_at": r["created_at"].isoformat(),
            "updated_at": r["updated_at"].isoformat(),
        }
        for r in rows
    ]


async def forget(user_id: str, memory_id: str) -> bool:
    """Delete a specific memory."""
    pool = await get_pool()
    result = await pool.execute(
        "DELETE FROM memories WHERE id = $1 AND user_id = $2",
        uuid.UUID(memory_id), uuid.UUID(user_id),
    )
    return "DELETE 1" in result


async def forget_category(user_id: str, category: str) -> int:
    """Delete all memories in a category."""
    pool = await get_pool()
    result = await pool.execute(
        "DELETE FROM memories WHERE user_id = $1 AND category = $2",
        uuid.UUID(user_id), category,
    )
    # Extract count from "DELETE N"
    try:
        return int(result.split(" ")[1])
    except (IndexError, ValueError):
        return 0


async def auto_summarize_session(
    user_id: str,
    session_id: str,
    vault: str,
    doc_titles: list[str],
    summary: str | None,
) -> dict | None:
    """Auto-create a work memory from session end data."""
    if not doc_titles and not summary:
        return None

    parts = []
    if summary:
        parts.append(summary)
    if doc_titles:
        parts.append(f"Documents: {', '.join(doc_titles)}")
    parts.append(f"Vault: {vault}")

    content = " | ".join(parts)

    return await remember(
        user_id=user_id,
        content=content,
        category="work",
        source="session_auto",
        session_id=session_id,
    )
