"""Unit tests for the canonical id on the vault member surface (#430).

``list_vault_members`` answers *who* holds access to a vault, and until the id
was projected here a consumer that keys membership on the canonical id — which
is the correct thing to store, since a username is not a durable handle —
could not match the roster to the identities it stores without a round trip
per person. ``search_users`` already projects the id (#413); the roster is the
same projection on the surface that names the role, and ``explain_vault_access``
carries it because that response is keyed by username but describes one person.

No live DB — the pool and the authorization check are patched, so what is
asserted is the projection and the statements, which is where an id can go
missing. The statements are asserted separately because the owner lookup, the
member listing, and the explain lookup each run exactly one statement, and a
projection added to two of the three would look right in every test that only
exercises the other two.
"""

from __future__ import annotations

import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock

from app.services import access_service


def _patch_pool(monkeypatch, *, fetchrow=None, fetch=()):
    conn = MagicMock()
    conn.fetchrow = AsyncMock(side_effect=list(fetchrow or []))
    conn.fetch = AsyncMock(return_value=list(fetch))
    conn.fetchval = AsyncMock(return_value=None)

    @asynccontextmanager
    async def _acquire():
        yield conn

    pool = MagicMock()
    pool.acquire = _acquire
    monkeypatch.setattr(
        "app.services.access_service.get_pool", AsyncMock(return_value=pool)
    )
    monkeypatch.setattr(
        "app.services.access_service.check_vault_access",
        AsyncMock(return_value={"vault_id": uuid.uuid4()}),
    )
    return conn


def _owner_row(uid: uuid.UUID) -> dict:
    return {
        "id": uid,
        "username": "owner",
        "display_name": "Owner",
        "email": "owner@example.invalid",
    }


def _member_row(username: str, uid: uuid.UUID) -> dict:
    return {
        "id": uid,
        "username": username,
        "display_name": username.title(),
        "email": f"{username}@example.invalid",
        "role": "reader",
        "created_at": datetime(2026, 8, 27, tzinfo=timezone.utc),
    }


async def test_roster_projects_the_canonical_id_on_every_row(monkeypatch):
    owner_id, member_id = uuid.uuid4(), uuid.uuid4()
    vault_id = uuid.uuid4()
    _patch_pool(
        monkeypatch,
        fetchrow=[{"id": vault_id, "owner_id": owner_id}, _owner_row(owner_id)],
        fetch=[_member_row("minji", member_id)],
    )

    out = await access_service.list_vault_members(str(uuid.uuid4()), "v")

    assert [(m["username"], m["id"], m["role"]) for m in out] == [
        ("owner", str(owner_id), "owner"),
        ("minji", str(member_id), "reader"),
    ]


async def test_ids_are_strings_not_uuid_objects(monkeypatch):
    # asyncpg hands back a uuid.UUID, which is not JSON-serialisable. Returning
    # it unconverted fails at the response encoder rather than here, far from
    # the cause — the same trap test_user_search_unit pins for search.
    owner_id, member_id = uuid.uuid4(), uuid.uuid4()
    vault_id = uuid.uuid4()
    _patch_pool(
        monkeypatch,
        fetchrow=[{"id": vault_id, "owner_id": owner_id}, _owner_row(owner_id)],
        fetch=[_member_row("minji", member_id)],
    )

    out = await access_service.list_vault_members(str(uuid.uuid4()), "v")

    for m in out:
        assert isinstance(m["id"], str)
        uuid.UUID(m["id"])  # parses, so it is the id and not a repr


async def test_all_three_statements_select_the_id(monkeypatch):
    # The owner lookup, the member listing, and the explain lookup each run one
    # statement. Asserting the projection on the response alone would pass with
    # any one of them missing its id as long as the fake rows carry one — so
    # the statements themselves are pinned.
    owner_id, member_id, target_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    vault_id = uuid.uuid4()
    conn = _patch_pool(
        monkeypatch,
        fetchrow=[{"id": vault_id, "owner_id": owner_id}, _owner_row(owner_id)],
        fetch=[_member_row("minji", member_id)],
    )

    await access_service.list_vault_members(str(uuid.uuid4()), "v")
    statements = [
        " ".join(call.args[0].split())
        for call in conn.fetchrow.await_args_list + conn.fetch.await_args_list
    ]
    assert any(
        "SELECT id, username, display_name, email FROM users WHERE id" in s
        for s in statements
    )
    assert any("SELECT u.id, u.username" in s for s in statements)

    conn2 = _patch_pool(
        monkeypatch,
        fetchrow=[
            {"id": target_id, "username": "minji"},
            {"id": uuid.uuid4(), "name": "v", "owner_id": uuid.uuid4(),
             "status": "active", "public_access": "none"},
        ],
    )
    monkeypatch.setattr(
        "app.services.access_service.list_contributions",
        AsyncMock(return_value=[]),
    )
    monkeypatch.setattr(
        "app.services.access_service.effective_role",
        MagicMock(return_value=None),
    )

    async def _fake_policy(_vault_id, conn=None):
        return None

    monkeypatch.setattr(
        "app.services.access_service.write_policy_repo.get_policy",
        _fake_policy,
    )

    out = await access_service.explain_vault_access(
        str(uuid.uuid4()), "v", "minji",
    )

    assert out["user_id"] == str(target_id)
    explain_statements = [
        " ".join(call.args[0].split())
        for call in conn2.fetchrow.await_args_list
    ]
    assert explain_statements[0].startswith("SELECT id, username FROM users")


async def test_roster_without_an_owner_row_stays_well_formed(monkeypatch):
    member_id = uuid.uuid4()
    vault_id = uuid.uuid4()
    _patch_pool(
        monkeypatch,
        fetchrow=[{"id": vault_id, "owner_id": uuid.uuid4()}, None],
        fetch=[_member_row("minji", member_id)],
    )

    out = await access_service.list_vault_members(str(uuid.uuid4()), "v")

    assert [(m["username"], m["id"]) for m in out] == [("minji", str(member_id))]
