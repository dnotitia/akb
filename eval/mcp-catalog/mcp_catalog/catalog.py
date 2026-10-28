"""Official FastMCP transports and unfiltered server catalog capture."""

from __future__ import annotations

import dataclasses
from dataclasses import dataclass
from typing import Any, cast

from fastmcp import Client
from fastmcp.client.transports.http import StreamableHttpTransport
from fastmcp.client.transports.stdio import StdioTransport
from mcp.types import DiscoverResult
from pydantic import BaseModel
from pydantic_ai.mcp import MCPToolset

from .contracts import CatalogSnapshot, PROTOCOL_REVISION, hash_json, token_estimate
from .runtime import RuntimeContractError, RuntimeFixture


@dataclass(frozen=True, slots=True)
class ConnectionSpec:
    transport: str
    token: str
    app_origin: str
    command: str | None = None
    args: tuple[str, ...] = ()
    environment: dict[str, str] | None = None


def _create_transport(spec: ConnectionSpec) -> Any:
    if spec.transport == "http":
        return StreamableHttpTransport(
            f"{spec.app_origin.rstrip('/')}/mcp/",
            headers={"Authorization": f"Bearer {spec.token}"},
        )
    if spec.transport == "stdio":
        if spec.command is None or spec.environment is None:
            raise RuntimeContractError("stdio connection is missing process coordinates")
        return StdioTransport(spec.command, list(spec.args), env=spec.environment)
    raise RuntimeContractError(f"unsupported benchmark transport: {spec.transport}")


async def discover_server(spec: ConnectionSpec) -> DiscoverResult:
    """Read real modern server metadata through the official FastMCP session."""

    probe = Client(
        cast(Any, _create_transport(spec)),
        mode=PROTOCOL_REVISION,
        auto_initialize=False,
        cache=False,
    )
    async with probe:
        raw_result = await probe.session.send_discover(PROTOCOL_REVISION)
    try:
        discovery = DiscoverResult.model_validate(raw_result)
    except ValueError as exc:
        raise RuntimeContractError(f"server/discover returned invalid metadata: {exc}") from exc
    if PROTOCOL_REVISION not in discovery.supported_versions:
        raise RuntimeContractError("server/discover did not confirm the pinned protocol revision")
    if discovery.result_type != "complete" or not isinstance(discovery.instructions, str) or not discovery.instructions.strip():
        raise RuntimeContractError("server/discover did not provide complete metadata and instructions")
    return discovery


def create_client(spec: ConnectionSpec, prior_discover: DiscoverResult) -> Client[Any]:
    """Create a modern FastMCP client pinned to the server's real discover result."""

    # Explicitly pin the modern MCP mode.  `auto` would make a protocol
    # negotiation choice that is not part of the benchmark comparison. The
    # discover result came from the server and is adopted by this client.
    return Client(
        cast(Any, _create_transport(spec)),
        mode=PROTOCOL_REVISION,
        prior_discover=prior_discover,
        cache=False,
    )


def create_toolset(client: Client[Any], recorder: Any = None) -> MCPToolset:
    """Build the only toolset given to the agent, with no filtering or retries."""

    return MCPToolset(
        client,
        max_retries=0,
        tool_error_behavior="failed",
        process_tool_call=recorder,
        prefer_tasks=False,
        cache_tools=False,
        cache_resources=False,
        cache_prompts=False,
        include_instructions=True,
    )


async def capture_catalog(
    fixture: RuntimeFixture,
    *,
    transport: str,
    token: str,
    source_revision: str,
    artifact_version: str,
    capability_profile: dict[str, Any],
) -> CatalogSnapshot:
    """Capture the complete server `tools/list` result through MCPToolset."""

    if transport == "stdio":
        command, args, environment = fixture.stdio_command(token)
        spec = ConnectionSpec(
            transport=transport,
            token=token,
            app_origin=fixture.descriptor.app_origin,
            command=command,
            args=tuple(args),
            environment=environment,
        )
    else:
        spec = ConnectionSpec(transport=transport, token=token, app_origin=fixture.descriptor.app_origin)
    discovery = await discover_server(spec)
    client = create_client(spec, discovery)
    toolset = create_toolset(client)
    async with toolset:
        tools = await toolset.list_tools()
    serialized = [_jsonable(tool) for tool in tools]
    if not all(isinstance(tool, dict) for tool in serialized):
        raise RuntimeContractError("MCP tools/list returned a non-object tool definition")
    tool_dicts = [tool for tool in serialized if isinstance(tool, dict)]
    return CatalogSnapshot(
        transport=transport,  # type: ignore[arg-type]
        source_revision=source_revision,
        artifact_version=artifact_version,
        capability_profile=capability_profile,
        tool_count=len(tool_dicts),
        catalog_hash=hash_json(tool_dicts),
        catalog_token_estimate=token_estimate(tool_dicts),
        server_discover=discovery.model_dump(mode="json", by_alias=True, exclude_none=False),
        tools=tool_dicts,
    )


def _jsonable(value: Any) -> Any:
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json", by_alias=True, exclude_none=False)
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return {field.name: _jsonable(getattr(value, field.name)) for field in dataclasses.fields(value)}
    if isinstance(value, dict):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)


def input_schemas_from_catalog(snapshot: CatalogSnapshot) -> dict[str, dict[str, Any]]:
    """Project the exact raw ``tools/list`` input schemas used by the scorer."""

    schemas: dict[str, dict[str, Any]] = {}
    for tool in snapshot.tools:
        name = tool.get("name")
        schema = tool.get("inputSchema")
        if isinstance(name, str) and isinstance(schema, dict):
            schemas[name] = schema
    if len(schemas) != snapshot.tool_count:
        raise RuntimeContractError("captured tools/list is missing an input schema")
    return schemas
