"""Transactional, independent notification outbox. Never retain source content."""
from __future__ import annotations

import json
import uuid

from app.config import settings

KINDS = frozenset({"document.update", "document.move", "document.archive", "document.restore", "document.delete", "access.granted", "access.changed", "access.revoked", "access.membership_removed", "access.ownership"})


async def enqueue_notification_event(conn, kind, *, source_key: str, vault_id=None,
                                     resource_id=None, actor_id=None,
                                     recipient_ids=None, payload=None):
    """Call inside the domain transaction; rollback also rolls back this work."""
    if not settings.notifications_enabled:
        return
    if kind not in KINDS:
        raise ValueError("Unsupported notification kind")
    if not source_key or len(source_key) > 200:
        raise ValueError("Invalid notification source key")
    safe = {k: (str(v)[:32] if v is not None else None) for k, v in (payload or {}).items() if k in {"role", "previous_role"}}
    if kind.startswith("document."):
        rows = await conn.fetch("SELECT user_id FROM notification_subscriptions WHERE resource_id=$1 ORDER BY user_id",
                                uuid.UUID(str(resource_id)) if resource_id else None)
        recipient_ids = [row["user_id"] for row in rows]
    recipients = sorted({str(uuid.UUID(str(x))) for x in (recipient_ids or [])})
    if not recipients:
        return
    await conn.execute(
        """INSERT INTO notification_work(source_key,kind,vault_id,resource_id,actor_id,recipient_ids,payload,created_at)
           VALUES($1,$2,$3,$4,$5,$6::uuid[],$7::jsonb,clock_timestamp()) ON CONFLICT(source_key) DO NOTHING""",
        source_key, kind, uuid.UUID(str(vault_id)) if vault_id else None,
        uuid.UUID(str(resource_id)) if resource_id else None,
        str(actor_id)[:200] if actor_id else None, [uuid.UUID(x) for x in recipients], json.dumps(safe),
    )
