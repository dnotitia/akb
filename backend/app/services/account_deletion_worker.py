"""Retry account role cleanup using DB locks; no credentials or account FK."""
from __future__ import annotations

import asyncio

from app.db.postgres import get_pool
from app.services._backfill import BackfillRunner
from app.services.role_sync import get_role_sync, token_role_name, user_role_name


async def process_once() -> int:
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute("""INSERT INTO account_deletion_worker_state(singleton,last_seen_at)
            VALUES(true,clock_timestamp()) ON CONFLICT(singleton) DO UPDATE SET last_seen_at=EXCLUDED.last_seen_at""")
    processed = 0
    for _ in range(8):
        async with pool.acquire() as conn:
            async with conn.transaction():
                row = await conn.fetchrow("""SELECT * FROM account_deletion_cleanup WHERE completed_at IS NULL
                    AND next_attempt_at<=clock_timestamp() ORDER BY next_attempt_at,role_kind,resource_id
                    FOR UPDATE SKIP LOCKED LIMIT 1""")
                if row is None:
                    break
                try:
                    async with conn.transaction():
                        await conn.execute("SET LOCAL lock_timeout='2s'")
                        table = "users" if row["role_kind"] == "user" else "tokens"
                        if await conn.fetchval(f"SELECT EXISTS(SELECT 1 FROM {table} WHERE id=$1)", row["resource_id"]):
                            raise RuntimeError("cleanup_resource_still_exists")
                        name = (user_role_name if row["role_kind"] == "user" else token_role_name)(row["resource_id"])
                        await get_role_sync()._drop_role_if_present(conn, name)
                except Exception as exc:
                    await conn.execute("""UPDATE account_deletion_cleanup SET attempts=attempts+1,
                        next_attempt_at=clock_timestamp()+make_interval(secs=>LEAST(3600,power(2,LEAST(attempts+1,11))::int)),
                        last_error_code=$3 WHERE role_kind=$1 AND resource_id=$2""",
                        row["role_kind"], row["resource_id"], type(exc).__name__)
                else:
                    await conn.execute("""UPDATE account_deletion_cleanup SET attempts=attempts+1,
                        completed_at=clock_timestamp(),last_error_code=NULL WHERE role_kind=$1 AND resource_id=$2""",
                        row["role_kind"], row["resource_id"])
                processed += 1
    return processed


async def pending_stats() -> dict:
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow("""SELECT count(*) AS pending,min(requested_at) AS oldest_pending
            FROM account_deletion_cleanup WHERE completed_at IS NULL""")
        ready = await conn.fetchval("""SELECT EXISTS(SELECT 1 FROM account_deletion_worker_state
            WHERE last_seen_at>clock_timestamp()-interval '60 seconds')""")
    return {**dict(row), "worker_ready": ready}


_runner = BackfillRunner("account_deletion_worker", process_once, idle_secs=10)
def start() -> None:
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return
    _runner.start()


stop = _runner.stop
