"""Human-session-only personal notifications and explicit document watches."""
import uuid
from datetime import datetime
from typing import Literal

from fastapi import APIRouter, Depends, Query, Response
from pydantic import BaseModel

from app.api.deps import get_current_user
from app.config import settings
from app.db.postgres import get_pool
from app.exceptions import AKBError
from app.repositories.document_repo import DocumentRepository
from app.services import notification_service as inbox
from app.services.access_service import check_vault_access
from app.services.auth_service import AuthenticatedUser
from app.services.uri_service import doc_uri, parse_uri

def private_response(response: Response):
    response.headers["Cache-Control"] = "private, no-store"


router = APIRouter(dependencies=[Depends(private_response)])


async def human_user(user: AuthenticatedUser = Depends(get_current_user)):
    if (user.auth_method not in {"jwt", "browser_session"} or user.account_kind != "human"
            or user.token_id is not None or user.key_class is not None
            or user.vault_scope is not None or user.token_scopes is not None):
        raise AKBError("Personal notifications require a human browser session", status_code=403, code="notifications_session_required")
    if not settings.notifications_enabled:
        raise AKBError("Personal notifications are unavailable", status_code=503, code="notifications_disabled")
    return user


class ReadChange(BaseModel):
    read: bool
    version: str


class ReadSnapshot(BaseModel):
    snapshot: str


class InboxMetadata(BaseModel):
    supported: bool
    snapshot: str
    unread_count: int
    retention_days: int


class NotificationTarget(BaseModel):
    uri: str
    vault: str


class NotificationItem(BaseModel):
    id: str
    kind: str
    title: str
    message: str
    created_at: datetime
    updated_at: datetime
    read: bool
    version: str
    target: NotificationTarget | None


class NotificationPage(InboxMetadata):
    category: Literal["all", "documents", "access"]
    items: list[NotificationItem]
    next_cursor: str | None


@router.get("/notifications", response_model=NotificationPage)
async def notifications(state: Literal["all", "unread"] = "all", cursor: str | None = None,
                        limit: int = Query(20, ge=1, le=100), user=Depends(human_user),
                        category: Literal["all", "documents", "access"] = "all"):
    pool = await get_pool()
    async with pool.acquire() as conn, conn.transaction(isolation="repeatable_read", readonly=True):
        return await inbox.list_notifications(conn, uuid.UUID(user.user_id), state, cursor, limit, category)


@router.get("/notifications/unread-count", response_model=InboxMetadata)
async def unread_count(user=Depends(human_user)):
    pool = await get_pool()
    async with pool.acquire() as conn, conn.transaction(isolation="repeatable_read", readonly=True):
        return await inbox.metadata(conn, uuid.UUID(user.user_id))


@router.patch("/notifications/{notification_id}", response_model=InboxMetadata)
async def change_read(notification_id: uuid.UUID, body: ReadChange, user=Depends(human_user)):
    uid = uuid.UUID(user.user_id)
    version = inbox.boundary(body.version)
    pool = await get_pool()
    async with pool.acquire() as conn, conn.transaction():
        await inbox.lock_inbox(conn, uid)
        row = await conn.fetchrow(f"""UPDATE user_notifications n SET read_version=CASE WHEN $3 THEN version ELSE 0 END
            WHERE user_id=$1 AND id=$2 AND version=$4 AND {inbox.visible()} RETURNING id""", uid, notification_id, body.read, version)
        if row is None:
            raise AKBError("Notification changed or is unavailable; refresh the inbox", status_code=409)
        return await inbox.metadata(conn, uid)


@router.post("/notifications/mark-read", response_model=InboxMetadata)
async def mark_read(body: ReadSnapshot, user=Depends(human_user)):
    uid = uuid.UUID(user.user_id)
    version = inbox.boundary(body.snapshot)
    pool = await get_pool()
    async with pool.acquire() as conn, conn.transaction():
        current = await inbox.lock_inbox(conn, uid)
        if version > current:
            raise AKBError("Invalid notification snapshot", status_code=400)
        await conn.execute(f"UPDATE user_notifications n SET read_version=version WHERE user_id=$1 AND version<=$2 AND {inbox.visible()}", uid, version)
        return await inbox.metadata(conn, uid)


async def resolve_document(uri, user, pool):
    try:
        parsed = parse_uri(uri)
    except ValueError:
        raise AKBError("Invalid document URI", status_code=400) from None
    if parsed is None or parsed.kind != "doc":
        raise AKBError("A document URI is required", status_code=400)
    access = await check_vault_access(user.user_id, parsed.vault, required_role="reader")
    if inbox.native_documents():
        from app.repositories.native_revision_repo import NativeRevisionRepository
        resource = await NativeRevisionRepository(pool).resolve_live_reference(
            namespace_id=access["vault_id"], surface="document", reference=parsed.identifier)
        doc = ({"id": resource["resource_id"], "vault_id": resource["namespace_id"],
                "path": resource["current_path"]} if resource else None)
    else:
        doc = await DocumentRepository(pool).find_by_ref(access["vault_id"], parsed.identifier)
    if doc is None:
        raise AKBError("Document is unavailable", status_code=404)
    return doc


@router.get("/notification-subscriptions")
async def subscriptions(uri: str | None = None, user=Depends(human_user)):
    uid = uuid.UUID(user.user_id)
    pool = await get_pool()
    if uri is not None:
        doc = await resolve_document(uri, user, pool)
        async with pool.acquire() as conn:
            watched = await conn.fetchval("SELECT EXISTS(SELECT 1 FROM notification_subscriptions WHERE user_id=$1 AND resource_id=$2)", uid, doc["id"])
        return {"resource_id": str(doc["id"]), "subscribed": watched}
    async with pool.acquire() as conn:
        rows = await conn.fetch(f"""SELECT d.id,d.title,d.path,v.name vault FROM notification_subscriptions n
            JOIN {inbox.document_source()} d ON d.id=n.resource_id JOIN vaults v ON v.id=d.vault_id
            WHERE n.user_id=$1 AND {inbox.ACCESS} ORDER BY n.created_at DESC LIMIT 500""", uid)
    return {"items": [{"resource_id": str(r["id"]), "title": r["title"], "vault": r["vault"],
                       "uri": doc_uri(r["vault"], r["path"])} for r in rows]}


@router.put("/notification-subscriptions")
async def subscribe(uri: str, user=Depends(human_user)):
    return await set_subscription(uri, user, True)


@router.delete("/notification-subscriptions")
async def unsubscribe(uri: str, user=Depends(human_user)):
    return await set_subscription(uri, user, False)


async def set_subscription(uri, user, enabled):
    pool = await get_pool()
    doc = await resolve_document(uri, user, pool)
    uid = uuid.UUID(user.user_id)
    async with pool.acquire() as conn:
        if enabled:
            await conn.execute("""INSERT INTO notification_subscriptions(user_id,resource_id,vault_id) VALUES($1,$2,$3)
                ON CONFLICT DO NOTHING""", uid, doc["id"], doc["vault_id"])
        else:
            await conn.execute("DELETE FROM notification_subscriptions WHERE user_id=$1 AND resource_id=$2", uid, doc["id"])
    return {"resource_id": str(doc["id"]), "subscribed": enabled}
