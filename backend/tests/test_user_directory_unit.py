"""Unit tests for user_directory.resolve_display_names.

The shared id-OR-username → display_name resolver behind document history,
vault activity, and created_by_name. These pin the dual-keying (lookup by
either id or username), the falsy-token drop, and the empty short-circuit
without a live DB.
"""

from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock

from app.services import user_directory


def _patch_pool(monkeypatch, *, fetch_rows):
    conn = MagicMock()
    conn.fetch = AsyncMock(return_value=fetch_rows)

    @asynccontextmanager
    async def _acquire():
        yield conn

    pool = MagicMock()
    pool.acquire = _acquire
    monkeypatch.setattr(
        "app.services.user_directory.get_pool", AsyncMock(return_value=pool)
    )
    return conn


async def test_resolves_by_username_and_uuid_dual_keyed(monkeypatch):
    rows = [
        {"id": "00000000-0000-0000-0000-000000000000",
         "username": "carol", "name": "Carol Carter"},
        {"id": "11111111-1111-1111-1111-111111111111",
         "username": "alice", "name": "Alice A"},
    ]
    conn = _patch_pool(monkeypatch, fetch_rows=rows)

    out = await user_directory.resolve_display_names(
        ["carol", "11111111-1111-1111-1111-111111111111", "ghost"]
    )

    # Each matched user is keyed by BOTH its id and its username, so a caller
    # resolves by whichever token it holds.
    assert out["carol"] == "Carol Carter"
    assert out["00000000-0000-0000-0000-000000000000"] == "Carol Carter"
    assert out["11111111-1111-1111-1111-111111111111"] == "Alice A"
    assert out["alice"] == "Alice A"
    assert "ghost" not in out
    # One batched query, passed the de-duplicated, non-falsy token list.
    conn.fetch.assert_called_once()
    keys = conn.fetch.call_args.args[1]
    assert set(keys) == {
        "carol", "11111111-1111-1111-1111-111111111111", "ghost",
    }


async def test_empty_and_falsy_tokens_skip_query(monkeypatch):
    conn = _patch_pool(monkeypatch, fetch_rows=[])

    assert await user_directory.resolve_display_names([]) == {}
    assert await user_directory.resolve_display_names([None, "", None]) == {}
    conn.fetch.assert_not_called()


async def test_deduplicates_repeated_tokens(monkeypatch):
    rows = [{"id": "00000000-0000-0000-0000-000000000000",
             "username": "bob", "name": "Bob B"}]
    conn = _patch_pool(monkeypatch, fetch_rows=rows)

    out = await user_directory.resolve_display_names(["bob", "bob", "bob"])

    assert out["bob"] == "Bob B"
    keys = conn.fetch.call_args.args[1]
    assert keys == ["bob"]
