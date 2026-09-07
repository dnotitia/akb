"""Fake OpenAI-compatible /v1/embeddings server for CI.

The shell e2e suite needs the backend to boot and the write path to
exercise indexing without depending on a real embedding provider —
otherwise CI would silently degrade (no key → indexing skipped → some
search tests fail) or require a hosted credential. This stub answers
the /v1/embeddings contract with deterministic fixed-dim vectors so
the indexer pipeline runs end-to-end. Search relevance is meaningless
under this stub (every vector is the same shape), but every test we
gate in CI either skips search or tolerates "0 results" — see
`.github/workflows/e2e.yml` for the CI gate's suite list.

Not for production use.
"""

from __future__ import annotations

from typing import List, Union

from fastapi import FastAPI
from pydantic import BaseModel

DEFAULT_DIM = 1536

app = FastAPI()


class EmbeddingRequest(BaseModel):
    input: Union[str, List[str]]
    model: str | None = None
    dimensions: int | None = None
    encoding_format: str | None = None


@app.post("/v1/embeddings")
async def embeddings(req: EmbeddingRequest) -> dict:
    inputs = req.input if isinstance(req.input, list) else [req.input]
    dim = req.dimensions or DEFAULT_DIM
    # Non-zero first component so the vector has unit-norm > 0 and the
    # cosine math downstream doesn't divide by zero.
    vec = [0.0] * dim
    vec[0] = 1.0
    return {
        "object": "list",
        "data": [
            {"object": "embedding", "embedding": vec, "index": i}
            for i, _ in enumerate(inputs)
        ],
        "model": req.model or "ci-embed-stub",
        "usage": {"prompt_tokens": 0, "total_tokens": 0},
    }


@app.get("/healthz")
async def healthz() -> dict:
    return {"status": "ok"}
