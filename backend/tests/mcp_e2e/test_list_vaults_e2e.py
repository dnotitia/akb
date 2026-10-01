"""The first live MCP behavior scenario."""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import NoReturn

import pytest
from mcp import Client
from mcp import types as mcp_types
from mcp_types.version import HANDSHAKE_PROTOCOL_VERSIONS, MODERN_PROTOCOL_VERSIONS

from .catalog_contract import assert_catalog_contract, call_target
from .runtime import RuntimeContext, redact_error


SCENARIO = "akb_list_vaults"
SUPPORTED_PROTOCOLS = set(HANDSHAKE_PROTOCOL_VERSIONS) | set(MODERN_PROTOCOL_VERSIONS)


def _fail(operation: str, detail: str) -> NoReturn:
    pytest.fail(f"scenario={SCENARIO} operation={operation}: {detail}")


def _assert_connection(
    protocol_version: str,
    server_info: mcp_types.Implementation | None,
    transport: str,
) -> None:
    if protocol_version not in SUPPORTED_PROTOCOLS:
        _fail("connect", f"unsupported negotiated protocol {protocol_version!r}")
    expected_name = {"http": "akb", "stdio": "akb-mcp"}.get(transport)
    if expected_name is None:
        _fail("connect", f"unsupported transport {transport!r}")
    if (
        server_info is None
        or server_info.name != expected_name
        or not isinstance(server_info.version, str)
    ):
        _fail("connect", f"connected server is not the expected {expected_name!r} server")


def _list_vaults_payload(result: mcp_types.CallToolResult) -> Mapping[str, object]:
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
    return public


def _assert_list_vaults_shape(public: Mapping[str, object]) -> None:
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


async def test_akb_list_vaults_mcp_e2e(
    mcp_client: Client,
    mcp_transport: str,
    runtime_session: RuntimeContext,
) -> None:
    _assert_connection(mcp_client.protocol_version, mcp_client.server_info, mcp_transport)

    try:
        tools = await mcp_client.list_tools(cache_mode="bypass")
    except Exception as exc:
        _fail("tools/list", redact_error(exc, runtime_session.secrets))
    assert_catalog_contract(tools.tools, transport=mcp_transport)

    try:
        name, arguments = call_target(tools.tools, "akb_list_vaults", {})
        result = await mcp_client.call_tool(name, arguments)
    except Exception as exc:
        _fail("tools/call akb_list_vaults", redact_error(exc, runtime_session.secrets))
    _assert_list_vaults_shape(_list_vaults_payload(result))
