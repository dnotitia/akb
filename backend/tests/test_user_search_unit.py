"""Unit tests for access_service.search_users.

These pin the one thing a caller cannot get anywhere else without system-admin
rights: the canonical ``id``. A username can be renamed, so a client that stores
one has a reference that silently detaches; the id is what makes a recorded
reference to a person durable, and until it was projected here the only source
was ``/admin/users``.

No live DB — the pool is patched, so what is asserted is the projection and the
statement, which is where the id can go missing.
"""

import uuid
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock

from app.services import access_service


def _patch_pool(monkeypatch, *, fetch_rows):
    conn = MagicMock()
    conn.fetch = AsyncMock(return_value=fetch_rows)

    @asynccontextmanager
    async def _acquire():
        yield conn

    pool = MagicMock()
    pool.acquire = _acquire
    monkeypatch.setattr(
        "app.services.access_service.get_pool", AsyncMock(return_value=pool)
    )
    return conn


def _row(username: str, *, uid: uuid.UUID | None = None) -> dict:
    return {
        "id": uid or uuid.uuid4(),
        "username": username,
        "display_name": username.title(),
        "email": f"{username}@example.invalid",
    }


async def test_search_projects_the_canonical_id(monkeypatch):
    uid = uuid.uuid4()
    _patch_pool(monkeypatch, fetch_rows=[_row("minji", uid=uid)])

    out = await access_service.search_users("min")

    assert out == [
        {
            "id": str(uid),
            "username": "minji",
            "display_name": "Minji",
            "email": "minji@example.invalid",
        }
    ]


async def test_id_is_a_string_not_a_uuid_object(monkeypatch):
    # asyncpg hands back a uuid.UUID, which is not JSON-serialisable. Returning
    # it unconverted fails at the response encoder rather than here, far from
    # the cause.
    _patch_pool(monkeypatch, fetch_rows=[_row("minji")])

    out = await access_service.search_users()

    assert isinstance(out[0]["id"], str)
    uuid.UUID(out[0]["id"])  # parses, so it is the id and not a repr


async def test_both_query_branches_select_the_id(monkeypatch):
    # Two statements, and only one of them runs per call — so a projection added
    # to the searched branch alone would look right in every test that passes a
    # query and be missing for every caller that does not.
    conn = _patch_pool(monkeypatch, fetch_rows=[_row("minji")])

    await access_service.search_users("min")
    searched = conn.fetch.await_args_list[-1].args[0]

    await access_service.search_users()
    unsearched = conn.fetch.await_args_list[-1].args[0]

    assert "SELECT id, username, display_name, email" in " ".join(searched.split())
    assert "SELECT id, username, display_name, email" in " ".join(unsearched.split())


async def test_empty_result_stays_empty(monkeypatch):
    _patch_pool(monkeypatch, fetch_rows=[])
    assert await access_service.search_users("nobody") == []


async def test_limit_is_passed_through_unchanged(monkeypatch):
    conn = _patch_pool(monkeypatch, fetch_rows=[])

    await access_service.search_users("min", limit=7)
    assert conn.fetch.await_args_list[-1].args[2] == 7

    await access_service.search_users(limit=7)
    assert conn.fetch.await_args_list[-1].args[1] == 7
