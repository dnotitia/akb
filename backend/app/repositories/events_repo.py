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
    resource_uri: str | None = None,
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

    `resource_uri` is the canonical akb:// handle for the resource the
    event is about — same format MCP clients see. Use the doc_uri /
    table_uri / file_uri helpers from `app.services.uri_service` to
    build it. Pass None for events that don't reference an in-vault
    resource (user / vault / collection — collections aren't URI-
    addressable; their identity goes in `payload.path` if needed).

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
        INSERT INTO events (vault_id, kind, resource_uri, actor_id, payload)
        VALUES ($1, $2, $3, $4, $5::jsonb)
        RETURNING id
        """,
        vault_uuid, kind, resource_uri, actor_id, payload_json,
    )


async def get_vault_event_bounds(
    conn,
    vault_id: uuid.UUID | str,
) -> tuple[int | None, int | None]:
    """Return the retained ``events.id`` range for one Vault.

    The range deliberately ignores kind filters. A filtered consumer still
    advances across every retained event, emitting checkpoints for kinds it
    did not select, so retention-gap validation must use the complete Vault
    tail rather than only the selected subset.
    """
    row = await conn.fetchrow(
        """
        SELECT MIN(id) AS earliest_id, MAX(id) AS latest_id
          FROM events
         WHERE vault_id = $1
        """,
        _to_uuid_or_none(vault_id),
    )
    return (
        int(row["earliest_id"]) if row["earliest_id"] is not None else None,
        int(row["latest_id"]) if row["latest_id"] is not None else None,
    )


async def list_vault_events(
    conn,
    vault_id: uuid.UUID | str,
    *,
    after_id: int,
    limit: int = 100,
) -> list[dict[str, Any]]:
    """Read retained Vault events in their monotonic database order."""
    if limit < 1:
        raise ValueError("event batch limit must be positive")

    vault_uuid = _to_uuid_or_none(vault_id)
    rows = await conn.fetch(
        """
        SELECT id, occurred_at, vault_id, kind, resource_uri, actor_id, payload
          FROM events
         WHERE vault_id = $1
           AND id > $2
         ORDER BY id ASC
         LIMIT $3
        """,
        vault_uuid, after_id, limit,
    )
    return [dict(row) for row in rows]


def _to_uuid_or_none(v: uuid.UUID | str | None) -> uuid.UUID | None:
    if v is None:
        return None
    if isinstance(v, uuid.UUID):
        return v
    return uuid.UUID(str(v))
