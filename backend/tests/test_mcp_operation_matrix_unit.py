"""Guard the runnable live SDK matrix against operation/test drift."""

from __future__ import annotations

import ast
from pathlib import Path
from types import SimpleNamespace

from tests.mcp_e2e.catalog_contract import (
    GROUP_ACTIONS,
    LIVE_OPERATION_CASES,
    LOGICAL_OPERATIONS,
    call_target,
)


_E2E_DIR = Path(__file__).parent / "mcp_e2e"


def test_flat_catalog_keeps_standalone_read_call_target() -> None:
    flat_tools = [SimpleNamespace(name=name) for name in LOGICAL_OPERATIONS]
    arguments = {"uri": "akb://probe/doc/a.md"}

    assert call_target(flat_tools, "akb_get", arguments) == ("akb_get", arguments)


def test_mixed_catalog_maps_read_call_target_to_group_action() -> None:
    grouped_operations = {
        operation
        for actions in GROUP_ACTIONS.values()
        for operation in actions.values()
    }
    mixed_names = (set(LOGICAL_OPERATIONS) - grouped_operations) | set(GROUP_ACTIONS)
    mixed_tools = [SimpleNamespace(name=name) for name in mixed_names]
    arguments = {"uri": "akb://probe/doc/a.md"}

    assert call_target(mixed_tools, "akb_get", arguments) == (
        "akb_document_read",
        {"action": "get", **arguments},
    )


def _function(tree: ast.Module, name: str) -> ast.FunctionDef | ast.AsyncFunctionDef:
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            return node
    raise AssertionError(f"live operation case {name} is missing")


def test_every_http_operation_has_a_live_sdk_case_on_both_transports() -> None:
    assert set(LIVE_OPERATION_CASES) == set(LOGICAL_OPERATIONS)
    assert len(LIVE_OPERATION_CASES) == 45

    for operation, (filename, test_name, call_literal) in LIVE_OPERATION_CASES.items():
        path = _E2E_DIR / filename
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        test = _function(tree, test_name)
        parameters = {arg.arg for arg in test.args.args}
        assert "mcp_client" in parameters, f"{operation} is not called through the Python MCP SDK"
        string_literals = {
            node.value for node in ast.walk(test) if isinstance(node, ast.Constant) and isinstance(node.value, str)
        }
        assert any(call_literal in literal for literal in string_literals), (
            f"{operation} call is missing from {filename}:{test_name}"
        )

    conftest = ast.parse(
        (_E2E_DIR / "conftest.py").read_text(encoding="utf-8"),
        filename="tests/mcp_e2e/conftest.py",
    )
    transport = _function(conftest, "mcp_transport")
    transport_params = next(
        keyword.value
        for decorator in transport.decorator_list
        if isinstance(decorator, ast.Call)
        and isinstance(decorator.func, ast.Attribute)
        and decorator.func.attr == "fixture"
        for keyword in decorator.keywords
        if keyword.arg == "params"
    )
    assert ast.literal_eval(transport_params) == ("http", "stdio")
