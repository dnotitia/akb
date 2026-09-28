"""Bootstrap releases all DDL locks before retrying conflicts with writers."""
import asyncio
from functools import partial

import asyncpg
import pytest

from app.db import postgres as pg
from tests.test_boot_schema_serialization_pg import _empty_database, _INIT_SQL

pytestmark = pytest.mark.asyncio


async def _no_migrations(conn, applied):
    return None


async def _blocked_on_chunks(observer, pid):
    async with asyncio.timeout(3):
        while not await observer.fetchval(
            "SELECT EXISTS(SELECT 1 FROM pg_locks WHERE pid=$1 "
            "AND relation='chunks'::regclass AND mode='ShareLock' AND NOT granted)", pid,
        ):
            await asyncio.sleep(0.005)


async def test_writer_lock_cycle_recovers_in_the_same_boot_without_replaying_migrations(monkeypatch, caplog):
    migrations = []
    async def applied(conn, filenames):
        migrations.append(filenames)
    monkeypatch.setattr(pg, "_apply_pending_migrations", applied)
    monkeypatch.setattr(pg, "_run_init_schema", partial(pg._run_init_schema, lock_timeout=0.1, backoff=0.02))
    async with _empty_database() as dsn:
        boot, writer, observer = await asyncio.gather(*(asyncpg.connect(dsn) for _ in range(3)))
        task = None
        try:
            await observer.execute(_INIT_SQL)
            await writer.execute("BEGIN; LOCK TABLE chunks IN ROW EXCLUSIVE MODE")
            task = asyncio.create_task(pg._run_boot_schema(boot, init_sql=_INIT_SQL))
            await _blocked_on_chunks(observer, boot.get_server_pid())
            assert await observer.fetchval(
                "SELECT EXISTS(SELECT 1 FROM pg_locks WHERE pid=$1 "
                "AND relation='documents'::regclass AND mode='ShareLock' AND granted)", boot.get_server_pid(),
            )
            # Opposite-order writer would deadlock without releasing the whole
            # init.sql transaction on timeout. It must finish before boot retries.
            await asyncio.wait_for(writer.execute("LOCK TABLE documents IN ROW EXCLUSIVE MODE"), 2)
            await writer.execute("ROLLBACK")
            await asyncio.wait_for(task, 4)
            assert len(migrations) == 1
            assert "Bootstrap schema blocked" in caplog.text
            assert await boot.fetchval("SELECT 1") == 1
            assert await boot.fetchval("SHOW lock_timeout") == "0"
        finally:
            if task is not None and not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
            await asyncio.gather(*(conn.close() for conn in (boot, writer, observer)))


async def test_exhaustion_rolls_back_schema_effects_and_releases_the_outer_lock(monkeypatch):
    monkeypatch.setattr(pg, "_apply_pending_migrations", _no_migrations)
    monkeypatch.setattr(pg, "_run_init_schema", partial(pg._run_init_schema, retries=2, lock_timeout=0.02, backoff=0))
    async with _empty_database() as dsn:
        boot, writer = await asyncio.gather(asyncpg.connect(dsn), asyncpg.connect(dsn))
        try:
            await writer.execute(_INIT_SQL)
            await writer.execute("BEGIN; LOCK TABLE chunks IN ROW EXCLUSIVE MODE")
            with pytest.raises(asyncpg.LockNotAvailableError):
                await pg._run_boot_schema(boot, init_sql="CREATE TABLE boot_attempt_receipt(id int);" + _INIT_SQL)
            assert await boot.fetchval("SELECT to_regclass('boot_attempt_receipt')") is None
            assert await boot.fetchval("SELECT 1") == 1
            assert await writer.fetchval("SELECT pg_try_advisory_lock($1)", pg._MIGRATION_LOCK_KEY)
            await writer.execute("SELECT pg_advisory_unlock($1)", pg._MIGRATION_LOCK_KEY)
        finally:
            await asyncio.gather(boot.close(), writer.close())


async def test_cancellation_releases_schema_and_boot_locks_for_the_next_caller(monkeypatch):
    monkeypatch.setattr(pg, "_apply_pending_migrations", _no_migrations)
    async with _empty_database() as dsn:
        boot, writer, observer = await asyncio.gather(*(asyncpg.connect(dsn) for _ in range(3)))
        task = None
        try:
            await observer.execute(_INIT_SQL)
            await writer.execute("BEGIN; LOCK TABLE chunks IN ROW EXCLUSIVE MODE")
            task = asyncio.create_task(pg._run_boot_schema(boot, init_sql=_INIT_SQL))
            await _blocked_on_chunks(observer, boot.get_server_pid())
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            await writer.execute("ROLLBACK")
            await asyncio.wait_for(pg._run_boot_schema(observer, init_sql=_INIT_SQL), 4)
        finally:
            if task is not None and not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
            await asyncio.gather(*(conn.close() for conn in (boot, writer, observer)))


async def test_deadlock_retry_is_atomic_and_non_lock_errors_are_not_retried(monkeypatch):
    monkeypatch.setattr(pg, "_apply_pending_migrations", _no_migrations)
    monkeypatch.setattr(pg, "_run_init_schema", partial(pg._run_init_schema, backoff=0))
    async with _empty_database() as dsn:
        conn = await asyncpg.connect(dsn)
        try:
            await conn.execute(_INIT_SQL)
            await conn.execute("CREATE SEQUENCE boot_retry_sequence")
            await pg._run_boot_schema(conn, init_sql="""
                CREATE TABLE boot_atomic_receipt(id int);
                SELECT nextval('boot_retry_sequence');
                DO $$ BEGIN
                    IF currval('boot_retry_sequence') = 1 THEN
                        RAISE EXCEPTION 'synthetic lock victim' USING ERRCODE = '40P01';
                    END IF;
                END $$;
            """)
            assert await conn.fetchval("SELECT last_value FROM boot_retry_sequence") == 2
            assert await conn.fetchval("SELECT to_regclass('boot_atomic_receipt')") is not None
            with pytest.raises(asyncpg.PostgresSyntaxError):
                await pg._run_boot_schema(conn, init_sql="""
                    SELECT nextval('boot_retry_sequence');
                    CREATE TABLE boot_invalid_receipt(id int);
                    NOT VALID SQL;
                """)
            assert await conn.fetchval("SELECT to_regclass('boot_invalid_receipt')") is None
            # Multi-statement syntax is parsed before it executes; no retry is
            # permitted, irrespective of when PostgreSQL refuses the statement.
            assert await conn.fetchval("SELECT last_value FROM boot_retry_sequence") == 2
        finally:
            await conn.close()
