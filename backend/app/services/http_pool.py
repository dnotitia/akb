"""Process-wide HTTP client pool for outbound calls (embedding/rerank/LLM).

Creating an `httpx.AsyncClient` per request pays full TCP+TLS handshake
each time — for the embedding/rerank endpoints (OpenRouter) that adds
hundreds of ms to every search. A single shared client keeps connections
alive across calls.

Per-call `timeout` is still passed at request time (`.post(..., timeout=t)`),
so each caller's existing timeout semantics are preserved.
"""

from __future__ import annotations

import httpx

_client: httpx.AsyncClient | None = None


def get_client() -> httpx.AsyncClient:
    global _client
    if _client is None:
        _client = httpx.AsyncClient(
            timeout=httpx.Timeout(60.0, connect=10.0),
            limits=httpx.Limits(
                max_keepalive_connections=20,
                max_connections=100,
                keepalive_expiry=30.0,
            ),
        )
    return _client


async def close_client() -> None:
    global _client
    if _client is not None:
        await _client.aclose()
        _client = None
