"""Unit tests for the literal-aware multi-statement guard (issue #180).

``table_service.execute_sql`` used to enforce its single-statement
boundary with a literal-blind ``";" in sql_check`` test, so a semicolon
inside a string literal (``VALUES ('Fix bug; refactor')``) was rejected
as multi-statement. The guard now reuses the rewriter's tokenizer via
``count_statement_separators``: a ``;`` only counts as a statement
separator outside string literals, quoted identifiers, comments, and
dollar-quoted blocks.

Two layers are pinned here:

  1. ``count_statement_separators`` itself — a pure function next to
     ``rewrite_table_names``, dependency-light (no DB, no pool).
  2. The wiring in ``execute_sql`` — verified by AST instead of
     importing ``table_service`` (which transitively pulls chunking /
     executor machinery), the same dependency-avoidance pattern as
     ``test_mcp_tool_validation_unit.py``.
"""

from __future__ import annotations

import ast
from pathlib import Path

from app.repositories.table_data_repo import count_statement_separators


# ── 1. separator counting is literal-aware ──────────────────────


def test_literal_semicolon_in_string_is_not_a_separator():
    """The motivating case: real-world text with a `;` must be storable
    losslessly through a single INSERT (issue #180)."""
    sql = "INSERT INTO source_github_issues (title) VALUES ('Fix bug; refactor')"
    assert count_statement_separators(sql) == 0


def test_genuine_multi_statement_still_detected():
    assert count_statement_separators("SELECT 1; SELECT 2") == 1
    assert count_statement_separators("SELECT 1; SELECT 2; SELECT 3") == 2
    # Mixed: one literal `;`, one real separator — still multi-statement.
    assert count_statement_separators("SELECT 'a;b'; SELECT 2") == 1


def test_semicolon_inside_dollar_quotes_is_not_a_separator():
    assert count_statement_separators("SELECT $$a; b$$") == 0
    assert count_statement_separators("SELECT $tag$one; two; three$tag$") == 0


def test_semicolon_inside_comments_is_not_a_separator():
    assert count_statement_separators("SELECT 1 -- trailing; note") == 0
    assert count_statement_separators("SELECT /* a; b; c */ 1") == 0


def test_semicolon_inside_quoted_identifier_is_not_a_separator():
    assert count_statement_separators('SELECT "weird;col" FROM pipeline') == 0


def test_escaped_quote_inside_string_does_not_desync_the_scan():
    """PG escapes a quote as '' inside a literal; the `;` after it is
    still inside the string."""
    assert count_statement_separators("SELECT 'it''s; fine'") == 0


def test_trailing_separator_is_still_counted_by_the_pure_helper():
    """Tolerance for a trailing `;` is execute_sql's policy (it
    `rstrip(";")`s before counting — pinned by AST below), not the
    helper's: the helper reports every separator it sees."""
    assert count_statement_separators("SELECT 1;") == 1
    assert count_statement_separators("SELECT 1;".rstrip(";").strip()) == 0


# ── 2. execute_sql wiring (AST; no heavy imports) ───────────────

_TABLE_SERVICE = (
    Path(__file__).resolve().parents[1] / "app" / "services" / "table_service.py"
)


def _execute_sql_fn() -> ast.AsyncFunctionDef:
    tree = ast.parse(_TABLE_SERVICE.read_text())
    for node in ast.walk(tree):
        if (
            isinstance(node, (ast.AsyncFunctionDef, ast.FunctionDef))
            and node.name == "execute_sql"
        ):
            return node
    raise AssertionError("execute_sql not found in table_service.py")


def _called_names(fn: ast.AST) -> set[str]:
    names: set[str] = set()
    for node in ast.walk(fn):
        if isinstance(node, ast.Call):
            f = node.func
            if isinstance(f, ast.Name):
                names.add(f.id)
            elif isinstance(f, ast.Attribute):
                names.add(f.attr)
    return names


def test_execute_sql_uses_the_literal_aware_guard():
    fn = _execute_sql_fn()
    assert "count_statement_separators" in _called_names(fn), (
        "execute_sql must route the multi-statement check through "
        "count_statement_separators"
    )


def test_execute_sql_dropped_the_literal_blind_membership_test():
    """The old guard was `";" in sql_check` — a Compare(In) against the
    constant ";". It must be gone, or literal semicolons are rejected
    again."""
    fn = _execute_sql_fn()
    for node in ast.walk(fn):
        if isinstance(node, ast.Compare) and any(
            isinstance(op, ast.In) for op in node.ops
        ):
            left = node.left
            assert not (
                isinstance(left, ast.Constant) and left.value == ";"
            ), 'execute_sql still contains the literal-blind `";" in ...` guard'


def test_execute_sql_keeps_trailing_semicolon_tolerance():
    """`SELECT 1;` must keep working: the guard strips trailing
    semicolons (`rstrip(";")`) before counting separators."""
    fn = _execute_sql_fn()
    for node in ast.walk(fn):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "rstrip"
            and node.args
            and isinstance(node.args[0], ast.Constant)
            and node.args[0].value == ";"
        ):
            return
    raise AssertionError(
        'execute_sql no longer rstrip(";")s before the separator count — '
        "trailing-semicolon tolerance lost"
    )
