"""Regression test for E08: `create_table` must refuse a vault+table
name combination whose PG identifier (`vt_<vault>__<table>`) overflows
NAMEDATALEN, returning a clean ``ValidationError`` (HTTP 422) *before*
any DDL — instead of letting ``role_sync`` raise deep in the GRANT path
and surface as an opaque 500.

DB-free: a minimal fake pool/conn carries the create path as far as the
length guard. ``create_dynamic_table`` is stubbed to blow up so the test
also proves the guard short-circuits before touching PG.
"""
from __future__ import annotations

import uuid

import pytest

from app.exceptions import ValidationError
from app.services import table_service

pytestmark = pytest.mark.asyncio


class _FakeTx:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class _FakeConn:
    def __init__(self, vault_name: str):
        self._vault_name = vault_name

    def transaction(self):
        return _FakeTx()

    async def fetchrow(self, sql: str, *params):
        if "FROM vaults" in sql:
            return {"name": self._vault_name}
        return None


class _FakeAcquire:
    def __init__(self, conn):
        self._conn = conn

    async def __aenter__(self):
        return self._conn

    async def __aexit__(self, *exc):
        return False


class _FakePool:
    def __init__(self, conn):
        self._conn = conn

    def acquire(self):
        return _FakeAcquire(self._conn)


async def test_create_table_rejects_overlong_pg_name(monkeypatch):
    """The exact E08 trigger: vault(27) + table(32) → 64-char identifier."""
    vault_name = "prod-conc-1780908249-8ml717"        # 27 chars
    table_name = "report_metrics_1780908249_8ml717"   # 32 chars
    assert len(table_service.table_data_repo.pg_table_name(vault_name, table_name)) == 64

    conn = _FakeConn(vault_name)

    async def _fake_get_pool():
        return _FakePool(conn)

    monkeypatch.setattr(table_service, "get_pool", _fake_get_pool)

    async def _no_existing(*a, **k):
        return None

    monkeypatch.setattr(table_service.table_registry_repo, "find_by_name", _no_existing)

    async def _must_not_run(*a, **k):
        raise AssertionError("create_dynamic_table must not run for an over-long name")

    monkeypatch.setattr(table_service.table_data_repo, "create_dynamic_table", _must_not_run)

    with pytest.raises(ValidationError) as ei:
        await table_service.create_table(
            uuid.uuid4(),
            table_name,
            [{"name": "amount", "type": "integer"}],
            actor_id="tester",
        )

    assert ei.value.status_code == 422
    assert "too long" in ei.value.message.lower()
    assert str(table_service.table_data_repo.PG_IDENT_MAX_LEN) in ei.value.message


async def test_create_table_invalid_name_is_422_not_500(monkeypatch):
    """The sibling name-shape check now also raises ValidationError, so a
    malformed name is a clean 422 rather than an uncaught ValueError 500."""
    with pytest.raises(ValidationError) as ei:
        await table_service.create_table(
            uuid.uuid4(),
            "Bad Name!",  # spaces + caps + punctuation
            [{"name": "amount", "type": "integer"}],
            actor_id="tester",
        )
    assert ei.value.status_code == 422
