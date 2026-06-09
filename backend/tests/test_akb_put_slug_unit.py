"""Unit tests for the optional ``slug`` argument on ``akb_put`` (MCP).

The backend ``DocumentPutRequest`` model and ``document_service._put`` already
honor a ``slug`` (``slug = (req.slug and _slugify(req.slug)) or _slugify(req.title)``);
these tests pin the two MCP-surface seams that expose it to a caller:

  1. ``tools.py`` advertises ``slug`` in the ``akb_put`` ``inputSchema``. Because
     ``server._TOOL_ARG_NAMES`` is schema-derived, advertising it here is also
     what makes the ``_dispatch`` arg-validator accept it (otherwise the call is
     rejected with ``unknown_argument``).
  2. ``_handle_put`` forwards ``args.get("slug")`` into ``DocumentPutRequest``.

Both are verified by AST instead of importing ``mcp_server.tools`` /
``mcp_server.server`` — the same dependency-avoidance pattern as
``test_mcp_tool_validation_unit.py`` (importing the server transitively pulls
psycopg / kiwipiepy / the MCP SDK).
"""

from __future__ import annotations

import ast
from pathlib import Path

_MCP = Path(__file__).resolve().parents[1] / "mcp_server"


def _tool_call(tree: ast.AST, tool_name: str) -> ast.Call:
    """Return the ``Tool(name="<tool_name>", ...)`` call node from tools.py."""
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        is_tool = (isinstance(func, ast.Name) and func.id == "Tool") or (
            isinstance(func, ast.Attribute) and func.attr == "Tool"
        )
        if not is_tool:
            continue
        for kw in node.keywords:
            if (
                kw.arg == "name"
                and isinstance(kw.value, ast.Constant)
                and kw.value.value == tool_name
            ):
                return node
    raise AssertionError(f"Tool(name={tool_name!r}) not found in tools.py")


def _kwarg_value(call: ast.Call, name: str) -> ast.expr:
    for kw in call.keywords:
        if kw.arg == name:
            return kw.value
    raise AssertionError(f"keyword {name!r} not found on call")


def _dict_value(node: ast.expr, key: str) -> ast.expr:
    assert isinstance(node, ast.Dict), f"expected a dict literal for {key!r}"
    for k, v in zip(node.keys, node.values):
        if isinstance(k, ast.Constant) and k.value == key:
            return v
    raise AssertionError(f"key {key!r} not found in dict literal")


def _akb_put_schema_property_names() -> set[str]:
    tree = ast.parse((_MCP / "tools.py").read_text())
    call = _tool_call(tree, "akb_put")
    schema = _kwarg_value(call, "inputSchema")
    props = _dict_value(schema, "properties")
    assert isinstance(props, ast.Dict)
    return {k.value for k in props.keys if isinstance(k, ast.Constant)}


def _handle_put_put_request_call() -> ast.Call:
    tree = ast.parse((_MCP / "server.py").read_text())
    fn = next(
        n
        for n in ast.walk(tree)
        if isinstance(n, (ast.AsyncFunctionDef, ast.FunctionDef)) and n.name == "_handle_put"
    )
    for node in ast.walk(fn):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "DocumentPutRequest":
            return node
    raise AssertionError("DocumentPutRequest(...) not found in _handle_put")


def test_akb_put_schema_exposes_slug() -> None:
    # Positive control: the schema is the one we think it is.
    names = _akb_put_schema_property_names()
    assert "title" in names and "content" in names
    assert "slug" in names, (
        "akb_put inputSchema must advertise `slug` so a caller can set it AND so "
        "the schema-derived arg-validator (_TOOL_ARG_NAMES) accepts it."
    )


def test_handle_put_forwards_slug_from_args() -> None:
    call = _handle_put_put_request_call()
    slug_kw = next((kw for kw in call.keywords if kw.arg == "slug"), None)
    assert slug_kw is not None, "_handle_put must forward `slug` into DocumentPutRequest"
    # Forwarded from the call args, not hardcoded: args.get("slug").
    value = slug_kw.value
    assert (
        isinstance(value, ast.Call)
        and isinstance(value.func, ast.Attribute)
        and value.func.attr == "get"
        and isinstance(value.func.value, ast.Name)
        and value.func.value.id == "args"
        and value.args
        and isinstance(value.args[0], ast.Constant)
        and value.args[0].value == "slug"
    ), "slug must be forwarded as args.get(\"slug\")"
