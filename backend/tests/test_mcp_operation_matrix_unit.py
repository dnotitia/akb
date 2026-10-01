"""Guard the runnable live SDK matrix against operation/test drift."""

from __future__ import annotations

import ast
from pathlib import Path

from tests.mcp_e2e.catalog_contract import LIVE_OPERATION_CASES, LOGICAL_OPERATIONS


_E2E_DIR = Path(__file__).parent / "mcp_e2e"


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
