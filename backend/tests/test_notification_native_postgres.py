"""Native authority notification proofs, with no legacy document projection."""
import uuid
from types import SimpleNamespace

import pytest

from app.config import settings
from app.services import notification_service as inbox
from app.services.m1_pg_body_store import M1PgBodyStore
from app.services.native_revision_service import NativeRevisionService
from tests.test_event_tail_postgres import _fresh_database
from tests.test_notifications_postgres import drain

pytestmark = pytest.mark.asyncio


@pytest.fixture(autouse=True)
def native_authority(monkeypatch):
    from app.services import revision_backend
    # Other suites import REST/MCP modules that select the process backend.
    # Runtime selection intentionally wins over settings, so isolate both and
    # restore the previous process selection after each case.
    monkeypatch.setattr(settings, "document_revision_backend", "postgres_native")
    monkeypatch.setattr(revision_backend, "_selected_backend", "postgres_native")


async def setup_native(pool):
    async with pool.acquire() as conn:
        owner = await conn.fetchval("INSERT INTO users(username,email,password_hash) VALUES('native-owner','native-owner@example.invalid','unused') RETURNING id")
        reader = await conn.fetchval("INSERT INTO users(username,email,password_hash) VALUES('native-reader','native-reader@example.invalid','unused') RETURNING id")
        vault = await conn.fetchval("INSERT INTO vaults(name,git_path,owner_id) VALUES('native-notify','/tmp/unused.git',$1) RETURNING id", owner)
        await conn.execute("INSERT INTO vault_access(vault_id,user_id,role) VALUES($1,$2,'reader')", vault, reader)
    service = NativeRevisionService(pool, payload_store=M1PgBodyStore(pool))
    created = await service.create_text(namespace_id=vault, surface="document", path="first.md",
                                        payload="---\ntitle: Native title\nstatus: active\n---\nFirst body",
                                        actor="native-owner", mutation_id=uuid.uuid4())
    async with pool.acquire() as conn:
        assert await conn.fetchval("SELECT count(*) FROM documents WHERE id=$1", created.resource_id) == 0
        await conn.execute("INSERT INTO notification_subscriptions(user_id,resource_id,vault_id) VALUES($1,$2,$3)", reader, created.resource_id, vault)
    return service, created, owner, reader, vault


async def test_native_move_archive_restore_delete_inbox_and_cleanup():
    async with _fresh_database() as pool:
        native, current, _, reader, vault = await setup_native(pool)
        doc_id = current.resource_id
        current = await native.move_text(namespace_id=vault, surface="document", path="first.md", path_to="renamed.md",
                                        actor="native-owner", mutation_id=uuid.uuid4(), expected_resource_id=doc_id,
                                        expected_revision_id=current.revision_id)
        for previous_status, status in [("active", "archived"), ("archived", "active")]:
            current = await native.replace_text(namespace_id=vault, surface="document", path="renamed.md",
                                                payload=f"---\ntitle: Updated native title\nstatus: {status}\n---\nBody",
                                                actor="native-owner", mutation_id=uuid.uuid4(), expected_resource_id=doc_id,
                                                expected_revision_id=current.revision_id,
                                                notification_previous_status=previous_status, notification_status=status)
        async with pool.acquire() as conn:
            await drain(conn)
            page = await inbox.list_notifications(conn, reader, "all", None, 20)
            assert {item["kind"] for item in page["items"]} == {"document.move", "document.archive", "document.restore"}
            assert page["unread_count"] == 3
            assert all(item["target"]["uri"].endswith("/doc/renamed.md") for item in page["items"])
            await inbox.cleanup(conn)
            assert await conn.fetchval("SELECT count(*) FROM notification_subscriptions WHERE resource_id=$1", doc_id) == 1
        await native.delete_resource(namespace_id=vault, surface="document", path="renamed.md", actor="native-owner",
                                     mutation_id=uuid.uuid4(), expected_revision_id=current.revision_id, expected_resource_id=doc_id)
        async with pool.acquire() as conn:
            await drain(conn)
            page = await inbox.list_notifications(conn, reader, "all", None, 20)
            assert len(page["items"]) == 1
            assert page["items"][0]["kind"] == "document.delete"
            assert page["items"][0]["target"] is None
            assert "Updated native title" not in str(page)


async def test_native_cleanup_preserves_watch_through_delete_and_restore():
    async with _fresh_database() as pool:
        native, current, _, reader, vault = await setup_native(pool)
        doc_id = current.resource_id
        deleted = await native.delete_resource(
            namespace_id=vault, surface="document", path="first.md", actor="native-owner",
            mutation_id=uuid.uuid4(), expected_revision_id=current.revision_id,
            expected_resource_id=doc_id,
        )
        async with pool.acquire() as conn:
            await drain(conn)
            await inbox.cleanup(conn)
            assert await conn.fetchval(
                "SELECT count(*) FROM notification_subscriptions WHERE user_id=$1 AND resource_id=$2",
                reader, doc_id,
            ) == 1
            page = await inbox.list_notifications(conn, reader, "all", None, 20)
            assert [item["kind"] for item in page["items"]] == ["document.delete"]
            assert page["items"][0]["target"] is None
        restored = await native.restore_text(
            namespace_id=vault, surface="document", path="first.md", payload="Restored body",
            actor="native-owner", mutation_id=uuid.uuid4(), expected_revision_id=deleted.revision_id,
            expected_resource_id=doc_id,
        )
        await native.replace_text(
            namespace_id=vault, surface="document", path="first.md", payload="Updated restored body",
            actor="native-owner", mutation_id=uuid.uuid4(), expected_revision_id=restored.revision_id,
            expected_resource_id=doc_id,
        )
        async with pool.acquire() as conn:
            await drain(conn)
            page = await inbox.list_notifications(conn, reader, "all", None, 20)
            assert {item["kind"] for item in page["items"]} == {
                "document.delete", "document.restore", "document.update",
            }
            for item in page["items"]:
                if item["kind"] != "document.delete":
                    assert item["target"]["uri"] == "akb://native-notify/doc/first.md"


async def test_native_transaction_rollback_and_mutation_replay_do_not_duplicate():
    async with _fresh_database() as pool:
        native, current, _, _, vault = await setup_native(pool)
        def fail(boundary):
            if boundary == "authority.after_activity":
                raise RuntimeError("rollback-native-notification")
        broken = NativeRevisionService(pool, payload_store=M1PgBodyStore(pool), failpoint=fail)
        kwargs = dict(namespace_id=vault, surface="document", path="first.md", payload="Changed body",
                      actor="native-owner", mutation_id=uuid.uuid4(), expected_revision_id=current.revision_id,
                      expected_resource_id=current.resource_id)
        with pytest.raises(RuntimeError, match="rollback-native-notification"):
            await broken.replace_text(**kwargs)
        async with pool.acquire() as conn:
            assert await conn.fetchval("SELECT count(*) FROM notification_work") == 0
        changed = await native.replace_text(**kwargs)
        replay = await native.replace_text(**kwargs)
        assert changed.revision_id == replay.revision_id
        async with pool.acquire() as conn:
            assert await conn.fetchval("SELECT count(*) FROM notification_work") == 1


async def test_native_subscription_api_resolves_current_authority_and_move_alias():
    from app.api.routes import notifications as routes
    async with _fresh_database() as pool:
        native, current, _, reader, vault = await setup_native(pool)
        user = SimpleNamespace(user_id=str(reader))
        uri = "akb://native-notify/doc/first.md"
        assert (await routes.set_subscription(uri, user, False))["subscribed"] is False
        assert (await routes.set_subscription(uri, user, True))["resource_id"] == str(current.resource_id)
        await native.move_text(namespace_id=vault, surface="document", path="first.md", path_to="renamed.md",
                               actor="native-owner", mutation_id=uuid.uuid4(), expected_resource_id=current.resource_id,
                               expected_revision_id=current.revision_id)
        resolved = await routes.subscriptions(uri=uri, user=user)
        assert resolved == {"resource_id": str(current.resource_id), "subscribed": True}
        listed = await routes.subscriptions(user=user)
        assert len(listed["items"]) == 1
        assert listed["items"][0]["uri"] == "akb://native-notify/doc/renamed.md"
