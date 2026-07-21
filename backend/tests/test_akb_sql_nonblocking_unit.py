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
from app.services.user_sql_executor import _coerce_rows_yielding, _coerce_value
from app.util.json_encode import encode_json_str, iter_json_chunks

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
    # NaN / ±Infinity have no JSON representation → normalise to null so the
    # response is valid JSON on BOTH the REST and MCP paths (Codex #293 P2).
    assert _coerce_value(float("nan")) is None
    assert _coerce_value(float("inf")) is None
    assert _coerce_value(float("-inf")) is None
    # Finite values pass through untouched.
    assert _coerce_value(3.14) == 3.14
    assert _coerce_value(0.0) == 0.0
    assert _coerce_value(42) == 42


@pytest.mark.asyncio
async def test_coerce_rows_yielding_emits_valid_json_for_non_finite(monkeypatch):
    monkeypatch.setattr(settings, "akb_sql_coerce_batch", 2000)
    rows = [{"x": float("nan"), "y": float("inf"), "z": 1.5}]
    out = await _coerce_rows_yielding(rows)
    assert out == [{"x": None, "y": None, "z": 1.5}]
    # The coerced result serialises to strict, parseable JSON — no NaN tokens.
    body = json.dumps({"items": out})
    assert "NaN" not in body and "Infinity" not in body
    assert json.loads(body) == {"items": [{"x": None, "y": None, "z": 1.5}]}


@pytest.mark.asyncio
async def test_encode_json_str_matches_dumps_yields_and_bounds_chunks():
    # The MCP path can't stream a single JSON-RPC result, so it chunk-encodes
    # into one string via the shared encoder (Codex #293 P1). Imported from the
    # side-effect-free util module (NOT mcp_server.server, whose module-level
    # DocumentService/GitService would touch /data/vaults) so the test is
    # hermetic (Codex re-review P2). Output must stay byte-for-value identical
    # to the json.dumps it replaced.
    import asyncio

    obj = {
        "kind": "table_query",
        "items": [{"n": i, "s": f"v{i}"} for i in range(3000)],
        "total": 3000,
    }
    assert await encode_json_str(obj, default=str) == json.dumps(
        obj, ensure_ascii=False, default=str
    )

    # Fragments are coalesced into BOUNDED chunks (not one giant string, and not
    # millions of tiny fragments held at once) — several ~flush_bytes chunks.
    chunks = [c async for c in iter_json_chunks(obj, default=str, flush_bytes=4096)]
    assert len(chunks) > 1
    assert all(isinstance(c, str) for c in chunks)
    # No chunk wildly exceeds the flush size (bounded buffering).
    assert max(len(c) for c in chunks) < 4096 * 4

    # And it actually yields to the loop during a large encode.
    ticks = 0
    stop = False

    async def _ticker():
        nonlocal ticks
        while not stop:
            await asyncio.sleep(0)
            ticks += 1

    t = asyncio.create_task(_ticker())
    await encode_json_str(obj, default=str, yield_every=50)
    during = ticks
    stop = True
    await t
    assert during >= 1


@pytest.mark.asyncio
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
