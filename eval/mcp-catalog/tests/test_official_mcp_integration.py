from __future__ import annotations

import hashlib
import json
import runpy
from pathlib import Path
from typing import Any, Literal

import httpx
import pytest

try:
    from fastmcp import Client, FastMCP
    from mcp.types import DiscoverResult
except ImportError:
    pytest.skip("FastMCP server extras are required for the synthetic server test", allow_module_level=True)
from pydantic_ai import Agent
from pydantic_ai.messages import (
    InstructionPart,
    ModelMessage,
    ModelRequest,
    ModelResponse,
    TextPart,
    ToolCallPart,
    ToolReturnPart,
    UserPromptPart,
)
from pydantic_ai.models.function import AgentInfo, FunctionModel

from mcp_catalog.catalog import create_toolset
from mcp_catalog.contracts import OPENROUTER_BASE_URL, PROTOCOL_REVISION, load_run_manifest
from mcp_catalog.execution import (
    MODEL_INPUT_EVIDENCE,
    ModelInputEvidence,
    ModelConfigurationError,
    SERVER_INSTRUCTIONS as SERVER_INSTRUCTIONS_CONTEXT,
    SYSTEM_PROMPT,
    ToolCallRecorder,
    TrialOutcome,
    _model_input_evidence_for,
    build_model,
    has_server_instructions_evidence,
)
from mcp_catalog.contracts import TaskManifest
from mcp_catalog.evidence import canonical_json

SERVER_INSTRUCTIONS = runpy.run_path(
    str(Path(__file__).resolve().parents[3] / "backend" / "mcp_server" / "instructions.py")
)["INSTRUCTIONS"]


def _contains_text(value: object, expected: str) -> bool:
    if isinstance(value, str):
        return expected in value
    if isinstance(value, dict):
        return any(_contains_text(item, expected) for item in value.values())
    if isinstance(value, list):
        return any(_contains_text(item, expected) for item in value)
    return False


@pytest.mark.parametrize("model_class", ["primary", "lightweight"])
@pytest.mark.asyncio
async def test_openrouter_wire_merges_system_and_actual_mcp_instructions_for_tool_round_trip(
    monkeypatch: pytest.MonkeyPatch,
    model_class: str,
) -> None:
    manifest = load_run_manifest(Path(__file__).resolve().parents[1] / "config" / "run.json")
    spec = next(model for model in manifest.models if model.class_name == model_class)
    monkeypatch.setenv(spec.base_url_env, OPENROUTER_BASE_URL)
    monkeypatch.setenv(spec.provider_key_env, "fixture-provider-key")
    model = build_model(spec)
    original_http_client = model.client._client

    requests: list[tuple[httpx.Request, dict[str, Any]]] = []
    strict_backend_violations: list[str] = []

    def handle_request(request: httpx.Request) -> httpx.Response:
        body: dict[str, Any] = json.loads(request.content)
        requests.append((request, body))
        messages = body["messages"]
        system_positions = [index for index, message in enumerate(messages) if message["role"] == "system"]
        if system_positions != [0]:
            strict_backend_violations.append(
                f"request {len(requests)} has system message positions {system_positions}"
            )
            return httpx.Response(
                400,
                json={"error": {"message": "System message must be at the beginning."}},
            )

        completion_index = len(requests)
        message: dict[str, object]
        finish_reason: str
        if completion_index == 1:
            message = {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": "call-inspect-record",
                        "type": "function",
                        "function": {
                            "name": "inspect_record",
                            "arguments": '{"value":"record-42"}',
                        },
                    }
                ],
            }
            finish_reason = "tool_calls"
        else:
            message = {"role": "assistant", "content": "The value is record-42."}
            finish_reason = "stop"
        return httpx.Response(
            200,
            json={
                "id": f"wire-response-{completion_index}",
                "object": "chat.completion",
                "created": 1,
                "model": spec.model_id,
                "choices": [{"index": 0, "finish_reason": finish_reason, "message": message}],
                "usage": {"prompt_tokens": 12, "completion_tokens": 4, "total_tokens": 16},
            },
        )

    mock_http_client = httpx.AsyncClient(transport=httpx.MockTransport(handle_request))
    model.client._client = mock_http_client
    server = FastMCP("wire-contract", instructions=SERVER_INSTRUCTIONS, version="1")

    @server.tool
    def inspect_record(value: str) -> dict[str, str]:
        return {"value": value}

    probe = Client(server, mode=PROTOCOL_REVISION, auto_initialize=False, cache=False)
    evidence: list[ModelInputEvidence] = []
    result = None
    run_error: Exception | None = None
    instructions_token = SERVER_INSTRUCTIONS_CONTEXT.set(SERVER_INSTRUCTIONS)
    evidence_token = MODEL_INPUT_EVIDENCE.set(evidence)
    try:
        async with probe:
            raw_discovery = await probe.session.send_discover(PROTOCOL_REVISION)
        discovery = DiscoverResult.model_validate(raw_discovery)
        assert discovery.instructions == SERVER_INSTRUCTIONS

        client = Client(server, mode=PROTOCOL_REVISION, prior_discover=discovery, cache=False)
        toolset = create_toolset(client)
        async with toolset:
            agent = Agent(model=model, system_prompt=SYSTEM_PROMPT, retries=0)
            try:
                result = await agent.run(
                    "Inspect record 42 and report its value.",
                    toolsets=[toolset],
                    model_settings=model.settings,
                    infer_name=False,
                )
            except Exception as exc:
                run_error = exc
    finally:
        MODEL_INPUT_EVIDENCE.reset(evidence_token)
        SERVER_INSTRUCTIONS_CONTEXT.reset(instructions_token)
        await mock_http_client.aclose()
        await original_http_client.aclose()

    assert not strict_backend_violations, (
        f"strict backend rejected the wire messages: {strict_backend_violations}; {run_error!r}"
    )
    assert run_error is None
    assert result is not None
    assert result.output == "The value is record-42."
    assert len(requests) == 2
    assert model.client.max_retries == 0

    spec_routing = spec.routing.request_body(
        input_price=spec.input_cost_per_million_usd,
        output_price=spec.output_cost_per_million_usd,
    )["provider"]
    expected_system_content = f"{SYSTEM_PROMPT}\n\n{SERVER_INSTRUCTIONS}"
    for request, body in requests:
        assert request.url.host == "openrouter.ai"
        assert request.headers["X-OpenRouter-Metadata"] == "enabled"
        assert body["model"] == spec.model_id
        assert body["provider"] == spec_routing
        assert body["max_tokens"] == spec.settings["max_tokens"]
        assert body["temperature"] == spec.settings["temperature"]
        assert [message["role"] for message in body["messages"]].count("system") == 1
        assert body["messages"][0] == {"role": "system", "content": expected_system_content}
        assert len(body["tools"]) == 1
        function = body["tools"][0]["function"]
        assert function["name"] == "inspect_record"
        assert function["parameters"]["type"] == "object"
        assert function["parameters"]["properties"] == {"value": {"type": "string"}}
        assert function["parameters"]["required"] == ["value"]

    first_messages = requests[0][1]["messages"]
    second_messages = requests[1][1]["messages"]
    assert [message["role"] for message in first_messages] == ["system", "user"]
    assert first_messages[1]["content"] == "Inspect record 42 and report its value."
    assert [message["role"] for message in second_messages] == ["system", "user", "assistant", "tool"]
    assert second_messages[1] == first_messages[1]
    assert second_messages[2]["tool_calls"][0]["function"] == {
        "name": "inspect_record",
        "arguments": '{"value":"record-42"}',
    }
    assert second_messages[3]["tool_call_id"] == "call-inspect-record"
    assert json.loads(second_messages[3]["content"]) == {"value": "record-42"}

    instructions_sha256 = hashlib.sha256(SERVER_INSTRUCTIONS.encode("utf-8")).hexdigest()
    assert len(evidence) == 2
    for request_index, item in enumerate(evidence, start=1):
        evidence_row = item.model_dump(mode="json")
        canonical_input = evidence_row["model_input_canonical_json"]
        assert evidence_row["request_index"] == request_index
        assert evidence_row["server_instructions_text"] == SERVER_INSTRUCTIONS
        assert evidence_row["server_instructions_sha256"] == instructions_sha256
        assert evidence_row["server_instructions_present"] is True
        assert evidence_row["model_input_sha256"] == hashlib.sha256(canonical_input.encode("utf-8")).hexdigest()
        assert SYSTEM_PROMPT in canonical_input
        canonical_messages = json.loads(canonical_input)
        assert _contains_text(canonical_messages, SERVER_INSTRUCTIONS.rstrip("\r\n"))


@pytest.mark.asyncio
async def test_official_fastmcp_client_and_pydantic_ai_toolset_cross_the_server_boundary() -> None:
    server_instructions = SERVER_INSTRUCTIONS
    server = FastMCP("catalog-test", instructions=server_instructions, version="1")

    @server.tool
    def inspect_record(value: str, action: Literal["read", "write"]) -> dict[str, str]:
        return {
            "value": value,
            "action": action,
            "markdown": "![benchmark sample image](/api/assets/fixture-image)",
        }

    probe = Client(server, mode=PROTOCOL_REVISION, auto_initialize=False, cache=False)
    async with probe:
        raw_discovery = await probe.session.send_discover(PROTOCOL_REVISION)
    discovery = DiscoverResult.model_validate(raw_discovery)
    assert discovery.instructions == server_instructions
    client = Client(server, mode=PROTOCOL_REVISION, prior_discover=discovery, cache=False)
    recorder = ToolCallRecorder(
        operation_map={"read": ["inspect_record"]},
        secrets=(),
        capture_result_fields={"inspect_record": ["markdown"]},
    )
    toolset = create_toolset(client, recorder)
    async with toolset:
        listed = await toolset.list_tools()
        assert [tool.name for tool in listed] == ["inspect_record"]

        seen_instructions: list[str] = []

        def model_function(messages: list[ModelMessage], _info: AgentInfo) -> ModelResponse:
            seen_instructions.extend(
                part.content
                for message in messages
                for part in message.parts
                if getattr(part, "part_kind", None) == "instruction"
            )
            seen_instructions.extend(
                message.instructions
                for message in messages
                if isinstance(message, ModelRequest) and message.instructions is not None
            )
            if any(isinstance(part, ToolReturnPart) for message in messages for part in message.parts):
                return ModelResponse(parts=[TextPart(content="완료")])
            return ModelResponse(parts=[ToolCallPart(tool_name="inspect_record", args={"value": "ok", "action": "read"})])

        agent = Agent(model=FunctionModel(model_function), retries=0)
        result = await agent.run("값을 읽고 결과를 말해 줘.", toolsets=[toolset], infer_name=False)

    assert result.output == "완료"
    assert any(
        server_instructions.rstrip("\r\n") == observed.rstrip("\r\n")
        for observed in seen_instructions
    )
    assert len(recorder.calls) == 1
    assert recorder.calls[0].server_args == {"value": "ok", "action": "read"}
    assert recorder.calls[0].result_fields == {"markdown": "![benchmark sample image](/api/assets/fixture-image)"}


def test_raw_and_server_arguments_are_separately_recorded() -> None:
    task = TaskManifest.model_validate(
        {
                "schema_version": 2,
                "id": "raw-server-args",
                "suite": "capability",
                "category": "single_operation",
                "locale": "en-US",
                "pair_id": "raw-server",
                "capability_families": ["document_discovery_read_history"],
                "user_outcome": "Read the requested value without mutating state.",
                "accepted_behaviors": [
                    {
                        "id": "read-value",
                        "description": "Return the requested value.",
                        "mode": "complete",
                        "required_operations": ["read"],
                        "required_resources": ["document"],
                    }
                ],
                "allowed_resources": ["document"],
                "prompt": "값을 읽어 줘.",
                "fixture": {"scenario": "empty", "transports": ["http"]},
                "allowed_material_operations": ["read"],
                "allowed_first_operations": ["read"],
            "expected_final_state": {
                "probe": {"service": "app", "path": "/state"},
                "unchanged": ["/items"],
            },
        }
    )
    recorder = ToolCallRecorder(operation_map={"read": ["inspect_record"]}, secrets=())
    recorder.calls.append(
        __import__("mcp_catalog.execution", fromlist=["_ObservedCall"])._ObservedCall(
            tool_name="inspect_record", server_args={"value": "ok"}, succeeded=True
        )
    )
    from mcp_catalog.execution import bind_tool_calls

    records = bind_tool_calls(
        [("inspect_record", '{"value":"ok"}')], recorder.calls, recorder.operation_map, ()
    )

    assert records[0].raw_model_args == '{"value":"ok"}'
    assert records[0].server_args == {"value": "ok"}
    assert records[0].argument_valid
    assert task.expected_final_state.probe.path == "/state"


def test_model_request_evidence_hashes_only_inputs_that_contain_server_instructions() -> None:
    instructions = SERVER_INSTRUCTIONS
    request = ModelRequest(parts=[InstructionPart(content=instructions)])
    evidence = _model_input_evidence_for([request], instructions, 1)
    outcome = TrialOutcome(
        task_id="instruction-evidence",
        category="single_operation",
        arm="candidate",
        model_class="primary",
        model_id="test-model",
        transport="http",
        model_requests=1,
        server_instructions_sha256=hashlib.sha256(instructions.encode("utf-8")).hexdigest(),
        model_input_evidence=[evidence],
    )

    assert evidence.server_instructions_present is True
    assert evidence.model_input_sha256 != evidence.server_instructions_sha256
    assert has_server_instructions_evidence(outcome, instructions)
    with pytest.raises(ModelConfigurationError, match="missing from the model input"):
        _model_input_evidence_for([ModelRequest(parts=[])], instructions, 1)


def test_model_input_evidence_preserves_redacted_sdk_messages_and_verifiable_hash() -> None:
    instructions = "## Tool guidance\nUse the returned URI.\nKeep the existing table."
    request = ModelRequest(
        parts=[
            InstructionPart(content=instructions),
            UserPromptPart(content="Source body: first line\nsecond line; Bearer pat-secret"),
            ToolReturnPart(
                tool_name="akb_put",
                content=(
                    '{"code":"vault_skill_required","vault_skill":'
                    '{"ack_token":"ack-secret"},"body":"source body preserved"}'
                ),
            ),
            UserPromptPart(content="OpenRouter=openrouter-secret; session=session-secret"),
        ]
    )

    evidence = _model_input_evidence_for(
        [request],
        instructions,
        1,
        secrets=("pat-secret", "ack-secret", "openrouter-secret", "session-secret"),
    )

    serialized = evidence.model_input_canonical_json
    assert hashlib.sha256(serialized.encode("utf-8")).hexdigest() == evidence.model_input_sha256
    parsed = json.loads(serialized)
    assert instructions in parsed[0]["parts"][0]["content"]
    assert "Source body: first line\nsecond line" in parsed[0]["parts"][1]["content"]
    assert "source body preserved" in serialized
    assert "pat-secret" not in serialized
    assert "ack-secret" not in serialized
    assert "openrouter-secret" not in serialized
    assert "session-secret" not in serialized

    outcome = TrialOutcome(
        task_id="instruction-evidence",
        category="single_operation",
        arm="candidate",
        model_class="primary",
        model_id="test-model",
        transport="http",
        model_requests=1,
        server_instructions_sha256=hashlib.sha256(instructions.encode("utf-8")).hexdigest(),
        model_input_evidence=[evidence],
    )
    assert has_server_instructions_evidence(outcome, instructions)
    assert has_server_instructions_evidence(outcome)
    assert evidence.server_instructions_text == instructions
    evidence.model_input_canonical_json += "tampered"
    assert not has_server_instructions_evidence(outcome, instructions)


@pytest.mark.parametrize(
    ("transport", "instructions", "sdk_instruction_text"),
    [
        ("http", SERVER_INSTRUCTIONS, SERVER_INSTRUCTIONS.rstrip("\r\n")),
        ("stdio", "first line\nsecond line", "first line\nsecond line"),
    ],
    ids=("http-terminal-lf", "stdio-multiline-raw"),
)
def test_instruction_evidence_checks_raw_source_and_sdk_input(
    transport: str,
    instructions: str,
    sdk_instruction_text: str,
) -> None:
    evidence = _model_input_evidence_for(
        [ModelRequest(parts=[InstructionPart(content=sdk_instruction_text)])],
        instructions,
        1,
    )
    source_sha256 = hashlib.sha256(instructions.encode("utf-8")).hexdigest()
    outcome = TrialOutcome(
        task_id="instruction-evidence",
        category="single_operation",
        arm="candidate",
        model_class="primary",
        model_id="test-model",
        transport=transport,
        model_requests=1,
        server_instructions_sha256=source_sha256,
        model_input_evidence=[evidence],
    )

    assert evidence.server_instructions_sha256 == source_sha256
    assert evidence.server_instructions_text == instructions
    assert has_server_instructions_evidence(outcome, instructions)
    assert has_server_instructions_evidence(outcome)

    parsed = json.loads(evidence.model_input_canonical_json)
    first_line = sdk_instruction_text.split("\n", 1)[0]
    parsed[0]["parts"][0]["content"] = sdk_instruction_text.replace(
        first_line,
        f"{first_line} changed",
        1,
    )
    changed_input = canonical_json(parsed)
    changed_evidence = evidence.model_copy(
        update={
            "model_input_canonical_json": changed_input,
            "model_input_sha256": hashlib.sha256(changed_input.encode("utf-8")).hexdigest(),
        }
    )
    changed_outcome = outcome.model_copy(update={"model_input_evidence": [changed_evidence]})
    assert not has_server_instructions_evidence(changed_outcome, instructions)

    wrong_source_hash = outcome.model_copy(update={"server_instructions_sha256": "0" * 64})
    assert not has_server_instructions_evidence(wrong_source_hash, instructions)

    wrong_evidence_hash = evidence.model_copy(update={"server_instructions_sha256": "0" * 64})
    assert not has_server_instructions_evidence(
        outcome.model_copy(update={"model_input_evidence": [wrong_evidence_hash]}),
        instructions,
    )

    changed_source_text = evidence.model_copy(update={"server_instructions_text": f"{instructions} changed"})
    assert not has_server_instructions_evidence(
        outcome.model_copy(update={"model_input_evidence": [changed_source_text]}),
    )
