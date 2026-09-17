"""Collector compatibility for ``akb_update`` metadata fields.

``type`` and ``domain`` already belong to ``DocumentUpdateRequest``.  MCP must
advertise and forward them directly so the collector can retain its canonical
empty string/list clears without a separate generic metadata surface.
"""

from __future__ import annotations

import ast
from pathlib import Path


_MCP = Path(__file__).resolve().parents[1] / "mcp_server"


def _tool_call(tree: ast.AST, tool_name: str) -> ast.Call:
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        if not isinstance(node.func, ast.Name) or node.func.id != "Tool":
            continue
        if any(
            keyword.arg == "name"
            and isinstance(keyword.value, ast.Constant)
            and keyword.value.value == tool_name
            for keyword in node.keywords
        ):
            return node
    raise AssertionError(f"Tool(name={tool_name!r}) not found")


def _dict_value(node: ast.expr, key: str) -> ast.expr:
    assert isinstance(node, ast.Dict), f"expected a dict literal for {key!r}"
    for item_key, item_value in zip(node.keys, node.values):
        if isinstance(item_key, ast.Constant) and item_key.value == key:
            return item_value
    raise AssertionError(f"key {key!r} not found")


def _update_schema_properties() -> dict[str, ast.expr]:
    tree = ast.parse((_MCP / "tools.py").read_text())
    tool = _tool_call(tree, "akb_update")
    schema = next(keyword.value for keyword in tool.keywords if keyword.arg == "input_schema")
    properties = _dict_value(schema, "properties")
    assert isinstance(properties, ast.Dict)
    return {
        key.value: value
        for key, value in zip(properties.keys, properties.values)
        if isinstance(key, ast.Constant) and isinstance(key.value, str)
    }


def _update_request_call() -> ast.Call:
    tree = ast.parse((_MCP / "server.py").read_text())
    handler = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "_handle_update"
    )
    for node in ast.walk(handler):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "DocumentUpdateRequest":
            return node
    raise AssertionError("DocumentUpdateRequest(...) not found in _handle_update")


def _is_args_get(value: ast.expr, field: str) -> bool:
    return (
        isinstance(value, ast.Call)
        and isinstance(value.func, ast.Attribute)
        and value.func.attr == "get"
        and isinstance(value.func.value, ast.Name)
        and value.func.value.id == "args"
        and len(value.args) == 1
        and isinstance(value.args[0], ast.Constant)
        and value.args[0].value == field
    )


def test_akb_update_exposes_collector_metadata_without_a_generic_metadata_argument() -> None:
    properties = _update_schema_properties()

    for field in ("type", "domain"):
        assert field in properties, f"akb_update must advertise collector field {field!r}"
        value = properties[field]
        assert isinstance(value, ast.Dict)
        type_value = _dict_value(value, "type")
        assert isinstance(type_value, ast.Constant) and type_value.value == "string"
        assert "minLength" not in {
            key.value for key in value.keys if isinstance(key, ast.Constant)
        }, f"{field} must continue to accept the collector's empty-string clear"

    assert "metadata" not in properties


def test_akb_update_forwards_collector_metadata_and_existing_empty_list_field_directly() -> None:
    request = _update_request_call()
    forwarded = {keyword.arg: keyword.value for keyword in request.keywords if keyword.arg is not None}

    for field in ("type", "domain", "tags"):
        assert field in forwarded, f"_handle_update must pass {field!r} to DocumentUpdateRequest"
        assert _is_args_get(forwarded[field], field), (
            f"_handle_update must forward {field!r} directly from args so empty "
            "collector clears are preserved"
        )
