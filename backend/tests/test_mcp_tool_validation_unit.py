"""Unit tests for MCP tool-call validation surface.

Two concerns covered here:

  1. ``fuzzy_hint`` (``app.util.text``) — shared between SQL column /
     table not-exist enrichment and ``_dispatch`` unknown-arg
     rejection. Verify the "Did you mean…?" and fallback shapes so the
     tone stays uniform across both callers.

  2. ``TOOLS`` ↔ ``_HANDLERS`` sync — every advertised tool must have
     a registered handler and vice versa. Without this test, a tool
     can drift (declared but unhandled, or registered but absent from
     ``tools/list``) and the agent would only learn at call time.

The handler list is extracted via AST grep instead of importing
``mcp_server.server`` (which transitively imports psycopg / kiwipiepy /
fs setup). Same dependency-avoidance pattern as ``test_mcp_init_unit``.
"""
from __future__ import annotations

import ast
from pathlib import Path

from app.util.text import fuzzy_hint
from mcp_server.tools import OPERATIONS, TOOLS, TOOL_GROUPS


# ── fuzzy_hint ────────────────────────────────────────────────


def test_fuzzy_hint_close_match():
    out = fuzzy_hint("athor", ["author", "vault", "limit"], label="arguments")
    assert out == "Did you mean: author?"


def test_fuzzy_hint_no_match_falls_back_to_list():
    out = fuzzy_hint("xyz", ["alpha", "beta", "gamma"], label="arguments")
    assert out.startswith("Available arguments: ")
    assert "alpha" in out and "gamma" in out


def test_fuzzy_hint_truncates_long_candidate_list():
    candidates = [f"cand{i}" for i in range(30)]
    out = fuzzy_hint("zzz_no_match", candidates, label="tables")
    assert " …" in out


# ── TOOLS ↔ _HANDLERS sync ────────────────────────────────────


def _extract_registered_handlers() -> set[str]:
    """Pick the name string out of every ``@_h("X")`` in server.py."""
    server_py = Path(__file__).resolve().parents[1] / "mcp_server" / "server.py"
    tree = ast.parse(server_py.read_text())
    names: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if not (isinstance(func, ast.Name) and func.id == "_h"):
            continue
        if not node.args:
            continue
        first = node.args[0]
        if isinstance(first, ast.Constant) and isinstance(first.value, str):
            names.add(first.value)
    return names


def test_tools_and_handlers_are_in_sync():
    declared = {t.name for t in OPERATIONS}
    registered = _extract_registered_handlers()

    missing_handler = declared - registered
    missing_tool = registered - declared

    assert not missing_handler, (
        f"Operations declared in OPERATIONS but no @_h handler in server.py: "
        f"{sorted(missing_handler)}"
    )
    assert not missing_tool, (
        f"@_h handlers in server.py but not in OPERATIONS: "
        f"{sorted(missing_tool)}"
    )


def test_flat_catalog_separates_read_grep_and_replacement():
    tools = {tool.name: tool for tool in OPERATIONS}
    assert len(tools) == 45
    assert "akb_grep_replace" in tools

    read_grep = tools["akb_grep"].input_schema["properties"]
    write_grep = tools["akb_grep_replace"].input_schema["properties"]
    assert "replace" not in read_grep
    assert "max_replacements" not in read_grep
    assert {"pattern", "replace", "vault"} <= write_grep.keys()
    assert "vault" in tools["akb_grep_replace"].input_schema["required"]


def test_candidate_catalog_groups_seventeen_read_operations_into_five_tools():
    operations = {tool.name for tool in OPERATIONS}
    catalog = {tool.name: tool for tool in TOOLS}
    grouped = {
        operation
        for actions in TOOL_GROUPS.values()
        for operation in actions.values()
    }

    assert len(operations) == 45
    assert len(grouped) == 17
    assert len(catalog) == 33
    assert set(TOOL_GROUPS) <= catalog.keys()
    assert grouped.isdisjoint(catalog)
    assert set(catalog) == (operations - grouped) | set(TOOL_GROUPS)

    for name, actions in TOOL_GROUPS.items():
        branches = catalog[name].input_schema["oneOf"]
        assert [branch["properties"]["action"]["const"] for branch in branches] == list(actions)
        for branch, operation in zip(branches, actions.values(), strict=True):
            assert branch["additionalProperties"] is False
            source = next(tool for tool in OPERATIONS if tool.name == operation)
            assert branch["required"] == [
                "action",
                *source.input_schema.get("required", []),
            ]
            assert set(branch["properties"]) == {
                "action",
                *source.input_schema["properties"],
            }
            for field, definition in source.input_schema["properties"].items():
                assert branch["properties"][field] == definition


def test_relation_tool_descriptions_match_the_vault_boundary():
    tools = {tool.name: tool for tool in OPERATIONS}

    for tool_name in ("akb_put", "akb_update"):
        properties = tools[tool_name].input_schema["properties"]
        for field in ("depends_on", "related_to"):
            description = properties[field]["description"].lower()
            assert "same-vault" in description
            assert "ordinary markdown link" in description
            assert "cross-vault" in description

    assert "same-vault knowledge graph" in tools["akb_graph"].description.lower()
    assert "visible same-vault relations" in tools["akb_provenance"].description.lower()

    relations = tools["akb_relations"].description.lower()
    assert "explicit" in relations and "implicit" in relations
    unlink = tools["akb_unlink"].description.lower()
    assert "explicit" in unlink and "implicit" in unlink
