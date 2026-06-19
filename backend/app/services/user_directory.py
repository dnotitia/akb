"""Resolve actor identifiers to human display names.

Across git-derived surfaces (document history, vault activity, ``created_by``)
the stored actor token is sometimes the user's UUID (older rows and some
lifecycle ops) and sometimes the username — the normal document write path
sets ``GIT_AUTHOR_NAME`` to ``agent_id``, which is the user's ``username``
(see ``DocumentService.put``/``update``). A single batched lookup keyed by
**id OR username** resolves either form to ``display_name`` (falling back to
``username``), so every surface shows a real name instead of a raw token.

Tokens that match no user (e.g. external-git imports, which carry arbitrary
author strings) are simply absent from the result; callers show them as-is.
"""

from __future__ import annotations

from collections.abc import Iterable

from app.db.postgres import get_pool


async def resolve_display_names(tokens: Iterable[str | None]) -> dict[str, str]:
    """Map each actor token (user UUID or username) to a display name.

    Returns a dict keyed by **both** the matched ``id`` and ``username`` so a
    caller can look up by whichever token it holds. One batched query; falsy
    tokens are dropped, and tokens that match no user are omitted from the map.
    """
    keys = [t for t in set(tokens) if t]
    if not keys:
        return {}
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT id::text AS id, username,
                   COALESCE(display_name, username) AS name
              FROM users
             WHERE id::text = ANY($1) OR username = ANY($1)
            """,
            keys,
        )
    out: dict[str, str] = {}
    for r in rows:
        out[r["id"]] = r["name"]
        out[r["username"]] = r["name"]
    return out
