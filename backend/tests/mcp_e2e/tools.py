"""Shared helpers for live MCP scenarios."""

from __future__ import annotations

import json
from typing import Any
from uuid import uuid4

import pytest
from mcp import Client
from mcp import types as mcp_types

from .runtime import RuntimeContext, redact_error


SCENARIO = "mcp_product_e2e"


def _new_vault_name(prefix: str) -> str:
    return f"mcp-sdk-{prefix}-{uuid4().hex[:10]}"


def _text_content(result: mcp_types.CallToolResult, operation: str) -> str:
    for item in result.content:
        if isinstance(item, mcp_types.TextContent):
            return item.text
    pytest.fail(f"scenario={SCENARIO} operation={operation}: tool returned no public JSON text")


async def _call_json(
    client: Client,
    runtime_session: RuntimeContext,
    name: str,
    arguments: dict[str, Any],
    *,
    expect_error: bool = False,
) -> dict[str, Any]:
    operation = f"tools/call {name}"
    try:
        result = await client.call_tool(name, arguments)
    except Exception as exc:
        pytest.fail(f"scenario={SCENARIO} operation={operation}: {redact_error(exc, runtime_session.secrets)}")

    try:
        public = json.loads(_text_content(result, operation))
    except (TypeError, ValueError) as exc:
        pytest.fail(f"scenario={SCENARIO} operation={operation}: tool returned invalid public JSON: {exc}")
    if not isinstance(public, dict):
        pytest.fail(f"scenario={SCENARIO} operation={operation}: public result is not an object")

    # The HTTP MCP backend returns product failures as a JSON error envelope;
    # the SDK's is_error flag is not guaranteed to mirror that application
    # payload. Reject protocol-level failures on successful calls.
    if expect_error:
        if "error" not in public:
            state = "SDK error" if result.is_error else "success"
            pytest.fail(
                f"scenario={SCENARIO} operation={operation}: expected an error envelope, got {state}"
            )
    elif result.is_error:
        pytest.fail(f"scenario={SCENARIO} operation={operation}: tool returned an SDK error")
    return public


async def _create_vault(
    client: Client,
    runtime_session: RuntimeContext,
    prefix: str,
    *,
    description: str = "MCP SDK behavior test",
) -> str:
    name = _new_vault_name(prefix)
    result = await _call_json(
        client,
        runtime_session,
        "akb_create_vault",
        {"name": name, "description": description},
    )
    assert result.get("name") == name
    assert isinstance(result.get("vault_id"), str)
    return name
