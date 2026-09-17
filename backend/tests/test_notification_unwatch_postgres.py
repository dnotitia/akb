"""Queued changes respect later explicit personal opt-out."""
import pytest

from tests.test_event_tail_postgres import _fresh_database
from tests.test_notifications_postgres import seed, queue, drain, inbox


@pytest.mark.asyncio
@pytest.mark.parametrize("resubscribe", [False, True])
async def test_unwatch_suppresses_pending_work_even_after_resubscribing(resubscribe):
    async with _fresh_database() as pool, pool.acquire() as conn:
        owner, reader, _, vault, doc = await seed(conn)
        await queue(conn, "before-unwatch", owner, vault, doc)
        await conn.execute("DELETE FROM notification_subscriptions WHERE user_id=$1", reader)
        if resubscribe:
            await conn.execute("INSERT INTO notification_subscriptions(user_id,resource_id,vault_id) VALUES($1,$2,$3)", reader, doc, vault)
        await drain(conn)
        assert (await inbox(conn, reader))["unread_count"] == 0
