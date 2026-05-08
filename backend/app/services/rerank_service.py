"""Reranker service — cross-encoder re-scoring of hybrid search candidates.

Provider-agnostic over any endpoint that speaks the Cohere `/rerank`
schema (`{model, query, documents[, top_n]}` → `{results: [{index,
relevance_score}]}`). Tested setups:

- `cohere/rerank-v3.5` via OpenRouter — multilingual (100+ langs incl.
  Korean), lightweight, charged per search. Default in `app.yaml`.
- Self-hosted open-weight rerankers (e.g. BAAI/bge-reranker-v2-m3,
  jinaai/jina-reranker-v2-base-multilingual) behind a Cohere-compatible
  shim (vLLM, Hugging Face TEI, sentence-transformers + a thin FastAPI
  wrapper). Set `rerank_base_url` to the on-prem endpoint and
  `rerank_model` to its model id.
- Jina hosted API at `https://api.jina.ai/v1` — same Cohere-compatible
  shape; set `rerank_base_url` + `rerank_api_key`.

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
    # `rerank_provider` is a label for logs/diagnostics. Dispatch is
    # always Cohere-compatible — operators point `rerank_base_url` at
    # whichever endpoint speaks that schema.

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

    # OpenRouter's Cohere route occasionally returns HTTP 200 with the
    # body shaped as `{"error": {...}}` (rate-limit, transient upstream
    # 5xx flattened to 200, etc.). Surface those as RerankError with the
    # actual upstream message instead of the generic "unexpected shape".
    err = body.get("error")
    if err is not None:
        msg = err.get("message") if isinstance(err, dict) else err
        raise RerankError(f"upstream rerank error: {msg}")

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
