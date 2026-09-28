"""Refuse unsafe VChord revert targets before attempting an index rewrite."""
import uuid

import pytest

from app.services import sparse_encoder
from scripts import compact_bm25_term_ids as compact
from tests.test_bm25_query_epoch_postgres import _seed
from tests.test_bm25_term_id_compaction_postgres import _installation, _state

pytestmark = pytest.mark.asyncio


class _NoTableRewrite:
    """An executable safety boundary: a regression cannot build an unsafe index."""

    def __init__(self, conn):
        self.conn = conn

    def __getattr__(self, name):
        return getattr(self.conn, name)

    async def execute(self, sql, *args, **kwargs):
        if sql.lstrip().upper().startswith("ALTER TABLE"):
            raise AssertionError("unsafe rewrite prevented by regression guard")
        return await self.conn.execute(sql, *args, **kwargs)


@pytest.mark.parametrize("old_id,late_term", [
    (1 << 30, False),  # A previously unindexable term becomes indexable after compaction.
    ((1 << 30) - 1, True),  # A late term would be allocated beyond the old maximum.
    (-1, False),  # Corrupt historical vocabulary must not be restored either.
])
async def test_revert_refuses_out_of_range_targets_without_changing_state(monkeypatch, old_id, late_term):
    async with _installation(monkeypatch) as install:
        await _seed(install, "vchord")
        async with install.pool.acquire() as conn:
            await conn.execute("UPDATE bm25_vocab SET term_id=$1 WHERE term='gamma'", old_id)
            await compact.apply(conn, schema="vector_index", shape="vchord")
        term = "latecomer" if late_term else "gamma"
        encoded = await sparse_encoder.encode_document_at_epoch(term, sparse_shape="vchord")
        async with install.pool.acquire() as conn:
            await install.store.upsert_one(
                conn=conn, chunk_id=str(uuid.UUID(int=3)), source_type="document",
                source_id=str(uuid.UUID(int=3)), vault_id=str(uuid.UUID(int=9)),
                section_path="", content=term, chunk_index=0, dense=None,
                sparse_indices=encoded.indices, sparse_values=encoded.values,
            )
            await conn.execute("VACUUM vector_index.chunks")
            before = await _state(conn)
            mapping = await conn.fetch("SELECT * FROM bm25_term_id_remap ORDER BY term")
            run = await conn.fetch("SELECT * FROM bm25_term_id_remap_run")
            with pytest.raises(compact.Refused):
                await compact.revert(_NoTableRewrite(conn), schema="vector_index", shape="vchord")
            assert await _state(conn) == before
            assert await conn.fetch("SELECT * FROM bm25_term_id_remap ORDER BY term") == mapping
            assert await conn.fetch("SELECT * FROM bm25_term_id_remap_run") == run
