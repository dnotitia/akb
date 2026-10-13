from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from mcp_catalog.contracts import StateExpectation, load_task_corpus
from mcp_catalog.state import expectation_holds


ROOT = Path(__file__).parents[1]
CANONICAL_URI = "akb://catalog-bench-io/coll/notes/doc/imported.md"
IMPORTED_PATH = "notes/imported.md"


def _browse_rows() -> list[dict[str, Any]]:
    fixture = json.loads((ROOT / "tests/fixtures/attempt03-import-export-browse.json").read_text(encoding="utf-8"))
    return fixture["rows"]


def _browse_expectation() -> StateExpectation:
    task = next(task for task in load_task_corpus(ROOT / "corpus/tasks.json") if task.id == "import-export-en")
    return task.expected_final_state.observation_sets[0].must[0]


def _identity_expectation() -> StateExpectation:
    return StateExpectation(
        pointer="/items",
        operator="contains",
        value={"type": "document", "path": IMPORTED_PATH, "uri": CANONICAL_URI},
    )


def _has_imported_document(row: dict[str, Any]) -> bool:
    return any(
        item.get("type") == "document" and item.get("path") == IMPORTED_PATH
        for item in row["items"]
    )


def test_attempt03_browse_contract_accepts_all_actual_imported_document_rows() -> None:
    rows = _browse_rows()
    expected = _browse_expectation()
    positive_rows = [row for row in rows if _has_imported_document(row)]

    assert len(rows) == 21
    assert len(positive_rows) == 20
    assert all(row["state_available_after"] and row["browse_available"] and row["browse_status_code"] == 200 for row in positive_rows)

    rejected = [
        f"{row['arm']}:{row['model_class']}:{row['transport']}:{row['task_id']}#{row['repeat_index']}"
        for row in positive_rows
        if not expectation_holds(row["items"], expected)
    ]

    assert not rejected, f"actual attempt03 browse rows rejected by corpus oracle: {rejected[:5]} ({len(rejected)} total)"


def test_attempt03_preserves_available_browse_row_where_imported_document_is_absent() -> None:
    rows = _browse_rows()
    missing = [row for row in rows if not _has_imported_document(row)]

    assert len(missing) == 1
    row = missing[0]
    assert (row["arm"], row["model_class"], row["transport"], row["task_id"], row["repeat_index"]) == (
        "candidate",
        "primary",
        "stdio",
        "import-export-en",
        1,
    )
    assert row["state_available_after"] is True
    assert row["browse_available"] is True
    assert row["browse_status_code"] == 200
    assert not expectation_holds(row["items"], _identity_expectation())


@pytest.mark.parametrize(
    ("field", "wrong_value"),
    [
        pytest.param("path", "notes/other.md", id="wrong-path"),
        pytest.param("uri", "akb://catalog-bench-io/coll/notes/doc/other.md", id="wrong-uri"),
        pytest.param("type", "file", id="wrong-type"),
    ],
)
def test_browse_identity_accepts_attempt03_item_and_rejects_near_misses(field: str, wrong_value: str) -> None:
    row = next(row for row in _browse_rows() if _has_imported_document(row))
    expectation = _identity_expectation()
    items = row["items"]

    assert expectation_holds(items, expectation)

    changed_items = [dict(item) for item in row["items"]]
    target = next(item for item in changed_items if item.get("path") == IMPORTED_PATH)
    target[field] = wrong_value

    assert not expectation_holds(changed_items, expectation)
