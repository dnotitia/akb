from __future__ import annotations

from typing import Literal

import pytest

try:
    from fastmcp import Client, FastMCP
except ImportError:
    pytest.skip("FastMCP server extras are required for the synthetic server test", allow_module_level=True)
from pydantic_ai import Agent
from pydantic_ai.messages import ModelMessage, ModelResponse, TextPart, ToolCallPart, ToolReturnPart
from pydantic_ai.models.function import AgentInfo, FunctionModel

from mcp_catalog.catalog import create_toolset
from mcp_catalog.contracts import PROTOCOL_REVISION
from mcp_catalog.execution import ToolCallRecorder
from mcp_catalog.contracts import TaskManifest


@pytest.mark.asyncio
async def test_official_fastmcp_client_and_pydantic_ai_toolset_cross_the_server_boundary() -> None:
    server = FastMCP("catalog-test", version="1")

    @server.tool
    def inspect_record(value: str, action: Literal["read", "write"]) -> dict[str, str]:
        return {"value": value, "action": action}

    client = Client(server, mode=PROTOCOL_REVISION, cache=False)
    recorder = ToolCallRecorder(operation_map={"read": ["inspect_record"]}, secrets=())
    toolset = create_toolset(client, recorder)
    async with toolset:
        listed = await toolset.list_tools()
        assert [tool.name for tool in listed] == ["inspect_record"]

        def model_function(messages: list[ModelMessage], _info: AgentInfo) -> ModelResponse:
            if any(isinstance(part, ToolReturnPart) for message in messages for part in message.parts):
                return ModelResponse(parts=[TextPart(content="완료")])
            return ModelResponse(parts=[ToolCallPart(tool_name="inspect_record", args={"value": "ok", "action": "read"})])

        agent = Agent(model=FunctionModel(model_function), retries=0)
        result = await agent.run("값을 읽고 결과를 말해 줘.", toolsets=[toolset], infer_name=False)

    assert result.output == "완료"
    assert len(recorder.calls) == 1
    assert recorder.calls[0].server_args == {"value": "ok", "action": "read"}


def test_raw_and_server_arguments_are_separately_recorded() -> None:
    task = TaskManifest.model_validate(
        {
            "schema_version": 1,
            "id": "raw-server-args",
            "category": "single_operation",
            "prompt": "값을 읽어 줘.",
            "fixture": {"scenario": "empty", "transports": ["http"]},
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
