"""Personal inbox: current ACL, serialized recipient versions, durable delivery."""
from __future__ import annotations

from app.config import settings
from app.exceptions import AKBError
from app.services.uri_service import doc_uri

# Shared by delivery, list, count, and subscription listing. Content from a
# revoked vault never contributes even to a badge. Account notices are redacted.
ACCESS = """EXISTS(SELECT 1 FROM vaults v JOIN users u ON u.id=$1
 WHERE v.id=n.vault_id AND (u.is_admin OR v.owner_id=u.id OR v.public_access<>'none'
 OR EXISTS(SELECT 1 FROM vault_access a WHERE a.vault_id=v.id AND a.user_id=u.id)))"""
def native_documents():
    from app.services.revision_backend import canonical_document_revision_backend, selected_document_revision_backend
    return (selected_document_revision_backend() or canonical_document_revision_backend(settings.document_revision_backend)) == "postgres_native"


def document_source():
    """A current-authority projection; never consult stale Legacy rows in Native."""
    if native_documents():
        return """(SELECT resource_id id, namespace_id vault_id,current_path path,
            regexp_replace(current_path,'^.*/','') title FROM native_resources
            WHERE surface='document' AND lifecycle='live')"""
    return "documents"


def visible():
    return f"(n.kind LIKE 'access.%' OR ({ACCESS} AND (n.kind='document.delete' OR EXISTS(SELECT 1 FROM {document_source()} d WHERE d.id=n.resource_id))))"
LABELS = {
    "document.update": "A watched document was updated",
    "document.move": "A watched document was moved",
    "document.archive": "A watched document was archived",
    "document.restore": "A watched document was restored",
    "document.delete": "A watched document was deleted",
    "access.granted": "Your vault access was granted",
    "access.changed": "Your vault access changed",
    "access.revoked": "Your vault access was removed",
    "access.membership_removed": "Your direct vault membership was removed",
    "access.ownership": "Your vault ownership changed",
}


def boundary(value: str) -> int:
    try:
        number = int(value)
        if not 0 <= number <= 9223372036854775807:
            raise ValueError
        return number
    except (ValueError, TypeError):
        raise AKBError("Invalid notification version", status_code=400) from None


async def lock_inbox(conn, uid):
    await conn.execute("INSERT INTO notification_inboxes(user_id) VALUES($1) ON CONFLICT DO NOTHING", uid)
    return await conn.fetchval("SELECT version FROM notification_inboxes WHERE user_id=$1 FOR UPDATE", uid)


async def process_work(conn, work):
    """Called in the same transaction as completion; replay cannot re-open a group."""
    if work["kind"] not in LABELS:
        raise ValueError("Unsupported notification kind")
    recipients = sorted(work["recipient_ids"])
    for uid in recipients:
        if work["kind"].startswith("document.") and str(uid) == work["actor_id"]:
            continue
        if not await conn.fetchval("SELECT EXISTS(SELECT 1 FROM users WHERE id=$1 AND account_kind='human')", uid):
            continue
        if work["kind"].startswith("document."):
            if not await conn.fetchval("""SELECT EXISTS(SELECT 1 FROM notification_subscriptions
                WHERE user_id=$1 AND resource_id=$2 AND created_at<=$3)""",
                uid, work["resource_id"], work["created_at"]):
                continue
            allowed = await conn.fetchval(f"SELECT {ACCESS} FROM (SELECT $2::uuid vault_id) n", uid, work["vault_id"])
            if not allowed:
                continue
        await lock_inbox(conn, uid)
        inserted = await conn.fetchval("""INSERT INTO notification_deliveries(source_key,user_id) VALUES($1,$2)
            ON CONFLICT DO NOTHING RETURNING user_id""", work["source_key"], uid)
        if not inserted:
            continue
        version = await conn.fetchval("UPDATE notification_inboxes SET version=version+1 WHERE user_id=$1 RETURNING version", uid)
        # Event-time buckets prevent delayed/retried work extending a group forever.
        group = work["source_key"]
        if work["kind"] == "document.update":
            bucket = int(work["created_at"].timestamp()) // 300
            group = f"document.update:{work['resource_id']}:{bucket}"
        await conn.execute("""INSERT INTO user_notifications(user_id,kind,vault_id,resource_id,group_key,version)
            VALUES($1,$2,$3,$4,$5,$6) ON CONFLICT(user_id,group_key) DO UPDATE
            SET updated_at=now(),version=EXCLUDED.version""", uid, work["kind"], work["vault_id"], work["resource_id"], group, version)
    await conn.execute("UPDATE notification_work SET processed_at=now(),lease_until=NULL WHERE id=$1", work["id"])


async def cleanup(conn):
    days = settings.notification_retention_days
    await conn.execute("DELETE FROM user_notifications WHERE updated_at < now()-make_interval(days => $1)", days)
    # Pending and failed jobs remain observable. Delivery keys outlive retained
    # completed jobs, so cleanup can never permit a queued replay to duplicate.
    await conn.execute("DELETE FROM notification_work WHERE processed_at < now()-make_interval(days => $1)", days)
    await conn.execute("""DELETE FROM notification_deliveries d WHERE created_at < now()-make_interval(days => $1)
        AND NOT EXISTS(SELECT 1 FROM notification_work w WHERE w.source_key=d.source_key)""", days)
    # Native tombstones retain their identity and can be restored. Keep their
    # watches, while the live-only projection still hides them from inbox reads.
    subscription_source = ("(SELECT resource_id id FROM native_resources WHERE surface='document' "
                           "AND lifecycle IN ('live','deleted'))" if native_documents() else "documents")
    await conn.execute(f"""DELETE FROM notification_subscriptions s WHERE NOT EXISTS(SELECT 1 FROM {subscription_source} d WHERE d.id=s.resource_id)
        AND NOT EXISTS(SELECT 1 FROM notification_work w WHERE w.resource_id=s.resource_id AND w.processed_at IS NULL)""")


async def metadata(conn, uid):
    version = await conn.fetchval("SELECT version FROM notification_inboxes WHERE user_id=$1", uid) or 0
    count = await conn.fetchval(f"SELECT count(*) FROM user_notifications n WHERE n.user_id=$1 AND n.read_version<n.version AND {visible()}", uid)
    return {"supported": True, "snapshot": str(version), "unread_count": count,
            "retention_days": settings.notification_retention_days}


async def list_notifications(conn, uid, state, cursor, limit, category="all"):
    if category not in {"all", "documents", "access"}:
        raise AKBError("Invalid notification category", status_code=400)
    meta = await metadata(conn, uid)
    rows = await conn.fetch(f"""SELECT n.*,v.name vault,d.path,d.title,({ACCESS}) allowed
       FROM user_notifications n LEFT JOIN vaults v ON v.id=n.vault_id
       LEFT JOIN {document_source()} d ON d.id=n.resource_id
       WHERE n.user_id=$1 AND {visible()} AND ($2::bigint IS NULL OR n.version<$2)
       AND ($3='all' OR n.read_version<n.version)
       AND ($5='all' OR ($5='documents' AND n.kind LIKE 'document.%')
            OR ($5='access' AND n.kind LIKE 'access.%'))
       ORDER BY n.version DESC LIMIT $4""",
       uid, boundary(cursor) if cursor else None, state, limit+1, category)
    items = []
    for row in rows[:limit]:
        target = None
        title = LABELS.get(row["kind"], "Your access changed")
        if row["allowed"] and row["vault"] and row["kind"].startswith("access.") and row["kind"] != "access.revoked":
            title = row["vault"]
            target = {"uri": f"akb://{row['vault']}", "vault": row["vault"]}
        # Deleted/revoked notices never retain titles or link to a dead target.
        if row["allowed"] and row["path"] and row["kind"] != "document.delete":
            title = row["title"] or title
            target = {"uri": doc_uri(row["vault"], row["path"]), "vault": row["vault"]}
        items.append({"id": str(row["id"]), "kind": row["kind"], "title": title,
            "message": LABELS.get(row["kind"], "Your access changed"),
            "created_at": row["created_at"].isoformat(), "updated_at": row["updated_at"].isoformat(),
            "version": str(row["version"]), "read": row["read_version"] >= row["version"], "target": target})
    return {**meta, "category": category, "items": items,
            "next_cursor": str(rows[limit-1]["version"]) if len(rows)>limit else None}
