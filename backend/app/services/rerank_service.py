"""Reranker service — cross-encoder re-scoring of hybrid search candidates.

Provider-agnostic. Currently ships one backend: `cohere/rerank-v3.5` via
OpenRouter's OpenAI-style `/rerank` endpoint. Same API key as LLM/embedding
path, 100+ languages including Korean, charged per search (not per token).

The interface returns `(original_index, relevance_score)` tuples sorted
descending. Callers reorder their candidate list with these.

Any HTTP or parsing failure raises `RerankError` (an `AKBError`). Callers
in the search path catch it and fall back to the pre-rerank RRF order.
"""
from __future__ import annotations

import logging
from typing import Sequence

import httpx

from app.config import settings
from app.exceptions import AKBError
from app.services import http_pool

logger = logging.getLogger("akb.rerank")


class RerankError(AKBError):
    def __init__(self, message: str):
        super().__init__(message, status_code=502)


async def rerank(
    query: str,
    documents: Sequence[str],
    top_n: int | None = None,
) -> list[tuple[int, float]]:
    """Score (query, doc) pairs and return `(original_index, score)` desc."""
    if not documents:
        return []
    if not settings.rerank_enabled:
        raise RerankError("rerank disabled (settings.rerank_enabled=False)")
    if settings.rerank_provider != "cohere":
        raise RerankError(f"unsupported rerank provider: {settings.rerank_provider}")

    base_url = (settings.rerank_base_url or settings.llm_base_url).rstrip("/")
    api_key = settings.rerank_api_key or settings.llm_api_key
    if not api_key:
        raise RerankError("no API key configured (rerank_api_key / llm_api_key)")

    payload: dict[str, object] = {
        "model": settings.rerank_model,
        "query": query,
        "documents": list(documents),
    }
    if top_n is not None:
        payload["top_n"] = int(top_n)

    client = http_pool.get_client()
    try:
        resp = await client.post(
            f"{base_url}/rerank",
            json=payload,
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=settings.rerank_timeout_seconds,
        )
        resp.raise_for_status()
        body = resp.json()
    except (httpx.ConnectError, httpx.HTTPStatusError,
            httpx.TimeoutException, httpx.UnsupportedProtocol) as e:
        raise RerankError(f"rerank HTTP call failed: {e}") from e

    results = body.get("results")
    if not isinstance(results, list):
        raise RerankError(f"unexpected rerank response shape: keys={list(body)[:5]}")

    out: list[tuple[int, float]] = []
    for item in results:
        try:
            out.append((int(item["index"]), float(item["relevance_score"])))
        except (KeyError, TypeError, ValueError) as e:
            raise RerankError(f"malformed rerank item {item!r}: {e}") from e
    return out
