"""Real PostgreSQL proofs for personal delivery, privacy and replay semantics."""

from __future__ import annotations

import pytest

from app.repositories.notifications_repo import enqueue_notification_event
from app.services import notification_service as service
from tests.test_event_tail_postgres import _fresh_database

pytestmark = pytest.mark.asyncio


async def seed(conn):
    owner = await conn.fetchval("INSERT INTO users(username,email,password_hash) VALUES('notify-owner','owner@example.invalid','unused') RETURNING id")
    reader = await conn.fetchval("INSERT INTO users(username,email,password_hash) VALUES('notify-reader','reader@example.invalid','unused') RETURNING id")
    stranger = await conn.fetchval("INSERT INTO users(username,email,password_hash) VALUES('notify-stranger','stranger@example.invalid','unused') RETURNING id")
    vault = await conn.fetchval("INSERT INTO vaults(name,git_path,owner_id) VALUES('notify-vault','/tmp/notify-test.git',$1) RETURNING id", owner)
    await conn.execute("INSERT INTO vault_access(vault_id,user_id,role) VALUES($1,$2,'reader')", vault, reader)
    doc = await conn.fetchval("INSERT INTO documents(vault_id,path,title,status) VALUES($1,'notes/example.md','Private example','active') RETURNING id", vault)
    await conn.execute("INSERT INTO notification_subscriptions(user_id,resource_id,vault_id) VALUES($1,$2,$3)", reader, doc, vault)
    return owner, reader, stranger, vault, doc


async def queue(conn, key, owner, vault, doc, kind="document.update", recipients=None):
    await enqueue_notification_event(conn, kind, source_key=key, vault_id=vault,
                                     resource_id=doc, actor_id=str(owner), recipient_ids=recipients)


async def drain(conn):
    async with conn.transaction():
        rows = await conn.fetch("SELECT * FROM notification_work WHERE processed_at IS NULL ORDER BY id")
        for row in rows:
            await service.process_work(conn, row)


async def inbox(conn, user, *, cursor=None, limit=20, state="all", category="all"):
    return await service.list_notifications(conn, user, state, cursor, limit, category)


async def test_category_filter_precedes_pagination_and_combines_with_unread():
    async with _fresh_database() as pool, pool.acquire() as conn:
        owner, reader, _, vault, doc = await seed(conn)
        for index in range(5):
            await queue(conn, f"category-doc-{index}", owner, vault, doc, "document.move")
            await queue(conn, f"category-access-{index}", owner, vault, None, "access.changed", [str(reader)])
        await drain(conn)
        full = await inbox(conn, reader)
        assert full["category"] == "all"
        assert len(full["items"]) == 10
        for category, prefix in [("documents", "document."), ("access", "access.")]:
            first = await inbox(conn, reader, category=category, limit=2)
            second = await inbox(conn, reader, category=category, limit=2, cursor=first["next_cursor"])
            third = await inbox(conn, reader, category=category, limit=2, cursor=second["next_cursor"])
            pages = [first, second, third]
            assert [len(page["items"]) for page in pages] == [2, 2, 1]
            assert third["next_cursor"] is None
            assert len({item["id"] for page in pages for item in page["items"]}) == 5
            assert all(item["kind"].startswith(prefix) for page in pages for item in page["items"])
            assert all(page["category"] == category for page in pages)
            assert all(page["unread_count"] == full["unread_count"] for page in pages)
            assert all(page["snapshot"] == full["snapshot"] for page in pages)
        await conn.execute("UPDATE user_notifications SET read_version=version WHERE user_id=$1 AND kind='document.move'", reader)
        documents = await inbox(conn, reader, category="documents", state="unread")
        access = await inbox(conn, reader, category="access", state="unread")
        assert documents["items"] == []
        assert documents["next_cursor"] is None
        assert documents["unread_count"] == access["unread_count"] == 5
        assert len(access["items"]) == 5


async def test_category_filter_preserves_current_acl_and_safe_access_redaction():
    async with _fresh_database() as pool, pool.acquire() as conn:
        owner, reader, stranger, vault, doc = await seed(conn)
        await queue(conn, "category-private", owner, vault, doc)
        await queue(conn, "category-revoked", owner, vault, None, "access.revoked", [str(reader)])
        await drain(conn)
        await conn.execute("DELETE FROM vault_access WHERE user_id=$1", reader)
        documents = await inbox(conn, reader, category="documents")
        access = await inbox(conn, reader, category="access")
        assert documents["items"] == []
        assert documents["unread_count"] == access["unread_count"] == 1
        assert len(access["items"]) == 1
        assert access["items"][0]["target"] is None
        assert "Private example" not in str(access)
        assert "notify-vault" not in str(access)
        assert (await inbox(conn, stranger, category="access"))["items"] == []


async def test_read_snapshot_does_not_swallow_later_updates_or_other_users():
    from app.api.routes.notifications import ReadChange, ReadSnapshot, change_read, mark_read
    from app.exceptions import AKBError
    from app.services.auth_service import AuthenticatedUser

    async with _fresh_database() as pool, pool.acquire() as conn:
        owner, reader, stranger, vault, doc = await seed(conn)
        user = AuthenticatedUser(user_id=str(reader), username="reader", email="reader@example.invalid",
                                 display_name=None, is_admin=False, auth_method="jwt")
        outsider = AuthenticatedUser(user_id=str(stranger), username="stranger", email="stranger@example.invalid",
                                     display_name=None, is_admin=False, auth_method="jwt")
        await queue(conn, "read-first", owner, vault, doc)
        await drain(conn)
        before = await inbox(conn, reader)
        item = before["items"][0]
        await queue(conn, "read-later", owner, vault, doc)
        await drain(conn)
        with pytest.raises(AKBError) as stale:
            await change_read(item["id"], ReadChange(read=True, version=item["version"]), user)
        assert stale.value.status_code == 409
        await mark_read(ReadSnapshot(snapshot=before["snapshot"]), user)
        current = await inbox(conn, reader)
        assert current["unread_count"] == 1
        latest = current["items"][0]
        with pytest.raises(AKBError) as denied:
            await change_read(latest["id"], ReadChange(read=True, version=latest["version"]), outsider)
        assert denied.value.status_code == 409
        await change_read(latest["id"], ReadChange(read=True, version=latest["version"]), user)
        assert (await inbox(conn, reader))["unread_count"] == 0
        await change_read(latest["id"], ReadChange(read=False, version=latest["version"]), user)
        assert (await inbox(conn, reader))["unread_count"] == 1


async def test_worker_recovers_expired_claim_and_rolls_back_failed_delivery(monkeypatch):
    from app.services import notification_worker as worker

    async with _fresh_database() as pool, pool.acquire() as conn:
        owner, reader, _, vault, doc = await seed(conn)
        await queue(conn, "worker-recovery", owner, vault, doc)
        await conn.execute("UPDATE notification_work SET lease_until=now()-interval '1 minute',attempts=1")
        original = service.process_work

        async def fail_after_delivery(connection, work):
            await original(connection, work)
            raise RuntimeError("simulated worker interruption")

        with monkeypatch.context() as patch:
            patch.setattr(service, "process_work", fail_after_delivery)
            assert await worker.run_once()
        assert (await inbox(conn, reader))["unread_count"] == 0
        assert await conn.fetchval("SELECT count(*) FROM notification_deliveries") == 0
        await conn.execute("UPDATE notification_work SET available_at=now()-interval '1 minute'")
        assert await worker.run_once()
        assert (await inbox(conn, reader))["unread_count"] == 1
        assert not await worker.run_once()


async def test_domain_rollback_discards_notification_work():
    async with _fresh_database() as pool, pool.acquire() as conn:
        owner, _, _, vault, doc = await seed(conn)
        with pytest.raises(RuntimeError, match="rollback"):
            async with conn.transaction():
                await queue(conn, "rollback", owner, vault, doc)
                raise RuntimeError("rollback")
        assert await conn.fetchval("SELECT count(*) FROM notification_work") == 0


async def test_watch_started_during_long_domain_transaction_receives_later_enqueue():
    async with _fresh_database() as pool, pool.acquire() as first, pool.acquire() as second:
        owner, reader, _, vault, doc = await seed(first)
        await first.execute("DELETE FROM notification_subscriptions WHERE user_id=$1", reader)
        async with first.transaction():
            await first.fetchval("SELECT now()")
            await second.execute("INSERT INTO notification_subscriptions(user_id,resource_id,vault_id) VALUES($1,$2,$3)", reader, doc, vault)
            await queue(first, "late-enqueue", owner, vault, doc)
        await drain(second)
        assert (await inbox(second, reader))["unread_count"] == 1


async def test_late_committing_lower_id_is_not_skipped_and_replay_is_idempotent():
    async with _fresh_database() as pool, pool.acquire() as first, pool.acquire() as second:
        owner, reader, _, vault, doc = await seed(first)
        transaction = first.transaction()
        await transaction.start()
        try:
            await queue(first, "late", owner, vault, doc, "document.move")
            await queue(second, "early", owner, vault, doc, "document.archive")
            await drain(second)
            assert len((await inbox(second, reader))["items"]) == 1
            await transaction.commit()
        except BaseException:
            await transaction.rollback()
            raise
        await drain(second)
        result = await inbox(second, reader)
        assert len(result["items"]) == 2
        snapshot = result["snapshot"]
        # Simulate a replay after a publisher failed to acknowledge its delivery.
        await second.execute("UPDATE notification_work SET processed_at=NULL")
        await drain(second)
        assert (await inbox(second, reader))["snapshot"] == snapshot
        assert await second.fetchval("SELECT count(*) FROM notification_deliveries") == 2


async def test_grouped_update_reopens_read_item_without_duplicate_rows():
    async with _fresh_database() as pool, pool.acquire() as conn:
        owner, reader, _, vault, doc = await seed(conn)
        await queue(conn, "update-1", owner, vault, doc)
        await drain(conn)
        initial = await inbox(conn, reader)
        await conn.execute("UPDATE user_notifications SET read_version=version WHERE user_id=$1", reader)
        await queue(conn, "update-2", owner, vault, doc)
        # Pin both event-time buckets to keep this proof independent of wall time.
        await conn.execute("UPDATE notification_work SET created_at=(SELECT created_at FROM notification_work WHERE source_key='update-1') WHERE source_key='update-2'")
        await drain(conn)
        result = await inbox(conn, reader)
        assert len(result["items"]) == 1
        assert result["unread_count"] == 1
        assert result["items"][0]["id"] == initial["items"][0]["id"]
        assert int(result["items"][0]["version"]) > int(initial["snapshot"])


async def test_revocation_removes_content_from_list_count_but_keeps_safe_account_notice():
    async with _fresh_database() as pool, pool.acquire() as conn:
        owner, reader, stranger, vault, doc = await seed(conn)
        await queue(conn, "private-update", owner, vault, doc)
        await drain(conn)
        assert (await inbox(conn, reader))["unread_count"] == 1
        assert (await inbox(conn, stranger))["items"] == []
        await conn.execute("DELETE FROM vault_access WHERE user_id=$1", reader)
        assert (await inbox(conn, reader))["items"] == []
        assert (await service.metadata(conn, reader))["unread_count"] == 0
        await queue(conn, "revoke", owner, vault, doc, "access.revoked", [str(reader)])
        await drain(conn)
        result = await inbox(conn, reader)
        assert len(result["items"]) == 1
        assert result["items"][0]["target"] is None
        assert "Private example" not in str(result)
        assert "notes/example.md" not in str(result)
        assert "notify-vault" not in str(result)


async def test_subscription_is_explicit_and_new_watchers_do_not_receive_queued_history():
    async with _fresh_database() as pool, pool.acquire() as conn:
        owner, reader, stranger, vault, doc = await seed(conn)
        await queue(conn, "before-watch", owner, vault, doc)
        await conn.execute("INSERT INTO vault_access(vault_id,user_id,role) VALUES($1,$2,'reader')", vault, stranger)
        await conn.execute("INSERT INTO notification_subscriptions(user_id,resource_id,vault_id) VALUES($1,$2,$3)", stranger, doc, vault)
        await drain(conn)
        assert (await inbox(conn, reader))["unread_count"] == 1
        assert (await inbox(conn, stranger))["unread_count"] == 0
        await queue(conn, "after-watch", owner, vault, doc, "document.move")
        await drain(conn)
        assert (await inbox(conn, stranger))["unread_count"] == 1


async def test_own_change_is_suppressed_and_subscription_survives_document_move():
    async with _fresh_database() as pool, pool.acquire() as conn:
        owner, reader, _, vault, doc = await seed(conn)
        await queue(conn, "own", reader, vault, doc)
        await drain(conn)
        assert (await inbox(conn, reader))["unread_count"] == 0
        await conn.execute("UPDATE documents SET path='other/renamed.md',title='Renamed example' WHERE id=$1", doc)
        await queue(conn, "moved", owner, vault, doc, "document.move")
        await drain(conn)
        item = (await inbox(conn, reader))["items"][0]
        assert item["title"] == "Renamed example"
        assert item["target"]["uri"].endswith("/coll/other/doc/renamed.md")
        assert await conn.fetchval("SELECT resource_id FROM notification_subscriptions WHERE user_id=$1", reader) == doc


async def test_deleted_document_has_safe_notice_without_stale_target():
    async with _fresh_database() as pool, pool.acquire() as conn:
        owner, reader, _, vault, doc = await seed(conn)
        async with conn.transaction():
            await queue(conn, "deleted", owner, vault, doc, "document.delete")
            await conn.execute("DELETE FROM documents WHERE id=$1", doc)
        await drain(conn)
        items = (await inbox(conn, reader))["items"]
        assert len(items) == 1
        assert items[0]["target"] is None
        assert "Private example" not in str(items)


async def test_pagination_returns_disjoint_pages_and_rejects_invalid_cursor():
    async with _fresh_database() as pool, pool.acquire() as conn:
        owner, reader, _, vault, doc = await seed(conn)
        for index in range(5):
            await queue(conn, f"page-{index}", owner, vault, doc, "document.move")
        await drain(conn)
        first = await inbox(conn, reader, limit=2)
        second = await inbox(conn, reader, limit=2, cursor=first["next_cursor"])
        third = await inbox(conn, reader, limit=2, cursor=second["next_cursor"])
        assert len({item["id"] for page in [first, second, third] for item in page["items"]}) == 5
        assert third["next_cursor"] is None
        from app.exceptions import AKBError
        with pytest.raises(AKBError):
            await inbox(conn, reader, cursor="not-a-cursor")


async def test_cleanup_preserves_pending_and_failed_work_independent_of_source_events():
    async with _fresh_database() as pool, pool.acquire() as conn:
        owner, _, _, vault, doc = await seed(conn)
        await queue(conn, "pending", owner, vault, doc)
        await queue(conn, "failed", owner, vault, doc)
        await conn.execute("UPDATE notification_work SET created_at=now()-interval '200 days'")
        await conn.execute("UPDATE notification_work SET failed_at=now() WHERE source_key='failed'")
        await service.cleanup(conn)
        assert await conn.fetchval("SELECT count(*) FROM notification_work") == 2


async def test_notification_schema_is_idempotent_on_existing_install():
    async with _fresh_database() as pool, pool.acquire() as conn:
        import importlib.util
        from pathlib import Path
        path = Path(__file__).parents[1] / "app/db/migrations/099_personal_notifications.py"
        spec = importlib.util.spec_from_file_location("notification_migration", path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        await module.migrate(conn)
        await module.migrate(conn)
        await conn.execute("SELECT id FROM notification_work LIMIT 0")
