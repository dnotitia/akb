"""Upgrade and isolation contract for the measurement-only placement migration."""

from __future__ import annotations

import asyncio
import importlib.util
import os
from pathlib import Path
import uuid

import asyncpg
import pytest

from app.services.m1_pg_body_store import M1PgBodyStore
from app.services.m1_reference_payload_store import M1ReferencePayloadStore
from app.services.native_revision_service import NativeRevisionService


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


async def _reachable() -> bool:
    try:
        conn = await asyncpg.connect(DSN, timeout=2)
    except (OSError, asyncpg.PostgresError):
        return False
    await conn.close()
    return True


def _database_dsn(name: str) -> str:
    base, _ = DSN.rsplit("/", 1)
    return f"{base}/{name}"


async def _create_database(admin: asyncpg.Connection, name: str) -> str:
    await admin.execute(f'CREATE DATABASE "{name}"')
    dsn = _database_dsn(name)
    conn = await asyncpg.connect(dsn)
    try:
        await conn.execute(INIT_SQL)
        await _migration("048_native_revision_core.py").migrate(conn=conn)
        await _migration("049_native_revision_m1_pg_body.py").migrate(conn=conn)
    finally:
        await conn.close()
    return dsn


@pytest.mark.asyncio
async def test_seeded_049_database_upgrades_idempotently_and_deduplicates_per_placement():
    if not await _reachable():
        pytest.skip("Postgres is not reachable for the M1 placement upgrade test")

    name = f"akb_revision_m1_measurement_upgrade_{uuid.uuid4().hex[:10]}"
    admin = await asyncpg.connect(DSN)
    pool = None
    try:
        dsn = await _create_database(admin, name)
        pool = await asyncpg.create_pool(dsn, min_size=1, max_size=16)
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
        conn = await asyncpg.connect(dsn)
        try:
            placement = _migration("053_native_revision_m1_payload_placement.py")
            await placement.migrate(conn=conn)
            await placement.migrate(conn=conn)
        finally:
            await conn.close()

        pool = await asyncpg.create_pool(dsn, min_size=1, max_size=16)
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
        await admin.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
        await admin.close()


@pytest.mark.asyncio
async def test_placement_migration_leaves_non_measurement_database_unchanged():
    if not await _reachable():
        pytest.skip("Postgres is not reachable for the M1 placement guard test")

    name = f"akb_non_measurement_guard_{uuid.uuid4().hex[:10]}"
    admin = await asyncpg.connect(DSN)
    try:
        dsn = await _create_database(admin, name)
        conn = await asyncpg.connect(dsn)
        try:
            before = await conn.fetchval(
                """
                SELECT pg_get_constraintdef(oid)
                  FROM pg_constraint
                 WHERE conrelid = 'm1_reference_payloads'::regclass
                   AND conname = 'm1_reference_payloads_dedup_key'
                """
            )
            await _migration("053_native_revision_m1_payload_placement.py").migrate(conn=conn)
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
    finally:
        await admin.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
        await admin.close()
