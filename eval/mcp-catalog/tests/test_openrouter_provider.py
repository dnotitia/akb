from __future__ import annotations

import asyncio
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
    GlobalWallDeadlineExceeded,
    MODEL_RESPONSES,
    ModelConfigurationError,
    OpenRouterChatModel,
    ProviderRequestTimeout,
    TrialContext,
    TrialExecutor,
    ToolCallRecorder,
    TrialOutcome,
    _has_terminal_response_after_tool,
    build_model,
    classify_failure,
    has_measured_evidence,
    outcome_from_run,
    validate_routing_evidence,
    worst_case_cost,
    run_agent_with_deadline,
)
from mcp_catalog.checkpoint import valid_completed_outcome
from mcp_catalog.runtime import StateObservation

ROOT = Path(__file__).parents[1]


def _model_spec():
    return load_run_manifest(ROOT / "config" / "run.json").models[0]


class _NeverReturningAgent:
    def __init__(self) -> None:
        self.settings: dict[str, object] | None = None

    async def run(self, _prompt: str, **kwargs: object) -> object:
        self.settings = kwargs["model_settings"]  # type: ignore[assignment]
        await asyncio.Event().wait()


@pytest.mark.asyncio
async def test_never_returning_provider_is_bounded_by_request_and_global_deadlines() -> None:
    agent = _NeverReturningAgent()

    with pytest.raises(ProviderRequestTimeout):
        await run_agent_with_deadline(
            agent,
            "hang",
            toolsets=[],
            model_settings={},
            usage_limits=SimpleNamespace(),
            request_timeout_seconds=0.01,
            remaining_wall_seconds=0.2,
        )
    assert agent.settings == {"timeout": 0.01}

    with pytest.raises(GlobalWallDeadlineExceeded):
        await run_agent_with_deadline(
            agent,
            "hang",
            toolsets=[],
            model_settings={},
            usage_limits=SimpleNamespace(),
            request_timeout_seconds=0.2,
            remaining_wall_seconds=0.01,
        )


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
            "allow_fallbacks": True,
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
        "allow_fallbacks": True,
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


def test_preferred_upstream_fallback_preserves_model_usage_cost_and_selected_route() -> None:
    spec = _model_spec()
    provider_request = spec.routing.request_body(
        input_price=spec.input_cost_per_million_usd,
        output_price=spec.output_cost_per_million_usd,
    )["provider"]
    assert provider_request == {
        "order": ["parasail"],
        "allow_fallbacks": True,
        "require_parameters": True,
        "max_price": {"prompt": 0.14, "completion": 0.28},
    }
    response = ChatCompletion.model_validate(
        {
            "id": "response-1",
            "choices": [{"index": 0, "finish_reason": "stop", "message": {"role": "assistant", "content": "ok"}}],
            "created": 1,
            "model": spec.model_id,
            "object": "chat.completion",
            "openrouter_metadata": {
                "endpoints": {"available": [{"provider": "OpenInference", "selected": True}]},
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
    assert outcome.provider_evidence[0]["routing"]["endpoints"]["available"] == [
        {"provider": "OpenInference", "selected": True}
    ]
    assert outcome.input_tokens == 10 and outcome.output_tokens == 2
    assert outcome.error is None
    assert has_measured_evidence(outcome)


def _partial_provider_message(spec, *, include_cost: bool = True) -> ModelResponse:
    usage = {"prompt_tokens": 10, "completion_tokens": 2}
    if include_cost:
        usage["cost"] = 0.00000196
    return ModelResponse(
        parts=[TextPart(content="partial response")],
        usage=RequestUsage(input_tokens=10, output_tokens=2),
        model_name=spec.model_id,
        provider_name="openai",
        provider_url=OPENROUTER_BASE_URL,
        provider_details={
            "openrouter_metadata": {
                "endpoints": {"available": [{"provider": "Parasail", "selected": True}]}
            },
            "openrouter_usage": usage,
        },
    )


@pytest.mark.parametrize(
    ("error", "failure_kind"),
    [
        ("Exceeded the request_limit of 8.", "request_limit"),
        ("Model token limit (8192) exceeded", "output_limit"),
        ("MCP tool error: server rejected the call", "tool"),
        ("terminal response was empty", "terminal_response"),
    ],
)
def test_provider_evidence_from_partial_responses_keeps_behavioral_failures_measured(error, failure_kind) -> None:
    spec = _model_spec()
    task = load_task_corpus(ROOT / "corpus" / "tasks.json")[0]
    outcome = outcome_from_run(
        task=task,
        arm="baseline",
        model_spec=spec,
        transport="http",
        result=None,
        partial_messages=[_partial_provider_message(spec)],
        recorder=ToolCallRecorder(operation_map={}, secrets=()),
        operation_map={},
        error=error,
        latency=0.1,
        secrets=(),
    ).model_copy(update={"state_available_before": True, "state_available_after": True})

    assert outcome.failure_kind == failure_kind
    assert not outcome.success
    assert outcome.model_requests == 1
    assert outcome.provider_cost_usd == pytest.approx(0.00000196)
    assert has_measured_evidence(outcome)
    assert valid_completed_outcome(outcome)


def test_missing_provider_cost_is_not_a_measured_outcome() -> None:
    spec = _model_spec()
    task = load_task_corpus(ROOT / "corpus" / "tasks.json")[0]
    outcome = outcome_from_run(
        task=task,
        arm="baseline",
        model_spec=spec,
        transport="http",
        result=None,
        partial_messages=[_partial_provider_message(spec, include_cost=False)],
        recorder=ToolCallRecorder(operation_map={}, secrets=()),
        operation_map={},
        error=None,
        latency=0.1,
        secrets=(),
    )

    assert outcome.error == "provider usage/cost evidence was incomplete"
    assert outcome.failure_kind == "provider"
    assert not has_measured_evidence(outcome)


@pytest.mark.asyncio
async def test_model_response_capture_survives_the_agent_request_boundary(monkeypatch: pytest.MonkeyPatch) -> None:
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
                "endpoints": {"available": [{"provider": "Parasail", "selected": True}]}
            },
            "usage": {"prompt_tokens": 10, "completion_tokens": 2, "total_tokens": 12, "cost": 0.00000196},
        }
    )
    monkeypatch.setattr(model, "_completions_create", AsyncMock(return_value=response))
    captured: list[ModelResponse] = []
    capture_token = MODEL_RESPONSES.set(captured)
    try:
        result = await model.request(
            [ModelRequest(parts=[UserPromptPart("hello")])],
            model.settings,
            ModelRequestParameters(),
        )
    finally:
        MODEL_RESPONSES.reset(capture_token)

    assert captured == [result]
    assert captured[0].provider_details["openrouter_usage"]["cost"] == 0.00000196


def test_smoke_terminal_response_requires_a_text_turn_after_a_tool_turn() -> None:
    tool_turn = ModelResponse(parts=[ToolCallPart(tool_name="akb_list_vaults", args={})])
    final_turn = ModelResponse(parts=[TextPart(content="확인했습니다.")])

    assert _has_terminal_response_after_tool([tool_turn, final_turn], "확인했습니다.", 1)
    assert not _has_terminal_response_after_tool([tool_turn], "확인했습니다.", 1)
    assert not _has_terminal_response_after_tool([tool_turn, final_turn], "", 1)


def test_failure_evidence_distinguishes_provider_output_terminal_and_budget() -> None:
    assert classify_failure("ModelHTTPError: status=429", result=None, final_answer="") == "provider"
    assert classify_failure("Model token limit (8192) exceeded", result=None, final_answer="") == "output_limit"
    assert classify_failure("Exceeded the request_limit of 8", result=None, final_answer="") == "request_limit"
    assert classify_failure("terminal response was empty", result=None, final_answer="") == "terminal_response"
    assert classify_failure("benchmark incomplete: max_cost_per_trial_usd exceeded", result=None, final_answer="") == "budget"


@pytest.mark.asyncio
async def test_budget_reserves_preregistered_worst_case_before_a_trial() -> None:
    manifest = load_run_manifest(ROOT / "config" / "run.json")
    ledger = BudgetLedger(manifest)

    await ledger.reserve_trial(manifest.budget.max_total_cost_usd)
    with pytest.raises(BudgetExceeded, match="worst-case"):
        await ledger.reserve_trial(0.01)

    await ledger.release_trial(manifest.budget.max_total_cost_usd)
    assert worst_case_cost(manifest.models[1], manifest.budget) == manifest.budget.max_cost_per_trial_usd


@pytest.mark.asyncio
async def test_over_trial_cost_is_recorded_and_blocks_followup_provider_reservations() -> None:
    registered = load_run_manifest(ROOT / "config" / "run.json")
    budget = registered.budget.model_copy(
        update={"max_total_cost_usd": 0.05, "max_cost_per_trial_usd": 0.01}
    )
    manifest = registered.model_copy(update={"budget": budget})
    ledger = BudgetLedger(manifest)
    await ledger.reserve_trial(0.01)
    outcome = TrialOutcome(
        task_id="over-limit-cost",
        category="single_operation",
        arm="baseline",
        model_class="primary",
        model_id=manifest.models[0].model_id,
        transport="http",
        input_tokens=10,
        output_tokens=2,
        total_tokens=12,
        model_requests=1,
        cost_usd=0.02,
        provider_evidence=[
            {"model": manifest.models[0].model_id, "usage": {"prompt_tokens": 10, "completion_tokens": 2, "cost": 0.02}}
        ],
        provider_cost_usd=0.02,
        cost_source="provider_response",
    )

    with pytest.raises(BudgetExceeded, match="max_cost_per_trial_usd"):
        await ledger.charge(outcome, reserved_cost_usd=0.01)

    assert ledger.cost_usd == pytest.approx(0.02)
    assert ledger.requests == 1
    assert ledger.input_tokens == 10
    assert ledger.output_tokens == 2
    await ledger.release_trial(0.01)
    with pytest.raises(BudgetExceeded, match="max_cost_per_trial_usd"):
        await ledger.reserve_trial(0.01)


@pytest.mark.asyncio
async def test_large_token_outcome_is_recorded_without_a_token_budget_gate() -> None:
    manifest = load_run_manifest(ROOT / "config" / "run.json")
    ledger = BudgetLedger(manifest)
    outcome = TrialOutcome(
        task_id="large-token-outcome",
        category="single_operation",
        arm="baseline",
        model_class="primary",
        model_id=manifest.models[0].model_id,
        transport="http",
        input_tokens=10_000_000,
        output_tokens=8_000_000,
        total_tokens=18_000_000,
        model_requests=1,
        cost_usd=0.01,
    )

    await ledger.charge(outcome)

    assert ledger.input_tokens == 10_000_000
    assert ledger.output_tokens == 8_000_000
    assert ledger.cost_usd == 0.01


@pytest.mark.asyncio
async def test_parallel_lane_work_does_not_trip_the_actual_wall_guard() -> None:
    loaded = load_run_manifest(ROOT / "config" / "run.json")
    manifest = loaded.model_copy(
        update={"budget": loaded.budget.model_copy(update={"max_wall_seconds": 10})}
    )
    ledger = BudgetLedger(manifest, wall_clock=lambda: 5.0)
    provider_calls = 0

    async def lane() -> None:
        nonlocal provider_calls
        outcome = TrialOutcome(
            task_id="parallel-lane",
            category="single_operation",
            arm="baseline",
            model_class="primary",
            model_id=manifest.models[0].model_id,
            transport="http",
            model_requests=1,
            input_tokens=1,
            output_tokens=1,
            total_tokens=2,
            latency_seconds=4.0,
            cost_usd=0.001,
        )
        await ledger.reserve_trial(0.001)
        provider_calls += 1
        await ledger.charge(outcome, reserved_cost_usd=0.001)

    await asyncio.gather(*(lane() for _ in range(4)))

    assert provider_calls == 4
    assert ledger.wall_seconds == pytest.approx(5.0)
    assert ledger.model_work_seconds == pytest.approx(16.0)
    assert ledger.wall_seconds < manifest.budget.max_wall_seconds


@pytest.mark.asyncio
async def test_actual_cumulative_wall_guard_blocks_before_provider_call() -> None:
    loaded = load_run_manifest(ROOT / "config" / "run.json")
    manifest = loaded.model_copy(
        update={"budget": loaded.budget.model_copy(update={"max_wall_seconds": 10})}
    )
    ledger = BudgetLedger(manifest, wall_clock=lambda: 10.0)
    provider_calls = 0

    with pytest.raises(BudgetExceeded, match="max_wall_seconds"):
        await ledger.reserve_trial(0.001)
        provider_calls += 1

    assert provider_calls == 0


def test_request_timeout_is_capped_by_remaining_global_wall() -> None:
    loaded = load_run_manifest(ROOT / "config" / "run.json")
    manifest = loaded.model_copy(
        update={
            "budget": loaded.budget.model_copy(
                update={"max_wall_seconds": 10, "request_timeout_seconds": 7}
            )
        }
    )
    ledger = BudgetLedger(manifest, wall_clock=lambda: 8.5)

    assert ledger.remaining_wall_seconds() == pytest.approx(1.5)
    assert ledger.request_timeout_seconds() == pytest.approx(1.5)


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
    assert outcome.failure_kind == "budget"


def test_routing_evidence_requires_the_requested_model_and_one_selected_provider() -> None:
    spec = _model_spec()
    fallback_evidence = [
        {
            "model": spec.model_id,
            "routing": {"endpoints": {"available": [{"provider": "OpenInference", "selected": True}]}},
        }
    ]

    assert validate_routing_evidence(fallback_evidence, spec) == (True, True)
    assert validate_routing_evidence(
        [{"model": "unregistered/model", "routing": fallback_evidence[0]["routing"]}],
        spec,
    ) == (False, False)
    assert validate_routing_evidence(
        [{"model": spec.model_id, "routing": {"endpoints": {"available": [{"provider": "OpenInference", "selected": False}]}}}],
        spec,
    ) == (False, False)
    assert validate_routing_evidence(
        [
            {
                "model": spec.model_id,
                "routing": {
                    "endpoints": {
                        "available": [
                            {"provider": "OpenInference", "selected": True},
                            {"provider": "Parasail", "selected": True},
                        ]
                    }
                },
            }
        ],
        spec,
    ) == (False, False)
