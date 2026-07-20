"""Regression: akb_sql caps the returned row count (event-loop stall).

An unbounded SELECT result is `_coerce_row`d + JSON-serialised on the single
event loop with NO await, so a huge result stalls /livez (~3s per 1M rows
measured) → liveness-probe timeout → 503 (2026-07-20 event-loop audit).
`UserSqlExecutor.execute` caps the returned rows to
`settings.akb_sql_max_rows` and sets `truncated`. Statement execution is
unchanged — only the returned rows are capped.

DB-free: a fake pool/conn whose `fetch` returns N synthetic rows.
"""

import pytest

from app.config import settings
from app.services.user_sql_executor import UserSqlExecutor

pytestmark = pytest.mark.asyncio


class _ACtx:
    def __init__(self, value=None):
        self._value = value

    async def __aenter__(self):
        return self._value

    async def __aexit__(self, *exc):
        return False


class _FakeConn:
    def __init__(self, nrows: int):
        self._nrows = nrows

    def transaction(self):
        return _ACtx()

    async def execute(self, *a, **k):
        return "SET"

    async def fetch(self, sql, *a):
        return [{"n": i} for i in range(self._nrows)]


class _FakePool:
    def __init__(self, conn):
        self._conn = conn

    def acquire(self):
        return _ACtx(self._conn)


async def _run(nrows: int) -> dict:
    ex = UserSqlExecutor(_FakePool(_FakeConn(nrows)))
    # is_admin=True skips the SET LOCAL ROLE role lookup; fetch=True forces the
    # SELECT (row-returning) path regardless of the SQL text.
    return await ex.execute(
        user_id="00000000-0000-0000-0000-000000000000",
        sql="SELECT n FROM t",
        fetch=True,
        is_admin=True,
        vault_names=["v"],
    )


async def test_akb_sql_caps_oversized_result_and_flags_truncated(monkeypatch):
    monkeypatch.setattr(settings, "akb_sql_max_rows", 100)
    out = await _run(250)
    assert out["truncated"] is True
    assert out["total"] == 100
    assert len(out["items"]) == 100


async def test_akb_sql_under_cap_is_not_truncated(monkeypatch):
    monkeypatch.setattr(settings, "akb_sql_max_rows", 100)
    out = await _run(5)
    assert out["truncated"] is False
    assert out["total"] == 5
    assert len(out["items"]) == 5


async def test_akb_sql_exactly_at_cap_is_not_truncated(monkeypatch):
    monkeypatch.setattr(settings, "akb_sql_max_rows", 100)
    out = await _run(100)
    assert out["truncated"] is False
    assert out["total"] == 100
