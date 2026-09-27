"""Fixture reset keeps the vocabulary epoch valid without masking production faults."""
import pytest
import asyncpg

from app.services import sparse_encoder
from tests.test_bm25_query_epoch_postgres import _seed
from tests.test_bm25_term_id_compaction_postgres import _installation
from tests.test_e2e_runtime_unit import E2ERuntime, make_config

pytestmark = pytest.mark.asyncio


@pytest.mark.parametrize("initial_epoch", [0, 7, None])
async def test_runtime_reset_preserves_or_restores_only_the_fixture_epoch(monkeypatch, tmp_path, initial_epoch):
    async with _installation(monkeypatch) as install:
        await _seed(install, "vchord")
        async with install.pool.acquire() as conn:
            if initial_epoch is None:
                await conn.execute("DELETE FROM bm25_vocab_epoch")
                # The production reader must still refuse corruption.
                with pytest.raises(RuntimeError, match="bm25_vocab_epoch has no row"):
                    await sparse_encoder._vocabulary_epoch(conn)
            else:
                await conn.execute("UPDATE bm25_vocab_epoch SET epoch=$1", initial_epoch)
        runtime = E2ERuntime(make_config(tmp_path))
        connect = asyncpg.connect

        async def fixture_connection(**_kwargs):
            return await connect(install.dsn)

        with monkeypatch.context() as fixture_patch:
            fixture_patch.setenv("TEST_USERNAME_ENV", "fixture-user")
            fixture_patch.setenv("TEST_PASSWORD_ENV", "fixture-password")
            fixture_patch.setattr(asyncpg, "connect", fixture_connection)
            await runtime._reset_postgres_in_place()
            # Repeated resets must not change the preserved epoch.
            await runtime._reset_postgres_in_place()
        async with install.pool.acquire() as conn:
            assert await conn.fetchval("SELECT count(*) FROM bm25_vocab") == 0
            assert await conn.fetchval("SELECT count(*) FROM vector_index.chunks") == 0
            assert await conn.fetchval("SELECT count(*) FROM bm25_vocab_epoch") == 1
            assert await sparse_encoder._vocabulary_epoch(conn) == (initial_epoch or 0)
        encoded = await sparse_encoder.encode_document_at_epoch("alpha", sparse_shape="vchord")
        assert encoded.indices and encoded.epoch == (initial_epoch or 0)
