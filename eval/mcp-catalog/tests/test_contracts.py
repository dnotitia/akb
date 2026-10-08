from __future__ import annotations

import json
from pathlib import Path

import pytest

from mcp_catalog.contracts import (
    BenchmarkRunManifest,
    Budget,
    CatalogSnapshot,
    OPENROUTER_BASE_URL_ENV,
    OPENROUTER_PROVIDER_KEY_ENV,
    hash_json,
    load_run_manifest,
    load_task_corpus,
    load_tool_coverage,
    source_blind_violations_for,
    token_estimate,
    validate_public_catalog,
)

ROOT = Path(__file__).parents[1]


def test_budget_contract_contains_only_cost_and_request_timeout_limits() -> None:
    budget = {
        "max_total_cost_usd": 50.0,
        "max_cost_per_trial_usd": 0.1,
        "request_timeout_seconds": 300,
    }
    assert Budget.model_validate(budget).model_dump() == budget

    for retired_limit in ("max_model_requests", "max_wall_seconds", "max_requests_per_trial"):
        with pytest.raises(ValueError):
            Budget.model_validate({**budget, retired_limit: 1})

    raw_manifest = json.loads((ROOT / "config" / "run.json").read_text(encoding="utf-8"))
    assert raw_manifest["budget"] == budget


@pytest.mark.parametrize("field", ("max_total_cost_usd", "max_cost_per_trial_usd"))
@pytest.mark.parametrize("value", (float("inf"), float("-inf"), float("nan"), 0, -1))
def test_budget_cost_limits_must_be_finite_and_positive(field: str, value: float) -> None:
    budget = {
        "max_total_cost_usd": 50.0,
        "max_cost_per_trial_usd": 0.1,
        "request_timeout_seconds": 300,
    }

    with pytest.raises(ValueError):
        Budget.model_validate({**budget, field: value})


def _public_catalog_with_action_selectors(
    *,
    selector_form: str,
    missing_action: tuple[str, str] | None = None,
) -> CatalogSnapshot:
    manifest = load_run_manifest(ROOT / "config" / "run.json")
    arm = "candidate"
    transport = "http"
    grouped_routes: dict[str, list[str | None]] = {}
    for operation in manifest.public_operations:
        if transport not in operation.transports:
            continue
        route = getattr(operation, arm)
        grouped_routes.setdefault(route.tool, []).append(route.action)

    tools = []
    for name, actions in sorted(grouped_routes.items()):
        registered_actions = [action for action in actions if action is not None]
        if not registered_actions:
            schema = {"type": "object", "properties": {}}
        elif selector_form == "enum":
            schema = {
                "type": "object",
                "properties": {"action": {"type": "string", "enum": registered_actions}},
            }
        else:
            branches = [
                {
                    "type": "object",
                    "properties": {"action": {"type": "string", "const": action}},
                    "required": ["action"],
                    "additionalProperties": False,
                }
                for action in registered_actions
                if missing_action != (name, action)
            ]
            schema = {"type": "object", "oneOf": branches}
        tools.append({"name": name, "inputSchema": schema})

    return CatalogSnapshot(
        transport=transport,
        source_revision=manifest.arm_source_revisions[arm],
        artifact_version="test",
        tool_count=len(tools),
        catalog_hash=hash_json(tools),
        catalog_token_estimate=token_estimate(tools),
        tools=tools,
    )


def test_registered_manifest_and_corpus_cover_every_category() -> None:
    manifest_path = ROOT / "config" / "run.json"
    raw_manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest = load_run_manifest(manifest_path)
    tasks = load_task_corpus(ROOT / "corpus" / "tasks.json")
    coverage = load_tool_coverage(ROOT / "corpus" / "tool-coverage.json")

    manifest.validate_tasks(tasks)
    coverage.validate_tasks(tasks)
    coverage.validate_manifest(manifest)
    assert len(tasks) == 40
    assert len(coverage.entries) == 51
    assert "public_operations" not in raw_manifest
    assert len(raw_manifest["candidate_route_overrides"]) == 17
    assert len(manifest.public_operations) == 51
    assert {model.class_name for model in manifest.models} == {"primary", "lightweight"}
    assert {task.category for task in tasks} == set(manifest.category_minimums)
    assert {task.suite for task in tasks} == {"capability", "tool_surface_risk"}
    assert source_blind_violations_for(tasks, manifest.operation_map) == []
    assert manifest.budget.max_total_cost_usd == 50.0
    assert all(value.startswith("git:") for value in raw_manifest["arm_source_revisions"].values())
    assert manifest.arm_source_revisions == {
        arm: revision.removeprefix("git:")
        for arm, revision in raw_manifest["arm_source_revisions"].items()
    }
    assert manifest.models[0].model_id == "deepseek/deepseek-v4-flash-0731"
    assert manifest.models[1].model_id == "qwen/qwen3.8-27b"
    assert all(model.provider == "openrouter" for model in manifest.models)
    assert all(model.base_url_env == OPENROUTER_BASE_URL_ENV for model in manifest.models)
    assert all(model.provider_key_env == OPENROUTER_PROVIDER_KEY_ENV for model in manifest.models)
    for model in manifest.models:
        provider = model.routing.request_body(
            input_price=model.input_cost_per_million_usd,
            output_price=model.output_cost_per_million_usd,
        )["provider"]
        assert provider["order"] == model.routing.order
        assert provider["allow_fallbacks"] is False
        assert provider["require_parameters"] is True
        assert provider["quantizations"] == ["fp8"]
        assert "models" not in provider
    assert [model.routing.order for model in manifest.models] == [["deepinfra"], ["akashml"]]
    assert (manifest.models[0].input_cost_per_million_usd, manifest.models[0].output_cost_per_million_usd) == (0.06, 0.18)
    assert (manifest.models[1].input_cost_per_million_usd, manifest.models[1].output_cost_per_million_usd) == (0.225, 1.98)
    assert manifest.budget.max_cost_per_trial_usd == 0.1
    assert manifest.budget.request_timeout_seconds == 300
    assert {model.settings["max_tokens"] for model in manifest.models} == {8192}

    absent_before_pairs = {
        "knowledge-workflow",
        "table-publication",
        "import-export",
        "overlapping-document",
        "grep-write",
        "document-revisions",
        "history-provenance",
        "collection-relation-lifecycle",
        "table-schema-conflict",
        "access-ownership-public",
    }
    for task in tasks:
        expected_before = 404 if task.pair_id in absent_before_pairs else task.expected_final_state.probe.expected_status
        assert task.expected_final_state.resolved_before_expected_status == expected_before

    import_export = {
        task.id: task
        for task in tasks
        if task.pair_id == "import-export"
    }
    assert set(import_export) == {"import-export-ko", "import-export-en"}
    for task in import_export.values():
        import_attempt = next(
            attempt
            for attempt in task.expected_material_attempts
            if attempt.logical_operation == "create" and attempt.resource_type == "archive"
        )
        imported_files = import_attempt.arguments["files"]
        assert isinstance(imported_files, dict)
        imported_text = imported_files["notes/imported.md"]
        export = task.expected_final_state.additional_observations[0].must[0]
        exported_text = export.value
        assert isinstance(imported_text, str)
        assert isinstance(exported_text, dict)
        assert imported_text == "---\ntype: note\n---\n# Imported\nportable"
        assert export.operator == "okf_document"
        assert exported_text == {
            "type": "note",
            "resource_uri": "akb://catalog-bench-io/coll/notes/doc/imported.md",
            "body": "# Imported\nportable",
        }


def test_manifest_rejects_expanded_public_operation_drift() -> None:
    raw = load_run_manifest(ROOT / "config" / "run.json").model_dump(mode="json")
    raw["public_operations"][0]["baseline"]["tool"] = "akb_wrong_route"

    with pytest.raises(ValueError, match="expanded public_operations"):
        BenchmarkRunManifest.model_validate(raw)


def test_manifest_rejects_provider_or_price_drift() -> None:
    raw = load_run_manifest(ROOT / "config" / "run.json").model_dump(mode="json")
    raw["models"][0]["input_cost_per_million_usd"] = 0.15

    with pytest.raises(ValueError, match="price ceiling"):
        BenchmarkRunManifest.model_validate(raw)

    raw["models"][0]["input_cost_per_million_usd"] = 0.06
    raw["models"][0]["routing"]["allow_fallbacks"] = True
    with pytest.raises(ValueError):
        BenchmarkRunManifest.model_validate(raw)

    raw["models"][0]["routing"]["allow_fallbacks"] = False
    raw["models"][0]["settings"]["max_tokens"] = 2048
    with pytest.raises(ValueError, match="output limits"):
        BenchmarkRunManifest.model_validate(raw)


def test_manifest_rejects_multiple_or_invalid_provider_slugs() -> None:
    raw = load_run_manifest(ROOT / "config" / "run.json").model_dump(mode="json")
    raw["models"][0]["routing"]["order"] = ["deepinfra", "akashml"]

    with pytest.raises(ValueError):
        BenchmarkRunManifest.model_validate(raw)

    raw["models"][0]["routing"]["order"] = ["DeepInfra"]
    with pytest.raises(ValueError, match="lowercase OpenRouter provider slug"):
        BenchmarkRunManifest.model_validate(raw)


def test_source_blind_check_rejects_tool_names_but_allows_logical_operations() -> None:
    task = load_task_corpus(ROOT / "corpus" / "tasks.json")[0]
    hinted = task.model_copy(update={"prompt": "사용 가능한 akb_get으로 읽어 줘."})

    violations = source_blind_violations_for([hinted], {"read": ["akb_get"]})

    assert violations == [(task.id, "akb_get")]


def test_catalog_hash_and_schema_preservation_are_deterministic() -> None:
    tools = [
        {
            "name": "records",
            "description": "A test catalog entry",
            "inputSchema": {
                "type": "object",
                "oneOf": [
                    {"type": "object", "properties": {"action": {"const": "read"}}},
                    {"type": "object", "properties": {"action": {"const": "write"}}},
                ],
            },
        }
    ]
    snapshot = CatalogSnapshot(
        transport="http",
        source_revision="0" * 40,
        artifact_version="0.0.0",
        tool_count=1,
        catalog_hash=hash_json(tools),
        catalog_token_estimate=token_estimate(tools),
        tools=tools,
    )

    assert snapshot.tools[0]["inputSchema"]["oneOf"][0]["properties"]["action"]["const"] == "read"
    assert snapshot.catalog_hash == hash_json(tools)
    assert snapshot.catalog_token_estimate == token_estimate(tools)


def test_catalog_snapshot_rejects_tampered_hash() -> None:
    tools = [{"name": "one", "inputSchema": {"type": "object"}}]

    with pytest.raises(ValueError, match="catalog_hash"):
        CatalogSnapshot(
            transport="http",
            source_revision="0" * 40,
            artifact_version="0.0.0",
            tool_count=1,
            catalog_hash="0" * 64,
            catalog_token_estimate=token_estimate(tools),
            tools=tools,
        )


def test_public_catalog_accepts_grouped_one_of_const_actions() -> None:
    manifest = load_run_manifest(ROOT / "config" / "run.json")
    snapshot = _public_catalog_with_action_selectors(selector_form="oneOf")

    validate_public_catalog(snapshot, arm="candidate", public_operations=manifest.public_operations)


def test_public_catalog_rejects_missing_grouped_one_of_action_branch() -> None:
    manifest = load_run_manifest(ROOT / "config" / "run.json")
    snapshot = _public_catalog_with_action_selectors(
        selector_form="oneOf",
        missing_action=("akb_identity", "whoami"),
    )

    with pytest.raises(ValueError, match="candidate catalog action akb_identity:whoami"):
        validate_public_catalog(snapshot, arm="candidate", public_operations=manifest.public_operations)


def test_public_catalog_still_accepts_top_level_action_enum() -> None:
    manifest = load_run_manifest(ROOT / "config" / "run.json")
    snapshot = _public_catalog_with_action_selectors(selector_form="enum")

    validate_public_catalog(snapshot, arm="candidate", public_operations=manifest.public_operations)
