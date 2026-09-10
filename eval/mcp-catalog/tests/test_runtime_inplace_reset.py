from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).parents[3]
SOURCE = ROOT / "scripts" / "ci" / "e2e_runtime.py"


def test_runtime_reset_is_in_place_and_checks_dependency_identity() -> None:
    tree = ast.parse(SOURCE.read_text(encoding="utf-8"))
    reset = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, (ast.AsyncFunctionDef, ast.FunctionDef)) and node.name == "reset_scenario"
    )
    calls = [
        node
        for node in ast.walk(reset)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id == "self"
    ]
    called = {
        node.attr
        for node in ast.walk(reset)
        if isinstance(node, ast.Attribute)
        and isinstance(node.value, ast.Name)
        and node.value.id == "self"
    }
    compose_calls = [node for node in calls if node.func.attr == "_compose"]

    assert not any(
        isinstance(argument, ast.Constant) and argument.value == "down"
        for node in compose_calls
        for argument in node.args
    )
    assert {"_dependency_identity_snapshot", "_reset_postgres_in_place", "_clear_minio_objects"} <= called
