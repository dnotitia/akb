"""Run the real directory policy and summary SQL on isolated temporary tables.

No tenant rows or schemas are modified. The temporary tables shadow only the
relations used by this query and are dropped when the fixture connection closes.
"""

from __future__ import annotations

import json
import os
import uuid
from contextlib import asynccontextmanager
from datetime import datetime
from unittest.mock import AsyncMock

import asyncpg
import pytest

from app.services import access_service, document_counters, workspace_summary


class _Pool:
    def __init__(self, conn):
        self.conn = conn
        self.query = None
        self.args = None

    @asynccontextmanager
    async def acquire(self):
        yield self

    def __getattr__(self, name):
        return getattr(self.conn, name)

    async def fetchrow(self, query, *args):
        self.query, self.args = query, args
        return await self.conn.fetchrow(query, *args)


@pytest.fixture
async def inventory(monkeypatch):
    dsn = os.environ.get("AKB_TEST_DSN", "postgresql://akb:akb@localhost:5433/akb")  # pragma: allowlist secret
    try:
        conn = await asyncpg.connect(dsn, timeout=2)
    except (OSError, asyncpg.PostgresError):
        if os.environ.get("REQUIRE_REAL_PG") == "1":
            pytest.fail("Required PostgreSQL fixture is unavailable")
        pytest.skip("PostgreSQL fixture unavailable; set AKB_TEST_DSN")
    try:
        await conn.execute("""
            CREATE TEMP TABLE users (id UUID PRIMARY KEY, is_admin BOOLEAN NOT NULL DEFAULT FALSE);
            CREATE TEMP TABLE vaults (
                id UUID PRIMARY KEY, owner_id UUID NOT NULL, name TEXT NOT NULL,
                description TEXT DEFAULT '', status TEXT DEFAULT 'active',
                public_access TEXT DEFAULT 'none', created_at TIMESTAMPTZ DEFAULT NOW()
            );
            CREATE TEMP TABLE vault_access (
                vault_id UUID NOT NULL, user_id UUID NOT NULL, role TEXT NOT NULL,
                PRIMARY KEY (vault_id, user_id)
            );
            CREATE TEMP TABLE vault_write_policy (vault_id UUID PRIMARY KEY, managed_by TEXT);
            CREATE TEMP TABLE documents (id UUID PRIMARY KEY, vault_id UUID NOT NULL, status TEXT DEFAULT 'active');
            CREATE INDEX ON documents(vault_id);
            CREATE TEMP TABLE native_resources (
                resource_id UUID PRIMARY KEY, namespace_id UUID NOT NULL,
                surface TEXT NOT NULL DEFAULT 'document', lifecycle TEXT NOT NULL DEFAULT 'live'
            );
            CREATE INDEX ON native_resources(namespace_id);
            CREATE TEMP TABLE vault_tables (id UUID PRIMARY KEY, vault_id UUID NOT NULL);
            CREATE INDEX ON vault_tables(vault_id);
            CREATE TEMP TABLE vault_files (
                id UUID PRIMARY KEY, vault_id UUID NOT NULL, kind TEXT NOT NULL, upload_state TEXT NOT NULL
            );
            CREATE INDEX ON vault_files(vault_id);
        """)
        pool = _Pool(conn)
        monkeypatch.setattr(workspace_summary, "get_pool", AsyncMock(return_value=pool))
        monkeypatch.setattr(access_service, "get_pool", AsyncMock(return_value=pool))
        monkeypatch.setattr(document_counters, "native_documents_are_authoritative", lambda: False)
        yield pool
    finally:
        await conn.close()


async def _seed(pool):
    actor, other, admin = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    await pool.conn.executemany("INSERT INTO users VALUES ($1, $2)", [(actor, False), (other, False), (admin, True)])
    vaults = {}
    for index, name in enumerate(("owned", "reader", "writer", "admin", "public-reader", "public-writer", "archived", "private")):
        vault = uuid.uuid4()
        vaults[name] = vault
        await pool.conn.execute(
            "INSERT INTO vaults (id, owner_id, name, status, public_access) VALUES ($1, $2, $3, $4, $5)",
            vault, actor if name in {"owned", "archived"} else other, name,
            "archived" if name == "archived" else "active",
            name.removeprefix("public-") if name.startswith("public-") else "none",
        )
        if name in {"owned", "reader", "writer", "admin"}:
            await pool.conn.execute("INSERT INTO vault_access VALUES ($1, $2, $3)", vault, actor, "admin" if name == "owned" else name)
        for number in range(index + 1):
            await pool.conn.execute("INSERT INTO documents VALUES ($1, $2, $3)", uuid.uuid4(), vault, "archived" if number == 0 else "active")
        # Native intentionally differs from the frozen legacy catalog; both
        # deleted documents and other resource surfaces must not inflate totals.
        for _ in range(index + 2):
            await pool.conn.execute("INSERT INTO native_resources VALUES ($1, $2, 'document', 'live')", uuid.uuid4(), vault)
        await pool.conn.execute("INSERT INTO native_resources VALUES ($1, $2, 'document', 'deleted')", uuid.uuid4(), vault)
        await pool.conn.execute("INSERT INTO native_resources VALUES ($1, $2, 'file', 'live')", uuid.uuid4(), vault)
        await pool.conn.execute("INSERT INTO vault_tables VALUES ($1, $2)", uuid.uuid4(), vault)
        for kind, state in (("file", "confirmed"), ("file", "staged"), ("attachment", "confirmed")):
            await pool.conn.execute("INSERT INTO vault_files VALUES ($1, $2, $3, $4)", uuid.uuid4(), vault, kind, state)
    return actor, other, admin, vaults


@pytest.mark.parametrize("native", [False, True])
async def test_counts_follow_directory_access_authority_and_confirmed_file_rules(inventory, monkeypatch, native):
    actor, other, admin, vaults = await _seed(inventory)
    monkeypatch.setattr(document_counters, "native_documents_are_authoritative", lambda: native)
    actor_dir = await access_service.list_accessible_vaults(str(actor))
    assert {row["name"] for row in actor_dir} == set(vaults) - {"private"}
    result = await workspace_summary.get_workspace_summary(str(actor))
    assert result["vault_count"] == len(actor_dir) == 7
    assert result["document_count"] == sum(range(2, 9) if native else range(1, 8))
    assert result["table_count"] == result["file_count"] == 7
    assert datetime.fromisoformat(result["observed_at"]).utcoffset().total_seconds() == 0

    admin_dir = await access_service.list_accessible_vaults(str(admin))
    admin_result = await workspace_summary.get_workspace_summary(str(admin))
    assert admin_result["vault_count"] == len(admin_dir) == 8
    assert admin_result["document_count"] == sum(range(2, 10) if native else range(1, 9))
    assert admin_result["table_count"] == admin_result["file_count"] == 8

    # Demotion and revoked membership take effect on the next snapshot, even
    # when a browser still holds an older admin session claim.
    await inventory.conn.execute("UPDATE users SET is_admin = FALSE WHERE id = $1", admin)
    demoted = await workspace_summary.get_workspace_summary(str(admin))
    assert demoted["vault_count"] == 2
    assert demoted["document_count"] == (13 if native else 11)
    await inventory.conn.execute("DELETE FROM vault_access WHERE user_id = $1 AND vault_id = $2", actor, vaults["reader"])
    revoked = await workspace_summary.get_workspace_summary(str(actor))
    assert revoked["vault_count"] == 6
    assert revoked["document_count"] == result["document_count"] - (3 if native else 2)


async def test_empty_directory_has_real_zero_counts(inventory):
    actor = uuid.uuid4()
    await inventory.conn.execute("INSERT INTO users VALUES ($1, FALSE)", actor)
    result = await workspace_summary.get_workspace_summary(str(actor))
    assert [result[key] for key in ("vault_count", "document_count", "table_count", "file_count")] == [0, 0, 0, 0]


async def test_large_directory_has_one_set_aggregate_not_per_vault_detail(inventory):
    actor = uuid.uuid4()
    await inventory.conn.execute("INSERT INTO users VALUES ($1, FALSE)", actor)
    await inventory.conn.execute("""
        INSERT INTO vaults (id, owner_id, name)
        SELECT md5('summary-vault-' || n)::uuid, $1, 'vault-' || n FROM generate_series(1, 100) n
    """, actor)
    await inventory.conn.execute("""
        INSERT INTO documents (id, vault_id)
        SELECT md5('summary-doc-' || n)::uuid, md5('summary-vault-' || ((n - 1) / 1000 + 1))::uuid
        FROM generate_series(1, 100000) n;
        ANALYZE vaults;
        ANALYZE documents;
    """)
    result = await workspace_summary.get_workspace_summary(str(actor))
    assert result["vault_count"] == 100
    assert result["document_count"] == 100000
    plan = json.loads(await inventory.conn.fetchval(
        "EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON) " + inventory.query, *inventory.args,
    ))[0]
    print(json.dumps({"vaults": 100, "documents": 100000, "execution_ms": plan["Execution Time"]}))
