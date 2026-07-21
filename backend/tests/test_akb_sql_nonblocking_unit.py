"""Regression: akb_sql must not block the event loop on a large result set.

The 2026-07-20 audit measured a single unbounded SELECT (`SELECT * FROM
generate_series(1, 1e6)`) stalling /livez ~3s → probe-timeout 503, from two
pure-Python CPU steps on the single event loop: per-row coercion and JSON
serialisation. Fixes:

  - Coercion (`_coerce_rows_yielding`) runs in `akb_sql_coerce_batch` chunks
    with `await asyncio.sleep(0)` between batches so the loop stays responsive.
  - Serialisation uses pydantic-core (Rust) `to_json` — one fast pass
    (~7-10x less CPU than driving stdlib `iterencode` fragment-by-fragment in
    Python) — on BOTH the REST route (`tables.execute_sql`) and the MCP tool
    result (`server.call_tool`), with `inf_nan_mode="null"` so a PG float8
    NaN/±Inf serialises to `null` (valid JSON) instead of a bare NaN token.

The result is NOT truncated (akb_sql is arbitrary SQL; callers bound their own
rows via LIMIT). DB-free. Runs in `pytest -k 'not _e2e'`.
"""

import asyncio
import json

import pytest
from pydantic_core import to_json

from app.config import settings
from app.services.user_sql_executor import _coerce_rows_yielding

# The async tests are marked individually so the one sync test
# (`test_non_finite_floats_serialise_to_null`) doesn't get a spurious mark.


@pytest.mark.asyncio
async def test_coerce_rows_yields_to_loop_and_preserves_all(monkeypatch):
    # Small batch so a modest row count still crosses several batches.
    monkeypatch.setattr(settings, "akb_sql_coerce_batch", 10)
    rows = [{"x": i} for i in range(105)]

    ticks = 0
    stop = False

    async def _ticker():
        nonlocal ticks
        while not stop:
            await asyncio.sleep(0)
            ticks += 1

    t = asyncio.create_task(_ticker())
    out = await _coerce_rows_yielding(rows)
    ticks_during = ticks  # accumulated while the coercion was yielding
    stop = True
    await t

    # No truncation: every row, in order.
    assert len(out) == 105
    assert out[0] == {"x": 0}
    assert out[-1] == {"x": 104}
    # The coercion yielded control back to the loop (ticker ran during it). A
    # revert to a single list comprehension yields zero times → ticks_during 0.
    assert ticks_during >= 1


@pytest.mark.asyncio
async def test_coerce_small_result_fast_path(monkeypatch):
    monkeypatch.setattr(settings, "akb_sql_coerce_batch", 2000)
    rows = [{"x": i} for i in range(5)]
    out = await _coerce_rows_yielding(rows)
    assert out == [{"x": i} for i in range(5)]


def test_non_finite_floats_serialise_to_null():
    # A PG float8 NaN/±Inf has no JSON representation; `to_json`'s default mode
    # emits bare NaN/Infinity — invalid JSON that browser JSON.parse rejects.
    # Both serialisation boundaries (REST + MCP) pass inf_nan_mode="null" so it
    # renders as `null` in the Rust pass; finite floats are untouched.
    body = to_json(
        {"a": float("nan"), "b": float("inf"), "c": float("-inf"), "d": 1.5},
        inf_nan_mode="null",
    ).decode("utf-8")
    assert "NaN" not in body and "Infinity" not in body
    assert json.loads(body) == {"a": None, "b": None, "c": None, "d": 1.5}


@pytest.mark.asyncio
async def test_coerced_envelope_serialises_to_valid_json(monkeypatch):
    # The full path both transports use: coerce rows, then to_json with
    # inf_nan_mode="null". Non-finite floats survive coercion and become null
    # only at serialisation. Must be strict, parseable JSON.
    monkeypatch.setattr(settings, "akb_sql_coerce_batch", 2000)
    rows = [
        {"x": float("nan"), "y": float("inf"), "z": 1.5},
        {"x": 2.0, "y": -1.0, "z": 3.0},
    ]
    items = await _coerce_rows_yielding(rows)
    envelope = {"kind": "table_query", "columns": ["x", "y", "z"], "items": items, "total": len(items)}

    text = to_json(envelope, inf_nan_mode="null").decode("utf-8")  # exactly what REST/MCP emit
    assert "NaN" not in text and "Infinity" not in text
    assert json.loads(text) == {
        "kind": "table_query",
        "columns": ["x", "y", "z"],
        "items": [{"x": None, "y": None, "z": 1.5}, {"x": 2.0, "y": -1.0, "z": 3.0}],
        "total": 2,
    }
