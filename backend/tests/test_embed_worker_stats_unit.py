from __future__ import annotations

import pytest

from app.services import delete_worker, embed_worker


@pytest.mark.asyncio
async def test_pending_stats_releases_chunk_connection_before_delete_stats(monkeypatch):
    active_connections = 0

    class FakeConn:
        async def fetchrow(self, *_args, **_kwargs):
            return {
                "pending": 1,
                "retrying": 2,
                "abandoned": 3,
                "indexed": 4,
            }

    class FakeAcquire:
        async def __aenter__(self):
            nonlocal active_connections
            active_connections += 1
            return FakeConn()

        async def __aexit__(self, *_exc):
            nonlocal active_connections
            active_connections -= 1

    class FakePool:
        def acquire(self):
            return FakeAcquire()

    async def fake_get_pool():
        return FakePool()

    async def fake_delete_outbox_stats():
        assert active_connections == 0
        return {"pending": 5, "abandoned": 6}

    monkeypatch.setattr(embed_worker, "get_pool", fake_get_pool)
    monkeypatch.setattr(delete_worker, "delete_outbox_stats", fake_delete_outbox_stats)

    assert await embed_worker.pending_stats() == {
        "upsert": {
            "pending": 1,
            "retrying": 2,
            "abandoned": 3,
            "indexed": 4,
        },
        "delete": {"pending": 5, "abandoned": 6},
    }
