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
