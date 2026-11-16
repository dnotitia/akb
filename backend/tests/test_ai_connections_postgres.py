"""Real PostgreSQL proofs for the AI connection registry.

The throttle, the first-insert notification and the PAT cascade are decided by
PostgreSQL, not by Python, so they are only checked against a real server.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import pytest

from app.services import ai_connections
from app.services import notification_service as inbox
from app.services.auth_service import AuthenticatedUser
from tests.test_event_tail_postgres import _fresh_database

pytestmark = pytest.mark.asyncio

T0 = datetime(2026, 11, 2, 9, 0, tzinfo=timezone.utc)


@pytest.fixture(autouse=True)
def _clean_registry():
    ai_connections.reset()
    yield
    ai_connections.reset()


async def _user(conn, name: str, kind: str = "human") -> uuid.UUID:
    return await conn.fetchval(
        "INSERT INTO users(username,email,password_hash,account_kind) VALUES($1,$2,'unused',$3) RETURNING id",
        name, f"{name}@example.invalid", kind,
    )


async def _token(conn, user_id: uuid.UUID, name: str = "laptop") -> uuid.UUID:
    return await conn.fetchval(
        "INSERT INTO tokens(user_id,name,token_hash,token_prefix) VALUES($1,$2,$3,'akb_test') RETURNING id",
        user_id, name, uuid.uuid4().hex,
    )


def _pat(user_id, token_id) -> AuthenticatedUser:
    return AuthenticatedUser(
        user_id=str(user_id), username="u", email="u@example.invalid", display_name=None,
        is_admin=False, auth_method="pat", token_id=str(token_id), key_class="pat",
    )


def _oauth(user_id, azp="dcr-client-1") -> AuthenticatedUser:
    return AuthenticatedUser(
        user_id=str(user_id), username="u", email="u@example.invalid", display_name=None,
        is_admin=False, auth_method="oauth", oauth_scopes=["vault:read"], oauth_client_id=azp,
    )


def _queue(user, *, at: datetime, name=None, version=None, user_agent=None):
    """Put one observation straight into the pending map at a chosen time.

    Bypasses the in-process throttle on purpose: these tests are about the
    database throttle, which must hold across replicas that each have their
    own in-process state.
    """
    key, token_id = ai_connections.connection_key(user)
    ai_connections._pending[key] = ai_connections._Observation(
        token_id=token_id, client_name=name, client_version=version,
        user_agent=user_agent, seen_at=at,
    )


async def _row(conn, user_id):
    return await conn.fetchrow("SELECT * FROM ai_connections WHERE user_id=$1", user_id)


async def _drain_notifications(conn):
    async with conn.transaction():
        for work in await conn.fetch("SELECT * FROM notification_work WHERE processed_at IS NULL ORDER BY id"):
            await inbox.process_work(conn, work)


async def test_first_sighting_records_the_connection_and_notifies_once():
    async with _fresh_database() as pool, pool.acquire() as conn:
        alice = await _user(conn, "conn-alice")
        _queue(_oauth(alice), at=T0, name="claude-code", version="2.1.0", user_agent="claude-code/2.1.0")
        assert await ai_connections.flush_once() == 1

        row = await _row(conn, alice)
        assert (row["auth_kind"], row["client_key"], row["token_id"]) == ("oauth", "dcr-client-1", None)
        assert (row["client_name"], row["client_version"]) == ("claude-code", "2.1.0")
        assert row["first_seen_at"] == row["last_seen_at"] == T0

        # A second replica sees the same connection a minute later: an update,
        # never a second "new app" notice.
        _queue(_oauth(alice), at=T0 + timedelta(minutes=5), name="claude-code", version="2.1.0")
        await ai_connections.flush_once()
        work = await conn.fetch("SELECT * FROM notification_work")
        assert len(work) == 1
        assert work[0]["kind"] == "account.ai_connection"
        assert work[0]["recipient_ids"] == [alice]
        assert work[0]["resource_id"] == row["id"]

        await _drain_notifications(conn)
        page = await inbox.list_notifications(conn, alice, "all", None, 20)
        [item] = page["items"]
        assert item["kind"] == "account.ai_connection"
        assert item["title"] == "A new AI app connected to your account: claude-code"
        assert item["message"] == "A new AI app connected to your account"
        assert item["target"] is None
        assert page["unread_count"] == 1
        # Account notices are not vault access notices.
        assert (await inbox.list_notifications(conn, alice, "all", None, 20, "access"))["items"] == []


async def test_last_seen_is_written_at_most_once_a_minute():
    async with _fresh_database() as pool, pool.acquire() as conn:
        alice = await _user(conn, "conn-throttle")
        user = _oauth(alice)
        _queue(user, at=T0, name="claude-code", version="1")
        await ai_connections.flush_once()

        for seconds in (1, 30, 59):
            _queue(user, at=T0 + timedelta(seconds=seconds), user_agent=None)
            await ai_connections.flush_once()
            assert (await _row(conn, alice))["last_seen_at"] == T0

        _queue(user, at=T0 + timedelta(seconds=60))
        await ai_connections.flush_once()
        assert (await _row(conn, alice))["last_seen_at"] == T0 + timedelta(seconds=60)

        # An out-of-order (requeued) observation never moves it backwards.
        _queue(user, at=T0 + timedelta(seconds=200))
        await ai_connections.flush_once()
        _queue(user, at=T0 + timedelta(seconds=120), name="claude-code", version="2")
        await ai_connections.flush_once()
        row = await _row(conn, alice)
        assert row["last_seen_at"] == T0 + timedelta(seconds=200)
        assert row["first_seen_at"] == T0


async def test_reported_name_and_version_track_the_client_inside_the_window():
    async with _fresh_database() as pool, pool.acquire() as conn:
        alice = await _user(conn, "conn-version")
        user = _oauth(alice)
        _queue(user, at=T0, name="claude-code", version="2.1.0")
        await ai_connections.flush_once()

        # An upgrade inside the window is written straight away.
        _queue(user, at=T0 + timedelta(seconds=10), name="claude-code", version="2.2.0")
        await ai_connections.flush_once()
        assert (await _row(conn, alice))["client_version"] == "2.2.0"

        # A later User-Agent-only request keeps the reported name, adds the agent.
        _queue(user, at=T0 + timedelta(seconds=20), user_agent="claude-code/2.2.0")
        await ai_connections.flush_once()
        row = await _row(conn, alice)
        assert (row["client_name"], row["client_version"], row["user_agent"]) == (
            "claude-code", "2.2.0", "claude-code/2.2.0",
        )
        assert await conn.fetchval("SELECT count(*) FROM notification_work") == 1


async def test_listing_is_per_account_and_falls_back_to_the_user_agent():
    async with _fresh_database() as pool, pool.acquire() as conn:
        alice = await _user(conn, "conn-list-alice")
        bob = await _user(conn, "conn-list-bob")
        token = await _token(conn, alice, "work laptop")
        _queue(_pat(alice, token), at=T0, user_agent="codex_cli_rs/0.46.0 (Mac OS; arm64)")
        _queue(_oauth(alice), at=T0 + timedelta(minutes=1), name="claude-code", version="2.1.0")
        _queue(_oauth(bob, "bob-client"), at=T0, name="other-app")
        await ai_connections.flush_once()

        listed = await ai_connections.list_connections(str(alice))
        assert [c["auth_kind"] for c in listed] == ["oauth", "pat"]  # most recent first
        oauth, pat = listed
        assert (oauth["client_name"], oauth["client_version"]) == ("claude-code", "2.1.0")
        assert oauth["token_id"] is None and oauth["token_prefix"] is None and oauth["token_name"] is None
        assert (pat["client_name"], pat["client_version"]) == ("codex_cli_rs", "0.46.0")
        assert (pat["token_id"], pat["token_prefix"], pat["token_name"]) == (str(token), "akb_test", "work laptop")
        assert pat["first_seen_at"] == T0.isoformat()
        assert all("user_agent" not in c and "client_key" not in c for c in listed)

        assert [c["client_name"] for c in await ai_connections.list_connections(str(bob))] == ["other-app"]


async def test_revoking_a_pat_removes_its_connection_and_keeps_a_generic_notice():
    async with _fresh_database() as pool, pool.acquire() as conn:
        alice = await _user(conn, "conn-revoke")
        token = await _token(conn, alice)
        _queue(_pat(alice, token), at=T0, name="codex", version="0.46")
        await ai_connections.flush_once()
        await _drain_notifications(conn)

        await conn.execute("DELETE FROM tokens WHERE id=$1", token)
        assert await ai_connections.list_connections(str(alice)) == []
        [item] = (await inbox.list_notifications(conn, alice, "all", None, 20))["items"]
        assert item["title"] == "A new AI app connected to your account"


async def test_a_credential_revoked_before_the_flush_is_dropped_not_retried():
    async with _fresh_database() as pool, pool.acquire() as conn:
        alice = await _user(conn, "conn-race")
        token = await _token(conn, alice)
        _queue(_pat(alice, token), at=T0, name="codex")
        _queue(_oauth(alice), at=T0, name="claude-code")
        await conn.execute("DELETE FROM tokens WHERE id=$1", token)

        assert await ai_connections.flush_once() == 1
        assert ai_connections.pending_depth() == 0
        assert ai_connections.dropped_count() == 1
        assert [c["auth_kind"] for c in await ai_connections.list_connections(str(alice))] == ["oauth"]
        assert await conn.fetchval("SELECT count(*) FROM notification_work") == 1


async def test_the_schema_rejects_an_inconsistent_pat_row():
    import asyncpg

    async with _fresh_database() as pool, pool.acquire() as conn:
        alice = await _user(conn, "conn-schema")
        token = await _token(conn, alice)
        with pytest.raises(asyncpg.CheckViolationError):
            await conn.execute(
                """INSERT INTO ai_connections(user_id,auth_kind,client_key,token_id,first_seen_at,last_seen_at)
                   VALUES($1,'pat','not-the-token',$2,now(),now())""",
                alice, token,
            )
        with pytest.raises(asyncpg.CheckViolationError):
            await conn.execute(
                """INSERT INTO ai_connections(user_id,auth_kind,client_key,token_id,first_seen_at,last_seen_at)
                   VALUES($1,'oauth','azp',$2,now(),now())""",
                alice, token,
            )


async def test_deleting_the_account_removes_its_connections():
    async with _fresh_database() as pool, pool.acquire() as conn:
        alice = await _user(conn, "conn-delete")
        _queue(_oauth(alice), at=T0, name="claude-code")
        await ai_connections.flush_once()
        await conn.execute("DELETE FROM users WHERE id=$1", alice)
        assert await conn.fetchval("SELECT count(*) FROM ai_connections") == 0


async def test_a_user_sql_role_cannot_read_the_registry():
    async with _fresh_database() as pool, pool.acquire() as conn:
        role = f"ai_conn_probe_{uuid.uuid4().hex[:8]}"
        await conn.execute(f'CREATE ROLE "{role}" NOLOGIN')
        try:
            readable = await conn.fetchval(
                "SELECT has_table_privilege($1, 'ai_connections', 'SELECT')", role,
            )
            assert readable is False
        finally:
            await conn.execute(f'DROP ROLE "{role}"')
