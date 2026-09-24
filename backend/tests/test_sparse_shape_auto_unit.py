"""`auto` is the default sparse shape, decided once per database at startup.

The decision runs against PostgreSQL and is held by
`test_sparse_shape_decision_postgres.py`. These hold what the rest of the
process does with it. Two rules carry the weight:

- An undecided `auto` is not a shape. Building a store, choosing an encoder
  weight convention or counting a shape's relations from it would pick a side
  silently, which is the akb#623 failure again.
- The external-statistics refresher runs only where something reads its output.
  A new VChord database has no `posting` table and nothing that reads those
  statistics, so it runs no corpus-wide recompute. A database with a `posting`
  table still keeps it current, because that table is a way back.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.config import Settings
from app.services.sparse_shapes import SPARSE_SHAPES, SparseShapeDecision


def _settings(**values) -> Settings:
    return Settings(document_revision_backend="bare_git", vector_store_driver="pgvector", **values)


def _decided(shape="vchord", decided_by="new_database", posting=False, **values) -> Settings:
    configured = _settings(**values)
    configured.apply_sparse_shape_decision(
        SparseShapeDecision(shape=shape, decided_by=decided_by, posting_table_present=posting)
    )
    return configured


def test_auto_is_the_default_for_the_shape_and_the_statistics_mode():
    configured = Settings(document_revision_backend="bare_git")
    assert configured.vector_store_sparse_shape == "auto"
    assert configured.bm25_external_stats_mode == "auto"


def test_an_undecided_auto_is_not_a_shape():
    with pytest.raises(RuntimeError, match="has not been decided"):
        _settings().effective_sparse_shape  # noqa: B018 — the read is the assertion


@pytest.mark.parametrize("shape", SPARSE_SHAPES)
def test_a_configured_shape_needs_no_decision(shape):
    assert _settings(vector_store_sparse_shape=shape).effective_sparse_shape == shape


def test_the_decision_is_what_auto_means_and_the_setting_keeps_what_was_written():
    configured = _decided(shape="posting", decided_by="existing_posting", posting=True)
    assert configured.effective_sparse_shape == "posting"
    assert configured.vector_store_sparse_shape == "auto"


def test_a_decision_cannot_contradict_a_configured_shape():
    configured = _settings(vector_store_sparse_shape="posting")
    with pytest.raises(ValueError, match="configured"):
        configured.apply_sparse_shape_decision(
            SparseShapeDecision(shape="vchord", decided_by="recorded", posting_table_present=True)
        )


@pytest.mark.parametrize(("posting", "consumers"), [
    (False, []),
    (True, ["posting_rollback_or_mixed_deployment"]),
])
def test_auto_statistics_follow_whether_a_posting_table_exists(posting, consumers):
    decided_by = "existing_bm25_index" if posting else "new_database"
    assert _decided(decided_by=decided_by, posting=posting).bm25_external_stats_consumers == consumers


def test_a_configured_vchord_with_auto_statistics_also_follows_the_table():
    assert _decided(decided_by="configured", posting=False,
                    vector_store_sparse_shape="vchord").bm25_external_stats_consumers == []


def test_before_the_decision_the_statistics_are_kept():
    """Conservative until startup knows: dropping them is the irreversible side."""
    assert _settings(vector_store_sparse_shape="vchord").bm25_external_stats_consumers == [
        "posting_rollback_or_mixed_deployment"
    ]
    assert _settings().bm25_external_stats_consumers == ["undecided_sparse_shape"]


def test_required_keeps_the_statistics_even_without_a_posting_table():
    assert _decided(decided_by="configured", posting=False, vector_store_sparse_shape="vchord",
                    bm25_external_stats_mode="required").bm25_external_stats_consumers == [
        "posting_rollback_or_mixed_deployment"
    ]


@pytest.mark.parametrize("shape", ["posting", "arrays"])
def test_auto_statistics_under_a_baked_shape_are_required(shape):
    assert _decided(shape=shape, decided_by="existing_posting" if shape == "posting" else "existing_arrays",
                    posting=shape == "posting").bm25_external_stats_consumers == [f"pgvector/{shape}"]


def test_the_verified_opt_out_still_needs_an_explicit_vchord():
    with pytest.raises(ValidationError, match="requires pgvector/vchord"):
        _settings(bm25_external_stats_mode="vchord_only_verified")


@pytest.mark.parametrize("driver", ["qdrant", "seahorse-cloud", "seahorse-db", "seahorse-db-grpc"])
def test_other_drivers_ignore_auto(driver):
    configured = Settings(document_revision_backend="bare_git", vector_store_driver=driver)
    assert configured.bm25_external_stats_consumers == [driver]


def test_the_store_is_built_with_the_decided_shape(monkeypatch):
    from app.services.vector_store import factory

    monkeypatch.setattr(factory, "settings", _decided())
    factory.reset_singleton_for_tests()
    try:
        store = factory.get_vector_store()
        assert store.sparse_shape == "vchord"
        # A new VChord database has no `posting` table to keep current.
        assert store._posting_weights is None
    finally:
        factory.reset_singleton_for_tests()


def test_a_way_back_is_kept_current_where_a_posting_table_exists(monkeypatch):
    from app.services.vector_store import factory

    monkeypatch.setattr(factory, "settings", _decided(decided_by="recorded", posting=True))
    factory.reset_singleton_for_tests()
    try:
        assert factory.get_vector_store()._posting_weights is not None
    finally:
        factory.reset_singleton_for_tests()


def test_the_store_refuses_an_undecided_auto(monkeypatch):
    from app.services.vector_store import factory

    monkeypatch.setattr(factory, "settings", _settings())
    factory.reset_singleton_for_tests()
    try:
        with pytest.raises(RuntimeError, match="has not been decided"):
            factory.get_vector_store()
    finally:
        factory.reset_singleton_for_tests()


def test_the_encoder_refuses_an_undecided_auto(monkeypatch):
    from app.services import sparse_encoder

    monkeypatch.setattr(sparse_encoder, "settings", _settings())
    with pytest.raises(RuntimeError, match="has not been decided"):
        sparse_encoder._use_raw_weights()
    monkeypatch.setattr(sparse_encoder, "settings", _decided())
    assert sparse_encoder._use_raw_weights() is True


def test_the_sampler_counts_nothing_for_an_undecided_shape(monkeypatch):
    """Absent, not a guess: the size field is omitted rather than wrong."""
    from app.stats import sampler

    monkeypatch.setattr(sampler, "settings", _settings())
    assert sampler.pgvector_relations() == ()
    monkeypatch.setattr(sampler, "settings", _decided(shape="posting", decided_by="recorded", posting=True))
    assert sampler.pgvector_relations() == ("chunks", "posting")


def test_health_names_the_configured_and_the_effective_shape():
    configured = _decided(shape="posting", decided_by="existing_posting", posting=True)
    assert configured.sparse_shape_snapshot() == {
        "configured": "auto",
        "effective": "posting",
        "decided_by": "existing_posting",
        "posting_table_present": True,
        "note": "",
    }
    assert _settings().sparse_shape_snapshot() == {"configured": "auto", "effective": None}
