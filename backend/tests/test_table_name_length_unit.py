"""`create_table` and a vault+table pair whose composed PG identifier
(`vt_<vault>__<table>`) would overflow NAMEDATALEN.

This began as E08's regression: the pair was REFUSED with a clean 422
before any DDL, which beat PG truncating silently and ``role_sync`` then
failing to GRANT deep in the stack. That judgement held while the only
names in play were ones a person chose. It stopped holding for a vault
named by a generator, which spends most of a 63-byte budget before a
table is named at all — ordinary table names then became uncreatable,
not intermittently but on an identifier computed before any work.

So the pair is now FITTED rather than refused, and these tests changed
with it. What did not change is the property underneath: a name that
reaches PG is within NAMEDATALEN and is not another table's.

DB-free: a minimal fake pool/conn carries the create path as far as the
DDL call, which is stubbed so nothing touches PG.
"""
from __future__ import annotations

import uuid

import pytest

from app.exceptions import ValidationError
from app.services import table_service

pytestmark = pytest.mark.asyncio


class _AsyncCtx:
    """Async context manager yielding a fixed value — covers both shapes
    create_table enters: ``pool.acquire()`` (→ conn) and
    ``conn.transaction()`` (→ value unused)."""

    def __init__(self, value=None):
        self._value = value

    async def __aenter__(self):
        return self._value

    async def __aexit__(self, *exc):
        return False


class _FakeConn:
    def __init__(self, vault_name: str):
        self._vault_name = vault_name

    def transaction(self, **kwargs):
        # create_table pins isolation="read_committed"
        return _AsyncCtx()

    async def fetchrow(self, sql: str, *params):
        if "FROM vaults" in sql:
            return {"name": self._vault_name}
        # find_by_name's "FROM vault_tables" lookup → no existing table,
        # so create_table proceeds to the length guard (no ConflictError).
        return None

    async def fetchval(self, sql: str, *params):
        # Physical-name fusion preflight (issue #285) → name is free, so
        # create_table proceeds past it to the DDL stage these tests probe.
        return False


class _FakePool:
    def __init__(self, conn):
        self._conn = conn

    def acquire(self):
        return _AsyncCtx(self._conn)


async def test_create_table_fits_an_overlong_pair_instead_of_refusing(monkeypatch):
    """The exact E08 trigger, which used to be a 422: vault(27) +
    table(32) composes to 64 characters. It now reaches DDL under a name
    that fits, because refusing left the table uncreatable and that is
    the failure this surface actually has."""
    vault_name = "prod-conc-1780908249-8ml717"        # 27 chars
    table_name = "report_metrics_1780908249_8ml717"   # 32 chars

    fitted = table_service.table_data_repo.pg_table_name(vault_name, table_name)
    assert len(fitted) <= table_service.table_data_repo.PG_IDENT_MAX_LEN

    async def _fake_get_pool():
        return _FakePool(_FakeConn(vault_name))

    monkeypatch.setattr(table_service, "get_pool", _fake_get_pool)

    reached = {}

    async def _reached_then_stop(conn, pg_name, *a, **k):
        reached["pg_name"] = pg_name
        raise RuntimeError("stop after naming")

    monkeypatch.setattr(table_service.table_data_repo, "create_dynamic_table", _reached_then_stop)

    with pytest.raises(RuntimeError, match="stop after naming"):
        await table_service.create_table(
            uuid.uuid4(),
            table_name,
            [{"name": "amount", "type": "int"}],
            actor_id="tester",
        )

    # The DDL is reached, and with the fitted name rather than the raw one.
    assert reached.get("pg_name") == fitted
    assert len(reached["pg_name"]) <= table_service.table_data_repo.PG_IDENT_MAX_LEN


async def test_create_table_accepts_pg_name_at_limit(monkeypatch):
    """Lower boundary: a 63-byte identifier (vault 27 + table 31) must
    PASS the length guard. Proves the check is `>` not `>=`/off-by-one —
    the math test alone can't catch a guard that rejects a legit 63-char
    name. We stub create_dynamic_table to record it was reached, then
    abort so the fake conn never has to carry the full DDL."""
    vault_name = "prod-conc-1780908249-8ml717"        # 27 chars
    table_name = "report_metrics_1780908249_8ml71"    # 31 chars → 3+27+2+31 = 63
    assert len(table_service.table_data_repo.pg_table_name(vault_name, table_name)) == 63

    async def _fake_get_pool():
        return _FakePool(_FakeConn(vault_name))

    monkeypatch.setattr(table_service, "get_pool", _fake_get_pool)

    reached = {}

    async def _reached_then_stop(*a, **k):
        reached["ddl"] = True  # guard let the 63-char name through
        raise RuntimeError("stop after guard")

    monkeypatch.setattr(table_service.table_data_repo, "create_dynamic_table", _reached_then_stop)

    with pytest.raises(RuntimeError, match="stop after guard"):
        await table_service.create_table(
            uuid.uuid4(),
            table_name,
            [{"name": "amount", "type": "int"}],
            actor_id="tester",
        )
    assert reached.get("ddl"), "the 63-char name was wrongly rejected by the length guard"


async def test_create_table_invalid_name_is_422_not_500():
    """The sibling name-shape check raises ValidationError synchronously
    (before `get_pool`), so a malformed name is a clean 422 rather than an
    uncaught ValueError 500 — no pool fixture needed."""
    with pytest.raises(ValidationError) as ei:
        await table_service.create_table(
            uuid.uuid4(),
            "Bad Name!",  # spaces + caps + punctuation
            [{"name": "amount", "type": "int"}],
            actor_id="tester",
        )
    assert ei.value.status_code == 422
