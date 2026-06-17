"""Unit coverage for the #189 Phase-2 vault filter on the locally-importable
Seahorse drivers (Coral REST + Seahorse Cloud). The security-load-bearing FILTER
logic is the shared `vault_filter_sql` helper (exhaustively tested in
test_seahorse_common_unit-style assertions below); these tests lock the STORAGE
side per driver — the create-table column and that every upsert record/row
carries vault_id (incl. the REST driver's inline-vs-_record_dict two-copy path,
the documented drift risk). Pure-mocked; no live Coral.

The gRPC driver's equivalents live in test_seahorse_db_grpc_unit.py (it imports
grpc/pyarrow at module top → runs in CI, collection-skips locally)."""
from __future__ import annotations

import json
import uuid

import pytest

from app.services.vector_store._seahorse_common import vault_filter_sql
from app.services.vector_store.seahorse_cloud import SeahorseCloudStore, COL_VAULT_ID
from app.services.vector_store.seahorse_db import SeahorseDbStore

_UUID = str(uuid.uuid4())
_DENSE = [0.1, 0.2, 0.3, 0.4]


# ── shared filter helper (the security-critical exactly-one contract) ──

def test_vault_filter_sql_contract():
    assert vault_filter_sql([_UUID], None).startswith("vault_id IN (")
    assert vault_filter_sql(None, [_UUID]).startswith("source_id IN (")
    assert vault_filter_sql(None, None) is None
    with pytest.raises(AssertionError):
        vault_filter_sql([_UUID], [_UUID])  # both → caller bug


# ── Coral REST driver ────────────────────────────────────────────

class _Resp:
    def __init__(self, status=200, body=None):
        self.status_code = status
        self._body = body if body is not None else {}
        self.headers = {"content-type": "application/json"}
        self.text = json.dumps(self._body)

    def json(self):
        return self._body


class _FakeHttp:
    def __init__(self, response=None):
        self.calls: list[dict] = []
        self._response = response or _Resp(200)

    async def post(self, url, *, json=None, content=None, headers=None):
        self.calls.append({"url": url, "json": json, "content": content})
        return self._response

    async def get(self, url, **kw):
        return _Resp(200)


def test_db_capability_flag():
    assert SeahorseDbStore.vault_filter_supported is True


def test_db_create_table_has_vault_id_column():
    s = SeahorseDbStore(coordinator_url="http://x", table_name="chunks", dense_dim=4)
    cols = {c["name"] for c in s._build_create_table_payload()["columns"]}
    assert "vault_id" in cols


def test_db_record_dict_carries_vault_id():
    s = SeahorseDbStore(coordinator_url="http://x", table_name="chunks", dense_dim=4)
    rec = s._record_dict(
        chunk_id=_UUID, content="x", section_path=None, chunk_index=0,
        dense=_DENSE, sparse_indices=[1], sparse_values=[0.5],
        source_type="document", source_id=_UUID, vault_id="vault-9",
    )
    assert rec["vault_id"] == "vault-9"


@pytest.mark.asyncio
async def test_db_upsert_one_inline_record_carries_vault_id():
    """upsert_one builds its OWN inline record (not via _record_dict) — capture
    the ndjson body to catch the two-copy drift the audit flagged."""
    fake = _FakeHttp(_Resp(200))
    s = SeahorseDbStore(coordinator_url="http://x", table_name="chunks", dense_dim=4)
    s._client = fake
    s._ensured_collection = True
    await s.upsert_one(
        chunk_id=_UUID, content="x", section_path=None, chunk_index=0,
        dense=_DENSE, sparse_indices=[1], sparse_values=[0.5],
        source_type="document", source_id=_UUID, vault_id="vault-9",
    )
    sent = json.loads(fake.calls[-1]["content"].decode("utf-8"))
    assert sent["vault_id"] == "vault-9"


@pytest.mark.asyncio
async def test_db_vault_backfill_pending_counts_and_fails_closed():
    s = SeahorseDbStore(coordinator_url="http://x", table_name="chunks", dense_dim=4)
    # empty scan → 0 pending (fresh/recreated table → vault path can activate)
    s._client = _FakeHttp(_Resp(200, {"data": []}))
    assert await s.vault_backfill_pending() == 0
    # one null row → still pending
    s._client = _FakeHttp(_Resp(200, {"data": [{"chunk_id": "x"}]}))
    assert await s.vault_backfill_pending() == 1
    # error / unexpected shape → FAIL-CLOSED (raise → worker stays gated)
    from app.services.vector_store.base import VectorStoreUnavailable
    s._client = _FakeHttp(_Resp(500, {"err": "boom"}))
    with pytest.raises(VectorStoreUnavailable):
        await s.vault_backfill_pending()
    s._client = _FakeHttp(_Resp(200, {"unexpected": 1}))  # no data/rows key
    with pytest.raises(VectorStoreUnavailable):
        await s.vault_backfill_pending()


# ── Seahorse Cloud driver ────────────────────────────────────────

def test_cloud_capability_flag():
    assert SeahorseCloudStore.vault_filter_supported is True


@pytest.mark.asyncio
async def test_cloud_upsert_one_row_carries_vault_id():
    fake = _FakeHttp(_Resp(200))
    s = SeahorseCloudStore(
        management_url="http://x", token="t", tenant_uuid="ten", table_name="chunks",
        dense_dim=4,
    )
    s._client = fake
    s._ensured = True
    s._table_host = "http://host"
    await s.upsert_one(
        chunk_id=_UUID, content="x", section_path=None, chunk_index=0,
        dense=_DENSE, sparse_indices=[1], sparse_values=[0.5],
        source_type="document", source_id=_UUID, vault_id="vault-9",
    )
    sent = json.loads(fake.calls[-1]["content"].decode("utf-8"))
    assert sent[COL_VAULT_ID] == "vault-9"
