"""Repository for the `events` outbox.

`emit_event` is the only insertion point — every domain service that
wants to broadcast a change calls it from inside its own transaction so
the event lands atomically with the change. The publisher worker drains
the table separately (`delete_worker` pattern).

Why no class wrapper: each call is a one-shot INSERT; class state
(pool, etc.) buys nothing and the explicit `conn` arg makes the
transactional contract obvious at the call site.
"""

from __future__ import annotations

import json
import uuid
from typing import Any


async def emit_event(
    conn,
    kind: str,
    *,
    vault_id: uuid.UUID | str | None = None,
    ref_type: str | None = None,
    ref_id: str | None = None,
    actor_id: str | None = None,
    payload: dict[str, Any] | None = None,
) -> int:
    """Append one event to the outbox. MUST run inside the same
    transaction as the domain change so a rollback also drops the
    event — that's the only way to guarantee subscribers never see an
    event for a write that didn't actually land.

    `kind` is the canonical event name. Convention: `<resource>.<verb>`
    (e.g. `document.put`, `document.update`, `document.delete`,
    `vault.grant`, `publication.publish`). Keep it short and stable;
    subscribers will filter on it.

    `actor_id` is TEXT — mirrors `documents.created_by`. The MCP path
    passes a username, not a UUID, so we don't try to coerce.

    `payload` is a small JSON blob — keep it bounded. Don't dump the
    full document body here; subscribers can `akb_get` if they need
    content. Useful: title, path, doc_type, prior commit hash, etc.

    Returns the new event id.
    """
    vault_uuid = _to_uuid_or_none(vault_id)
    payload_json = json.dumps(payload or {})
    return await conn.fetchval(
        """
        INSERT INTO events (vault_id, kind, ref_type, ref_id, actor_id, payload)
        VALUES ($1, $2, $3, $4, $5, $6::jsonb)
        RETURNING id
        """,
        vault_uuid, kind, ref_type, ref_id, actor_id, payload_json,
    )


def _to_uuid_or_none(v: uuid.UUID | str | None) -> uuid.UUID | None:
    if v is None:
        return None
    if isinstance(v, uuid.UUID):
        return v
    return uuid.UUID(str(v))
