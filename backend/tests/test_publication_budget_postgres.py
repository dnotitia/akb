"""Real PG atomicity, lifetime, expiry and write-amplification regression proof."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from dataclasses import asdict
import importlib.util
import itertools
import json
import os
from pathlib import Path
import uuid

import asyncpg
import pytest

from app.services import publication_rate_limit as rl

pytestmark = pytest.mark.asyncio
_BACKEND = Path(__file__).resolve().parents[1]
_DSN = os.getenv("AKB_TEST_DSN", "postgresql://akb:akb@localhost:15432/akb")


async def _migrate(conn):
    path = _BACKEND / "app/db/migrations/116_publication_password_budgets.py"
    spec = importlib.util.spec_from_file_location(path.stem, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    await module.migrate(conn=conn)


@asynccontextmanager
async def _database(monkeypatch):
    try:
        admin = await asyncpg.connect(_DSN, timeout=2)
    except (OSError, asyncpg.PostgresError):
        if os.getenv("REQUIRE_REAL_PG") == "1":
            pytest.fail("required PostgreSQL is unavailable")
        pytest.skip("PostgreSQL unavailable")
    name = "akb_pub_budget_" + uuid.uuid4().hex[:12]
    await admin.execute(f'CREATE DATABASE "{name}"')
    dsn = _DSN.rsplit("/", 1)[0] + "/" + name
    first = await asyncpg.create_pool(dsn, min_size=1, max_size=8)
    second = await asyncpg.create_pool(dsn, min_size=1, max_size=8)
    try:
        async with first.acquire() as conn:
            await conn.execute((_BACKEND / "app/db/init.sql").read_text())
            await _migrate(conn)
            await _migrate(conn)
            vault = await conn.fetchval("INSERT INTO vaults(name,git_path) VALUES('budget-test','/tmp/budget.git') RETURNING id")
            for slug, protected in (("protected", True), ("other", True), ("plain", False)):
                await conn.execute(
                    "INSERT INTO publications(slug,vault_id,resource_type,password_hash) VALUES($1,$2,'table_query',$3)",
                    slug, vault, "non-secret-test-hash" if protected else None,
                )
        pools = itertools.cycle([first, second])

        async def pool():
            return next(pools)

        monkeypatch.setattr(rl, "get_pool", pool)
        yield first, second, dsn
    finally:
        await first.close()
        await second.close()
        await admin.execute(f'DROP DATABASE "{name}" WITH (FORCE)')
        await admin.close()


async def test_two_pools_atomically_reserve_and_locked_retries_do_not_rewrite(monkeypatch):
    async with _database(monkeypatch) as (pool, _other, _dsn):
        attempts = await asyncio.gather(*[rl.reserve("protected", f"rotating-ip-{i}") for i in range(50)])
        assert attempts.count(0) == rl._FREE_SLUG + 1
        assert all(wait > 0 for wait in attempts if wait != 0)
        async with pool.acquire() as conn:
            before = await conn.fetchrow("SELECT xmin::text,state FROM publication_password_budgets")
            state = json.loads(before["state"])
            assert state["total"]["failures"] == 31
            assert len(state["ips"]) == 31
            assert "rotating-ip" not in before["state"]
        assert await rl.reserve("protected", "follower") > 0
        async with pool.acquire() as conn:
            assert await conn.fetchrow("SELECT xmin::text,state FROM publication_password_budgets") == before
        assert await rl.reserve("other", "follower") == 0


async def test_successful_refunds_are_atomic_and_recreated_client_preserves_lock(monkeypatch):
    async with _database(monkeypatch) as (pool, _other, dsn):
        for _ in range(rl._FREE_IP + 1):
            assert await rl.reserve("protected", "same-ip") == 0
        replacement = await asyncpg.create_pool(dsn, min_size=1, max_size=2)
        try:
            monkeypatch.setattr(rl, "get_pool", lambda: replacement)
            assert await rl.reserve("protected", "same-ip") > 0
            await rl.release("protected", "same-ip")
            assert await rl.reserve("protected", "same-ip") == 0
            await rl.release("protected", "same-ip")
            for i in range(20):
                assert await rl.reserve("protected", f"valid-{i}") == 0
            await asyncio.gather(*[rl.release("protected", f"valid-{i}") for i in range(20)])
            async with pool.acquire() as conn:
                state = rl._Budget.load(await conn.fetchval("SELECT state FROM publication_password_budgets"))
                assert state.total.failures == rl._FREE_IP
                assert state.ips == {}
        finally:
            await replacement.close()


async def test_quiet_expiry_unknown_slugs_and_cascade_cleanup(monkeypatch):
    async with _database(monkeypatch) as (pool, _other, _dsn):
        for _ in range(rl._FREE_IP + 1):
            await rl.reserve("protected", "ip")
        async with pool.acquire() as conn:
            stale = rl._Budget.load(await conn.fetchval("SELECT state FROM publication_password_budgets"))
            for entry in (stale.total, *stale.ips.values()):
                entry.last = 1
                entry.locked_until = 2
            await conn.execute("UPDATE publication_password_budgets SET state=$1::jsonb", json.dumps(asdict(stale)))
        assert await rl.reserve("protected", "new-ip") == 0
        assert await rl.reserve("plain", "ip") == 0
        for i in range(25):
            assert await rl.reserve(f"unknown-{i}", "ip") == 0
        async with pool.acquire() as conn:
            assert await conn.fetchval("SELECT count(*) FROM publication_password_budgets") == 1
            state = rl._Budget.load(await conn.fetchval("SELECT state FROM publication_password_budgets"))
            assert state.total.failures == 1 and len(state.ips) == 1
            await conn.execute("DELETE FROM publications WHERE slug='protected'")
            assert await conn.fetchval("SELECT count(*) FROM publication_password_budgets") == 0


async def test_concurrent_per_ip_budget_and_expired_publication_do_not_allocate(monkeypatch):
    async with _database(monkeypatch) as (pool, _other, _dsn):
        results = await asyncio.gather(*[rl.reserve("protected", "same-ip") for _ in range(20)])
        assert results.count(0) == rl._FREE_IP + 1
        assert sum(wait > 0 for wait in results) == 14
        async with pool.acquire() as conn:
            await conn.execute("UPDATE publications SET expires_at=NOW()-INTERVAL '1 second' WHERE slug='other'")
        assert await rl.reserve("other", "ip") == 0
        async with pool.acquire() as conn:
            assert await conn.fetchval("SELECT count(*) FROM publication_password_budgets") == 1


async def test_contended_budget_fails_closed_without_blocking_other_publications(monkeypatch):
    async with _database(monkeypatch) as (pool, _other, _dsn):
        await rl.reserve("protected", "ip")
        async with pool.acquire() as conn:
            before = await conn.fetchval("SELECT state FROM publication_password_budgets")
            async with conn.transaction():
                await conn.execute("SELECT FROM publication_password_budgets FOR UPDATE")
                waiting = asyncio.create_task(rl.reserve("protected", "ip"))
                assert await rl.reserve("other", "ip") == 0
                with pytest.raises(rl.BudgetUnavailable):
                    await waiting
            after = await conn.fetchval(
                "SELECT state FROM publication_password_budgets WHERE publication_id=(SELECT id FROM publications WHERE slug='protected')",
            )
            assert after == before
