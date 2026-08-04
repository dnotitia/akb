"""Shared AST helpers for the tests that scan production sources.

Two guards enumerate call sites across the tree and compare them against an
allowlist keyed by (file, function) — `test_document_delete_chokepoint_unit`
for `DELETE FROM documents`, `test_external_git_cascade_unit` for
`vault_external_git` creation. They need the same two primitives, so they
live here rather than being maintained twice.

`test_` is deliberately absent from the module name: pytest would otherwise
collect it, and it holds no tests. Same convention as `extgit_http.py`.
"""

from __future__ import annotations

import ast
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]


def python_files(roots: tuple[str, ...]) -> list[Path]:
    """Every `.py` under each root, sorted so a failure names sites in a
    stable order. Asserts the scan found something — a mistyped root would
    otherwise make the guard pass by looking at nothing."""
    files: list[Path] = []
    for root in roots:
        files.extend(sorted((BACKEND / root).rglob("*.py")))
    assert files, f"no production sources found under {roots} — the scan root is wrong"
    return files


def enclosing_function(tree: ast.AST, lineno: int) -> str:
    """The innermost def containing `lineno` — the unit an allowlist entry
    should name, so a second site in an already-blessed file cannot ride in
    on the entry that blessed the first. `<module>` when at module level."""
    best, best_line = "<module>", -1
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            end = node.end_lineno or node.lineno
            if node.lineno <= lineno <= end and node.lineno > best_line:
                best, best_line = node.name, node.lineno
    return best
