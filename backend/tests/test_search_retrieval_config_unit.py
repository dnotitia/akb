"""Operators can bound pgvector retrieval without changing other drivers."""
from unittest.mock import Mock

import pytest
from pydantic import ValidationError

from app.config import Settings
from app.services.vector_store import factory, pgvector


def test_retrieval_budget_keeps_existing_default():
    configured = Settings(document_revision_backend="bare_git")
    assert configured.search_retrieval_timeout_secs == 30.0


@pytest.mark.parametrize("budget", [0, -1, 30.1, float("inf"), float("nan")])
def test_retrieval_budget_rejects_unbounded_or_invalid_values(budget):
    with pytest.raises(ValidationError, match="search_retrieval_timeout_secs"):
        Settings(document_revision_backend="bare_git", search_retrieval_timeout_secs=budget)


def test_factory_passes_operator_budget_to_pgvector(monkeypatch):
    configured = Settings(document_revision_backend="bare_git", vector_store_driver="pgvector",
                          vector_store_sparse_shape="posting", search_retrieval_timeout_secs=3.5)
    constructor = Mock()
    monkeypatch.setattr(factory, "settings", configured)
    monkeypatch.setattr(factory, "_singleton", None)
    monkeypatch.setattr(pgvector, "PgvectorStore", constructor)
    assert factory.get_vector_store() is constructor.return_value
    assert constructor.call_args.kwargs["retrieval_timeout_secs"] == 3.5
    assert constructor.call_args.kwargs["sparse_shape"] == "posting"
