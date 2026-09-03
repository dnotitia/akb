"""The first product behavior scenario: authenticated list-vaults."""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

import pytest

from .driver import DriverResult, McpClientDriver

SCENARIO = "akb_list_vaults"
PROTOCOL_VERSION = "2026-07-28"


def _payload(driver: McpClientDriver, operation: str, result: DriverResult) -> dict[str, Any]:
    if not result.passed:
        detail = result.error or "operation failed"
        pytest.fail(f"scenario={SCENARIO} transport={driver.transport} operation={operation}: {detail}")
    assert result.output is not None
    payload = result.output.get("result")
    if not isinstance(payload, dict):
        pytest.fail(f"scenario={SCENARIO} transport={driver.transport} operation={operation}: missing result object")
    return payload


def test_akb_list_vaults_mcp_e2e(mcp_driver: McpClientDriver) -> None:
    initialize = _payload(mcp_driver, "initialize", mcp_driver.initialize())
    if initialize.get("protocolVersion") != PROTOCOL_VERSION:
        pytest.fail(f"scenario={SCENARIO} transport={mcp_driver.transport} operation=initialize: unsupported protocol")
    server_info = initialize.get("serverInfo")
    if not isinstance(server_info, Mapping):
        metadata = initialize.get("_meta")
        server_info = metadata.get("io.modelcontextprotocol/serverInfo") if isinstance(metadata, Mapping) else None
    if (
        not isinstance(server_info, Mapping)
        or server_info.get("name") != "akb"
        or not isinstance(server_info.get("version"), str)
    ):
        pytest.fail(f"scenario={SCENARIO} transport={mcp_driver.transport} operation=initialize: unexpected server")

    tools = _payload(mcp_driver, "tools/list", mcp_driver.list_tools())
    catalog = tools.get("tools")
    if not isinstance(catalog, list) or not any(
        isinstance(tool, Mapping)
        and tool.get("name") == "akb_list_vaults"
        and isinstance(tool.get("inputSchema"), Mapping)
        for tool in catalog
    ):
        pytest.fail(
            f"scenario={SCENARIO} transport={mcp_driver.transport} operation=tools/list: akb_list_vaults is missing"
        )

    call = _payload(mcp_driver, "tools/call", mcp_driver.call_tool("akb_list_vaults", {}))
    if call.get("isError") is not False:
        pytest.fail(
            f"scenario={SCENARIO} transport={mcp_driver.transport} operation=tools/call: tool returned an error"
        )
    content = call.get("content")
    text = content[0].get("text") if isinstance(content, list) and content and isinstance(content[0], Mapping) else None
    if not isinstance(text, str):
        pytest.fail(f"scenario={SCENARIO} transport={mcp_driver.transport} operation=tools/call: missing public JSON")
    try:
        public = json.loads(text)
    except TypeError, ValueError:
        pytest.fail(f"scenario={SCENARIO} transport={mcp_driver.transport} operation=tools/call: invalid public JSON")
    if not isinstance(public, Mapping):
        pytest.fail(
            f"scenario={SCENARIO} transport={mcp_driver.transport} operation=tools/call: result is not an object"
        )
    vaults = public.get("vaults")
    total = public.get("total")
    returned = public.get("returned")
    if (
        not isinstance(vaults, list)
        or type(total) is not int
        or type(returned) is not int
        or returned != len(vaults)
        or total < returned
    ):
        pytest.fail(
            f"scenario={SCENARIO} transport={mcp_driver.transport} operation=tools/call: invalid list-vaults result"
        )
