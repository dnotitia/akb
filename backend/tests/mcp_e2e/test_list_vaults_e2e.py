"""The first live MCP behavior scenario."""

from __future__ import annotations

import json
from collections.abc import Mapping

import pytest
from mcp import Client
from mcp import types as mcp_types
from mcp_types.version import HANDSHAKE_PROTOCOL_VERSIONS, MODERN_PROTOCOL_VERSIONS

from .runtime import RuntimeContext, redact_error


SCENARIO = "akb_list_vaults"
SUPPORTED_PROTOCOLS = set(HANDSHAKE_PROTOCOL_VERSIONS) | set(MODERN_PROTOCOL_VERSIONS)


def _fail(operation: str, detail: str) -> None:
    pytest.fail(f"scenario={SCENARIO} operation={operation}: {detail}")


async def test_akb_list_vaults_mcp_e2e(
    mcp_client: Client,
    runtime_session: RuntimeContext,
) -> None:
    if mcp_client.protocol_version not in SUPPORTED_PROTOCOLS:
        _fail("connect", f"unsupported negotiated protocol {mcp_client.protocol_version!r}")

    server_info = mcp_client.server_info
    if server_info is None or server_info.name != "akb" or not isinstance(server_info.version, str):
        _fail("connect", "connected server is not the expected AKB server")

    try:
        tools = await mcp_client.list_tools(cache_mode="bypass")
    except Exception as exc:
        _fail("tools/list", redact_error(exc, runtime_session.secrets))
    list_vaults = next((tool for tool in tools.tools if tool.name == "akb_list_vaults"), None)
    if list_vaults is None or not isinstance(list_vaults.input_schema, Mapping):
        _fail("tools/list", "akb_list_vaults is missing from the typed tool catalog")

    try:
        result = await mcp_client.call_tool("akb_list_vaults", {})
    except Exception as exc:
        _fail("tools/call akb_list_vaults", redact_error(exc, runtime_session.secrets))
    if result.is_error is not False:
        _fail("tools/call akb_list_vaults", "tool returned an error")
    if not result.content or not isinstance(result.content[0], mcp_types.TextContent):
        _fail("tools/call akb_list_vaults", "tool returned no public JSON text")

    try:
        public = json.loads(result.content[0].text)
    except (TypeError, ValueError):
        _fail("tools/call akb_list_vaults", "tool returned invalid public JSON")
    if not isinstance(public, Mapping):
        _fail("tools/call akb_list_vaults", "public result is not an object")

    vaults = public.get("vaults")
    total = public.get("total")
    returned = public.get("returned")
    if not isinstance(vaults, list):
        _fail("tools/call akb_list_vaults", "vaults is not an array")
    if type(total) is not int or type(returned) is not int:
        _fail("tools/call akb_list_vaults", "total and returned must be integers")
    if returned != len(vaults):
        _fail("tools/call akb_list_vaults", "returned does not match vaults length")
    if total < returned:
        _fail("tools/call akb_list_vaults", "total is smaller than returned")
