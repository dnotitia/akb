"""Small transactional adapters; never replay the historical event stream."""

from app.repositories.notifications_repo import enqueue_notification_event


def document_notification_kind(kind, previous_status=None, status=None):
    if kind == "document.update" and previous_status != status:
        if status == "archived":
            return "document.archive"
        if previous_status == "archived" and status is not None:
            return "document.restore"
    return kind


async def enqueue_document_change(
    conn, kind, *, source_key, vault_id, resource_id, actor_username=None,
    previous_status=None, status=None,
):
    # Document APIs explicitly supply usernames. A UUID-shaped username must
    # never be interpreted as a different account's primary key.
    actor_id = None
    if actor_username:
        actor_id = await conn.fetchval(
            "SELECT id FROM users WHERE username = $1", actor_username,
        )
    await enqueue_notification_event(
        conn, document_notification_kind(kind, previous_status, status),
        source_key=source_key, vault_id=vault_id, resource_id=resource_id,
        actor_id=str(actor_id) if actor_id else None,
    )


def access_notification(kind, payload):
    """Return a personal account fact, accounting for surviving access bases."""
    if kind == "access.transfer_ownership":
        before, after = payload.get("from_user_id"), payload.get("to_user_id")
        if before and after and before != after:
            return "access.ownership", [before, after], {}
        return None
    if not payload.get("applied") or not payload.get("target_user_id"):
        return None
    levels = {None: 0, "none": 0, "reader": 1, "writer": 2, "admin": 3, "owner": 4}
    public = payload.get("public_access")
    def effective(role):
        return max((role, public), key=lambda value: levels.get(value, 0)) or None
    before = effective(payload.get("previous_effective_role"))
    after = effective(payload.get("effective_role"))
    before = None if before == "none" else before
    after = None if after == "none" else after
    if payload.get("target_is_owner") or payload.get("target_is_admin"):
        before = after = "owner"
    if kind == "access.revoke" and payload.get("direct_removed"):
        notification_kind = "access.membership_removed" if after else "access.revoked"
    elif before == after:
        return None
    else:
        notification_kind = "access.revoked" if after is None else (
            "access.granted" if before is None else "access.changed"
        )
    return notification_kind, [payload["target_user_id"]], {"previous_role": before, "role": after}


async def enqueue_domain_event(conn, event_id, kind, *, vault_id, actor_id, payload):
    if kind in {"document.update", "document.move", "document.delete"}:
        if payload.get("resource_id"):
            await enqueue_document_change(
                conn, kind, source_key=f"event:{event_id}", vault_id=vault_id,
                resource_id=payload["resource_id"], actor_username=actor_id,
                previous_status=payload.get("previous_status"), status=payload.get("status"),
            )
    elif kind in {"access.grant", "access.revoke", "access.transfer_ownership"}:
        fact = access_notification(kind, payload)
        if fact:
            notification_kind, recipients, safe_payload = fact
            await enqueue_notification_event(
                conn, notification_kind, source_key=f"event:{event_id}",
                vault_id=vault_id, actor_id=actor_id,
                recipient_ids=recipients, payload=safe_payload,
            )
