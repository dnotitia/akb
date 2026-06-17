"""Unit coverage for the qdrant driver's #189 Phase-2 vault filter — storage,
the security-load-bearing hybrid_search filter branch, the readiness count, and
the payload index. Pure-mocked (no live qdrant); the qdrant_client `models` (qm)
are the real library so the asserted Filter/Condition objects are real."""
from __future__ import annotations

import pytest
from qdrant_client import models as qm

from app.services.vector_store.qdrant import (
    QdrantStore,
    PAYLOAD_SOURCE_ID,
    PAYLOAD_VAULT_ID,
    _acl_filter,
)


class _FakeClient:
    def __init__(self):
        self.upserts: list = []
        self.indexes: list[str] = []
        self.count_filter = None
        self.count_exact = None

    async def collection_exists(self, name):  # ensure_collection: skip create
        return True

    async def create_payload_index(self, *, collection_name, field_name, field_schema):
        self.indexes.append(field_name)

    async def upsert(self, *, collection_name, points):
        self.upserts.extend(points)

    async def count(self, *, collection_name, count_filter, exact):
        self.count_filter = count_filter
        self.count_exact = exact
        return type("R", (), {"count": 7})()

    async def query_points(self, **kw):
        self.last_query = kw
        return type("R", (), {"points": []})()


def _store_with(fake: _FakeClient) -> QdrantStore:
    s = QdrantStore(url="http://x", api_key=None, collection="chunks", dense_dim=4)
    s._client = fake
    s._ensured_collection = True  # skip the create path; index asserted separately
    return s


def test_capability_flag():
    assert QdrantStore.vault_filter_supported is True


def test_acl_filter_branches():
    fv = _acl_filter(vault_ids=["v1", "v2"], source_ids=None)
    assert fv.must[0].key == PAYLOAD_VAULT_ID
    assert fv.must[0].match.any == ["v1", "v2"]
    fs = _acl_filter(vault_ids=None, source_ids=["s1"])
    assert fs.must[0].key == PAYLOAD_SOURCE_ID
    assert _acl_filter(vault_ids=None, source_ids=None) is None
    # vault_ids wins if both somehow present (defensive, matches pgvector)
    fb = _acl_filter(vault_ids=["v1"], source_ids=["s1"])
    assert fb.must[0].key == PAYLOAD_VAULT_ID


@pytest.mark.asyncio
async def test_upsert_one_writes_vault_id_payload():
    fake = _FakeClient()
    store = _store_with(fake)
    await store.upsert_one(
        chunk_id="c1", content="hi", section_path=None, chunk_index=0,
        dense=[0.1, 0.2, 0.3, 0.4], sparse_indices=[1], sparse_values=[0.5],
        source_type="document", source_id="s1", vault_id="vault-9",
    )
    assert len(fake.upserts) == 1
    assert fake.upserts[0].payload[PAYLOAD_VAULT_ID] == "vault-9"
    assert fake.upserts[0].payload[PAYLOAD_SOURCE_ID] == "s1"


@pytest.mark.asyncio
async def test_hybrid_search_filters_by_vault_then_source_and_rejects_both():
    fake = _FakeClient()
    store = _store_with(fake)
    common = dict(
        query_text="q", query_dense=[0.1, 0.2, 0.3, 0.4],
        query_sparse_indices=[], query_sparse_values=[],
        limit=10, prefetch_per_leg=50,
    )
    # vault path → filter on vault_id
    await store.hybrid_search(source_ids=None, vault_ids=["v1"], **common)
    assert store._client.last_query["query_filter"].must[0].key == PAYLOAD_VAULT_ID
    # source path → filter on source_id
    await store.hybrid_search(source_ids=["s1"], vault_ids=None, **common)
    assert store._client.last_query["query_filter"].must[0].key == PAYLOAD_SOURCE_ID
    # both at once → caller bug → AssertionError (security: exactly one)
    with pytest.raises(AssertionError):
        await store.hybrid_search(source_ids=["s1"], vault_ids=["v1"], **common)


@pytest.mark.asyncio
async def test_hybrid_search_dual_prefetch_filters_both_legs():
    """The production-common path (dense AND sparse present) builds two Prefetch
    legs and must attach the vault filter to BOTH — a leg missing the filter
    would retrieve cross-vault candidates before RRF fusion."""
    fake = _FakeClient()
    store = _store_with(fake)
    await store.hybrid_search(
        query_text="q", query_dense=[0.1, 0.2, 0.3, 0.4],
        query_sparse_indices=[1, 2], query_sparse_values=[0.5, 0.5],
        source_ids=None, vault_ids=["v1"], limit=10, prefetch_per_leg=50,
    )
    prefetch = store._client.last_query["prefetch"]
    assert len(prefetch) == 2
    for leg in prefetch:
        assert leg.filter.must[0].key == PAYLOAD_VAULT_ID


@pytest.mark.asyncio
async def test_vault_backfill_pending_counts_null_exactly():
    fake = _FakeClient()
    store = _store_with(fake)
    n = await store.vault_backfill_pending()
    assert n == 7
    assert fake.count_exact is True  # approximate count could flip the gate early
    cond = fake.count_filter.must[0]
    # IsEmpty (missing OR null), NOT IsNull (explicit-null only) — verified
    # against qdrant 1.18.2: IsNull misses pre-upgrade points that never had the
    # key, which under-counts and flips the readiness gate early.
    assert isinstance(cond, qm.IsEmptyCondition)
    assert cond.is_empty.key == PAYLOAD_VAULT_ID


@pytest.mark.asyncio
async def test_ensure_collection_indexes_vault_id():
    fake = _FakeClient()
    store = QdrantStore(url="http://x", api_key=None, collection="chunks", dense_dim=4)
    store._client = fake
    await store.ensure_collection()
    assert PAYLOAD_VAULT_ID in fake.indexes


# ── #207: client errors → VectorStoreUnavailable (write + search paths) ──

from app.services.vector_store.base import VectorStoreUnavailable


class _BoomClient(_FakeClient):
    """A client whose network calls raise a chosen exception."""

    def __init__(self, exc: BaseException):
        super().__init__()
        self._exc = exc

    async def collection_exists(self, name):
        raise self._exc

    async def create_payload_index(self, **kw):
        raise self._exc

    async def upsert(self, *, collection_name, points):
        raise self._exc

    async def delete(self, *, collection_name, points_selector):
        raise self._exc

    async def query_points(self, **kw):
        raise self._exc


async def _do_upsert(store):
    await store.upsert_one(
        chunk_id="c1", content="x", section_path=None, chunk_index=0,
        dense=[0.1, 0.2, 0.3, 0.4], sparse_indices=[1], sparse_values=[0.5],
        source_type="document", source_id="s1", vault_id="v1",
    )


async def _do_search(store):
    await store.hybrid_search(
        query_text="q", query_dense=[0.1, 0.2, 0.3, 0.4],
        query_sparse_indices=[], query_sparse_values=[],
        source_ids=None, vault_ids=["v1"], limit=10, prefetch_per_leg=50,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("action", [_do_upsert, _do_search])
async def test_transient_client_error_becomes_unavailable(action):
    """A transient qdrant-client failure on the write/search path surfaces as
    VectorStoreUnavailable (so _run_vector_search labels it correctly), not a
    raw client exception."""
    store = _store_with(_BoomClient(RuntimeError("connection refused")))
    with pytest.raises(VectorStoreUnavailable):
        await action(store)


@pytest.mark.asyncio
async def test_delete_point_error_becomes_unavailable():
    store = _store_with(_BoomClient(RuntimeError("5xx")))
    with pytest.raises(VectorStoreUnavailable):
        await store.delete_point("c1")


@pytest.mark.asyncio
async def test_ensure_collection_error_becomes_unavailable():
    store = QdrantStore(url="http://x", api_key=None, collection="chunks", dense_dim=4)
    store._client = _BoomClient(RuntimeError("ping failed"))
    with pytest.raises(VectorStoreUnavailable):
        await store.ensure_collection()


@pytest.mark.asyncio
@pytest.mark.parametrize("action", [_do_upsert, _do_search])
async def test_programming_error_is_not_masked(action):
    """A TypeError/ValueError is a programming bug (bad qm.* arg), NOT an outage —
    it must propagate unmasked rather than become VectorStoreUnavailable."""
    store = _store_with(_BoomClient(TypeError("bad arg")))
    with pytest.raises(TypeError):
        await action(store)
