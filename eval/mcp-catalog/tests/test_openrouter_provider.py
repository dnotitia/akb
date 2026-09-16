from __future__ import annotations

import asyncio
from decimal import Decimal
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

import mcp_catalog.execution as execution_module
from mcp_catalog.contracts import OPENROUTER_BASE_URL, load_run_manifest, load_task_corpus
from mcp_catalog.execution import (
    BudgetExceeded,
    BudgetLedger,
    CURRENT_TRIAL,
    GlobalWallDeadlineExceeded,
    MODEL_RESPONSES,
    ModelConfigurationError,
    OpenRouterChatModel,
    ProviderRequestReceipt,
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

    owner = await ledger.reserve_trial(Decimal(str(manifest.budget.max_total_cost_usd)))
    with pytest.raises(BudgetExceeded, match="worst-case"):
        await ledger.reserve_trial(0.01)

    await owner.release()
    assert worst_case_cost(manifest.models[1], manifest.budget) == Decimal(
        str(manifest.budget.max_cost_per_trial_usd)
    )


@pytest.mark.asyncio
async def test_over_trial_cost_is_recorded_and_blocks_followup_provider_reservations() -> None:
    registered = load_run_manifest(ROOT / "config" / "run.json")
    budget = registered.budget.model_copy(
        update={"max_total_cost_usd": 0.025, "max_cost_per_trial_usd": 0.01}
    )
    manifest = registered.model_copy(update={"budget": budget})
    ledger = BudgetLedger(manifest)
    guard = await ledger.reserve_trial(Decimal("0.01"))
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
        await ledger.charge(outcome, guard=guard)

    assert ledger.cost_usd == Decimal("0.02")
    assert ledger.requests == 1
    assert ledger.input_tokens == 10
    assert ledger.output_tokens == 2
    assert guard.is_open is False
    assert await guard.release() is False
    with pytest.raises(BudgetExceeded, match="worst-case"):
        await ledger.reserve_trial(0.01)


@pytest.mark.asyncio
async def test_provider_request_admission_enforces_global_request_limit() -> None:
    registered = load_run_manifest(ROOT / "config" / "run.json")
    budget = registered.budget.model_copy(update={"max_model_requests": 1})
    manifest = registered.model_copy(update={"budget": budget})
    ledger = BudgetLedger(manifest)
    guard = await ledger.reserve_trial(Decimal("0.01"))

    await guard()
    with pytest.raises(BudgetExceeded, match="max_model_requests"):
        await guard()

    assert ledger.requests == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("second_cost", "second_response_fails", "expected_spend"),
    [(0.006, True, 0.012), (0.004, False, 0.01)],
)
async def test_provider_response_cost_blocks_followup_request_before_the_trial_finishes(
    monkeypatch: pytest.MonkeyPatch,
    second_cost: float,
    second_response_fails: bool,
    expected_spend: float,
) -> None:
    registered = load_run_manifest(ROOT / "config" / "run.json")
    budget = registered.budget.model_copy(
        update={"max_total_cost_usd": 0.05, "max_cost_per_trial_usd": 0.01}
    )
    manifest = registered.model_copy(update={"budget": budget})
    spec = manifest.models[0]
    ledger = BudgetLedger(manifest)
    reservation = Decimal("0.01")
    request_guard = await ledger.reserve_trial(reservation)
    model = OpenRouterChatModel(
        spec.model_id,
        provider=OpenAIProvider(
            base_url=OPENROUTER_BASE_URL,
            **{"api_" + "key": "fixture-provider-key"},
        ),
    )
    def make_response(cost: float) -> ChatCompletion:
        return ChatCompletion.model_validate(
            {
                "id": f"response-cost-{cost}",
                "choices": [{"index": 0, "finish_reason": "stop", "message": {"role": "assistant", "content": "ok"}}],
                "created": 1,
                "model": spec.model_id,
                "object": "chat.completion",
                "openrouter_metadata": {
                    "endpoints": {"available": [{"provider": "OpenInference", "selected": True}]},
                },
                "usage": {"prompt_tokens": 10, "completion_tokens": 2, "total_tokens": 12, "cost": cost},
            }
        )
    response = make_response(0.006)
    followup = make_response(second_cost)
    wire_request = AsyncMock(side_effect=[response, followup])
    monkeypatch.setattr(model.client.chat.completions, "create", wire_request)
    guard_token = execution_module.PROVIDER_REQUEST_GUARD.set(request_guard)
    call = [ModelRequest(parts=[UserPromptPart("hello")])]
    try:
        await model.request(call, model.settings, ModelRequestParameters())
        if second_response_fails:
            with pytest.raises(BudgetExceeded, match="max_cost_per_trial_usd"):
                await model.request(call, model.settings, ModelRequestParameters())
        else:
            await model.request(call, model.settings, ModelRequestParameters())
        with pytest.raises(BudgetExceeded, match="max_cost_per_trial_usd"):
            await model.request(call, model.settings, ModelRequestParameters())
    finally:
        execution_module.PROVIDER_REQUEST_GUARD.reset(guard_token)

    assert wire_request.await_count == 2
    assert ledger.cost_usd == Decimal(str(expected_spend))
    assert ledger.reserved_cost_usd == Decimal("0")
    assert request_guard.requests == 2
    assert request_guard.provider_cost_usd == Decimal(str(expected_spend))
    await request_guard.release()
    next_guard = await ledger.reserve_trial(reservation)
    await next_guard.release()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("settlement_order", "cancelled_cells"),
    [
        ((0, 1, 2, 3), {1}),
        ((3, 2, 1, 0), {2}),
        ((1, 3, 0, 2), set()),
    ],
)
async def test_four_smoke_cells_settle_exact_provider_costs_without_orphan_reservations(
    settlement_order: tuple[int, int, int, int],
    cancelled_cells: set[int],
) -> None:
    manifest = load_run_manifest(ROOT / "config" / "run.json")
    ledger = BudgetLedger(manifest)
    reservation = Decimal("0.1")
    response_costs = (
        (Decimal("0.00308504"), Decimal("0.00396760")),
        (Decimal("0.00174552"), Decimal("0.00192584")),
        (Decimal("0.00221522"), Decimal("0.00239988")),
        (Decimal("0.00290518"), Decimal("0.00290518")),
    )

    guards = [await ledger.reserve_trial(reservation) for _ in range(4)]

    def assert_reserved_matches_open_owners() -> None:
        assert ledger.reserved_cost_usd == sum(
            (guard.reserved_cost_usd for guard in guards if guard.is_open),
            Decimal("0"),
        )

    assert_reserved_matches_open_owners()

    async def admit_and_record(guard, costs) -> None:
        for cost in costs:
            receipt = await guard()
            await asyncio.sleep(0)
            assert await ledger.record_provider_response_cost(guard, receipt, cost)
            assert not await ledger.record_provider_response_cost(guard, receipt, cost)
            assert_reserved_matches_open_owners()

    await asyncio.gather(
        *(admit_and_record(guard, costs) for guard, costs in zip(guards, response_costs))
    )

    settlement_turns = [asyncio.Event() for _ in guards]
    settlement_turns[settlement_order[0]].set()

    async def settle_cell(cell_index: int) -> None:
        await settlement_turns[cell_index].wait()
        guard = guards[cell_index]
        if cell_index in cancelled_cells:
            assert await guard.release()
            assert not await guard.release()
            assert_reserved_matches_open_owners()
        else:
            model_spec = manifest.models[cell_index % len(manifest.models)]
            cell_cost = sum(response_costs[cell_index], Decimal("0"))
            outcome = TrialOutcome(
                task_id=f"smoke-cell-{cell_index}",
                category="single_operation",
                arm="baseline",
                model_class=model_spec.class_name,
                model_id=model_spec.model_id,
                transport="stdio" if cell_index % 2 else "http",
                input_tokens=10,
                output_tokens=2,
                total_tokens=12,
                model_requests=len(response_costs[cell_index]),
                cost_usd=float(cell_cost),
                provider_cost_usd=float(cell_cost),
                cost_source="provider_response",
            )
            await ledger.charge(outcome, guard=guard)
            assert not await guard.release()
            assert_reserved_matches_open_owners()
        next_position = settlement_order.index(cell_index) + 1
        if next_position < len(settlement_order):
            settlement_turns[settlement_order[next_position]].set()

    await asyncio.gather(*(settle_cell(index) for index in range(4)))

    assert ledger.cost_usd == Decimal("0.02114946")
    assert ledger.reserved_cost_usd == Decimal("0")
    assert all(not guard.is_open for guard in guards)


@pytest.mark.asyncio
async def test_response_admission_interleaves_with_sibling_charge_and_cancellation() -> None:
    manifest = load_run_manifest(ROOT / "config" / "run.json")
    ledger = BudgetLedger(manifest)
    guards = [await ledger.reserve_trial(Decimal("0.1")) for _ in range(4)]
    admitted = asyncio.Event()
    record_response = asyncio.Event()
    response_recorded = asyncio.Event()
    charge_first = asyncio.Event()
    first_receipt: list[ProviderRequestReceipt] = []

    async def finish_first_cell() -> None:
        receipt = await guards[0]()
        first_receipt.append(receipt)
        admitted.set()
        await record_response.wait()
        assert await ledger.record_provider_response_cost(guards[0], receipt, Decimal("0.004"))
        response_recorded.set()
        await charge_first.wait()
        outcome = TrialOutcome(
            task_id="interleaved-smoke-0",
            category="single_operation",
            arm="baseline",
            model_class=manifest.models[0].class_name,
            model_id=manifest.models[0].model_id,
            transport="http",
            model_requests=1,
            cost_usd=0.004,
            provider_cost_usd=0.004,
            cost_source="provider_response",
        )
        await ledger.charge(outcome, guard=guards[0])

    first_task = asyncio.create_task(finish_first_cell())
    await admitted.wait()

    # A sibling settles two provider responses while the first admitted request
    # is still waiting for its provider response callback.
    for cost in (Decimal("0.003"), Decimal("0.002")):
        receipt = await guards[1]()
        await ledger.record_provider_response_cost(guards[1], receipt, cost)
    sibling_one = TrialOutcome(
        task_id="interleaved-smoke-1",
        category="single_operation",
        arm="baseline",
        model_class=manifest.models[1].class_name,
        model_id=manifest.models[1].model_id,
        transport="http",
        model_requests=2,
        cost_usd=0.005,
        provider_cost_usd=0.005,
        cost_source="provider_response",
    )
    await ledger.charge(sibling_one, guard=guards[1])

    # A different sibling completes provider-cost admission but is cancelled
    # before terminal charge.
    late_receipt = None
    for index, cost in enumerate((Decimal("0.002"), Decimal("0.001"))):
        receipt = await guards[2]()
        if index == 0:
            await ledger.record_provider_response_cost(guards[2], receipt, cost)
        else:
            late_receipt = receipt
    assert await guards[2].release()
    cost_before_late_response = ledger.cost_usd
    assert late_receipt is not None
    assert not await ledger.record_provider_response_cost(guards[2], late_receipt, Decimal("0.001"))
    assert not await ledger.record_provider_response_cost(guards[2], late_receipt, Decimal("NaN"))
    assert ledger.cost_usd == cost_before_late_response

    # Admit and record the last sibling, then hold it open until after the first
    # response callback has been admitted but before its terminal charge.
    sibling_three_costs = (Decimal("0.001"), Decimal("0.003"))
    for cost in sibling_three_costs:
        receipt = await guards[3]()
        await ledger.record_provider_response_cost(guards[3], receipt, cost)
    record_response.set()
    await response_recorded.wait()
    assert ledger.cost_usd == Decimal("0.015")
    assert ledger.reserved_cost_usd == sum(
        (guard.reserved_cost_usd for guard in guards if guard.is_open),
        Decimal("0"),
    )

    sibling_three = TrialOutcome(
        task_id="interleaved-smoke-3",
        category="single_operation",
        arm="baseline",
        model_class=manifest.models[1].class_name,
        model_id=manifest.models[1].model_id,
        transport="stdio",
        model_requests=2,
        cost_usd=0.004,
        provider_cost_usd=0.004,
        cost_source="provider_response",
    )
    await ledger.charge(sibling_three, guard=guards[3])
    charge_first.set()
    await first_task

    assert await ledger.record_provider_response_cost(
        guards[0],
        first_receipt[0],
        Decimal("0.004"),
    ) is False
    assert await guards[0].release() is False
    assert ledger.cost_usd == Decimal("0.015")
    assert ledger.requests == 7
    assert ledger.reserved_cost_usd == Decimal("0")
    assert all(not guard.is_open and guard.reserved_cost_usd == 0 for guard in guards)
    reopened = await ledger.reserve_trial(Decimal("0.1"))
    await reopened.release()


@pytest.mark.asyncio
async def test_terminal_charge_accounts_provider_response_missing_from_cancelled_callback() -> None:
    manifest = load_run_manifest(ROOT / "config" / "run.json")
    model_spec = manifest.models[0]
    ledger = BudgetLedger(manifest)
    guard = await ledger.reserve_trial(Decimal("0.1"))
    first_receipt = await guard()
    await ledger.record_provider_response_cost(guard, first_receipt, Decimal("0.01"))

    # The second response is present in the terminal usage snapshot, but its
    # callback was interrupted before the guard could admit its cost.
    await guard()
    outcome = TrialOutcome(
        task_id="cancelled-response-callback",
        category="single_operation",
        arm="baseline",
        model_class=model_spec.class_name,
        model_id=model_spec.model_id,
        transport="http",
        input_tokens=20,
        output_tokens=4,
        total_tokens=24,
        model_requests=2,
        cost_usd=0.03,
        provider_cost_usd=0.03,
        cost_source="provider_response",
        provider_evidence=[
            {"model": model_spec.model_id, "usage": {"prompt_tokens": 10, "completion_tokens": 2, "cost": 0.01}},
            {"model": model_spec.model_id, "usage": {"prompt_tokens": 10, "completion_tokens": 2, "cost": 0.02}},
        ],
    )

    await ledger.charge(outcome, guard=guard)

    assert ledger.cost_usd == Decimal("0.03")
    assert ledger.requests == 2
    assert ledger.reserved_cost_usd == Decimal("0")
    assert not guard.is_open


@pytest.mark.asyncio
async def test_restored_budget_failure_blocks_new_trial_reservations() -> None:
    manifest = load_run_manifest(ROOT / "config" / "run.json")
    ledger = BudgetLedger(manifest)
    ledger.restore(
        model_requests=0,
        input_tokens=0,
        output_tokens=0,
        cost_usd=0.0,
        wall_seconds=0.0,
        budget_failure="max_total_cost_usd exceeded",
    )
    with pytest.raises(BudgetExceeded, match="max_total_cost_usd"):
        await ledger.reserve_trial(Decimal("0.1"))
    assert ledger.requests == 0
    assert ledger.reserved_cost_usd == Decimal("0")


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

    guard = await ledger.reserve_trial(Decimal("0.1"))
    await guard()
    await ledger.charge(outcome, guard=guard)

    assert ledger.input_tokens == 10_000_000
    assert ledger.output_tokens == 8_000_000
    assert ledger.cost_usd == Decimal("0.01")


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
        guard = await ledger.reserve_trial(Decimal("0.001"))
        await guard()
        provider_calls += 1
        await ledger.charge(outcome, guard=guard)

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
    full_reservation = await ledger.reserve_trial(Decimal(str(manifest.budget.max_total_cost_usd)))
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
    await full_reservation.release()


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
