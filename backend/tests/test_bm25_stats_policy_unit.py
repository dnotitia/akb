"""External statistics remain required until a deployment is VChord-only."""
from unittest.mock import AsyncMock, Mock

import pytest
from pydantic import ValidationError

from app.config import Settings
from app.services import sparse_encoder


@pytest.mark.parametrize("driver,shape,consumer", [
    ("pgvector", "posting", "pgvector/posting"),
    ("pgvector", "arrays", "pgvector/arrays"),
    ("pgvector", "vchord", "posting_rollback_or_mixed_deployment"),
    ("qdrant", "posting", "qdrant"),
    ("seahorse-cloud", "posting", "seahorse-cloud"),
    ("seahorse-db", "posting", "seahorse-db"),
    ("seahorse-db-grpc", "posting", "seahorse-db-grpc"),
])
def test_existing_consumers_keep_external_stats(driver, shape, consumer):
    configured = Settings(document_revision_backend="bare_git", vector_store_driver=driver, vector_store_sparse_shape=shape)
    assert configured.bm25_external_stats_mode == "required"
    assert configured.bm25_external_stats_consumers == [consumer]


@pytest.mark.parametrize("driver,shape", [
    ("pgvector", "posting"), ("pgvector", "arrays"),
    ("qdrant", "vchord"), ("seahorse-cloud", "vchord"),
    ("seahorse-db", "vchord"), ("seahorse-db-grpc", "vchord"),
])
def test_external_stats_opt_out_rejects_a_live_consumer(driver, shape):
    with pytest.raises(ValidationError, match="requires pgvector/vchord"):
        Settings(document_revision_backend="bare_git", vector_store_driver=driver, vector_store_sparse_shape=shape,
                 bm25_external_stats_mode="vchord_only_verified")


@pytest.mark.asyncio
async def test_verified_vchord_only_skips_startup_and_periodic_work(monkeypatch):
    configured = Settings(document_revision_backend="bare_git", vector_store_driver="pgvector", vector_store_sparse_shape="vchord",
                          bm25_external_stats_mode="vchord_only_verified")
    monkeypatch.setattr(sparse_encoder, "settings", configured)
    runner = Mock()
    runner.is_running.return_value = False
    monkeypatch.setattr(sparse_encoder, "_refresher", runner)
    gate, recompute = AsyncMock(), AsyncMock()
    monkeypatch.setattr(sparse_encoder, "_should_recompute", gate)
    monkeypatch.setattr(sparse_encoder, "recompute_stats", recompute)

    sparse_encoder.start_stats_refresher()
    assert await sparse_encoder._refresh_tick() == 0
    runner.start.assert_not_called()
    gate.assert_not_awaited()
    recompute.assert_not_awaited()
    assert sparse_encoder.external_stats_policy_snapshot() == {
        "mode": "vchord_only_verified", "required": False, "consumers": [],
        "refresher_running_in_this_process": False,
    }


@pytest.mark.asyncio
async def test_vchord_with_posting_rollback_still_refreshes(monkeypatch):
    configured = Settings(document_revision_backend="bare_git", vector_store_driver="pgvector", vector_store_sparse_shape="vchord")
    monkeypatch.setattr(sparse_encoder, "settings", configured)
    runner = Mock()
    runner.is_running.return_value = False
    monkeypatch.setattr(sparse_encoder, "_refresher", runner)
    monkeypatch.setattr(sparse_encoder, "_should_recompute", AsyncMock(return_value=True))
    recompute = AsyncMock(return_value={"skipped": False})
    monkeypatch.setattr(sparse_encoder, "recompute_stats", recompute)
    sparse_encoder.start_stats_refresher(120)
    runner.start.assert_called_once()
    runner.configure_idle_secs.assert_called_once_with(120)
    assert await sparse_encoder._refresh_tick() == 0
    recompute.assert_awaited_once_with(defer_if_vector_queue=True)
    assert sparse_encoder.external_stats_policy_snapshot()["required"] is True


@pytest.mark.asyncio
@pytest.mark.parametrize("has_stats", [False, True])
async def test_disabled_health_retains_shared_success_and_progress(monkeypatch, has_stats):
    from datetime import datetime, timezone
    from unittest.mock import MagicMock

    configured = Settings(document_revision_backend="bare_git", vector_store_driver="pgvector", vector_store_sparse_shape="vchord",
                          bm25_external_stats_mode="vchord_only_verified")
    monkeypatch.setattr(sparse_encoder, "settings", configured)
    success = datetime(2026, 1, 1, tzinfo=timezone.utc)
    row = {
        "total_docs": 25, "avgdl": 4, "tokenizer_name": "kiwi",
        "tokenizer_version": "test", "source_revision": 10,
        "source_chunk_count": 25, "updated_at": success,
    } if has_stats else None
    conn = Mock(fetchrow=AsyncMock(return_value=row), fetchval=AsyncMock(return_value=30))
    pool = MagicMock()
    pool.acquire.return_value.__aenter__.return_value = conn
    monkeypatch.setattr(sparse_encoder, "get_pool", AsyncMock(return_value=pool))
    monkeypatch.setattr(sparse_encoder, "_current_corpus_revision", AsyncMock(return_value=15))
    progress = {"chunks_scanned": 12, "documents_counted": 11}
    monkeypatch.setattr(sparse_encoder, "_run_progress", AsyncMock(return_value=progress))
    monkeypatch.setattr(sparse_encoder.bm25_maintenance, "active_bm25_recompute",
                        AsyncMock(return_value=True))
    snapshot = await sparse_encoder.stats_snapshot()
    assert snapshot["external_stats"]["required"] is False
    assert snapshot["last_recomputed_at"] == (success.isoformat() if has_stats else None)
    assert snapshot["recompute_in_flight"] == progress
    assert snapshot["recompute_active"] is True
    assert snapshot["vocab_size"] == 30
