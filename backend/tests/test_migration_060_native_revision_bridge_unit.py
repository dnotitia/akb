"""PostgreSQL contract tests for migration 060's additive C9 substrate."""

from __future__ import annotations

import importlib.util
import os
import uuid
from contextlib import asynccontextmanager
from pathlib import Path

import asyncpg
import pytest

from app.services.native_revision_service import NativeRevisionService


pytestmark = pytest.mark.asyncio

_BACKEND = Path(__file__).resolve().parents[1]
_INIT_SQL = (_BACKEND / "app" / "db" / "init.sql").read_text(encoding="utf-8")
_MIGRATIONS = _BACKEND / "app" / "db" / "migrations"
_DSN = os.environ.get(
    "AKB_TEST_DSN",
    "postgresql://akb:akb@localhost:5433/akb",  # pragma: allowlist secret
)


async def _reachable() -> bool:
    try:
        conn = await asyncpg.connect(_DSN, timeout=2)
    except (OSError, asyncpg.PostgresError):
        return False
    await conn.close()
    return True


def _database_dsn(name: str) -> str:
    return f"{_DSN.rsplit('/', 1)[0]}/{name}"


def _load(filename: str):
    path = _MIGRATIONS / filename
    spec = importlib.util.spec_from_file_location(f"migration_060_test_{filename}", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@asynccontextmanager
async def _fresh_schema():
    if not await _reachable():
        pytest.skip(f"Postgres not reachable at {_DSN}")

    name = f"akb_native_revision_bridge_{uuid.uuid4().hex[:12]}"
    admin = await asyncpg.connect(_DSN)
    conn = None
    pool = None
    try:
        await admin.execute(f'CREATE DATABASE "{name}"')
        dsn = _database_dsn(name)
        conn = await asyncpg.connect(dsn)
        await conn.execute(_INIT_SQL)
        await _load("048_native_revision_core.py").migrate(conn=conn)
        migration = _load("060_native_revision_migration_bridge.py")
        await migration.migrate(conn=conn)
        await migration.migrate(conn=conn)
        await conn.close()
        conn = None
        pool = await asyncpg.create_pool(dsn, min_size=1, max_size=4)
        yield pool
    finally:
        if pool is not None:
            await pool.close()
        if conn is not None and not conn.is_closed():
            await conn.close()
        await admin.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
        await admin.close()


async def test_fresh_and_reapplied_schema_enforces_c9_keys_fks_and_cascades():
    async with _fresh_schema() as pool:
        async with pool.acquire() as conn:
            tables = await conn.fetch(
                """
                SELECT table_name
                  FROM information_schema.tables
                 WHERE table_schema = 'public'
                   AND table_name = ANY($1::text[])
                 ORDER BY table_name
                """,
                [
                    "native_revision_migration_runs",
                    "native_revision_migration_items",
                    "legacy_revision_mappings",
                ],
            )
            assert [row["table_name"] for row in tables] == [
                "legacy_revision_mappings",
                "native_revision_migration_items",
                "native_revision_migration_runs",
            ]

            constraint_counts = await conn.fetch(
                """
                SELECT conname, count(*)::int AS copies
                  FROM pg_constraint
                 WHERE conname LIKE ANY($1::text[])
                 GROUP BY conname
                 ORDER BY conname
                """,
                [
                    "native_revision_migration_%",
                    "legacy_revision_mappings_%",
                ],
            )
            assert all(row["copies"] == 1 for row in constraint_counts)
            assert await conn.fetchval(
                """
                SELECT count(*)
                  FROM pg_indexes
                 WHERE schemaname = 'public'
                   AND indexname = 'uq_legacy_revision_mappings_native_revision'
                """
            ) == 1

            namespace_one = await conn.fetchval(
                "INSERT INTO vaults (name, git_path) VALUES ($1, $2) RETURNING id",
                f"bridge-one-{uuid.uuid4().hex}",
                "/tmp/bridge-one-unused.git",
            )
            namespace_two = await conn.fetchval(
                "INSERT INTO vaults (name, git_path) VALUES ($1, $2) RETURNING id",
                f"bridge-two-{uuid.uuid4().hex}",
                "/tmp/bridge-two-unused.git",
            )
            document_one = await conn.fetchval(
                """
                INSERT INTO documents (vault_id, path, title)
                VALUES ($1, 'one.md', 'one')
                RETURNING id
                """,
                namespace_one,
            )
            document_two = await conn.fetchval(
                """
                INSERT INTO documents (vault_id, path, title)
                VALUES ($1, 'two.md', 'two')
                RETURNING id
                """,
                namespace_two,
            )

        async with pool.acquire() as conn:
            run_one = await conn.fetchval(
                """
                INSERT INTO native_revision_migration_runs
                    (namespace_id, fixed_git_oid, coverage_version, inventory_digest)
                VALUES ($1, $2, 'c9-v1', $3)
                RETURNING run_id
                """,
                namespace_one,
                "a" * 40,
                "b" * 64,
            )
            await conn.execute(
                """
                INSERT INTO native_revision_migration_items
                    (run_id, namespace_id, legacy_document_id, native_resource_id,
                     captured_path, legacy_head_oid, body_digest, byte_size, status)
                VALUES ($1, $2, $3, $3, 'one.md', $4, $5, 9, 'pending')
                """,
                run_one,
                namespace_one,
                document_one,
                "c" * 40,
                "d" * 64,
            )
            assert await conn.fetchval(
                "SELECT count(*) FROM native_resources WHERE resource_id = $1",
                document_one,
            ) == 0

        service = NativeRevisionService(pool)
        native_one = await service.create_text(
            namespace_id=namespace_one,
            surface="document",
            path="one.md",
            payload="one body\n",
            actor="bridge-test",
            mutation_id=uuid.uuid4(),
            resource_id=document_one,
        )
        native_two = await service.create_text(
            namespace_id=namespace_two,
            surface="document",
            path="two.md",
            payload="two body\n",
            actor="bridge-test",
            mutation_id=uuid.uuid4(),
            resource_id=document_two,
        )

        async with pool.acquire() as conn:
            with pytest.raises(asyncpg.CheckViolationError):
                await conn.execute(
                    """
                    INSERT INTO native_revision_migration_runs
                        (namespace_id, fixed_git_oid, coverage_version, inventory_digest)
                    VALUES ($1, 'not-an-oid', 'c9-v1', $2)
                    """,
                    namespace_one,
                    "b" * 64,
                )

            with pytest.raises(asyncpg.CheckViolationError):
                await conn.execute(
                    """
                    INSERT INTO native_revision_migration_items
                        (run_id, namespace_id, legacy_document_id, native_resource_id,
                         captured_path, legacy_head_oid, body_digest, byte_size,
                         status)
                    VALUES ($1, $2, $3, $3, 'one.md', $4, $5, -1, 'pending')
                    """,
                    run_one,
                    namespace_one,
                    document_one,
                    "c" * 40,
                    "d" * 64,
                )
            with pytest.raises(asyncpg.UniqueViolationError):
                await conn.execute(
                    """
                    INSERT INTO native_revision_migration_items
                        (run_id, namespace_id, legacy_document_id, native_resource_id,
                         captured_path, legacy_head_oid, body_digest, byte_size, status)
                    VALUES ($1, $2, $3, $3, 'one.md', $4, $5, 9, 'pending')
                    """,
                    run_one,
                    namespace_one,
                    document_one,
                    "c" * 40,
                    "d" * 64,
                )

            run_two = await conn.fetchval(
                """
                INSERT INTO native_revision_migration_runs
                    (namespace_id, fixed_git_oid, coverage_version, inventory_digest)
                VALUES ($1, $2, 'c9-v2', $3)
                RETURNING run_id
                """,
                namespace_one,
                "e" * 40,
                "f" * 64,
            )
            with pytest.raises(asyncpg.UniqueViolationError):
                await conn.execute(
                    """
                    INSERT INTO native_revision_migration_items
                        (run_id, namespace_id, legacy_document_id, native_resource_id,
                         captured_path, legacy_head_oid, body_digest, byte_size, status)
                    VALUES ($1, $2, $3, $3, 'one.md', $4, $5, 9, 'pending')
                    """,
                    run_two,
                    namespace_one,
                    document_one,
                    "c" * 40,
                    "d" * 64,
                )

            with pytest.raises(asyncpg.ForeignKeyViolationError):
                await conn.execute(
                    """
                    INSERT INTO native_revision_migration_items
                        (run_id, namespace_id, legacy_document_id, native_resource_id,
                         captured_path, legacy_head_oid, body_digest, byte_size, status)
                    VALUES ($1, $2, $3, $3, 'two.md', $4, $5, 9, 'pending')
                    """,
                    run_one,
                    namespace_two,
                    document_two,
                    "1" * 40,
                    "2" * 64,
                )

            run_three = await conn.fetchval(
                """
                INSERT INTO native_revision_migration_runs
                    (namespace_id, fixed_git_oid, coverage_version, inventory_digest)
                VALUES ($1, $2, 'c9-v3', $3)
                RETURNING run_id
                """,
                namespace_one,
                "6" * 40,
                "7" * 64,
            )
            await conn.execute(
                """
                INSERT INTO native_revision_migration_items
                    (run_id, namespace_id, legacy_document_id, native_resource_id,
                     captured_path, legacy_head_oid, native_head_revision_id,
                     body_digest, byte_size, status)
                VALUES ($1, $2, $3, $3, 'one.md', $4, $5, $6, 9, 'complete')
                """,
                run_three,
                namespace_one,
                document_one,
                "8" * 40,
                native_one.revision_id,
                "9" * 64,
            )

            run_four = await conn.fetchval(
                """
                INSERT INTO native_revision_migration_runs
                    (namespace_id, fixed_git_oid, coverage_version, inventory_digest)
                VALUES ($1, $2, 'c9-v4', $3)
                RETURNING run_id
                """,
                namespace_one,
                "a" * 40,
                "b" * 64,
            )
            with pytest.raises(asyncpg.ForeignKeyViolationError):
                await conn.execute(
                    """
                    INSERT INTO native_revision_migration_items
                        (run_id, namespace_id, legacy_document_id, native_resource_id,
                         captured_path, legacy_head_oid, native_head_revision_id,
                         body_digest, byte_size, status)
                    VALUES ($1, $2, $3, $3, 'one.md', $4, $5, $6, 9, 'complete')
                    """,
                    run_four,
                    namespace_one,
                    document_one,
                    "0" * 40,
                    native_two.revision_id,
                    "1" * 64,
                )

            run_five = await conn.fetchval(
                """
                INSERT INTO native_revision_migration_runs
                    (namespace_id, fixed_git_oid, coverage_version, inventory_digest)
                VALUES ($1, $2, 'c9-v5', $3)
                RETURNING run_id
                """,
                namespace_two,
                "c" * 40,
                "d" * 64,
            )
            with pytest.raises(asyncpg.ForeignKeyViolationError):
                await conn.execute(
                    """
                    INSERT INTO native_revision_migration_items
                        (run_id, namespace_id, legacy_document_id, native_resource_id,
                         captured_path, legacy_head_oid, native_head_revision_id,
                         body_digest, byte_size, status)
                    VALUES ($1, $2, $3, $3, 'two.md', $4, $5, $6, 9, 'complete')
                    """,
                    run_five,
                    namespace_two,
                    document_two,
                    "e" * 40,
                    native_one.revision_id,
                    "f" * 64,
                )

            failed_id = uuid.uuid4()
            await conn.execute(
                """
                INSERT INTO native_revision_migration_items
                    (run_id, namespace_id, legacy_document_id, native_resource_id,
                     captured_path, legacy_head_oid, body_digest, byte_size,
                     status, error_code)
                VALUES ($1, $2, $3, $3, 'one.md', $4, $5, 9, 'failed', $6)
                """,
                run_two,
                namespace_one,
                failed_id,
                "9" * 40,
                "a" * 64,
                "body_unavailable",
            )

            invalid_states = (
                (
                    "pending_with_head",
                    "b" * 40,
                    native_one.revision_id,
                    None,
                    "pending",
                ),
                ("complete_without_head", "c" * 40, None, None, "complete"),
                (
                    "failed_with_head",
                    "d" * 40,
                    native_one.revision_id,
                    "body_unavailable",
                    "failed",
                ),
                ("failed_without_error", "e" * 40, None, None, "failed"),
            )
            for label, oid, head, error_code, status in invalid_states:
                invalid_id = uuid.uuid5(uuid.NAMESPACE_URL, label)
                with pytest.raises(asyncpg.CheckViolationError):
                    await conn.execute(
                        """
                        INSERT INTO native_revision_migration_items
                            (run_id, namespace_id, legacy_document_id, native_resource_id,
                             captured_path, legacy_head_oid, native_head_revision_id,
                             body_digest, byte_size, status, error_code)
                        VALUES ($1, $2, $3, $3, 'one.md', $4, $5, $6, 9, $7, $8)
                        """,
                        run_two,
                        namespace_one,
                        invalid_id,
                        oid,
                        head,
                        "f" * 64,
                        status,
                        error_code,
                    )

            await conn.execute(
                """
                INSERT INTO legacy_revision_mappings
                    (namespace_id, resource_id, legacy_git_oid, path_at_revision,
                     resolution, run_id, lineage_ordinal)
                VALUES ($1, $2, $3, 'one.md', 'bridge', $4, 0)
                """,
                namespace_one,
                document_one,
                "1" * 40,
                run_one,
            )
            await conn.execute(
                """
                INSERT INTO legacy_revision_mappings
                    (namespace_id, resource_id, legacy_git_oid, path_at_revision,
                     resolution, native_revision_id, run_id, lineage_ordinal)
                VALUES ($1, $2, $3, 'one.md', 'native', $4, $5, 1)
                """,
                namespace_one,
                document_one,
                "2" * 40,
                native_one.revision_id,
                run_one,
            )
            with pytest.raises(asyncpg.CheckViolationError):
                await conn.execute(
                    """
                    INSERT INTO legacy_revision_mappings
                        (namespace_id, resource_id, legacy_git_oid, path_at_revision,
                         resolution, native_revision_id, run_id, lineage_ordinal)
                    VALUES ($1, $2, $3, 'one.md', 'native', NULL, $4, 2)
                    """,
                    namespace_one,
                    document_one,
                    "3" * 40,
                    run_one,
                )
            with pytest.raises(asyncpg.CheckViolationError):
                await conn.execute(
                    """
                    INSERT INTO legacy_revision_mappings
                        (namespace_id, resource_id, legacy_git_oid, path_at_revision,
                         resolution, native_revision_id, run_id, lineage_ordinal)
                    VALUES ($1, $2, $3, 'one.md', 'bridge', $4, $5, 2)
                    """,
                    namespace_one,
                    document_one,
                    "4" * 40,
                    native_one.revision_id,
                    run_one,
                )
            with pytest.raises(asyncpg.UniqueViolationError):
                await conn.execute(
                    """
                    INSERT INTO legacy_revision_mappings
                        (namespace_id, resource_id, legacy_git_oid, path_at_revision,
                         resolution, native_revision_id, run_id, lineage_ordinal)
                    VALUES ($1, $2, $3, 'one.md', 'native', $4, $5, 2)
                    """,
                    namespace_one,
                    document_one,
                    "7" * 40,
                    native_one.revision_id,
                    run_one,
                )
            with pytest.raises(asyncpg.ForeignKeyViolationError):
                await conn.execute(
                    """
                    INSERT INTO legacy_revision_mappings
                        (namespace_id, resource_id, legacy_git_oid, path_at_revision,
                         resolution, native_revision_id, run_id, lineage_ordinal)
                    VALUES ($1, $2, $3, 'two.md', 'native', $4, $5, 2)
                    """,
                    namespace_one,
                    document_one,
                    "5" * 40,
                    native_two.revision_id,
                    run_one,
                )

            await conn.execute("DELETE FROM documents WHERE id = $1", document_one)
            assert await conn.fetchval(
                "SELECT count(*) FROM documents WHERE id = $1",
                document_one,
            ) == 0
            assert await conn.fetchval(
                "SELECT count(*) FROM native_revision_migration_items WHERE run_id = $1",
                run_one,
            ) == 1

            assert await conn.fetchval(
                "SELECT count(*) FROM legacy_revision_mappings WHERE run_id = $1",
                run_one,
            ) == 2
            await conn.execute(
                "DELETE FROM native_revision_migration_runs WHERE run_id = $1",
                run_one,
            )
            assert await conn.fetchval(
                "SELECT count(*) FROM native_revision_migration_items WHERE run_id = $1",
                run_one,
            ) == 0
            assert await conn.fetchval(
                "SELECT count(*) FROM legacy_revision_mappings WHERE run_id = $1",
                run_one,
            ) == 0

            # A different namespace cannot reuse the first run's key even if
            # the UUIDs and selector values are otherwise valid.
            with pytest.raises(asyncpg.ForeignKeyViolationError):
                await conn.execute(
                    """
                    INSERT INTO legacy_revision_mappings
                        (namespace_id, resource_id, legacy_git_oid, path_at_revision,
                         resolution, native_revision_id, run_id, lineage_ordinal)
                    VALUES ($1, $2, $3, 'two.md', 'native', $4, $5, 0)
                    """,
                    namespace_two,
                    document_two,
                    "6" * 40,
                    native_two.revision_id,
                    run_two,
                )
