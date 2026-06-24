"""Unit tests for the multi-vault search scope (PR #245).

- `_normalize_vault_scope`: str | list | None → canonical list-or-None, with
  blank/empty entries dropped (the type invariant the whole feature rests on).
- `_accessible_vault_ids`: the no-leak ACL INTERSECTION — a named vault the
  caller can't read must drop out, never error or leak. Mocks the DB conn
  (routing fetch() by SQL substring, same as test_search_source_uris_unit.py)
  so the intersection is asserted without a live database.
"""
from __future__ import annotations

import uuid

from app.services.search_service import SearchService, _normalize_vault_scope

# asyncio_mode = "auto" (pyproject) marks the async tests; the sync
# `_normalize_vault_scope` test must NOT carry an asyncio mark.


def test_normalize_vault_scope():
    # single name (MCP / legacy) → one-element list
    assert _normalize_vault_scope("eng") == ["eng"]
    # list (REST multi-vault scope) → kept
    assert _normalize_vault_scope(["a", "b"]) == ["a", "b"]
    # None / empty / all-blank / blanks-mixed → None ("every accessible vault")
    assert _normalize_vault_scope(None) is None
    assert _normalize_vault_scope([]) is None
    assert _normalize_vault_scope([""]) is None
    assert _normalize_vault_scope(["", ""]) is None
    # a stray empty entry (e.g. `?v=a,,b`) is dropped, the rest kept
    assert _normalize_vault_scope(["a", "", "b"]) == ["a", "b"]


class _AclConn:
    """asyncpg-conn stand-in for _accessible_vault_ids. `accessible` is the set
    of vault names the (non-admin) user may read; `all_vaults` maps name→id."""

    def __init__(self, all_vaults: dict, accessible: set):
        self._all = all_vaults
        self._acc = accessible

    async def fetch(self, sql: str, *params):
        if "vault_access" in sql:
            # authenticated path: WHERE v.name = ANY($2) AND (<acl>)
            _user, names = params
            return [{"id": self._all[n]} for n in names if n in self._all and n in self._acc]
        # admin / anon path: WHERE name = ANY($1)
        (names,) = params
        return [{"id": self._all[n]} for n in names if n in self._all]


async def test_accessible_vault_ids_intersects_with_acl_no_leak():
    """A named vault the user can't read drops out (no leak); the readable one stays."""
    mine, theirs = uuid.uuid4(), uuid.uuid4()
    conn = _AclConn(all_vaults={"mine": mine, "theirs": theirs}, accessible={"mine"})
    ids = await SearchService()._accessible_vault_ids(
        conn, user_uuid=uuid.uuid4(), is_admin=False, vaults=["mine", "theirs"],
    )
    assert ids == [str(mine)]  # "theirs" intersected away


async def test_accessible_vault_ids_all_unreadable_returns_empty():
    """Scoping ONLY to unreadable vaults → [] (caller short-circuits to no results)."""
    conn = _AclConn(all_vaults={"theirs": uuid.uuid4()}, accessible=set())
    ids = await SearchService()._accessible_vault_ids(
        conn, user_uuid=uuid.uuid4(), is_admin=False, vaults=["theirs"],
    )
    assert ids == []


async def test_accessible_vault_ids_no_scope_returns_none_for_admin():
    """admin with no named scope → None (unscoped)."""
    conn = _AclConn(all_vaults={}, accessible=set())
    ids = await SearchService()._accessible_vault_ids(
        conn, user_uuid=uuid.uuid4(), is_admin=True, vaults=None,
    )
    assert ids is None


async def test_accessible_vault_ids_admin_named_scope_resolves_to_ids():
    """A named scope ALWAYS resolves to ids (a list), never None — even for admin."""
    a, b = uuid.uuid4(), uuid.uuid4()
    conn = _AclConn(all_vaults={"a": a, "b": b}, accessible=set())
    ids = await SearchService()._accessible_vault_ids(
        conn, user_uuid=None, is_admin=True, vaults=["a", "b"],
    )
    assert sorted(ids) == sorted([str(a), str(b)])
