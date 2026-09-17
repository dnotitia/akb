"""The Home watch filter is server-side, identity-based and permission-safe."""

import uuid

import pytest

from app.config import settings
from app.services.auth_service import AuthenticatedUser
from app.services.native_revision_backend import NativeRevisionBackend
from app.services.revision_backend import LegacyRevisionBackend
from tests.test_event_tail_postgres import _fresh_database
from tests.test_notification_native_postgres import setup_native
from tests.test_notifications_postgres import seed, queue, drain, inbox

pytestmark = pytest.mark.asyncio


@pytest.fixture
def activity(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "git_storage_path", str(tmp_path))
    from app.api.routes import activity
    return activity


def principal(user_id):
    return AuthenticatedUser(user_id=str(user_id), username="reader", email="r@example.invalid",
                             display_name=None, is_admin=False, auth_method="jwt")


async def test_watching_filters_before_limit_and_pages_ties_without_touching_inbox(monkeypatch, tmp_path, activity):
    monkeypatch.setattr(settings, "git_storage_path", str(tmp_path))
    monkeypatch.setattr(settings, "notifications_enabled", True)
    monkeypatch.setattr(activity, "revision_backend", LegacyRevisionBackend())
    async with _fresh_database() as pool, pool.acquire() as conn:
        owner, reader, _, vault, first = await seed(conn)
        second = await conn.fetchval("""INSERT INTO documents(vault_id,path,title)
            VALUES($1,'other.md','Other') RETURNING id""", vault)
        await conn.execute("""INSERT INTO notification_subscriptions(user_id,resource_id,vault_id)
            VALUES($1,$2,$3)""", reader, second, vault)
        await conn.execute("UPDATE documents SET updated_at='2026-01-01T00:00:00Z'")
        await conn.execute("""INSERT INTO documents(vault_id,path,title)
            SELECT $1, 'new-' || n || '.md', 'New' FROM generate_series(1,105) n""", vault)
        await queue(conn, "watch-home-not-read", owner, vault, first)
        await drain(conn)
        before = await inbox(conn, reader)
        user = principal(reader)
        all_page = await activity.recent_changes(vault=None, limit=100, user=user)
        assert all_page["scope"] == "all"
        assert str(first) not in [r["resource_id"] for r in all_page["changes"]]
        page = await activity.recent_changes(vault=None, limit=1, user=user, scope="watching")
        last = await activity.recent_changes(vault=None, limit=1, user=user, scope="watching",
                                             cursor=page["next_cursor"])
        assert page["scope"] == "watching"
        assert {page["changes"][0]["resource_id"], last["changes"][0]["resource_id"]} == {str(first), str(second)}
        assert last["next_cursor"] is None
        assert await inbox(conn, reader) == before
        # Moving/renaming preserves the watch; duplicate titles remain separate identities.
        await conn.execute("UPDATE documents SET path='moved/renamed.md', title='Other' WHERE id=$1", first)
        moved = await activity.recent_changes(vault=None, limit=20, user=user, scope="watching")
        assert len(moved["changes"]) == 2
        assert any(r["path"] == "moved/renamed.md" for r in moved["changes"])
        await conn.execute("DELETE FROM notification_subscriptions WHERE resource_id=$1", second)
        assert len((await activity.recent_changes(vault=None, limit=20, user=user, scope="watching"))["changes"]) == 1
        await conn.execute("DELETE FROM vault_access WHERE user_id=$1", reader)
        assert (await activity.recent_changes(vault=None, limit=20, user=user, scope="watching"))["changes"] == []
        # Public read restores visibility, never watch another user's resources.
        await conn.execute("UPDATE vaults SET public_access='reader' WHERE id=$1", vault)
        assert len((await activity.recent_changes(vault=None, limit=20, user=user, scope="watching"))["changes"]) == 1
        assert (await activity.recent_changes(vault=None, limit=20, user=principal(owner), scope="watching"))["changes"] == []
        await conn.execute("DELETE FROM documents WHERE id=$1", first)
        assert (await activity.recent_changes(vault=None, limit=20, user=user, scope="watching"))["changes"] == []


async def test_native_watching_uses_live_native_identity_and_acl(monkeypatch, activity):
    monkeypatch.setattr(settings, "notifications_enabled", True)
    async with _fresh_database() as pool:
        native, current, _, reader, vault = await setup_native(pool)
        backend = NativeRevisionBackend(pool=pool)
        monkeypatch.setattr(activity, "revision_backend", backend)
        user = principal(reader)
        page = await activity.recent_changes(vault=None, limit=1, user=user, scope="watching")
        assert page["changes"][0]["title"] == "Native title"
        assert page["changes"][0]["resource_id"] == str(current.resource_id)
        await native.create_text(namespace_id=vault, surface="document", path="unwatched.md",
                                 payload="New unwatched", actor="native-owner", mutation_id=uuid.uuid4())
        assert len((await activity.recent_changes(vault=None, limit=1, user=user, scope="watching"))["changes"]) == 1
        current = await native.move_text(namespace_id=vault, surface="document", path="first.md",
                                        path_to="moved.md", actor="native-owner", mutation_id=uuid.uuid4(),
                                        expected_resource_id=current.resource_id, expected_revision_id=current.revision_id)
        assert (await activity.recent_changes(vault=None, limit=1, user=user, scope="watching"))["changes"][0]["path"] == "moved.md"
        async with pool.acquire() as conn:
            await conn.execute("DELETE FROM vault_access WHERE user_id=$1", reader)
        assert await backend.recent_changes(str(reader), vault="native-notify", limit=20, watching=True) == []
        async with pool.acquire() as conn:
            await conn.execute("INSERT INTO vault_access(vault_id,user_id,role) VALUES($1,$2,'reader')", vault, reader)
        await native.delete_resource(namespace_id=vault, surface="document", path="moved.md", actor="native-owner",
                                     mutation_id=uuid.uuid4(), expected_resource_id=current.resource_id,
                                     expected_revision_id=current.revision_id)
        assert (await activity.recent_changes(vault=None, limit=20, user=user, scope="watching"))["changes"] == []


async def test_native_cursor_pages_equal_timestamps(monkeypatch, activity):
    monkeypatch.setattr(settings, "notifications_enabled", True)
    async with _fresh_database() as pool:
        native, first, _, reader, vault = await setup_native(pool)
        second = await native.create_text(namespace_id=vault, surface="document", path="second.md",
                                          payload="Second", actor="native-owner", mutation_id=uuid.uuid4())
        async with pool.acquire() as conn:
            await conn.execute("""INSERT INTO notification_subscriptions(user_id,resource_id,vault_id)
                VALUES($1,$2,$3)""", reader, second.resource_id, vault)
            await conn.execute("UPDATE native_resources SET updated_at='2026-09-01T00:00:00Z'")
        monkeypatch.setattr(activity, "revision_backend", NativeRevisionBackend(pool=pool))
        page = await activity.recent_changes(vault=None, limit=1, user=principal(reader), scope="watching")
        last = await activity.recent_changes(vault=None, limit=1, user=principal(reader), scope="watching",
                                             cursor=page["next_cursor"])
        assert page["next_cursor"] is not None
        assert last["next_cursor"] is None
        assert {r["resource_id"] for p in [page, last] for r in p["changes"]} == {
            str(first.resource_id), str(second.resource_id)}
