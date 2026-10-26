"""Transient faults must not become invalid SQL, empty data, or leaked internals."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import asyncpg
from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest

from app.db.transient import transient_error
from app.repositories import table_data_repo
from app.util.errors import exception_envelope


@pytest.fixture
def main(monkeypatch, tmp_path):
    # Importing routes constructs the legacy revision service. Keep focused
    # test collection independent of deployment paths and test import order.
    from app.config import settings

    monkeypatch.setattr(settings, "git_storage_path", str(tmp_path / "vaults"))
    from app import main as application

    return application


FAULTS = [
    (asyncpg.ConnectionDoesNotExistError("private-host/query"), "service_unavailable"),
    (asyncpg.AdminShutdownError("private-host/query"), "service_unavailable"),
    (asyncpg.TooManyConnectionsError("private-host/query"), "service_unavailable"),
    (asyncpg.QueryCanceledError("private-host/query"), "operation_timeout"),
    (TimeoutError("private-host/query"), "operation_timeout"),
]


@pytest.mark.parametrize(("failure", "code"), FAULTS)
def test_rest_transient_response_is_sanitized_503(main, failure, code):
    app = FastAPI(exception_handlers=dict(main.app.exception_handlers))

    @app.get("/failure")
    async def fail():
        raise failure

    response = TestClient(app, raise_server_exceptions=False).get("/failure")
    assert response.status_code == 503
    assert response.json()["code"] == code
    assert "private-host" not in response.text
    assert "query" not in response.text
    assert response.headers["Retry-After"] == "1"


@pytest.mark.parametrize(("failure", "code"), FAULTS)
def test_mcp_transient_envelope_matches_rest_without_internal_details(failure, code):
    body = exception_envelope(failure)
    assert body["code"] == code
    assert "private-host" not in str(body)
    assert "write" in body["hint"].lower()


@pytest.mark.asyncio
async def test_count_rows_does_not_report_timeout_as_zero():
    conn = SimpleNamespace(fetchval=AsyncMock(side_effect=TimeoutError()))
    with pytest.raises(TimeoutError):
        await table_data_repo.count_rows(conn, "vt_fixture")


@pytest.mark.asyncio
async def test_count_rows_keeps_missing_table_fallback():
    conn = SimpleNamespace(fetchval=AsyncMock(side_effect=asyncpg.UndefinedTableError()))
    assert await table_data_repo.count_rows(conn, "vt_fixture") == 0


@pytest.mark.asyncio
async def test_readiness_db_failure_does_not_echo_connection_details(main, monkeypatch):
    monkeypatch.setattr(main, "get_pool", AsyncMock(side_effect=ConnectionRefusedError("private-host")))
    ok, detail = await main._probe_ready()
    assert ok is False
    assert detail["db"] == "error:unavailable"
    assert "private-host" not in str(detail)


@pytest.mark.asyncio
async def test_readiness_success_ttl_failed_probe_and_soft_vector(main, monkeypatch):
    pool = SimpleNamespace(fetchval=AsyncMock(return_value=1), get_size=lambda: 2,
                           get_idle_size=lambda: 2, get_max_size=lambda: 2)
    monkeypatch.setattr(main, "get_pool", AsyncMock(return_value=pool))
    monkeypatch.setattr(main, "get_vector_store", lambda: SimpleNamespace(health=AsyncMock(return_value=False)))
    ok, detail = await main._probe_ready()
    assert ok is True
    assert detail["vector_store"] == "degraded:unreachable"
    now = main.time.monotonic()
    assert main._cache_fresh(main._ReadyState(now, True, detail))
    assert not main._cache_fresh(main._ReadyState(now - 30.1, True, detail))
    assert not main._cache_fresh(main._ReadyState(now, False, detail))


@pytest.mark.asyncio
async def test_readiness_pool_initialization_is_inside_db_deadline(main, monkeypatch):
    cancelled = asyncio.Event()

    async def unavailable_pool():
        try:
            await asyncio.sleep(60)
        finally:
            cancelled.set()

    monkeypatch.setattr(main, "get_pool", unavailable_pool)
    # The production budget is2s; the outer guard catches an unbounded get_pool.
    ok, detail = await asyncio.wait_for(main._probe_ready(), timeout=3)
    assert ok is False
    assert detail["db"] == "error:timeout"
    assert cancelled.is_set()


@pytest.mark.parametrize("failure", [
    asyncpg.PostgresSyntaxError(), asyncpg.InsufficientPrivilegeError(),
    asyncpg.UniqueViolationError(), asyncpg.ProtocolViolationError(),
    asyncpg.InterfaceError("another operation is in progress"),
    asyncpg.InvalidPasswordError(), asyncpg.ConfigurationLimitExceededError(),
    OSError("file permission"), RuntimeError("programming bug"),
])
def test_non_transient_faults_are_not_hidden_as_availability(failure):
    assert transient_error(failure) is None


def test_closed_transaction_cleanup_preserves_typed_connection_loss_only():
    cleanup = asyncpg.InterfaceError("closed transaction")
    cleanup.__context__ = asyncpg.ConnectionDoesNotExistError("private-host")
    assert transient_error(cleanup).code == "service_unavailable"
    cleanup.__context__ = asyncpg.PostgresSyntaxError("bad SQL")
    assert transient_error(cleanup) is None


def test_write_failure_is_not_automatically_replayed_or_given_retry_header(main, ):
    app = FastAPI(exception_handlers=dict(main.app.exception_handlers))
    calls = []

    @app.post("/write")
    async def write():
        calls.append(1)
        raise asyncpg.ConnectionDoesNotExistError("private-host")

    response = TestClient(app, raise_server_exceptions=False).post("/write")
    assert response.status_code == 503
    assert response.json()["code"] == "service_unavailable"
    assert "Retry-After" not in response.headers
    assert "completion may be unknown" in response.json()["hint"]
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_ready_cache_can_outlive_data_availability_but_failure_is_not_cached(main, monkeypatch):
    monkeypatch.setattr(main, "_ready_state", main._ReadyState())
    monkeypatch.setattr(main, "_ready_lock", asyncio.Lock())
    healthy = AsyncMock(return_value=(True, {"db": "ok", "vector_store": "degraded:unreachable"}))
    monkeypatch.setattr(main, "_probe_ready", healthy)
    assert (await main.readyz())["cached"] is False
    failed = AsyncMock(return_value=(False, {"db": "error:unavailable"}))
    monkeypatch.setattr(main, "_probe_ready", failed)
    assert (await main.readyz())["cached"] is True
    failed.assert_not_awaited()
    main._ready_state.ts -= 31
    with pytest.raises(main.HTTPException) as caught:
        await main.readyz()
    assert caught.value.status_code == 503
    monkeypatch.setattr(main, "_probe_ready", healthy)
    assert (await main.readyz())["cached"] is False
    assert (await main.livez()) == {"status": "alive"}


def test_failed_readiness_uses_unavailable_code_and_preserves_probe_detail(main, monkeypatch):
    monkeypatch.setattr(main, "_ready_state", main._ReadyState())
    monkeypatch.setattr(main, "_ready_lock", asyncio.Lock())
    monkeypatch.setattr(main, "_probe_ready", AsyncMock(return_value=(False, {"db": "error:unavailable"})))
    app = FastAPI(exception_handlers=dict(main.app.exception_handlers))
    app.add_api_route("/readyz", main.readyz)
    response = TestClient(app).get("/readyz")
    assert response.status_code == 503
    assert response.json()["code"] == "service_unavailable"
    assert response.json()["detail"]["status"] == "not_ready"
    assert response.json()["detail"]["detail"]["db"] == "error:unavailable"


@pytest.mark.asyncio
async def test_executor_transient_error_bypasses_sql_enrichment(main, monkeypatch):
    from contextlib import asynccontextmanager
    from app.services import table_service
    from app.services.user_sql_executor import UserSqlExecutor

    metadata_acquires = []

    @asynccontextmanager
    async def metadata():
        metadata_acquires.append(1)
        yield object()

    @asynccontextmanager
    async def disconnected():
        raise asyncpg.AdminShutdownError("private-host")
        yield  # pragma: no cover

    monkeypatch.setattr(table_service, "get_pool", AsyncMock(return_value=SimpleNamespace(acquire=metadata)))
    monkeypatch.setattr(table_service.table_data_repo, "build_table_name_map", AsyncMock(return_value={}))
    executor = UserSqlExecutor(SimpleNamespace(acquire=disconnected))
    monkeypatch.setattr(table_service, "get_user_sql_executor", lambda: executor)
    with pytest.raises(main.TransientDependencyError) as caught:
        await table_service.execute_sql(vault_names=[], user_id="fixture", actor_id="fixture",
                                        sql="SELECT 1", is_admin=True)
    assert caught.value.code == "service_unavailable"
    assert len(metadata_acquires) == 1  # No second connection for SQL-error hints.
