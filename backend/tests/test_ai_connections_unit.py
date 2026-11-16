"""AI connection registry: extraction, throttle, gate wiring and the REST route.

Database-free. What PostgreSQL itself decides (the UPSERT throttle, the
first-insert notification, cascades) is proven in
``test_ai_connections_postgres.py``.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

import httpx
import pytest
from fastapi.testclient import TestClient

from app.config import settings
from app.services import ai_connections
from app.services.auth_service import AuthenticatedUser
from mcp_server import http_app
from mcp_server.http_app import MCPApp


@pytest.fixture(autouse=True)
def _clean_registry():
    ai_connections.reset()
    yield
    ai_connections.reset()


def _pat_user(**overrides) -> AuthenticatedUser:
    values = dict(
        user_id=str(uuid.uuid4()),
        username="pat-user",
        email="pat-user@example.invalid",
        display_name=None,
        is_admin=False,
        auth_method="pat",
        token_id=str(uuid.uuid4()),
        key_class="pat",
        token_scopes=frozenset({"read", "write"}),
    )
    values.update(overrides)
    return AuthenticatedUser(**values)


def _oauth_user(azp: str | None = "mcp-client-registration-1", **overrides) -> AuthenticatedUser:
    values = dict(
        user_id=str(uuid.uuid4()),
        username="oauth-user",
        email="oauth-user@example.invalid",
        display_name=None,
        is_admin=False,
        auth_method="oauth",
        oauth_scopes=["vault:read"],
        oauth_client_id=azp,
    )
    values.update(overrides)
    return AuthenticatedUser(**values)


def _initialize(name: str = "claude-code", version: str | None = "2.1.0") -> dict:
    info: dict = {"name": name}
    if version is not None:
        info["version"] = version
    return {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": info},
    }


class _Clock:
    def __init__(self) -> None:
        self.now = 1_000.0

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def clock(monkeypatch) -> _Clock:
    fake = _Clock()
    monkeypatch.setattr(ai_connections.time, "monotonic", fake)
    return fake


# ── clientInfo / User-Agent extraction ─────────────────────────


def test_initialize_client_info_is_read_from_params():
    assert ai_connections.client_info_from_body(_initialize()) == ("claude-code", "2.1.0")


def test_modern_envelope_client_info_is_read_from_meta():
    body = {
        "jsonrpc": "2.0",
        "id": 2,
        "method": "tools/list",
        "params": {"_meta": {"io.modelcontextprotocol/clientInfo": {"name": "codex", "version": "0.46"}}},
    }
    assert ai_connections.client_info_from_body(body) == ("codex", "0.46")


@pytest.mark.parametrize(
    "body",
    [
        None,
        {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "akb_search"}},
        # Only `initialize` may name the client outside `_meta`: a tool call
        # whose arguments happen to contain `clientInfo` reports nothing.
        {"jsonrpc": "2.0", "id": 4, "method": "tools/call", "params": {"clientInfo": {"name": "spoof"}}},
        {"jsonrpc": "2.0", "id": 5, "method": "initialize", "params": {"clientInfo": "not-an-object"}},
        {"jsonrpc": "2.0", "id": 6, "method": "initialize", "params": {"clientInfo": {"version": "1"}}},
        {"jsonrpc": "2.0", "id": 7, "method": "initialize", "params": "x"},
    ],
)
def test_requests_without_client_info_report_nothing(body):
    assert ai_connections.client_info_from_body(body) == (None, None)


def test_self_reported_names_are_cleaned_and_bounded():
    body = _initialize(name="evil\x00‮-client\n" + "x" * 500, version="1\t" + "9" * 200)
    name, version = ai_connections.client_info_from_body(body)
    assert name is not None and version is not None
    assert "\x00" not in name and "‮" not in name and "\n" not in name
    assert name.startswith("evil-client")
    assert len(name) == ai_connections.NAME_MAX
    assert len(version) == ai_connections.VERSION_MAX


@pytest.mark.parametrize(
    ("user_agent", "expected"),
    [
        ("claude-code/2.0.14 (external, cli)", ("claude-code", "2.0.14")),
        ("codex_cli_rs/0.46.0 (Mac OS 15.6; arm64)", ("codex_cli_rs", "0.46.0")),
        ("node", ("node", None)),
        ("", (None, None)),
        (None, (None, None)),
        ("   ", (None, None)),
    ],
)
def test_user_agent_product_is_the_display_fallback(user_agent, expected):
    assert ai_connections.user_agent_product(user_agent) == expected


# ── which principals are connections ───────────────────────────


def test_pat_connection_is_keyed_by_token_id():
    user = _pat_user()
    key, token_id = ai_connections.connection_key(user)
    assert key == (user.user_id, "pat", user.token_id)
    assert token_id == user.token_id


def test_oauth_connection_is_keyed_by_authorized_party():
    user = _oauth_user("dcr-client-42")
    key, token_id = ai_connections.connection_key(user)
    assert key == (user.user_id, "oauth", "dcr-client-42")
    assert token_id is None


@pytest.mark.parametrize(
    "user",
    [
        _pat_user(account_kind="service"),
        _pat_user(key_class="service"),
        _pat_user(key_class="publishable"),
        _pat_user(token_id=None),
        _oauth_user(None),
        _oauth_user(account_kind="service"),
        _pat_user(auth_method="jwt", token_id=None, key_class=None),
        _pat_user(auth_method="browser_session", token_id=None, key_class=None),
    ],
)
def test_non_app_principals_are_not_recorded(user):
    assert ai_connections.connection_key(user) is None
    ai_connections.observe(user, body=_initialize())
    assert ai_connections.pending_depth() == 0


# ── throttle ───────────────────────────────────────────────────


def test_first_request_is_queued_with_its_client_info(clock):
    user = _pat_user()
    ai_connections.observe(user, body=_initialize(), user_agent="claude-code/2.1.0")
    [(key, obs)] = ai_connections._pending.items()
    assert key.auth_kind == "pat"
    assert (obs.client_name, obs.client_version) == ("claude-code", "2.1.0")
    assert obs.user_agent == "claude-code/2.1.0"
    assert obs.token_id == user.token_id


def test_repeat_requests_inside_the_window_are_not_queued_again(clock):
    user = _pat_user()
    ai_connections.observe(user, body=_initialize(), user_agent="ua/1")
    ai_connections._pending.clear()  # as if flushed

    for _ in range(50):
        clock.now += 1
        ai_connections.observe(user, body={"jsonrpc": "2.0", "method": "tools/call", "params": {}}, user_agent="ua/1")
    assert ai_connections.pending_depth() == 0

    clock.now += ai_connections.SEEN_WINDOW_SECS
    ai_connections.observe(user, body=None, user_agent="ua/1")
    [obs] = ai_connections._pending.values()
    # A User-Agent-only request never erases the name the client reported.
    assert obs.client_name is None
    assert ai_connections._throttle[next(iter(ai_connections._pending))][1:] == ("claude-code", "2.1.0")


def test_a_new_client_version_inside_the_window_is_queued(clock):
    user = _oauth_user()
    ai_connections.observe(user, body=_initialize(version="2.1.0"))
    ai_connections._pending.clear()
    clock.now += 5
    ai_connections.observe(user, body=_initialize(version="2.1.0"))
    assert ai_connections.pending_depth() == 0
    clock.now += 5
    ai_connections.observe(user, body=_initialize(version="2.2.0"))
    [obs] = ai_connections._pending.values()
    assert obs.client_version == "2.2.0"


def test_pending_observations_fold_into_one_entry_per_connection(clock):
    user = _oauth_user()
    ai_connections.observe(user, body=_initialize(version="1"))
    clock.now += ai_connections.SEEN_WINDOW_SECS
    ai_connections.observe(user, body=None, user_agent="later/1")
    assert ai_connections.pending_depth() == 1
    [obs] = ai_connections._pending.values()
    assert (obs.client_name, obs.client_version, obs.user_agent) == ("claude-code", "1", "later/1")


def test_distinct_credentials_of_one_account_are_distinct_connections(clock):
    user = _pat_user()
    ai_connections.observe(user, body=_initialize("claude-code"))
    ai_connections.observe(_pat_user(user_id=user.user_id), body=_initialize("codex"))
    assert ai_connections.pending_depth() == 2


def test_overflow_of_new_connections_is_counted_not_silent(monkeypatch, clock):
    monkeypatch.setattr(ai_connections, "MAX_PENDING", 2)
    for _ in range(3):
        ai_connections.observe(_pat_user(), body=_initialize())
    assert ai_connections.pending_depth() == 2
    assert ai_connections.dropped_count() == 1
    assert ai_connections.stats()["lost"] == 1


def test_throttle_map_stays_bounded(monkeypatch, clock):
    monkeypatch.setattr(ai_connections, "MAX_THROTTLE", 3)
    for _ in range(10):
        ai_connections.observe(_pat_user(), body=_initialize())
        ai_connections._pending.clear()
        clock.now += 1
    assert len(ai_connections._throttle) <= 3


def test_observe_never_raises(monkeypatch):
    def broken(_user):
        raise RuntimeError("boom")

    monkeypatch.setattr(ai_connections, "connection_key", broken)
    ai_connections.observe(_pat_user(), body=_initialize())
    assert ai_connections.dropped_count() == 1


# ── flush failure handling ─────────────────────────────────────


@pytest.mark.asyncio
async def test_failed_flush_returns_the_batch_to_pending(monkeypatch, clock):
    async def no_pool():
        raise ConnectionError("database restarting")

    monkeypatch.setattr(ai_connections, "get_pool", no_pool)
    user = _pat_user()
    ai_connections.observe(user, body=_initialize(version="1"))
    # A database outage is retried on the next tick, not raised into the
    # runner's exception log every few seconds.
    assert await ai_connections.flush_once() == 0
    assert ai_connections.pending_depth() == 1
    assert ai_connections.stats()["consecutive_flush_failures"] == 1

    # A newer observation that arrived meanwhile wins over the requeued one.
    clock.now += ai_connections.SEEN_WINDOW_SECS
    ai_connections.observe(user, body=_initialize(version="2"))
    assert await ai_connections.flush_once() == 0
    [obs] = ai_connections._pending.values()
    assert obs.client_version == "2"
    assert ai_connections.stats()["consecutive_flush_failures"] == 2


@pytest.mark.asyncio
async def test_cancelled_flush_keeps_its_batch(monkeypatch):
    import asyncio

    async def cancelled_pool():
        raise asyncio.CancelledError()

    monkeypatch.setattr(ai_connections, "get_pool", cancelled_pool)
    ai_connections.observe(_pat_user(), body=_initialize())
    with pytest.raises(asyncio.CancelledError):
        await ai_connections.flush_once()
    assert ai_connections.pending_depth() == 1


@pytest.mark.asyncio
async def test_empty_flush_touches_no_database(monkeypatch):
    async def no_pool():
        raise AssertionError("must not connect")

    monkeypatch.setattr(ai_connections, "get_pool", no_pool)
    assert await ai_connections.flush_once() == 0


# ── MCP gate wiring ────────────────────────────────────────────


async def _resolved(user):
    return user


def _headers() -> dict[str, str]:
    return {
        "authorization": "Bearer token",
        "content-type": "application/json",
        "accept": "application/json",
        "user-agent": "claude-code/2.1.0 (cli)",
    }


@pytest.mark.asyncio
async def test_the_mcp_gate_observes_the_authenticated_client(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "git_storage_path", str(tmp_path / "vaults"))
    user = _pat_user()
    monkeypatch.setattr(http_app, "resolve_mcp_authorization", lambda _h: _resolved(user))

    app = MCPApp()
    async with app.run():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            response = await client.post("/mcp/", headers=_headers(), json=_initialize())
            assert response.status_code == 200
            response = await client.post(
                "/mcp/", headers=_headers(),
                json={"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
            )
            assert response.status_code == 200

    [(key, obs)] = ai_connections._pending.items()
    assert key == (user.user_id, "pat", user.token_id)
    assert (obs.client_name, obs.client_version) == ("claude-code", "2.1.0")
    assert obs.user_agent == "claude-code/2.1.0 (cli)"


@pytest.mark.asyncio
async def test_a_failing_registry_cannot_fail_the_mcp_request(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "git_storage_path", str(tmp_path / "vaults"))
    monkeypatch.setattr(http_app, "resolve_mcp_authorization", lambda _h: _resolved(_pat_user()))

    def broken(_user):
        raise RuntimeError("registry bug")

    monkeypatch.setattr(ai_connections, "connection_key", broken)
    app = MCPApp()
    async with app.run():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            response = await client.post("/mcp/", headers=_headers(), json=_initialize())
    assert response.status_code == 200
    assert response.json()["result"]["protocolVersion"] == "2025-06-18"


@pytest.mark.asyncio
async def test_rejected_credentials_are_never_observed(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "git_storage_path", str(tmp_path / "vaults"))
    monkeypatch.setattr(http_app, "resolve_mcp_authorization", lambda _h: _resolved(None))
    app = MCPApp()
    async with app.run():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            response = await client.post("/mcp/", headers=_headers(), json=_initialize())
    assert response.status_code == 401
    assert ai_connections.pending_depth() == 0


# ── REST route ─────────────────────────────────────────────────


def _route_client(monkeypatch, tmp_path, user):
    monkeypatch.setattr(settings, "git_storage_path", str(tmp_path / "vaults"))
    from app.api.deps import get_current_user
    from app.main import app

    app.dependency_overrides[get_current_user] = lambda: user
    return app, TestClient(app)


def test_connections_route_lists_the_callers_connections(monkeypatch, tmp_path):
    user = _oauth_user()
    seen: list[str] = []
    connection_id, token_id = uuid.uuid4(), uuid.uuid4()
    at = datetime(2026, 11, 1, 12, 0, tzinfo=timezone.utc)

    async def fake_list(user_id):
        seen.append(user_id)
        return [
            {
                "id": str(connection_id), "client_name": "claude-code", "client_version": "2.1.0",
                "auth_kind": "pat", "token_id": str(token_id), "token_prefix": "akb_abcd",
                "token_name": "laptop", "first_seen_at": at.isoformat(), "last_seen_at": at.isoformat(),
            },
            {
                "id": str(uuid.uuid4()), "client_name": None, "client_version": None,
                "auth_kind": "oauth", "token_id": None, "token_prefix": None, "token_name": None,
                "first_seen_at": at.isoformat(), "last_seen_at": at.isoformat(),
            },
        ]

    monkeypatch.setattr(ai_connections, "list_connections", fake_list)
    app, client = _route_client(monkeypatch, tmp_path, user)
    try:
        response = client.get("/api/v1/auth/connections")
    finally:
        app.dependency_overrides.clear()
    assert response.status_code == 200
    assert response.headers["cache-control"] == "private, no-store"
    assert seen == [user.user_id]
    pat, oauth = response.json()["connections"]
    assert pat == {
        "id": str(connection_id), "client_name": "claude-code", "client_version": "2.1.0",
        "auth_kind": "pat", "token_id": str(token_id), "token_prefix": "akb_abcd",
        "token_name": "laptop", "first_seen_at": "2026-11-01T12:00:00Z", "last_seen_at": "2026-11-01T12:00:00Z",
    }
    assert oauth["auth_kind"] == "oauth"
    assert oauth["token_id"] is None and oauth["token_prefix"] is None


def test_connections_route_refuses_service_accounts(monkeypatch, tmp_path):
    async def must_not_list(_user_id):
        raise AssertionError("must not read")

    monkeypatch.setattr(ai_connections, "list_connections", must_not_list)
    app, client = _route_client(monkeypatch, tmp_path, _pat_user(account_kind="service", key_class="service"))
    try:
        response = client.get("/api/v1/auth/connections")
    finally:
        app.dependency_overrides.clear()
    assert response.status_code == 403


def test_connections_route_is_published_in_openapi(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "git_storage_path", str(tmp_path / "vaults"))
    from app.main import app

    operation = app.openapi()["paths"]["/api/v1/auth/connections"]["get"]
    assert operation["summary"] == "List the AI apps connected to your account"
    props = app.openapi()["components"]["schemas"]["AIConnection"]["properties"]
    assert set(props) == {
        "id", "client_name", "client_version", "auth_kind", "token_id", "token_prefix",
        "token_name", "first_seen_at", "last_seen_at",
    }
    # Nothing that locates or reads a person's traffic is part of the contract.
    assert not {"ip", "ip_address", "user_agent", "client_key"} & set(props)
