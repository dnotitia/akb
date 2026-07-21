"""Regression: akb_sql must not block the event loop on a large result set.

The 2026-07-20 audit measured a single unbounded SELECT (`SELECT * FROM
generate_series(1, 1e6)`) stalling /livez ~3s → probe-timeout 503, from two
pure-Python CPU steps on the single event loop: per-row coercion and JSON
serialisation. Fixes:

  - Coercion (`_coerce_rows_yielding`) runs in `akb_sql_coerce_batch` chunks
    with `await asyncio.sleep(0)` between batches, and normalises non-finite
    floats (NaN/±Inf) to null so the output is valid JSON.
  - Serialisation uses pydantic-core (Rust) `to_json` — one fast pass
    (~7-10x less CPU than driving stdlib `iterencode` fragment-by-fragment in
    Python) whose single loop block stays far under the /livez probe timeout —
    on BOTH the REST route (`tables.execute_sql`) and the MCP tool result
    (`server.call_tool`).

The result is NOT truncated (akb_sql is arbitrary SQL; callers bound their own
rows via LIMIT). DB-free. Runs in `pytest -k 'not _e2e'`.
"""

import json

import pytest
from pydantic_core import to_json

from app.config import settings
from app.services.user_sql_executor import _coerce_rows_yielding, _coerce_value

# Async tests are marked individually (not module-wide) so the one sync test
# (`test_coerce_value_*`) doesn't get a spurious asyncio mark.


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
            import asyncio
            await asyncio.sleep(0)
            ticks += 1

    import asyncio
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


def test_coerce_value_non_finite_floats_become_null():
    # NaN / ±Infinity have no JSON representation. pydantic-core `to_json` (like
    # stdlib) would emit bare NaN/Infinity tokens — invalid JSON that browser
    # JSON.parse rejects — so coercion normalises them to null upstream, on both
    # the REST and MCP paths.
    assert _coerce_value(float("nan")) is None
    assert _coerce_value(float("inf")) is None
    assert _coerce_value(float("-inf")) is None
    # Finite values pass through untouched.
    assert _coerce_value(3.14) == 3.14
    assert _coerce_value(0.0) == 0.0
    assert _coerce_value(42) == 42


@pytest.mark.asyncio
async def test_coerced_envelope_serialises_to_valid_json_via_to_json(monkeypatch):
    # The full serialisation path both transports now use: coerce (incl.
    # NaN→null), then pydantic-core `to_json`. Must be strict, parseable JSON.
    monkeypatch.setattr(settings, "akb_sql_coerce_batch", 2000)
    rows = [{"x": float("nan"), "y": float("inf"), "z": 1.5}, {"x": 2.0, "y": -1.0, "z": 3.0}]
    items = await _coerce_rows_yielding(rows)
    assert items[0] == {"x": None, "y": None, "z": 1.5}

    envelope = {"kind": "table_query", "columns": ["x", "y", "z"], "items": items, "total": len(items)}
    body = to_json(envelope)  # bytes, exactly what REST/MCP emit
    assert isinstance(body, (bytes, bytearray))
    text = body.decode("utf-8")
    # No non-finite tokens leaked; round-trips to the same structure.
    assert "NaN" not in text and "Infinity" not in text
    assert json.loads(text) == envelope
