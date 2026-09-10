from __future__ import annotations

from pathlib import Path

import pytest

from mcp_catalog.contracts import (
    BenchmarkRunManifest,
    CatalogSnapshot,
    OPENROUTER_BASE_URL_ENV,
    OPENROUTER_PROVIDER_KEY_ENV,
    hash_json,
    load_run_manifest,
    load_task_corpus,
    source_blind_violations_for,
    token_estimate,
)

ROOT = Path(__file__).parents[1]


def test_registered_manifest_and_corpus_cover_every_category() -> None:
    manifest = load_run_manifest(ROOT / "config" / "run.json")
    tasks = load_task_corpus(ROOT / "corpus" / "tasks.json")

    manifest.validate_tasks(tasks)
    assert len(tasks) == 16
    assert {model.class_name for model in manifest.models} == {"primary", "lightweight"}
    assert {task.category for task in tasks} == set(manifest.category_minimums)
    assert source_blind_violations_for(tasks, manifest.operation_map) == []
    assert manifest.budget.max_total_cost_usd == 50.0
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
        assert provider["order"] == ["parasail"]
        assert provider["allow_fallbacks"] is False
        assert provider["require_parameters"] is True
        assert "models" not in provider
    assert (manifest.models[0].input_cost_per_million_usd, manifest.models[0].output_cost_per_million_usd) == (0.14, 0.28)
    assert (manifest.models[1].input_cost_per_million_usd, manifest.models[1].output_cost_per_million_usd) == (0.24, 2.2)
    assert manifest.budget.max_input_tokens_per_trial + manifest.budget.max_output_tokens_per_trial == manifest.budget.max_tokens_per_trial
    assert manifest.budget.max_input_tokens_per_trial == 40000
    assert manifest.budget.max_output_tokens_per_trial == 1600
    assert manifest.budget.max_tokens_per_trial == 41600


def test_manifest_rejects_provider_or_price_drift() -> None:
    raw = load_run_manifest(ROOT / "config" / "run.json").model_dump(mode="json")
    raw["models"][0]["input_cost_per_million_usd"] = 0.15

    with pytest.raises(ValueError, match="pricing snapshot"):
        BenchmarkRunManifest.model_validate(raw)

    raw["models"][0]["input_cost_per_million_usd"] = 0.14
    raw["models"][0]["routing"]["allow_fallbacks"] = True
    with pytest.raises(ValueError):
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
