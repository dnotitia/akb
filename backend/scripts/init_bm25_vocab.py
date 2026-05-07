"""One-shot / periodic: rebuild BM25 vocab + corpus stats from all chunks.

Run after adding many documents, changing tokenizer version, or when
`bm25_stats` is empty (first bootstrap). Idempotent and safe.

Example (local / docker-compose):
    python -m scripts.init_bm25_vocab
    docker compose exec backend python -m scripts.init_bm25_vocab

Example (Kubernetes):
    kubectl exec -n <namespace> deploy/backend -- python -m scripts.init_bm25_vocab
"""

from __future__ import annotations

import asyncio
import logging
import sys
from pathlib import Path

# Ensure backend/ is on path when invoked directly
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.db.postgres import close_pool, init_db
from app.services import sparse_encoder


async def _main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    log = logging.getLogger("init_bm25_vocab")
    log.info("Initializing / refreshing BM25 vocabulary and corpus stats…")

    await init_db()
    try:
        result = await sparse_encoder.recompute_stats()
        log.info("Done: %s", result)
    finally:
        await close_pool()
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(_main()))
