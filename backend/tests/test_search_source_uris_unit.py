"""Unit tests for SearchService._resolve_source_uris (issue #159).

Mocks the DB connection so we can assert the URI → per-kind source-id
resolution without a live database:
  - doc   URI → matched by full vault-relative path
  - table URI → matched by table name
  - file  URI → identifier IS the uuid (validated, used as-is)
  - coll/vault/garbage URIs are skipped
ACL is enforced by the candidate queries downstream, not here.
"""
from __future__ import annotations

import uuid

import pytest

from app.services.search_service import SearchService

pytestmark = pytest.mark.asyncio


class _FakeConn:
    """Minimal asyncpg-conn stand-in: routes fetch() by SQL substring."""

    def __init__(self, vaults: dict, docs: dict, tables: dict):
        self._vaults = vaults  # name -> uuid
        self._docs = docs      # (vault_id, ref) -> uuid
        self._tables = tables  # (vault_id, name) -> uuid

    async def fetch(self, sql: str, *params):
        if "FROM vaults" in sql:
            (names,) = params
            return [{"id": self._vaults[n], "name": n} for n in names if n in self._vaults]
        if "FROM documents" in sql:
            vid, refs = params
            return [{"id": self._docs[(vid, r)]} for r in refs if (vid, r) in self._docs]
        if "FROM vault_tables" in sql:
            vid, names = params
            return [{"id": self._tables[(vid, n)]} for n in names if (vid, n) in self._tables]
        return []


async def test_resolve_source_uris_per_kind():
    v_id = uuid.uuid4()
    d_id = uuid.uuid4()
    t_id = uuid.uuid4()
    f_id = str(uuid.uuid4())
    conn = _FakeConn(
        vaults={"eng": v_id},
        docs={(v_id, "specs/auth.md"): d_id},
        tables={(v_id, "metrics"): t_id},
    )
    doc_ids, table_ids, file_ids = await SearchService()._resolve_source_uris(
        conn,
        [
            "akb://eng/coll/specs/doc/auth.md",    # doc  → path "specs/auth.md"
            "akb://eng/coll/data/table/metrics",   # table → "metrics"
            f"akb://eng/file/{f_id}",              # file → uuid
            "akb://eng/coll/specs",                # coll  → skipped
            "akb://eng",                           # vault → skipped
            "not-a-uri",                           # garbage → skipped
        ],
    )
    assert doc_ids == [str(d_id)]
    assert table_ids == [str(t_id)]
    assert file_ids == [f_id]


async def test_resolve_skips_unknown_vault_and_bad_file_uuid():
    v_id = uuid.uuid4()
    conn = _FakeConn(vaults={"eng": v_id}, docs={}, tables={})
    doc_ids, table_ids, file_ids = await SearchService()._resolve_source_uris(
        conn,
        [
            "akb://ghost/coll/x/doc/y.md",   # vault not found → no doc id
            "akb://eng/file/not-a-uuid",     # invalid uuid → skipped
        ],
    )
    assert doc_ids == []
    assert table_ids == []
    assert file_ids == []
