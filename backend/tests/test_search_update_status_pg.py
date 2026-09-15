"""Execute current-Head health SQL on isolated temporary PostgreSQL ledgers.

The temporary tables shadow the application relations on one test connection;
no existing tenant rows are read or changed. Set AKB_TEST_DSN to a fixture DB.
"""

from __future__ import annotations

import json
import os
import uuid
from contextlib import asynccontextmanager

import asyncpg
import pytest

from app.services import native_derived_worker
from app.services._backfill import MAX_RETRIES


class _ConnectionPool:
    def __init__(self, conn):
        self.conn = conn
        self.last_query = None
        self.last_args = None

    @asynccontextmanager
    async def acquire(self):
        yield self

    async def fetchrow(self, query, *args):
        self.last_query, self.last_args = query, args
        return await self.conn.fetchrow(query, *args)


@pytest.fixture
async def ledger():
    dsn = os.environ.get("AKB_TEST_DSN", "postgresql://akb:akb@localhost:5433/akb")  # pragma: allowlist secret
    try:
        conn = await asyncpg.connect(dsn, timeout=2)
    except (OSError, asyncpg.PostgresError):
        if os.environ.get("REQUIRE_REAL_PG") == "1":
            pytest.fail("Required PostgreSQL fixture is unavailable")
        pytest.skip("PostgreSQL fixture unavailable; set AKB_TEST_DSN")
    try:
        # Relevant column types and indexes match migrations 048 and 054.
        await conn.execute("""
            CREATE TEMP TABLE native_resources (
                resource_id UUID PRIMARY KEY,
                namespace_id UUID NOT NULL,
                head_revision_id TEXT,
                lifecycle TEXT NOT NULL DEFAULT 'live',
                UNIQUE (namespace_id, resource_id)
            );
            CREATE TEMP TABLE native_invalidation_intents (
                intent_id UUID PRIMARY KEY,
                namespace_id UUID NOT NULL,
                resource_id UUID NOT NULL,
                revision_id TEXT NOT NULL UNIQUE,
                occurred_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                completed_at TIMESTAMPTZ,
                retry_count INTEGER NOT NULL DEFAULT 0,
                delivery_outcome TEXT
            );
            CREATE INDEX ON native_invalidation_intents(occurred_at, intent_id)
                WHERE completed_at IS NULL;
        """)
        yield _ConnectionPool(conn)
    finally:
        await conn.close()  # Drops only this connection's temporary fixture tables.


async def test_current_heads_exclude_history_include_deleted_and_keep_vault_scope(ledger):
    vault, other = uuid.uuid4(), uuid.uuid4()
    resources = [uuid.uuid4() for _ in range(7)]
    for index, resource in enumerate(resources):
        namespace = other if index == 6 else vault
        await ledger.conn.execute(
            "INSERT INTO native_resources VALUES ($1, $2, $3, $4)",
            resource, namespace, f"head-{index}", "deleted" if index == 2 else "live",
        )
        # Every Resource has an old terminal failure, including the applied Head.
        await ledger.conn.execute(
            """INSERT INTO native_invalidation_intents
                   (intent_id, namespace_id, resource_id, revision_id, completed_at, delivery_outcome)
               VALUES ($1, $2, $3, $4, NOW(), 'abandoned')""",
            uuid.uuid4(), namespace, resource, f"old-{index}",
        )
        outcome = "applied" if index == 0 else "abandoned" if index in {1, 2, 6} else None
        retries = MAX_RETRIES if index == 5 else 1 if index == 4 else 0
        await ledger.conn.execute(
            """INSERT INTO native_invalidation_intents
                   (intent_id, namespace_id, resource_id, revision_id, completed_at, retry_count, delivery_outcome)
               VALUES ($1, $2, $3, $4, CASE WHEN $6::text IS NULL THEN NULL ELSE NOW() END, $5, $6)""",
            uuid.uuid4(), namespace, resource, f"head-{index}", retries, outcome,
        )
    assert await native_derived_worker._current_head_stats(ledger, vault) == {
        "pending": 3, "retrying": 1, "exhausted": 1, "abandoned": 2,
    }
    assert await native_derived_worker._current_head_stats(ledger, other) == {
        "pending": 0, "retrying": 0, "exhausted": 0, "abandoned": 1,
    }
    assert (await native_derived_worker._pending_stats(ledger, vault))["abandoned"] == 8

    # Superseding the failed current Heads clears current warnings, retaining history.
    for index in (1, 2):
        revision = f"replacement-{index}"
        await ledger.conn.execute(
            "UPDATE native_resources SET head_revision_id = $2 WHERE resource_id = $1", resources[index], revision,
        )
        await ledger.conn.execute(
            """INSERT INTO native_invalidation_intents
                   (intent_id, namespace_id, resource_id, revision_id, completed_at, delivery_outcome)
               VALUES ($1, $2, $3, $4, NOW(), 'applied')""",
            uuid.uuid4(), vault, resources[index], revision,
        )
    assert (await native_derived_worker._current_head_stats(ledger, vault))["abandoned"] == 0
    assert (await native_derived_worker._pending_stats(ledger, vault))["abandoned"] == 8


async def test_current_head_query_on_million_intent_ledger(ledger):
    """Measure the actual query with existing key indexes, not a new index."""
    await ledger.conn.execute("""
        INSERT INTO native_resources (resource_id, namespace_id, head_revision_id)
        SELECT md5('resource-' || n)::uuid, md5(((n - 1) / 100)::text)::uuid, 'r-' || n || '-100'
          FROM generate_series(1, 10000) n;
        INSERT INTO native_invalidation_intents
            (intent_id, namespace_id, resource_id, revision_id, completed_at, retry_count, delivery_outcome)
        SELECT md5(n || '-' || revision)::uuid,
               md5(((n - 1) / 100)::text)::uuid,
               md5('resource-' || n)::uuid,
               'r-' || n || '-' || revision,
               CASE WHEN revision = 100 AND n % 4 < 2 THEN NULL ELSE NOW() END,
               CASE WHEN revision = 100 AND n % 4 = 1 THEN 8 ELSE 0 END,
               CASE WHEN revision = 100 AND n % 4 < 2 THEN NULL
                    WHEN revision = 99 OR n % 4 = 2 THEN 'abandoned' ELSE 'applied' END
          FROM generate_series(1, 10000) n CROSS JOIN generate_series(1, 100) revision;
        ANALYZE native_resources;
        ANALYZE native_invalidation_intents;
    """)
    namespace = await ledger.conn.fetchval("SELECT md5('0')::uuid")
    assert await native_derived_worker._current_head_stats(ledger, namespace) == {
        "pending": 50, "retrying": 0, "exhausted": 25, "abandoned": 25,
    }
    plan = json.loads(await ledger.conn.fetchval(
        "EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON) " + ledger.last_query, *ledger.last_args,
    ))[0]
    print(json.dumps({
        "intent_rows": 1_000_000, "resources": 10_000, "vaults": 100,
        "observed_vault_heads": 100, "execution_ms": plan["Execution Time"],
        "planning_ms": plan["Planning Time"], "plan": plan["Plan"],
    }))
