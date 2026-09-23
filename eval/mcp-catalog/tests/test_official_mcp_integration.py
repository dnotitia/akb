from __future__ import annotations

from typing import Any, Literal, cast

import httpx
import pytest

try:
    from fastmcp import Client, FastMCP
    from fastmcp.client.transports.http import StreamableHttpTransport
    from fastmcp.tools import Tool
    from mcp.types import ToolAnnotations
except ImportError:
    pytest.skip("FastMCP server extras are required for the synthetic server test", allow_module_level=True)
from pydantic_ai import Agent
from pydantic_ai.messages import ModelMessage, ModelResponse, TextPart, ToolCallPart, ToolReturnPart
from pydantic_ai.models.function import AgentInfo, FunctionModel

import mcp_catalog.catalog as catalog_module
from mcp_catalog.catalog import ConnectionSpec, capture_catalog, create_toolset
from mcp_catalog.contracts import PROTOCOL_REVISION
from mcp_catalog.execution import ToolCallRecorder
from mcp_catalog.contracts import TaskManifest
from mcp_catalog.runtime import RuntimeDescriptor, RuntimeFixture


@pytest.mark.asyncio
async def test_official_fastmcp_client_and_pydantic_ai_toolset_cross_the_server_boundary() -> None:
    server = FastMCP("catalog-test", version="1")

    @server.tool
    def inspect_record(value: str, action: Literal["read", "write"]) -> dict[str, str]:
        return {
            "value": value,
            "action": action,
            "markdown": "![benchmark sample image](/api/assets/fixture-image)",
        }

    client = Client(server, mode=PROTOCOL_REVISION, cache=False)
    recorder = ToolCallRecorder(
        operation_map={"read": ["inspect_record"]},
        secrets=(),
        capture_result_fields={"inspect_record": ["markdown"]},
    )
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
    assert recorder.calls[0].result_fields == {"markdown": "![benchmark sample image](/api/assets/fixture-image)"}


@pytest.mark.asyncio
async def test_ac8_candidate_http_catalog_preserves_action_schema_and_calls(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The benchmark boundary must preserve candidate action schemas over HTTP.

    This is intentionally a deterministic in-process HTTP seam: it exercises
    RuntimeFixture, capture_catalog, create_toolset, and the official
    Streamable HTTP transport without changing the live runtime or benchmark
    corpus. The orchestrator still supplies the final proof against a hosted
    candidate runtime.
    """

    discover_schema = {
        "type": "object",
        "oneOf": [
            {
                "type": "object",
                "properties": {"action": {"const": "list_vaults"}},
                "required": ["action"],
                "additionalProperties": False,
            },
            {
                "type": "object",
                "properties": {
                    "action": {"const": "search"},
                    "query": {"type": "string", "minLength": 1},
                },
                "required": ["action", "query"],
                "additionalProperties": False,
            },
        ],
    }
    document_schema = {
        "type": "object",
        "oneOf": [
            {
                "type": "object",
                "properties": {
                    "action": {"const": "get"},
                    "uri": {"type": "string", "minLength": 1},
                },
                "required": ["action", "uri"],
                "additionalProperties": False,
            },
        ],
    }

    server = FastMCP("candidate-http", version="1")

    def discover(action: str, query: str | None = None) -> dict[str, Any]:
        if action == "list_vaults":
            return {"vaults": [{"name": "candidate-vault"}], "total": 1, "returned": 1}
        return {"results": [{"title": query, "uri": "akb://candidate-vault/doc/result.md"}]}

    def document_read(action: str, uri: str) -> dict[str, Any]:
        return {"action": action, "uri": uri, "content": "# candidate"}

    discover_tool = Tool.from_function(
        discover,
        name="akb_discover",
        annotations=ToolAnnotations(read_only_hint=True, idempotent_hint=True),
    )
    discover_tool.parameters = discover_schema
    server.add_tool(discover_tool)

    document_tool = Tool.from_function(
        document_read,
        name="akb_document_read",
        annotations=ToolAnnotations(read_only_hint=True, idempotent_hint=True),
    )
    document_tool.parameters = document_schema
    server.add_tool(document_tool)

    http_app = server.http_app(path="/mcp/", json_response=True, stateless_http=True)

    def make_client(spec: Any) -> Client[Any]:
        def httpx_client_factory(**kwargs: Any) -> httpx.AsyncClient:
            return httpx.AsyncClient(
                transport=httpx.ASGITransport(app=http_app),
                **kwargs,
            )

        transport = StreamableHttpTransport(
            f"{spec.app_origin.rstrip('/')}/mcp/",
            httpx_client_factory=cast(Any, httpx_client_factory),
        )
        return Client(transport, mode=PROTOCOL_REVISION, cache=False)

    monkeypatch.setattr(catalog_module, "create_client", make_client)
    descriptor = RuntimeDescriptor.from_dict(
        {
            "schema_version": 2,
            "status": "ready",
            "scenario": "candidate-http",
            "services": {
                "app": {
                    "origin": "http://candidate.test",
                    "health": {"method": "GET", "url": "/readyz"},
                    "discovery": {"method": "GET", "url": "/openapi.json"},
                },
                "fixture": {
                    "origin": "http://fixture.test",
                    "health": {"method": "GET", "url": "/health"},
                    "reset": {
                        "method": "POST",
                        "url": "/reset",
                        "body": {"scenario": "candidate-http"},
                    },
                    "discovery": {"method": "GET", "url": "/discover"},
                },
            },
            "credentials": {
                "username_env": "AKB_TEST_USERNAME",
                "password_env": "AKB_TEST_PASSWORD",  # pragma: allowlist secret
                "pat_env": "AKB_TEST_PAT",
            },
        }
    )
    fixture = RuntimeFixture(descriptor)

    try:
        async with http_app.router.lifespan_context(http_app):
            snapshot = await capture_catalog(
                fixture,
                transport="http",
                token="candidate-token",
                source_revision="a" * 40,
                artifact_version="candidate-test",
                capability_profile={"candidate": True},
            )
            assert [tool["name"] for tool in snapshot.tools] == [
                "akb_discover",
                "akb_document_read",
            ]
            schemas = {tool["name"]: tool["inputSchema"] for tool in snapshot.tools}
            assert schemas["akb_discover"] == discover_schema
            assert schemas["akb_document_read"] == document_schema
            assert "akb_list_vaults" not in schemas

            client = make_client(
                ConnectionSpec(
                    transport="http",
                    token="candidate-token",
                    app_origin="http://candidate.test",
                )
            )
            toolset = create_toolset(client)
            async with toolset:
                assert [tool.name for tool in await toolset.list_tools()] == [
                    "akb_discover",
                    "akb_document_read",
                ]
                assert await toolset.direct_call_tool(
                    "akb_discover", {"action": "list_vaults"}
                ) == {
                    "vaults": [{"name": "candidate-vault"}],
                    "total": 1,
                    "returned": 1,
                }
                assert await toolset.direct_call_tool(
                    "akb_discover", {"action": "search", "query": "needle"}
                ) == {
                    "results": [
                        {
                            "title": "needle",
                            "uri": "akb://candidate-vault/doc/result.md",
                        }
                    ]
                }
                assert await toolset.direct_call_tool(
                    "akb_document_read",
                    {"action": "get", "uri": "akb://candidate-vault/doc/result.md"},
                ) == {
                    "action": "get",
                    "uri": "akb://candidate-vault/doc/result.md",
                    "content": "# candidate",
                }
    finally:
        await fixture.close()


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
