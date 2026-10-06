"""Bulk reads behind a full-vault browse: one query per listing, same answers."""
import uuid

import pytest

from app.repositories.table_data_repo import EXACT_ROW_COUNT_LIMIT, row_counts
from app.services.m1_pg_body_store import M1PgBodyStore, PgBodyIntegrityError
from app.services.native_revision_service import NativeRevisionService
from tests.test_event_tail_postgres import _fresh_database, _make_vault

pytestmark = pytest.mark.asyncio


async def test_live_head_snapshots_match_per_resource_reads_and_fail_closed():
    async with _fresh_database() as pool:
        vault = await _make_vault(pool, "bulk-heads")
        native = NativeRevisionService(pool, payload_store=M1PgBodyStore(pool))
        common = dict(namespace_id=vault, surface="document", actor="writer")
        first = await native.create_text(**common, path="a/one.md", payload="one", mutation_id=uuid.uuid4())
        await native.create_text(**common, path="a/b/two.md", payload="two", mutation_id=uuid.uuid4())
        await native.replace_text(
            **common, path=first.path, payload="one v2", mutation_id=uuid.uuid4(),
            expected_revision_id=first.revision_id, expected_resource_id=first.resource_id,
        )

        bulk = await native.list_live_head_snapshots(namespace_id=vault, surface="document")
        single = [
            await native.get_resource_revision(
                namespace_id=vault, surface="document",
                resource_id=s.resource_id, revision_id=s.revision_id,
            )
            for s in bulk
        ]
        assert bulk == single
        assert sorted(s.text for s in bulk) == ["one v2", "two"]

        shallow = await native.list_live_head_snapshots(
            namespace_id=vault, surface="document", path_like="a/%", max_slashes=1,
        )
        assert [s.path for s in shallow] == ["a/one.md"]

        # PostgreSQL constraints keep stored bytes consistent, so corrupt a
        # fetched row to prove the bulk path still verifies each body.
        rows = await native.repository.list_live_heads_with_payloads(
            namespace_id=vault, surface="document",
        )
        for row in rows:
            row["path"] = row["path_at_revision"]
        rows[0]["payload_canonical_bytes"] = b"x" * rows[0]["payload_byte_size"]
        stores = [native._read_store(row["selected_placement"]) for row in rows]
        with pytest.raises(PgBodyIntegrityError, match="digest"):
            NativeRevisionService._verify_head_rows(rows, stores)


async def test_row_counts_are_exact_for_small_tables_and_estimated_for_large():
    async with _fresh_database() as pool, pool.acquire() as conn:
        await conn.execute("CREATE TABLE vt_small__t (v int)")
        await conn.execute("CREATE TABLE vt_large__t (v int)")
        await conn.execute("INSERT INTO vt_small__t SELECT generate_series(1, 3)")
        await conn.execute(
            "INSERT INTO vt_large__t SELECT generate_series(1, $1)", EXACT_ROW_COUNT_LIMIT * 3,
        )
        await conn.execute("ANALYZE vt_large__t")
        # An exact count would now say 0; the estimate still says 30,000.
        await conn.execute("DELETE FROM vt_large__t")

        counts = await row_counts(conn, ["vt_small__t", "vt_large__t", "vt_missing__t"])

        assert counts == {"vt_small__t": 3, "vt_large__t": EXACT_ROW_COUNT_LIMIT * 3}
