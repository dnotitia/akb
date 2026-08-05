from __future__ import annotations

import ast
from pathlib import Path

import pytest

from app.services.m1_pg_body_store import M1PgBodyStore
from app.services.m1_reference_payload_store import M1ReferencePayloadStore
from app.services.native_payload_verification import (
    NativePayloadPlacementError,
    payload_store_for_placement,
)


def test_payload_store_selection_reuses_the_injected_pg_store():
    pool = object()
    pg_store = M1PgBodyStore(pool)  # type: ignore[arg-type]

    assert payload_store_for_placement(
        pool,  # type: ignore[arg-type]
        M1PgBodyStore.selected_placement,
        pg_body_store=pg_store,
    ) is pg_store


def test_payload_store_selection_constructs_the_matching_reference_store():
    pool = object()

    selected = payload_store_for_placement(
        pool,  # type: ignore[arg-type]
        M1ReferencePayloadStore.selected_placement,
    )

    assert isinstance(selected, M1ReferencePayloadStore)
    assert selected.pool is pool


def test_payload_store_selection_rejects_an_unknown_placement():
    with pytest.raises(NativePayloadPlacementError, match="Unsupported native payload placement"):
        payload_store_for_placement(object(), "unknown-placement-v1")  # type: ignore[arg-type]


@pytest.mark.parametrize("module", ("m1_native_grep_service.py", "search_service.py"))
def test_public_native_consumers_do_not_import_the_derived_worker(module):
    services = Path(__file__).resolve().parents[1] / "app" / "services"
    tree = ast.parse((services / module).read_text())

    assert not any(
        isinstance(node, ast.ImportFrom)
        and node.module == "app.services.native_derived_worker"
        for node in ast.walk(tree)
    )
