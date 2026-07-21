"""Non-blocking JSON encoding — chunked `iterencode` with loop yields.

A large payload (e.g. a million-row `akb_sql` result) handed to one synchronous
`json.dumps` holds the single event loop for seconds → `/livez` probe timeout →
503. These helpers drive `JSONEncoder.iterencode` and `await asyncio.sleep(0)`
between fragments so the loop stays responsive, while **coalescing fragments
into bounded chunks** so the encode never retains millions of tiny string
objects (an OOM risk on the exact large result it aims to protect).

Side-effect-free module — safe to import from anywhere (tests included) without
constructing services. The REST SQL route streams the byte chunks over the
wire; the MCP tool encoder joins them into the single JSON-RPC string it must
return.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any, AsyncIterator


async def iter_json_chunks(
    obj: Any,
    *,
    compact: bool = False,
    default: Any = None,
    flush_bytes: int = 65536,
    yield_every: int = 1000,
) -> AsyncIterator[str]:
    """Yield `obj` as JSON in ~`flush_bytes` string chunks.

    `iterencode` emits many tiny fragments; we coalesce them into `flush_bytes`
    chunks (so at most one flush-window of fragments is held at once) and
    `await asyncio.sleep(0)` every `yield_every` fragments so the loop can run
    other tasks (health probes, other requests) mid-encode.

    `compact=True` uses `(",", ":")` separators (matches Starlette's default
    `JSONResponse`); `compact=False` keeps `json.dumps` default separators.
    `default` is the fallback serialiser (e.g. `str`), as in `json.dumps`.
    """
    separators = (",", ":") if compact else None
    encoder = json.JSONEncoder(
        ensure_ascii=False, separators=separators, default=default
    )
    buf: list[str] = []
    size = 0
    n = 0
    for piece in encoder.iterencode(obj):
        buf.append(piece)
        size += len(piece)
        n += 1
        if size >= flush_bytes:
            yield "".join(buf)
            buf.clear()
            size = 0
        if n % yield_every == 0:
            await asyncio.sleep(0)
    if buf:
        yield "".join(buf)


async def encode_json_str(obj: Any, *, default: Any = None, **kwargs: Any) -> str:
    """Full JSON string built from bounded chunks without blocking the loop.

    For consumers that need one payload in hand (an MCP tool result → a single
    JSON-RPC message). Holds only the bounded chunk list — not the millions of
    tiny `iterencode` fragments — plus the final joined string.
    """
    chunks: list[str] = []
    async for chunk in iter_json_chunks(obj, default=default, **kwargs):
        chunks.append(chunk)
    return "".join(chunks)
