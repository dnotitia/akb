"""Upgrade and isolation contract for the measurement-only placement migration."""

from __future__ import annotations

import asyncio
import importlib.util
import os
import uuid
from collections.abc import AsyncIterator
from pathlib import Path

import asyncpg
import pytest
import pytest_asyncio

from app.services.m1_pg_body_store import M1PgBodyStore
from app.services.m1_reference_payload_store import M1ReferencePayloadStore
from app.services.native_revision_service import NativeRevisionService
from tests.helpers.pg_database import TestDatabase, database_template, temporary_database


BACKEND = Path(__file__).resolve().parents[1]
INIT_SQL = (BACKEND / "app" / "db" / "init.sql").read_text(encoding="utf-8")
MIGRATIONS = BACKEND / "app" / "db" / "migrations"
DSN = os.environ.get(
    "AKB_TEST_DSN",
    "postgresql://akb:akb@localhost:5433/akb",  # pragma: allowlist secret
)


def _migration(filename: str):
    path = MIGRATIONS / filename
    name = "test_m1_placement_" + filename.replace(".", "_")
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


async def _initialize_pre_057_schema(conn: asyncpg.Connection) -> None:
    await conn.execute(INIT_SQL)
    await _migration("015_events_outbox.py").migrate(conn=conn)
    await _migration("048_native_revision_core.py").migrate(conn=conn)
    await _migration("053_native_revision_m1_pg_body.py").migrate(conn=conn)
    await _migration("107_native_document_asset_refs.py").migrate(conn=conn)


@pytest_asyncio.fixture(scope="module", loop_scope="module")
async def pre_057_template() -> AsyncIterator[TestDatabase]:
    async with database_template(DSN, "akb_pre_057_", _initialize_pre_057_schema) as template:
        yield template


@pytest_asyncio.fixture
async def measurement_dsn(pre_057_template: TestDatabase) -> AsyncIterator[str]:
    async with temporary_database(
        DSN, "akb_revision_m1_measurement_upgrade_", template=pre_057_template,
    ) as database:
        yield database.dsn


@pytest_asyncio.fixture
async def non_measurement_dsn(pre_057_template: TestDatabase) -> AsyncIterator[str]:
    async with temporary_database(
        DSN, "akb_non_measurement_guard_", template=pre_057_template,
    ) as database:
        yield database.dsn


@pytest.mark.asyncio
async def test_seeded_pre_057_database_upgrades_idempotently_and_deduplicates_per_placement(measurement_dsn):
    pool = None
    try:
        pool = await asyncpg.create_pool(measurement_dsn, min_size=1, max_size=16)
        async with pool.acquire() as conn:
            vault_id = await conn.fetchval(
                "INSERT INTO vaults (name, git_path) VALUES ($1, $2) RETURNING id",
                f"m1-placement-{uuid.uuid4().hex}",
                "/tmp/m1-placement-unused.git",
            )

        shared_body = "placement-scoped seeded body\n"
        reference_store = M1ReferencePayloadStore(pool)
        reference_service = NativeRevisionService(pool, payload_store=reference_store)
        seeded = await reference_service.create_text(
            namespace_id=vault_id,
            surface="document",
            path="seeded.md",
            payload=shared_body,
            actor="m1-placement-test",
            mutation_id=uuid.uuid4(),
        )
        seeded_payload = await reference_store.prepare_text(
            namespace_id=vault_id,
            payload=shared_body,
        )
        assert seeded.payload_manifest_id is not None

        await pool.close()
        pool = None
        conn = await asyncpg.connect(measurement_dsn)
        try:
            placement = _migration("057_native_revision_m1_payload_placement.py")
            await placement.migrate(conn=conn)
            await placement.migrate(conn=conn)
        finally:
            await conn.close()

        pool = await asyncpg.create_pool(measurement_dsn, min_size=1, max_size=16)
        reference_store = M1ReferencePayloadStore(pool)
        pg_store = M1PgBodyStore(pool)

        snapshot = await NativeRevisionService(
            pool,
            payload_store=reference_store,
        ).get_resource_revision(
            namespace_id=vault_id,
            surface="document",
            resource_id=seeded.resource_id,
            revision_id=seeded.revision_id,
        )
        assert snapshot.text == shared_body
        assert snapshot.payload_manifest_id == seeded.payload_manifest_id
        assert snapshot.selected_placement == M1ReferencePayloadStore.selected_placement

        reference_results, pg_results = await asyncio.gather(
            asyncio.gather(*(
                reference_store.prepare_text(namespace_id=vault_id, payload=shared_body)
                for _ in range(12)
            )),
            asyncio.gather(*(
                pg_store.prepare_text(namespace_id=vault_id, payload=shared_body)
                for _ in range(12)
            )),
        )
        reference_ids = {result.payload_id for result in reference_results}
        pg_ids = {result.payload_id for result in pg_results}
        assert reference_ids == {seeded_payload.payload_id}
        assert len(pg_ids) == 1
        assert reference_ids.isdisjoint(pg_ids)

        async with pool.acquire() as conn:
            placements = await conn.fetch(
                """
                SELECT selected_placement, count(*)::int AS payloads
                  FROM m1_reference_payloads
                 WHERE namespace_id = $1
                 GROUP BY selected_placement
                 ORDER BY selected_placement
                """,
                vault_id,
            )
            constraint_is_valid = await conn.fetchval(
                """
                SELECT convalidated
                  FROM pg_constraint
                 WHERE conrelid = 'native_payload_manifests'::regclass
                   AND conname = 'native_payload_manifests_reference_fkey'
                """
            )
        assert [(row["selected_placement"], row["payloads"]) for row in placements] == [
            (M1ReferencePayloadStore.selected_placement, 1),
            (M1PgBodyStore.selected_placement, 1),
        ]
        assert constraint_is_valid is True
    finally:
        if pool is not None:
            await pool.close()


@pytest.mark.asyncio
async def test_placement_migration_leaves_non_measurement_database_unchanged(non_measurement_dsn):
    conn = await asyncpg.connect(non_measurement_dsn)
    try:
        before = await conn.fetchval(
            """
            SELECT pg_get_constraintdef(oid)
              FROM pg_constraint
             WHERE conrelid = 'm1_reference_payloads'::regclass
               AND conname = 'm1_reference_payloads_dedup_key'
            """
        )
        await _migration("057_native_revision_m1_payload_placement.py").migrate(conn=conn)
        after = await conn.fetchval(
            """
            SELECT pg_get_constraintdef(oid)
              FROM pg_constraint
             WHERE conrelid = 'm1_reference_payloads'::regclass
               AND conname = 'm1_reference_payloads_dedup_key'
            """
        )
        trigger_exists = await conn.fetchval(
            """
            SELECT EXISTS (
                SELECT 1
                  FROM pg_trigger
                 WHERE tgrelid = 'm1_reference_payloads'::regclass
                   AND tgname = 'trg_m1_namespace_payload_placement'
                   AND NOT tgisinternal
            )
            """
        )
    finally:
        await conn.close()

    assert before == after
    assert "namespace_id, digest, byte_size" in after
    assert "selected_placement" not in after
    assert trigger_exists is True
