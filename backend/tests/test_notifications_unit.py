"""Personal notification credential and bounded input contracts."""
import uuid
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.api.routes.notifications import human_user
from app.exceptions import AKBError
from app.repositories.notifications_repo import enqueue_notification_event
from app.services.auth_service import AuthenticatedUser
from app.services.notification_service import boundary


def principal(**changes):
    fields = dict(user_id=str(uuid.uuid4()), username="reader", email="reader@example.test",
                  display_name=None, is_admin=False, auth_method="jwt")
    fields.update(changes)
    return AuthenticatedUser(**fields)


@pytest.mark.asyncio
@pytest.mark.parametrize("method", ["jwt", "browser_session"])
async def test_human_sessions(method):
    user = principal(auth_method=method)
    assert await human_user(user) is user


@pytest.mark.asyncio
@pytest.mark.parametrize("changes", [
    {"auth_method": "pat"}, {"auth_method": "oauth"}, {"account_kind": "service"},
    {"token_id": str(uuid.uuid4())}, {"key_class": "service"}, {"token_scopes": frozenset()},
])
async def test_automation_never_reads_global_inbox(changes):
    with pytest.raises(AKBError) as caught:
        await human_user(principal(**changes))
    assert caught.value.status_code == 403


@pytest.mark.parametrize("value", ["-1", "9223372036854775808", "garbage", "1.0"])
def test_versions_are_bounded(value):
    with pytest.raises(AKBError):
        boundary(value)


@pytest.mark.asyncio
async def test_enqueue_copies_only_safe_fields_and_snapshots_subscribers():
    conn = AsyncMock()
    user_id, document_id = uuid.uuid4(), uuid.uuid4()
    conn.fetch.return_value = [{"user_id": user_id}]
    await enqueue_notification_event(conn, "document.update", source_key="event:123",
        resource_id=document_id, payload={"body": "secret", "title": "secret title", "role": "reader"})
    arguments = conn.execute.call_args.args
    assert arguments[-2] == [user_id]
    assert arguments[-1] == '{"role": "reader"}'
    assert "ON CONFLICT(source_key) DO NOTHING" in arguments[0]
    assert "clock_timestamp()" in arguments[0]


@pytest.mark.asyncio
async def test_invalid_kind_never_writes():
    conn = AsyncMock()
    with pytest.raises(ValueError):
        await enqueue_notification_event(conn, "token.secret", source_key="x")
    conn.execute.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["document.update", "access.changed"])
async def test_no_recipients_creates_no_durable_work(kind):
    conn = AsyncMock()
    conn.fetch.return_value = []
    await enqueue_notification_event(conn, kind, source_key="empty-recipients", resource_id=uuid.uuid4())
    conn.execute.assert_not_called()


@pytest.mark.asyncio
async def test_invalid_category_never_queries_database():
    from app.services.notification_service import list_notifications

    conn = AsyncMock()
    with pytest.raises(AKBError) as caught:
        await list_notifications(conn, uuid.uuid4(), "all", None, 20, "unknown")
    assert caught.value.status_code == 400
    conn.fetchval.assert_not_called()
    conn.fetch.assert_not_called()


@pytest.mark.parametrize("category", ["all", "documents", "access"])
def test_category_route_contract_and_validation(monkeypatch, category):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from app.api.routes import notifications as routes

    user = principal()
    app = FastAPI()
    app.include_router(routes.router)
    app.dependency_overrides[routes.human_user] = lambda: user
    conn = MagicMock()
    conn.transaction.return_value.__aenter__ = AsyncMock()
    conn.transaction.return_value.__aexit__ = AsyncMock()
    pool = MagicMock()
    pool.acquire.return_value.__aenter__ = AsyncMock(return_value=conn)
    pool.acquire.return_value.__aexit__ = AsyncMock()
    monkeypatch.setattr(routes, "get_pool", AsyncMock(return_value=pool))
    listing = AsyncMock(return_value={"supported": True, "snapshot": "7", "unread_count": 5,
        "retention_days": 90, "category": category, "items": [], "next_cursor": None})
    monkeypatch.setattr(routes.inbox, "list_notifications", listing)
    client = TestClient(app)
    response = client.get("/notifications", params={"category": category, "state": "unread", "limit": 2, "cursor": "6"})
    assert response.status_code == 200
    assert response.json()["category"] == category
    assert response.json()["unread_count"] == 5
    assert response.headers["Cache-Control"] == "private, no-store"
    listing.assert_awaited_once_with(conn, uuid.UUID(user.user_id), "unread", "6", 2, category)
    listing.reset_mock()
    assert client.get("/notifications", params={"category": "unknown"}).status_code == 422
    listing.assert_not_called()
