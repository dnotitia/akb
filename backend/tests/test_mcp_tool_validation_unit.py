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
from mcp_server.tools import TOOLS


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
    declared = {t.name for t in TOOLS}
    registered = _extract_registered_handlers()

    missing_handler = declared - registered
    missing_tool = registered - declared

    assert not missing_handler, (
        f"Tools declared in TOOLS but no @_h handler in server.py: "
        f"{sorted(missing_handler)}"
    )
    assert not missing_tool, (
        f"@_h handlers in server.py but not in TOOLS list: "
        f"{sorted(missing_tool)}"
    )


def test_relation_tool_descriptions_match_the_vault_boundary():
    tools = {tool.name: tool for tool in TOOLS}

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


def test_document_reads_share_one_public_tool():
    tools = {tool.name: tool for tool in TOOLS}
    assert "akb_drill_down" not in tools
    assert len(tools) == 43
    schema = tools["akb_get"].input_schema["properties"]
    assert schema["mode"]["enum"] == ["full", "sections", "outline"]
    assert schema["mode"]["default"] == "full"
    assert {"section", "pattern", "version"} <= schema.keys()


async def test_document_read_modes_and_invalid_arguments(monkeypatch, tmp_path):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    from app.config import settings

    monkeypatch.setattr(settings, "git_storage_path", str(tmp_path))
    from mcp_server import server

    monkeypatch.setattr(server, "check_vault_access", AsyncMock())
    monkeypatch.setattr(server.doc_service, "get", AsyncMock(return_value=SimpleNamespace(model_dump=lambda: {"content": "whole", "current_commit": "abc"})))
    monkeypatch.setattr(server.search_service, "drill_down", AsyncMock(return_value=[{"section_path": "# 한글", "content": "selected"}]))
    monkeypatch.setattr(server.search_service, "list_section_headings", AsyncMock(return_value=["# 한글"]))
    user = server._MCPUser(user_id="u")
    uri = "akb://v/doc/a.md"
    full = await server._dispatch("akb_get", {"uri": uri}, user)
    assert full["content"] == "whole"
    sections = await server._dispatch("akb_get", {"uri": uri, "mode": "sections", "section": "한글"}, user)
    assert sections["sections"][0]["content"] == "selected"
    outline = await server._dispatch("akb_get", {"uri": uri, "mode": "outline"}, user)
    assert outline["outline"] == ["# 한글"]
    for bad in ({"mode": None}, {"mode": "unknown"}, {"section": "한글"}, {"mode": "outline", "pattern": "x"}, {"mode": "sections", "version": "abcdef0"}, {"mode": "sections", "section": None}, {"version": 4}, {"version": ""}):
        result = await server._dispatch("akb_get", {"uri": uri, **bad}, user)
        assert "error" in result, bad
