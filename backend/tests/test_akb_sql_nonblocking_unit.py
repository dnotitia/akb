"""Regression: akb_sql must not block the event loop on a large result set.

The 2026-07-20 audit measured a single unbounded SELECT (`SELECT * FROM
generate_series(1, 1e6)`) stalling /livez ~3s → probe-timeout 503, from the
per-row coercion (`_coerce_rows_yielding`, pure-Python CPU) and the final JSON
serialisation (`_stream_json`, FastAPI). The fix chunks BOTH with periodic
`await asyncio.sleep(0)`, so the loop stays responsive — WITHOUT truncating the
result (akb_sql is arbitrary SQL; callers bound their own rows via LIMIT).

DB-free. Runs in `pytest -k 'not _e2e'`.
"""

import json

import pytest

from app.config import settings
from app.api.routes.tables import _stream_json
from app.services.user_sql_executor import _coerce_rows_yielding

pytestmark = pytest.mark.asyncio


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


async def test_coerce_small_result_fast_path(monkeypatch):
    monkeypatch.setattr(settings, "akb_sql_coerce_batch", 2000)
    rows = [{"x": i} for i in range(5)]
    out = await _coerce_rows_yielding(rows)
    assert out == [{"x": i} for i in range(5)]


async def test_stream_json_emits_one_valid_document_equal_to_dumps():
    obj = {
        "kind": "table_query",
        "vaults": ["v"],
        "columns": ["x"],
        "items": [{"x": i} for i in range(3000)],
        "total": 3000,
    }
    chunks: list[bytes] = []
    async for c in _stream_json(obj, flush_bytes=512, yield_every=50):
        assert isinstance(c, bytes)
        chunks.append(c)
    body = b"".join(chunks).decode("utf-8")
    # Streamed output is ONE ordinary JSON document, byte-for-value identical.
    assert json.loads(body) == obj
    # And it was emitted in multiple chunks (streaming, not one blob).
    assert len(chunks) > 1
