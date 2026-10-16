"""Cancel only explicitly known reads, and drain their request task."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app import main
from app.api.deps import get_current_user
from app.api.routes import tables
from app.http_middleware import CancelDisconnectedReadMiddleware, NoStorePublicSurfacesMiddleware


def _scope(path, method="GET"):
    return {"type": "http", "asgi": {"version": "3.0", "spec_version": "2.4"},
            "http_version": "1.1", "method": method, "scheme": "http", "path": path,
            "raw_path": path.encode(), "query_string": b"", "headers": [],
            "client": ("127.0.0.1", 1), "server": ("127.0.0.1", 80), "root_path": ""}


@pytest.mark.asyncio
async def test_main_read_disconnect_drains_handler(monkeypatch):
    started, drained = asyncio.Event(), asyncio.Event()
    messages = asyncio.Queue()
    await messages.put({"type": "http.request", "body": b"", "more_body": False})

    async def slow_read(**kwargs):
        started.set()
        try:
            await asyncio.sleep(60)
        finally:
            # Model awaited transaction/pool cleanup, not just task.cancel().
            await asyncio.sleep(0.01)
            drained.set()

    monkeypatch.setattr(tables, "check_vault_access", AsyncMock(return_value={"vault_id": "fixture"}))
    monkeypatch.setattr(tables.table_row_query, "select_rows", slow_read)
    user = SimpleNamespace(user_id="fixture", username="fixture", is_admin=False)
    main.app.dependency_overrides[get_current_user] = lambda: user
    sent = []

    async def send(message):
        sent.append(message)

    task = asyncio.create_task(main.app(_scope("/api/v1/tables/fixture/items/rows"), messages.get, send))
    try:
        await asyncio.wait_for(started.wait(), 1)
        await messages.put({"type": "http.disconnect"})
        await asyncio.wait_for(task, 0.5)
        assert drained.is_set()
        assert not sent
    finally:
        main.app.dependency_overrides.pop(get_current_user, None)
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)


@pytest.mark.parametrize(("path", "method"), [
    ("/api/v1/tables/v/sql", "POST"), ("/api/v1/documents", "POST"),
    ("/api/v1/documents/v/doc", "PATCH"), ("/api/v1/events/stream", "GET"),
    ("/api/v1/auth/keycloak/callback", "GET"), ("/mcp", "POST"),
])
@pytest.mark.asyncio
async def test_writes_auth_and_streams_are_not_monitored(path, method):
    called = []

    async def downstream(scope, receive, send):
        called.append(1)

    receive = AsyncMock(side_effect=AssertionError("Must not consume this request stream"))
    await CancelDisconnectedReadMiddleware(downstream)(_scope(path, method), receive, AsyncMock())
    assert called == [1]
    receive.assert_not_awaited()


@pytest.mark.asyncio
async def test_unusual_get_body_is_replayed_without_a_second_receive_consumer():
    messages = [{"type": "http.request", "body": b"part1", "more_body": True},
                {"type": "http.request", "body": b"part2", "more_body": False}]
    seen = []

    async def downstream(scope, receive, send):
        seen.extend([await receive(), await receive()])

    await CancelDisconnectedReadMiddleware(downstream)(
        _scope("/api/v1/recent"), AsyncMock(side_effect=messages), AsyncMock(),
    )
    assert seen == messages


@pytest.mark.asyncio
async def test_normal_response_cleans_watcher_and_propagates_downstream_exception():
    receive = asyncio.Queue()
    await receive.put({"type": "http.request", "body": b"", "more_body": False})

    async def downstream(scope, recv, send):
        assert (await recv())["type"] == "http.request"
        raise ValueError("real bug")

    tasks = set(asyncio.all_tasks())
    with pytest.raises(ValueError, match="real bug"):
        await CancelDisconnectedReadMiddleware(downstream)(_scope("/api/v1/recent"), receive.get, AsyncMock())
    assert set(asyncio.all_tasks()) == tasks


@pytest.mark.asyncio
async def test_outer_shutdown_during_disconnect_cleanup_is_drained_then_propagated():
    started, cleanup_started, cleanup_gate, drained = (asyncio.Event() for _ in range(4))
    receive = asyncio.Queue()
    await receive.put({"type": "http.request", "body": b"", "more_body": False})

    async def downstream(scope, recv, send):
        started.set()
        try:
            await asyncio.sleep(60)
        finally:
            cleanup_started.set()
            await cleanup_gate.wait()
            drained.set()

    tasks = set(asyncio.all_tasks())
    task = asyncio.create_task(CancelDisconnectedReadMiddleware(downstream)(
        _scope("/api/v1/recent"), receive.get, AsyncMock(),
    ))
    try:
        await asyncio.wait_for(started.wait(), 1)
        await receive.put({"type": "http.disconnect"})
        await asyncio.wait_for(cleanup_started.wait(), 1)
        task.cancel()  # A shutdown cancellation must not interrupt rollback.
        await asyncio.sleep(0)
        cleanup_gate.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert drained.is_set()
        assert set(asyncio.all_tasks()) == tasks
    finally:
        cleanup_gate.set()
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)


@pytest.mark.parametrize(("path", "cache", "pragma"), [
    ("/api/v1/public/slug", "no-store", None),
    ("/api/v1/publications/slug", "no-store", None),
    ("/api/v1/oembed", "no-store", None),
    ("/api/v1/auth/tokens/issuance", "no-store", "no-cache"),
    ("/api/v1/admin/users", "no-store", "no-cache"),
    ("/api/v1/apps/a/installations/b", "no-store", "no-cache"),
    ("/api/v1/publicX", None, None), ("/api/v1/recent", None, None),
])
@pytest.mark.asyncio
async def test_no_store_header_boundaries_and_duplicate_cookies_survive(path, cache, pragma):
    from starlette.datastructures import Headers

    sent = []

    async def downstream(scope, receive, send):
        await send({"type": "http.response.start", "status": 200,
                    "headers": [(b"set-cookie", b"a=1"), (b"set-cookie", b"b=2")]})
        await send({"type": "http.response.body", "body": b"ok"})

    async def send(message):
        sent.append(message)

    await NoStorePublicSurfacesMiddleware(downstream)(_scope(path), AsyncMock(), send)
    headers = Headers(scope=sent[0])
    assert headers.get("Cache-Control") == cache
    assert headers.get("Pragma") == pragma
    assert headers.getlist("set-cookie") == ["a=1", "b=2"]
    assert sent[1]["body"] == b"ok"
