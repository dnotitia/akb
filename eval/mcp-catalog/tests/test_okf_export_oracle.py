from __future__ import annotations

import json
from pathlib import Path

import pytest

from mcp_catalog.contracts import StateExpectation, load_task_corpus
from mcp_catalog.runtime import StateObservation
from mcp_catalog.state import evaluate_state_contract, expectation_holds, json_pointer


ROOT = Path(__file__).parents[1]
CANONICAL_URI = "akb://catalog-bench-io/coll/notes/doc/imported.md"
EXPECTED_BODY = "# Imported\nportable"
EXPECTED_OKF_DOCUMENT = {
    "type": "note",
    "resource_uri": CANONICAL_URI,
    "body": EXPECTED_BODY,
}


def _traces() -> list[dict[str, object]]:
    return json.loads((ROOT / "tests/fixtures/okf-import-export-actual-traces.json").read_text(encoding="utf-8"))


def _okf_expectation() -> StateExpectation:
    return StateExpectation(
        pointer="/files/notes~1imported.md",
        operator="okf_document",
        value=EXPECTED_OKF_DOCUMENT,
    )


def test_import_export_contract_accepts_all_four_recorded_exports_and_document_reads() -> None:
    task = next(task for task in load_task_corpus(ROOT / "corpus/tasks.json") if task.id == "import-export-en")
    contract = task.expected_final_state
    traces = _traces()

    assert len(traces) == 4
    assert len(contract.observation_sets) == 3
    assert contract.observation_sets[1].probe.path == "/api/v1/vaults/catalog-bench-io/export?format=okf&as=json"
    assert contract.observation_sets[2].probe.path == "/api/v1/documents/catalog-bench-io/notes/imported.md"

    for trace in traces:
        assert trace["export_paths"] == ["notes/imported.md"]
        assert trace["document_path"] == "/api/v1/documents/catalog-bench-io/notes/imported.md"
        assert trace["document_content"] == EXPECTED_BODY
        assert trace["revision_nonempty"] is True
        before = [
            StateObservation(True, item.resolved_before_expected_status, {})
            for item in contract.observation_sets
        ]
        after = [
            StateObservation(
                True,
                item.probe.expected_status,
                payload,
            )
            for item, payload in zip(
                contract.observation_sets,
                [
                    {
                        "items": [
                            {
                                "name": "imported.md",
                                "type": "document",
                                "path": "notes/imported.md",
                                "uri": CANONICAL_URI,
                            }
                        ]
                    },
                    {"files": {"notes/imported.md": trace["exported_document"]}},
                    {
                        "path": "notes/imported.md",
                        "uri": CANONICAL_URI,
                        "content": trace["document_content"],
                        "current_commit": "f" * 40 if trace["revision_nonempty"] else "",
                    },
                ],
                strict=True,
            )
        ]

        passed, checks = evaluate_state_contract(contract, before, after)

        assert passed, [(check.pointer, check.operator, check.reason) for check in checks if not check.passed]


@pytest.mark.parametrize(
    "mutate",
    [
        pytest.param(lambda text: text.replace("# Imported\nportable\n", "# Imported\nchanged\n", 1), id="body-changed"),
        pytest.param(lambda text: text.replace("# Imported\nportable\n", "# Imported\n", 1), id="body-truncated"),
        pytest.param(lambda text: text.replace("# Imported\nportable\n", "# Imported\nportable\nadded\n", 1), id="body-added"),
        pytest.param(lambda text: text.replace("type: note\n", "type: table\n", 1), id="type-wrong"),
        pytest.param(lambda text: text.replace("type: note\n", "", 1), id="type-missing"),
        pytest.param(
            lambda text: text.replace(CANONICAL_URI, "akb://catalog-bench-io/coll/notes/doc/other.md", 1),
            id="resource-wrong",
        ),
        pytest.param(
            lambda text: text.replace(
                f"akb_uri: {CANONICAL_URI}",
                "akb_uri: akb://catalog-bench-io/coll/notes/doc/other.md",
                1,
            ),
            id="akb-uri-wrong",
        ),
        pytest.param(lambda text: text.replace("type: note\n", "type: [malformed\n", 1), id="yaml-malformed"),
        pytest.param(lambda _text: "---\n- note\n---\n\n# Imported\nportable\n", id="yaml-not-a-mapping"),
    ],
)
def test_import_export_oracle_rejects_near_miss_documents(mutate) -> None:
    expectation = _okf_expectation()
    original = str(_traces()[0]["exported_document"])
    actual = mutate(original)

    assert expectation_holds(actual, expectation) is False


def test_import_export_oracle_requires_the_expected_bundle_path() -> None:
    expectation = _okf_expectation()
    actual = {"files": {"notes/other.md": _traces()[0]["exported_document"]}}

    assert expectation_holds(json_pointer(actual, expectation.pointer), expectation) is False


def test_import_export_oracle_normalizes_only_canonical_body_line_endings() -> None:
    expectation = _okf_expectation()
    actual = str(_traces()[0]["exported_document"])
    marker = "\n---\n\n"
    frontmatter, separator, body = actual.partition(marker)
    assert separator == marker
    crlf_body = frontmatter + separator + body.replace("\n", "\r\n")

    assert expectation_holds(crlf_body, expectation) is True


def test_import_export_document_read_requires_canonical_path_uri_body_and_nonempty_revision() -> None:
    task = next(task for task in load_task_corpus(ROOT / "corpus/tasks.json") if task.id == "import-export-en")
    document_contract = task.expected_final_state.observation_sets[2]
    observation = StateObservation(
        True,
        200,
        {
            "path": "notes/imported.md",
            "uri": CANONICAL_URI,
            "content": EXPECTED_BODY,
            "current_commit": "a" * 40,
        },
    )

    assert all(expectation_holds(json_pointer(observation.payload, item.pointer), item) for item in document_contract.must)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("path", "notes/other.md"),
        ("uri", "akb://catalog-bench-io/doc/other.md"),
        ("content", "# Imported\nchanged"),
        ("current_commit", ""),
        ("current_commit", None),
    ],
)
def test_import_export_document_read_rejects_near_miss_field(field: str, value: object) -> None:
    task = next(task for task in load_task_corpus(ROOT / "corpus/tasks.json") if task.id == "import-export-en")
    document_contract = task.expected_final_state.observation_sets[2]
    payload: dict[str, object] = {
        "path": "notes/imported.md",
        "uri": CANONICAL_URI,
        "content": EXPECTED_BODY,
        "current_commit": "a" * 40,
    }
    payload[field] = value

    assert not all(
        expectation_holds(json_pointer(payload, item.pointer), item)
        for item in document_contract.must
    )
