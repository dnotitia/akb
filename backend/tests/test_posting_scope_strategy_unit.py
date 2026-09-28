"""Common-term routing saves work without moving rare queries off the index."""
import pytest

from app.services.vector_store.posting_search import choose_scope_strategy


@pytest.mark.parametrize("scope,postings,terms,expected", [
    (3796, 1_800_000, 1, "point"),
    (3796, 1_800_040, 3, "point"),
    (104, 1_800_040, 3, "point"),
    (3796, 40, 2, "term"),
    (104, 40, 2, "term"),
    (490_000, 1_800_000, 1, "scope"),
    (490_000, 40, 2, "term"),
    (2_101_940, 1_800_000, 1, "term"),
    (5000, 100_000_000, 100, "scope"),
    (None, 1_800_000, 1, "term"),
    (3796, None, 1, "term"),
    (0, 1_800_000, 1, "term"),
])
def test_routes_by_scope_and_posting_work(scope, postings, terms, expected):
    assert choose_scope_strategy(scope, postings, terms, total_chunks=2_101_940) == expected
