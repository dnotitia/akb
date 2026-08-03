"""Closure guard: agent-facing prose may only name tools that actually exist.

Two hand-authored surfaces are shipped to every MCP client but are never
checked against the tool registry:

  1. ``mcp_server/instructions.py`` — ``INSTRUCTIONS`` is sent verbatim at
     ``initialize`` (``server.py``), so a stale tool name there is advertised
     to every connected agent.
  2. ``mcp_server/help.py`` — the ``HELP`` topic bodies, in particular the
     root "Tool Categories" table, which maps a drill-down topic to the tools
     it covers.

Neither is generated from ``TOOLS``, so a tool removal that misses them leaves
the server claiming a capability it does not have. That is exactly how
``akb_todo`` / ``akb_todos`` / ``akb_todo_update`` survived their removal in
PR #43 (`1c57350`) — the tools were deleted, the prose was not, and the
``todos`` help topic the table pointed at never existed at all.

These tests close the loop: every ``akb_*`` name and every drill-down topic
that appears in the prose must resolve.

Everything is read by AST rather than by importing ``mcp_server.tools`` /
``mcp_server.help`` — importing the server transitively pulls psycopg /
kiwipiepy / the MCP SDK. Same dependency-avoidance pattern as
``test_akb_put_slug_unit.py`` and ``test_mcp_tool_validation_unit.py``.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

_MCP = Path(__file__).resolve().parents[1] / "mcp_server"
# Plugin skills are shipped to users through .claude-plugin/marketplace.json,
# so a ghost tool name in one of them is advertised outside this repo. They
# name tools both bare (`akb_put`) and namespaced (`mcp__akb__akb_put`, in
# `allowed-tools`).
_PLUGINS = Path(__file__).resolve().parents[2] / "plugins"

# Tools the backend deliberately does NOT define: the Node stdio proxy owns
# local filesystem access, so it synthesises these three itself and they never
# appear in the backend `TOOLS` list (see packages/akb-mcp-client/lib/proxy.mjs
# and the boundary rule in AGENTS.md). Prose is allowed to name them.
_PROXY_ONLY_TOOLS = frozenset({"akb_put_file", "akb_get_file", "akb_delete_file"})

# The trailing \b and the digit class matter: `\bakb_[a-z_]+` would match the
# `akb_search` PREFIX of a ghost named `akb_search2` and wave it through as a
# known tool.
_AKB_NAME = re.compile(r"\bakb_[a-z_][a-z0-9_]*\b")
_PLUGIN_AKB_NAME = re.compile(r"\b(?:mcp__akb__)?(akb_[a-z_][a-z0-9_]*)\b")

# Plugin surfaces that reach a user. Frontmatter is YAML and manifests are
# JSON/TOML, so a tool name can appear in any of these — scanning only
# Markdown would leave the declaration side unguarded.
_PLUGIN_SUFFIXES = ("*.md", "*.json", "*.yaml", "*.yml", "*.toml")


def _module_tree(name: str) -> ast.AST:
    return ast.parse((_MCP / name).read_text())


def _tool_names() -> set[str]:
    """Names from every ``Tool(name="…", …)`` literal in tools.py."""
    names: set[str] = set()
    for node in ast.walk(_module_tree("tools.py")):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        is_tool = (isinstance(func, ast.Name) and func.id == "Tool") or (
            isinstance(func, ast.Attribute) and func.attr == "Tool"
        )
        if not is_tool:
            continue
        for kw in node.keywords:
            if kw.arg == "name" and isinstance(kw.value, ast.Constant):
                names.add(kw.value.value)
    assert names, "no Tool(name=…) literals found in tools.py"
    return names


def _known_tools() -> set[str]:
    return _tool_names() | set(_PROXY_ONLY_TOOLS)


def _help_topics() -> dict[str | None, str]:
    """The ``HELP`` dict literal as {topic: body}."""
    for node in ast.walk(_module_tree("help.py")):
        if not isinstance(node, ast.Assign):
            continue
        if not any(isinstance(t, ast.Name) and t.id == "HELP" for t in node.targets):
            continue
        assert isinstance(node.value, ast.Dict), "HELP must be a dict literal"
        out: dict[str | None, str] = {}
        for key, value in zip(node.value.keys, node.value.values):
            if not (isinstance(key, ast.Constant) and isinstance(value, ast.Constant)):
                continue
            if isinstance(value.value, str):
                out[key.value] = value.value
        return out
    raise AssertionError("HELP assignment not found in help.py")


def _instructions_text() -> str:
    for node in ast.walk(_module_tree("instructions.py")):
        if not isinstance(node, ast.Assign):
            continue
        if not any(isinstance(t, ast.Name) and t.id == "INSTRUCTIONS" for t in node.targets):
            continue
        assert isinstance(node.value, ast.Constant) and isinstance(node.value.value, str)
        return node.value.value
    raise AssertionError("INSTRUCTIONS assignment not found in instructions.py")


def _table_rows(body: str, header_contains: str | None = None) -> list[list[str]]:
    """Cells of every markdown table row in ``body``.

    With ``header_contains``, only rows of the table whose header line contains
    that substring are returned (rows run until the first non-``|`` line).
    """
    rows: list[list[str]] = []
    in_target = header_contains is None
    for line in body.splitlines():
        stripped = line.strip()
        if not stripped.startswith("|"):
            if header_contains is not None:
                in_target = False
            continue
        if header_contains is not None and header_contains in stripped:
            in_target = True
            continue
        if not in_target:
            continue
        cells = [c.strip() for c in stripped.strip("|").split("|")]
        if all(set(c) <= set("-: ") for c in cells):  # separator row
            continue
        rows.append(cells)
    return rows


def _cell_token(cell: str) -> str | None:
    """The bare identifier in a table cell, ignoring markdown emphasis.

    Accepts `topic`, **topic**, and plain topic — a ghost row written without
    backticks must not slip past the guard just because it broke convention.
    Returns None for anything that is not a single identifier-shaped token
    (header labels, prose, separators).
    """
    stripped = cell.strip().strip("`*_ ")
    return stripped if re.fullmatch(r"[a-z][a-z0-9-]*", stripped) else None


def _tool_token(cell_entry: str) -> str | None:
    """A tool name from the Tools column, ignoring backticks and call parens.

    ``put``, ```put```, and ``put()`` all name the same tool; only ``—``,
    ``...`` and prose should be skipped.
    """
    stripped = cell_entry.strip().strip("`").removesuffix("()").strip()
    return stripped if re.fullmatch(r"[a-z_]+", stripped) else None


def test_prose_never_names_a_nonexistent_akb_tool() -> None:
    """Every ``akb_*`` token in INSTRUCTIONS + HELP resolves to a real tool."""
    known = _known_tools()
    sources: list[tuple[str, str]] = [("instructions.INSTRUCTIONS", _instructions_text())]
    sources += [(f"help[{k!r}]", v) for k, v in _help_topics().items()]

    ghosts: dict[str, list[str]] = {}
    for label, text in sources:
        for name in sorted(set(_AKB_NAME.findall(text))):
            if name not in known:
                ghosts.setdefault(name, []).append(label)

    assert not ghosts, (
        "agent-facing prose advertises tools that do not exist in TOOLS "
        f"(or the proxy-only set {sorted(_PROXY_ONLY_TOOLS)}): "
        + "; ".join(f"{name} in {locs}" for name, locs in sorted(ghosts.items()))
    )


def test_help_root_table_topics_are_real_help_topics() -> None:
    """Every drill-down topic the root help table points at exists as a key.

    A row here is an instruction to the agent to call
    ``akb_help(topic="<first cell>")``; if the key is absent that call errors.
    """
    topics = _help_topics()
    root = topics[None]
    known = set(topics)

    rows = _table_rows(root)
    assert rows, "no table rows found in the root help topic — parser is broken"

    missing = [
        t
        for row in rows
        if row and (t := _cell_token(row[0])) is not None
        if t not in known
    ]
    assert not missing, (
        "root help table offers drill-down topics that have no HELP entry — "
        f"akb_help(topic=...) on these fails: {sorted(missing)}"
    )


def test_help_root_table_tools_all_exist() -> None:
    """The Tools column of the root "Tool Categories" table lists real tools.

    Entries are written without the ``akb_`` prefix (``put, get, update``), so
    they are invisible to the ``akb_*`` scan above and need their own check.
    Non-tool cell content (``—``, ``...``) is skipped.
    """
    known = _known_tools()
    rows = _table_rows(_help_topics()[None], header_contains="| Tools |")
    # Without this the test silently passes if the header is ever reworded
    # (e.g. "| Available tools |") and the parser matches nothing.
    assert rows, (
        "the root 'Tool Categories' table was not found — its header no longer "
        "contains '| Tools |', so this guard was checking nothing. Update "
        "header_contains to match."
    )

    ghosts: dict[str, str] = {}
    for row in rows:
        if len(row) < 2:
            continue
        topic = _cell_token(row[0]) or row[0]
        for entry in row[1].split(","):
            token = _tool_token(entry)
            if token is None:
                continue  # "—", "...", prose
            if f"akb_{token}" not in known:
                ghosts[f"akb_{token}"] = topic

    assert not ghosts, (
        "root help 'Tool Categories' table lists tools that do not exist: "
        + ", ".join(f"{name} (row {topic!r})" for name, topic in sorted(ghosts.items()))
    )


def test_shipped_plugin_skills_never_name_a_nonexistent_akb_tool() -> None:
    """Distributed plugin skills may only reference tools that exist.

    Covers both the `allowed-tools` frontmatter (``mcp__akb__akb_*``) and the
    body prose. A removed tool left in `allowed-tools` is granted-but-absent;
    left in the body it is an instruction the agent cannot carry out. The
    session-ingest skill carried all three ``akb_todo*`` names in both places
    for a year after the tools were deleted.
    """
    if not _PLUGINS.is_dir():
        pytest.skip("plugins/ not present in this checkout")

    known = _known_tools()
    ghosts: dict[str, set[str]] = {}
    files = [p for ext in _PLUGIN_SUFFIXES for p in _PLUGINS.rglob(ext)]
    assert files, "no plugin skill files discovered — test is mis-scoped"

    for path in files:
        for name in set(_PLUGIN_AKB_NAME.findall(path.read_text())):
            if name not in known:
                ghosts.setdefault(name, set()).add(str(path.relative_to(_PLUGINS)))

    assert not ghosts, (
        "shipped plugin skills reference tools that do not exist: "
        + "; ".join(f"{n} in {sorted(f)}" for n, f in sorted(ghosts.items()))
    )
