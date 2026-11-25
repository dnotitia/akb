"""Real HTTP contracts for the endpoints used by Kubernetes and monitors."""

import asyncio
from time import monotonic


async def test_probe_payloads(api):
    liveness = await api.get("/livez")
    assert liveness.status_code == 200
    assert liveness.json() == {"status": "alive"}

    readiness = await api.get("/readyz")
    assert readiness.status_code == 200
    ready = readiness.json()
    assert ready["status"] == "ready"
    assert {"pool", "db"} <= ready["detail"].keys()
    vector_status = ready["detail"]["vector_store"]
    assert vector_status == "ok" or vector_status.startswith("degraded:")

    health = await api.get("/health")
    assert health.status_code == 200
    detailed = health.json()
    assert {"vector_store", "metadata_backfill", "external_git"} <= detailed.keys()
    vector_store = detailed["vector_store"]
    assert "upsert" in vector_store["backfill"]
    assert {
        "total_docs", "avgdl", "tokenizer", "vocab_size", "last_recomputed_at"
    } <= vector_store["bm25"].keys()


async def test_liveness_stays_fast_under_ten_concurrent_requests(api):
    async def measure():
        started = monotonic()
        response = await api.get("/livez")
        assert response.status_code == 200
        assert response.json() == {"status": "alive"}
        return monotonic() - started

    elapsed = await asyncio.gather(*(measure() for _ in range(10)))
    assert max(elapsed) < 3.0
