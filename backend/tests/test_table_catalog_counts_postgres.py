"""A schema-only catalog must not scan data; default counts stay exact."""

from datetime import datetime, timezone
from unittest.mock import AsyncMock
import uuid

import pytest

from app.repositories import table_data_repo, table_registry_repo
from app.db.postgres import _load_migration
from app.services import table_service
from tests.test_event_tail_postgres import _fresh_database

pytestmark = pytest.mark.asyncio


@pytest.mark.parametrize("counted", [None, True, False])
async def test_catalog_counts_are_explicit_without_changing_default(monkeypatch, counted):
    async with _fresh_database() as pool:
        monkeypatch.setattr(table_service, "get_pool", AsyncMock(return_value=pool))
        async with pool.acquire() as conn:
            vault = await conn.fetchval(
                "INSERT INTO vaults(name,git_path) VALUES('catalog-count','/tmp/catalog-count.git') RETURNING id"
            )
            for number in range(2):
                name = f"records_{number}"
                physical = table_data_repo.pg_table_name("catalog-count", name)
                await table_registry_repo.insert(
                    conn, table_id=uuid.uuid4(), vault_id=vault, name=name,
                    description="Catalog schema", columns=[{"name": "title", "type": "text"}],
                    created_by=None, now=datetime.now(timezone.utc),
                )
                await table_data_repo.create_dynamic_table(
                    conn, physical, [{"name": "title", "type": "text"}],
                    vault_id=vault, resource_uri=f"akb://catalog-count/tbl/{name}",
                )
                await conn.execute(f"INSERT INTO {physical}(title) SELECT 'Row ' || n FROM generate_series(1,$1) n", number + 2)
        count = AsyncMock(wraps=table_data_repo.count_rows)
        monkeypatch.setattr(table_data_repo, "count_rows", count)
        options = {} if counted is None else {"include_row_counts": counted}
        catalog = await table_service.list_tables(vault, **options)
        assert [item["name"] for item in catalog] == ["records_0", "records_1"]
        assert all(item["columns"] == [{"name": "title", "type": "text"}] for item in catalog)
        if counted is False:
            assert all("row_count" not in item for item in catalog)
            count.assert_not_awaited()
        else:
            assert [item["row_count"] for item in catalog] == [2, 3]
            assert count.await_count == 2


async def test_existing_and_new_tables_share_default_page_index(monkeypatch):
    async with _fresh_database() as pool, pool.acquire() as conn:
        vault = await conn.fetchval(
            "INSERT INTO vaults(name,git_path) VALUES('page-index','/tmp/page-index.git') RETURNING id"
        )
        physical = table_data_repo.pg_table_name("page-index", "records")
        await table_registry_repo.insert(
            conn, table_id=uuid.uuid4(), vault_id=vault, name="records", description="",
            columns=[{"name": "title", "type": "text"}], created_by=None, now=datetime.now(timezone.utc),
        )
        await table_data_repo.create_dynamic_table(
            conn, physical, [{"name": "title", "type": "text"}],
            vault_id=vault, resource_uri="akb://page-index/tbl/records",
        )
        index = table_data_repo.generate_constraint_name(physical, ["created_at", "id"], kind="page_idx")
        async def definition():
            return await conn.fetchval("SELECT pg_get_indexdef(to_regclass($1))", index)
        assert "(created_at DESC, id DESC)" in await definition()
        await conn.execute(f"DROP INDEX {index}")
        migration = _load_migration("118_table_page_order.py")
        await migration.migrate(conn)
        await migration.migrate(conn)
        assert "(created_at DESC, id DESC)" in await definition()
        # UUID is the tie-breaker for rows committed at the same timestamp.
        ids = sorted([uuid.uuid4() for _ in range(3)], reverse=True)
        for row_id in ids:
            await conn.execute(f"INSERT INTO {physical}(id,title,created_at) VALUES($1,'same','2026-01-01T00:00:00Z')", row_id)
        rows = await conn.fetch(f"SELECT id FROM {physical} ORDER BY created_at DESC,id DESC LIMIT 2")
        assert [row["id"] for row in rows] == ids[:2]
