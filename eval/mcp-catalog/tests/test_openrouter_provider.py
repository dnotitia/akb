from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from openai.types.chat import ChatCompletion
from pydantic_ai.messages import ModelRequest, ModelResponse, TextPart, ToolCallPart, UserPromptPart
from pydantic_ai.models import ModelRequestParameters
from pydantic_ai.providers.openai import OpenAIProvider
from pydantic_ai.tools import ToolDefinition
from pydantic_ai.usage import RequestUsage, RunUsage

from mcp_catalog.contracts import OPENROUTER_BASE_URL, load_run_manifest, load_task_corpus
from mcp_catalog.execution import (
    BudgetExceeded,
    BudgetLedger,
    CURRENT_TRIAL,
    ModelConfigurationError,
    OpenRouterChatModel,
    TrialContext,
    TrialExecutor,
    ToolCallRecorder,
    _has_terminal_response_after_tool,
    build_model,
    outcome_from_run,
    validate_routing_evidence,
    worst_case_cost,
)
from mcp_catalog.runtime import StateObservation

ROOT = Path(__file__).parents[1]


def _model_spec():
    return load_run_manifest(ROOT / "config" / "run.json").models[0]


def test_build_model_uses_declared_openrouter_environment_and_forces_routing(monkeypatch: pytest.MonkeyPatch) -> None:
    spec = _model_spec()
    provider_key_env = "MCP_BENCH_OPENROUTER_" + "API_KEY"
    monkeypatch.setenv(spec.base_url_env, OPENROUTER_BASE_URL)
    monkeypatch.setenv(provider_key_env, "fixture-provider-key")

    model = build_model(spec)
    settings = model.settings

    assert isinstance(model, OpenRouterChatModel)
    assert settings["extra_body"] == {
        "provider": {
            "order": ["parasail"],
            "allow_fallbacks": False,
            "require_parameters": True,
            "max_price": {"prompt": 0.14, "completion": 0.28},
        }
    }
    assert settings["extra_headers"] == {"X-OpenRouter-Metadata": "enabled"}
    assert "models" not in settings["extra_body"]


@pytest.mark.asyncio
async def test_full_catalog_wire_path_does_not_enable_strict_tool_definitions(monkeypatch: pytest.MonkeyPatch) -> None:
    spec = _model_spec()
    provider_key_env = "MCP_BENCH_OPENROUTER_" + "API_KEY"
    monkeypatch.setenv(spec.base_url_env, OPENROUTER_BASE_URL)
    monkeypatch.setenv(provider_key_env, "fixture-provider-key")
    model = build_model(spec)
    response = ChatCompletion.model_validate(
        {
            "id": "response-1",
            "choices": [{"index": 0, "finish_reason": "stop", "message": {"role": "assistant", "content": "ok"}}],
            "created": 1,
            "model": spec.model_id,
            "object": "chat.completion",
            "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
        }
    )
    request = AsyncMock(return_value=response)
    monkeypatch.setattr(model.client.chat.completions, "create", request)
    definitions = [
        ToolDefinition(
            name=f"catalog_tool_{index}",
            parameters_json_schema={"type": "object", "properties": {"value": {"type": "string"}}},
            strict=True,
        )
        for index in range(44)
    ]

    await model._completions_create(
        [ModelRequest(parts=[UserPromptPart("hello")])],
        False,
        model.settings,
        ModelRequestParameters(function_tools=definitions),
    )

    tools = request.await_args.kwargs["tools"]
    assert model.profile["openai_supports_strict_tool_definition"] is False
    assert len(tools) == 44
    assert all("strict" not in tool["function"] for tool in tools)


def test_model_preflight_rejects_a_nonregistered_gateway(monkeypatch: pytest.MonkeyPatch) -> None:
    spec = _model_spec()
    provider_key_env = "MCP_BENCH_OPENROUTER_" + "API_KEY"
    monkeypatch.setenv(spec.base_url_env, "https://example.invalid/v1")
    monkeypatch.setenv(provider_key_env, "fixture-provider-key")

    with pytest.raises(ModelConfigurationError, match="registered endpoint"):
        build_model(spec)


@pytest.mark.asyncio
async def test_pydantic_ai_wire_request_preserves_openrouter_extra_body(monkeypatch: pytest.MonkeyPatch) -> None:
    spec = _model_spec()
    provider_key_env = "MCP_BENCH_OPENROUTER_" + "API_KEY"
    monkeypatch.setenv(spec.base_url_env, OPENROUTER_BASE_URL)
    monkeypatch.setenv(provider_key_env, "fixture-provider-key")
    model = build_model(spec)
    response = ChatCompletion.model_validate(
        {
            "id": "response-1",
            "choices": [{"index": 0, "finish_reason": "stop", "message": {"role": "assistant", "content": "ok"}}],
            "created": 1,
            "model": spec.model_id,
            "object": "chat.completion",
            "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
        }
    )
    request = AsyncMock(return_value=response)
    monkeypatch.setattr(model.client.chat.completions, "create", request)

    await model._completions_create(
        [ModelRequest(parts=[UserPromptPart("hello")])],
        False,
        model.settings,
        ModelRequestParameters(),
    )

    kwargs = request.await_args.kwargs
    assert kwargs["extra_body"]["provider"] == {
        "order": ["parasail"],
        "allow_fallbacks": False,
        "require_parameters": True,
        "max_price": {"prompt": 0.14, "completion": 0.28},
    }
    assert kwargs["extra_headers"]["X-OpenRouter-Metadata"] == "enabled"
    assert "models" not in kwargs


def test_openrouter_response_evidence_preserves_provider_usage_and_routing() -> None:
    spec = _model_spec()
    model = OpenRouterChatModel(
        spec.model_id,
        provider=OpenAIProvider(
            base_url=OPENROUTER_BASE_URL,
            **{"api_" + "key": "fixture-provider-key"},
        ),
    )
    response = ChatCompletion.model_validate(
        {
            "id": "response-1",
            "choices": [{"index": 0, "finish_reason": "stop", "message": {"role": "assistant", "content": "ok"}}],
            "created": 1,
            "model": spec.model_id,
            "object": "chat.completion",
            "openrouter_metadata": {
                "endpoints": {"available": [{"provider": "Parasail", "selected": True}]},
                "requested": spec.model_id,
            },
            "usage": {
                "prompt_tokens": 10,
                "completion_tokens": 2,
                "total_tokens": 12,
                "cost": 0.00000196,
                "cost_details": {"upstream_inference_cost": 0.00000196},
            },
        }
    )

    details = model._process_provider_details(response)

    assert details is not None
    assert details["openrouter_metadata"]["endpoints"]["available"][0]["provider"] == "Parasail"
    assert details["openrouter_usage"]["cost"] == 0.00000196
    assert details["openrouter_usage"]["prompt_tokens"] == 10


def test_outcome_records_provider_cost_usage_and_route_from_model_response() -> None:
    spec = _model_spec()
    response = ChatCompletion.model_validate(
        {
            "id": "response-1",
            "choices": [{"index": 0, "finish_reason": "stop", "message": {"role": "assistant", "content": "ok"}}],
            "created": 1,
            "model": spec.model_id,
            "object": "chat.completion",
            "openrouter_metadata": {
                "endpoints": {"available": [{"provider": "Parasail", "selected": True}]},
            },
            "usage": {"prompt_tokens": 10, "completion_tokens": 2, "total_tokens": 12, "cost": 0.00000196},
        }
    )
    model = OpenRouterChatModel(
        spec.model_id,
        provider=OpenAIProvider(
            base_url=OPENROUTER_BASE_URL,
            **{"api_" + "key": "fixture-provider-key"},
        ),
    )
    details = model._process_provider_details(response)
    message = ModelResponse(
        parts=[TextPart(content="ok")],
        usage=RequestUsage(input_tokens=10, output_tokens=2),
        model_name=spec.model_id,
        provider_name="openai",
        provider_url=OPENROUTER_BASE_URL,
        provider_details=details,
    )
    result = SimpleNamespace(
        output="ok",
        all_messages=lambda: [message],
        usage=RunUsage(input_tokens=10, output_tokens=2, requests=1),
    )
    task = load_task_corpus(ROOT / "corpus" / "tasks.json")[0]

    outcome = outcome_from_run(
        task=task,
        arm="baseline",
        model_spec=spec,
        transport="http",
        result=result,
        recorder=ToolCallRecorder(operation_map={}, secrets=()),
        operation_map={},
        error=None,
        latency=0.1,
        secrets=(),
    )

    assert outcome.provider_cost_usd == pytest.approx(0.00000196)
    assert outcome.cost_source == "provider_response"
    assert outcome.routing_observed and outcome.routing_valid
    assert outcome.input_tokens == 10 and outcome.output_tokens == 2
    assert outcome.error is None


def test_smoke_terminal_response_requires_a_text_turn_after_a_tool_turn() -> None:
    tool_turn = ModelResponse(parts=[ToolCallPart(tool_name="akb_list_vaults", args={})])
    final_turn = ModelResponse(parts=[TextPart(content="확인했습니다.")])

    assert _has_terminal_response_after_tool([tool_turn, final_turn], "확인했습니다.", 1)
    assert not _has_terminal_response_after_tool([tool_turn], "확인했습니다.", 1)
    assert not _has_terminal_response_after_tool([tool_turn, final_turn], "", 1)


@pytest.mark.asyncio
async def test_budget_reserves_preregistered_worst_case_before_a_trial() -> None:
    manifest = load_run_manifest(ROOT / "config" / "run.json")
    ledger = BudgetLedger(manifest)

    await ledger.reserve_trial(manifest.budget.max_total_cost_usd)
    with pytest.raises(BudgetExceeded, match="worst-case"):
        await ledger.reserve_trial(0.01)

    await ledger.release_trial(manifest.budget.max_total_cost_usd)
    assert worst_case_cost(manifest.models[1], manifest.budget) < manifest.budget.max_cost_per_trial_usd


@pytest.mark.asyncio
async def test_trial_does_not_create_a_provider_client_after_budget_reservation_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    manifest = load_run_manifest(ROOT / "config" / "run.json")
    task = load_task_corpus(ROOT / "corpus" / "tasks.json")[0]
    spec = manifest.models[0]
    model = OpenRouterChatModel(
        spec.model_id,
        provider=OpenAIProvider(
            base_url=OPENROUTER_BASE_URL,
            **{"api_" + "key": "fixture-provider-key"},
        ),
    )
    ledger = BudgetLedger(manifest)
    await ledger.reserve_trial(manifest.budget.max_total_cost_usd)
    monkeypatch.setattr("mcp_catalog.execution.create_client", lambda _spec: pytest.fail("client must not be created"))
    token = CURRENT_TRIAL.set(
        TrialContext(
            task=task,
            token="fixture-token",
            before=StateObservation(True, 200, {}),
            secrets=("fixture-token",),
        )
    )
    try:
        executor = TrialExecutor(
            manifest,
            arm="baseline",
            model_spec=spec,
            model=model,
            transport="http",
            fixture=None,  # type: ignore[arg-type]
            token_for=lambda _profile: "fixture-token",
            secrets_for=lambda _profile: ("fixture-token",),
            ledger=ledger,
        )
        outcome = await executor.execute(task)
    finally:
        CURRENT_TRIAL.reset(token)

    assert outcome.error is not None and outcome.error.startswith("benchmark incomplete:")


def test_routing_evidence_accepts_only_the_registered_model_and_upstream() -> None:
    spec = _model_spec()
    evidence = [
        {
            "model": spec.model_id,
            "routing": {"endpoints": {"available": [{"provider": "Parasail", "selected": True}]}},
        }
    ]

    assert validate_routing_evidence(evidence, spec) == (True, True)
    assert validate_routing_evidence(
        [{"model": spec.model_id, "routing": {"endpoints": {"available": [{"provider": "OpenAI", "selected": True}]}}}],
        spec,
    ) == (True, False)
