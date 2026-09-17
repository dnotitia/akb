"""PostgreSQL-only inbox worker with expiring claims and bounded retries."""
import asyncio
import logging

from app.config import settings
from app.db.postgres import get_pool
from app.services import notification_service

logger = logging.getLogger(__name__)
_task = None
_stopping = False


async def run_once():
    pool = await get_pool()
    async with pool.acquire() as conn:
        work = await conn.fetchrow("""WITH candidate AS (
            SELECT id FROM notification_work WHERE processed_at IS NULL AND failed_at IS NULL
             AND available_at<=now() AND (lease_until IS NULL OR lease_until<now())
             ORDER BY id FOR UPDATE SKIP LOCKED LIMIT 1)
            UPDATE notification_work w SET lease_until=now()+interval '60 seconds',attempts=attempts+1
            FROM candidate c WHERE w.id=c.id RETURNING w.*""")
        if work is None:
            return False
        try:
            async with conn.transaction():
                # Lock prevents an expired claimant racing delivery with us.
                current = await conn.fetchrow("SELECT * FROM notification_work WHERE id=$1 FOR UPDATE", work["id"])
                if current["processed_at"] is None and current["attempts"] == work["attempts"]:
                    await notification_service.process_work(conn, current)
        except Exception:
            logger.exception("Notification delivery failed for work %s", work["id"])
            await conn.execute("""UPDATE notification_work SET lease_until=NULL,
                available_at=now()+make_interval(secs => LEAST(3600, power(2,LEAST(attempts,10))::int)),
                failed_at=CASE WHEN attempts>=8 THEN now() ELSE NULL END
                WHERE id=$1 AND attempts=$2 AND processed_at IS NULL""", work["id"], work["attempts"])
        return True


async def pending_stats():
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow("""SELECT
            count(*) FILTER(WHERE processed_at IS NULL AND failed_at IS NULL) pending,
            count(*) FILTER(WHERE failed_at IS NOT NULL) failed,
            min(created_at) FILTER(WHERE processed_at IS NULL AND failed_at IS NULL) oldest_pending
            FROM notification_work""")
    return {"enabled": settings.notifications_enabled, **dict(row)}


async def _run():
    sweep_at = 0.0
    while not _stopping:
        try:
            if asyncio.get_running_loop().time() >= sweep_at:
                pool = await get_pool()
                async with pool.acquire() as conn:
                    await notification_service.cleanup(conn)
                sweep_at = asyncio.get_running_loop().time() + 3600
            if await run_once():
                continue
        except Exception:
            logger.exception("Notification worker iteration failed")
        await asyncio.sleep(2)


def start():
    global _task, _stopping
    if not settings.notifications_enabled or (_task and not _task.done()):
        return
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return
    _stopping = False
    _task = loop.create_task(_run(), name="notification_worker")


def request_stop():
    global _stopping
    _stopping = True


async def stop():
    global _task
    request_stop()
    if _task:
        _task.cancel()
        try:
            await _task
        except asyncio.CancelledError:
            pass
        _task = None
