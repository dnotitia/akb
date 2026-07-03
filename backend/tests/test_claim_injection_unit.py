"""Unit coverage for BaaS claim injection and request.jwt.claims wiring."""

from __future__ import annotations

import json
from typing import Any, cast

import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient

from app.api import deps
from app.models.vault_scope import (
    RequestJwtClaims,
    current_request_jwt_claims,
    parse_request_jwt_claims_header,
)
from app.services.auth_service import AuthenticatedUser


def _auth_user(key_class: str = "service") -> AuthenticatedUser:
    return AuthenticatedUser(
        user_id="11111111-1111-1111-1111-111111111111",
        username="alice",
        email="alice@example.com",
        display_name=None,
        is_admin=False,
        auth_method="pat",
        key_class=key_class,
        token_scopes=frozenset({"read", "write"}),
    )


def _claims_header() -> str:
    return json.dumps(
        {
            "sub": "end-user-1",
            "app_metadata": {"org_id": "org-1", "role": "member"},
        }
    )


def _claims_app() -> FastAPI:
    app = FastAPI()

    @app.get("/claims")
    async def read_claims(_user: AuthenticatedUser = Depends(deps.get_current_user)):
        claims = current_request_jwt_claims.get()
        return {"claims": claims.to_db_json() if claims is not None else None}

    return app


def test_claim_header_requires_service_key(monkeypatch):
    async def fake_resolve(_authorization: str) -> AuthenticatedUser:
        return _auth_user(key_class="pat")

    monkeypatch.setattr(deps, "resolve_token", fake_resolve)

    response = TestClient(_claims_app()).get(
        "/claims",
        headers={
            "Authorization": "Bearer akb_fixture",
            "X-Akb-Claims": _claims_header(),
        },
    )

    assert response.status_code == 403
    assert response.json()["detail"]["code"] == "claims_require_service_key"


def test_service_key_accepts_valid_claim_header(monkeypatch):
    async def fake_resolve(_authorization: str) -> AuthenticatedUser:
        return _auth_user(key_class="service")

    monkeypatch.setattr(deps, "resolve_token", fake_resolve)

    response = TestClient(_claims_app()).get(
        "/claims",
        headers={
            "Authorization": "Bearer akb_secret_fixture",
            "X-Akb-Claims": _claims_header(),
        },
    )

    assert response.status_code == 200
    assert response.json() == {
        "claims": {
            "sub": "end-user-1",
            "app_metadata": {"org_id": "org-1", "role": "member"},
        }
    }


def test_service_key_rejects_invalid_claim_shape(monkeypatch):
    async def fake_resolve(_authorization: str) -> AuthenticatedUser:
        return _auth_user(key_class="service")

    monkeypatch.setattr(deps, "resolve_token", fake_resolve)

    response = TestClient(_claims_app()).get(
        "/claims",
        headers={
            "Authorization": "Bearer akb_secret_fixture",
            "X-Akb-Claims": json.dumps({"sub": "end-user-1", "app_metadata": {}}),
        },
    )

    assert response.status_code == 403
    assert response.json()["detail"]["code"] == "invalid_claims"


def test_no_claim_header_leaves_request_claims_empty(monkeypatch):
    async def fake_resolve(_authorization: str) -> AuthenticatedUser:
        return _auth_user(key_class="service")

    monkeypatch.setattr(deps, "resolve_token", fake_resolve)

    response = TestClient(_claims_app()).get(
        "/claims",
        headers={"Authorization": "Bearer akb_secret_fixture"},
    )

    assert response.status_code == 200
    assert response.json() == {"claims": None}


@pytest.mark.parametrize(
    "raw",
    [
        "not-json",
        json.dumps([]),
        json.dumps({"app_metadata": {"org_id": "org-1", "role": "member"}}),
        json.dumps({"sub": "u", "app_metadata": {"role": "member"}}),
        json.dumps({"sub": "u", "app_metadata": {"org_id": "org-1"}}),
    ],
)
def test_claim_header_shape_parser_rejects_invariant_breaks(raw: str):
    with pytest.raises(ValueError):
        parse_request_jwt_claims_header(raw)


class _Txn:
    async def __aenter__(self) -> "_Txn":
        return self

    async def __aexit__(self, *exc: object) -> bool:
        return False


class _Conn:
    def __init__(self) -> None:
        self.executed: list[tuple[str, tuple[Any, ...]]] = []

    def transaction(self) -> _Txn:
        return _Txn()

    async def execute(self, sql: str, *args: Any) -> str:
        self.executed.append((sql, args))
        return "OK"

    async def fetch(self, _sql: str) -> list[dict[str, Any]]:
        claims_args = [
            args[0]
            for sql, args in self.executed
            if sql == "SELECT set_config('request.jwt.claims', $1, true)"
        ]
        return [{"claims": claims_args[-1]}]


class _Acquire:
    def __init__(self, conn: _Conn) -> None:
        self._conn = conn

    async def __aenter__(self) -> _Conn:
        return self._conn

    async def __aexit__(self, *exc: object) -> bool:
        return False


class _Pool:
    def __init__(self, conn: _Conn) -> None:
        self._conn = conn

    def acquire(self) -> _Acquire:
        return _Acquire(self._conn)


def _async_return(value: Any):
    async def _coro() -> Any:
        return value

    return _coro()


@pytest.mark.asyncio
async def test_execute_sql_rejects_claim_guc_spoof_before_executor(monkeypatch):
    from app.services import table_service
    from app.util.errors import METHOD_NOT_ALLOWED

    async def fake_pool() -> _Pool:
        return _Pool(_Conn())

    class _Executor:
        async def execute(self, **_kwargs: Any) -> dict[str, Any]:
            raise AssertionError("set_config spoof reached the SQL executor")

    monkeypatch.setattr(table_service, "get_pool", fake_pool)
    monkeypatch.setattr(
        table_service.table_data_repo,
        "build_table_name_map",
        lambda _conn, _vaults: _async_return({}),
    )
    monkeypatch.setattr(
        table_service.table_data_repo,
        "rewrite_table_names",
        lambda sql, _table_map: sql,
    )
    monkeypatch.setattr(table_service, "get_user_sql_executor", lambda: _Executor())

    result = await table_service.execute_sql(
        vault_names=["demo"],
        user_id="11111111-1111-1111-1111-111111111111",
        sql=(
            "WITH _ AS ("
            "SELECT set_config('request.jwt.claims', '{\"sub\":\"mallory\"}', true)"
            ") SELECT 1"
        ),
        is_admin=True,
    )

    assert result["code"] == METHOD_NOT_ALLOWED
    assert "set_config()" in result["error"]


@pytest.mark.asyncio
async def test_execute_sql_rejects_unicode_escaped_set_config_spoof(monkeypatch):
    from app.services import table_service
    from app.util.errors import METHOD_NOT_ALLOWED

    async def fake_pool() -> _Pool:
        return _Pool(_Conn())

    class _Executor:
        async def execute(self, **_kwargs: Any) -> dict[str, Any]:
            raise AssertionError("unicode-escaped set_config reached the executor")

    monkeypatch.setattr(table_service, "get_pool", fake_pool)
    monkeypatch.setattr(
        table_service.table_data_repo,
        "build_table_name_map",
        lambda _conn, _vaults: _async_return({}),
    )
    monkeypatch.setattr(
        table_service.table_data_repo,
        "rewrite_table_names",
        lambda sql, _table_map: sql,
    )
    monkeypatch.setattr(table_service, "get_user_sql_executor", lambda: _Executor())

    result = await table_service.execute_sql(
        vault_names=["demo"],
        user_id="11111111-1111-1111-1111-111111111111",
        sql='SELECT U&"set\\005fconfig"(\'request.jwt.claims\', \'{}\', true)',
        is_admin=True,
    )

    assert result["code"] == METHOD_NOT_ALLOWED
    assert "Unicode-escaped quoted identifiers" in result["error"]


@pytest.mark.asyncio
async def test_execute_sql_rejects_pg_settings_claim_guc_spoof(monkeypatch):
    from app.services import table_service
    from app.util.errors import METHOD_NOT_ALLOWED

    async def fake_pool() -> _Pool:
        return _Pool(_Conn())

    class _Executor:
        async def execute(self, **_kwargs: Any) -> dict[str, Any]:
            raise AssertionError("pg_settings spoof reached the executor")

    monkeypatch.setattr(table_service, "get_pool", fake_pool)
    monkeypatch.setattr(
        table_service.table_data_repo,
        "build_table_name_map",
        lambda _conn, _vaults: _async_return({}),
    )
    monkeypatch.setattr(
        table_service.table_data_repo,
        "rewrite_table_names",
        lambda sql, _table_map: sql,
    )
    monkeypatch.setattr(table_service, "get_user_sql_executor", lambda: _Executor())

    result = await table_service.execute_sql(
        vault_names=["demo"],
        user_id="11111111-1111-1111-1111-111111111111",
        sql=(
            "WITH _ AS ("
            "UPDATE pg_catalog.pg_settings SET setting = '{\"sub\":\"mallory\"}' "
            "WHERE name = 'request.jwt.claims' RETURNING 1"
            ") SELECT 1"
        ),
        is_admin=True,
    )

    assert result["code"] == METHOD_NOT_ALLOWED
    assert "pg_settings" in result["error"]


@pytest.mark.asyncio
async def test_user_sql_executor_sets_claims_guc_transaction_local():
    from app.services.user_sql_executor import UserSqlExecutor

    conn = _Conn()
    executor = UserSqlExecutor(cast(Any, _Pool(conn)))
    claims = RequestJwtClaims(sub="end-user-1", org_id="org-1", role="member")
    token = current_request_jwt_claims.set(claims)
    try:
        result = await executor.execute(
            user_id="11111111-1111-1111-1111-111111111111",
            sql="SELECT current_setting('request.jwt.claims') AS claims",
            is_admin=True,
        )
    finally:
        current_request_jwt_claims.reset(token)

    claims_json = claims.to_json()
    assert (
        "SELECT set_config('request.jwt.claims', $1, true)",
        (claims_json,),
    ) in conn.executed
    assert result == {
        "kind": "table_query",
        "vaults": [],
        "columns": ["claims"],
        "items": [{"claims": claims_json}],
        "total": 1,
    }
