"""Unit tests for the error-envelope builder.

Two concerns covered here:

  1. Envelope shape — minimal / with hint / with details kwargs / `code`
     always present.

  2. Catalogue enforcement — every `err(..., code=X)` call site in the
     backend uses a string constant defined in ``app/util/errors.py``,
     not an ad-hoc literal. Without this, 0.5.6's "one shape" promise
     drifts back to ~6 shapes within a quarter (the same way the
     original error sprawl happened).

The catalogue check uses AST grep so it stays dependency-light and
doesn't trigger import of the full handler chain. Same pattern as
``test_mcp_tool_validation_unit``'s TOOLS↔_HANDLERS sync.
"""
from __future__ import annotations

import ast
from pathlib import Path

from app.util import errors as errors_mod
from app.util.errors import (
    err,
    NOT_FOUND,
    PERMISSION_DENIED,
    UNKNOWN_ARGUMENT,
    VAULT_ARCHIVED,
)


def test_minimal_envelope_has_error_and_code_only():
    out = err("Vault is archived", code=VAULT_ARCHIVED)
    assert out == {"error": "Vault is archived", "code": "vault_archived"}
    assert "hint" not in out
    assert "details" not in out


def test_hint_is_optional_and_top_level():
    out = err("Doc not found", code=NOT_FOUND, hint="Try `akb_browse` first")
    assert out["hint"] == "Try `akb_browse` first"
    assert "details" not in out


def test_details_collects_kwargs_under_details_key():
    out = err(
        "Unknown argument 'user'",
        code=UNKNOWN_ARGUMENT,
        hint="Did you mean: author?",
        available_arguments=["author", "collection", "vault"],
    )
    assert out["details"] == {
        "available_arguments": ["author", "collection", "vault"],
    }
    # Top-level stays clean — no leakage of details keys
    assert "available_arguments" not in out


def test_multiple_details_kwargs_all_land_under_details():
    out = err(
        "permission denied for table foo",
        code=PERMISSION_DENIED,
        pg_sqlstate="42501",
        attempted_action="SELECT",
    )
    assert out["details"] == {
        "pg_sqlstate": "42501",
        "attempted_action": "SELECT",
    }


def test_code_field_is_always_set():
    """Regression guard for the original drift problem — never let a
    bare-error dict slip through without a `code`."""
    out = err("oops", code="something")
    assert "code" in out
    assert out["code"] == "something"


# ── Catalogue enforcement ─────────────────────────────────────


_BACKEND_ROOT = Path(__file__).resolve().parents[1]
_SCAN_ROOTS = [
    _BACKEND_ROOT / "app",
    _BACKEND_ROOT / "mcp_server",
]


def _catalogue_constants() -> dict[str, str]:
    """{NAME: value} for every UPPER_SNAKE str constant in errors.py."""
    return {
        name: value
        for name, value in vars(errors_mod).items()
        if name.isupper() and isinstance(value, str)
    }


def _collect_err_code_args() -> list[tuple[Path, int, ast.AST]]:
    """Find every `err(..., code=X)` call. Returns (file, line, code_arg_node)."""
    findings: list[tuple[Path, int, ast.AST]] = []
    for root in _SCAN_ROOTS:
        for py in root.rglob("*.py"):
            try:
                tree = ast.parse(py.read_text())
            except SyntaxError:
                continue
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                func = node.func
                if not (isinstance(func, ast.Name) and func.id == "err"):
                    continue
                for kw in node.keywords:
                    if kw.arg == "code":
                        findings.append((py, node.lineno, kw.value))
    return findings


def test_every_err_call_uses_catalogue_constant():
    """Every `code=` argument to `err()` must be either:
      - an `ast.Name` whose id is a catalogue constant (preferred), or
      - an `ast.Constant` whose string value is in the catalogue values
        (allowed but discouraged — flagged in the error message).
    Ad-hoc strings unknown to the catalogue fail the test — that's the
    drift this gate is meant to catch.
    """
    catalogue = _catalogue_constants()
    catalogue_names = set(catalogue.keys())
    catalogue_values = set(catalogue.values())

    offenders: list[str] = []
    for py, lineno, code_node in _collect_err_code_args():
        rel = py.relative_to(_BACKEND_ROOT)
        if isinstance(code_node, ast.Name):
            if code_node.id not in catalogue_names:
                offenders.append(
                    f"{rel}:{lineno}: code={code_node.id!s} — name not in catalogue"
                )
        elif isinstance(code_node, ast.Constant) and isinstance(code_node.value, str):
            if code_node.value not in catalogue_values:
                offenders.append(
                    f"{rel}:{lineno}: code={code_node.value!r} — literal not in catalogue"
                )
        else:
            offenders.append(
                f"{rel}:{lineno}: non-constant `code=` expression"
                f" ({ast.dump(code_node)}); use a catalogue constant instead"
            )

    assert not offenders, (
        "err() calls using codes outside app/util/errors.py catalogue:\n  "
        + "\n  ".join(offenders)
    )


def test_catalogue_has_no_orphan_constants():
    """Every catalogue constant should be imported by at least one
    caller — orphan constants are a YAGNI smell that already bit us
    (`CROSS_VAULT_LINK`, `INTERNAL_ERROR` were carried for one release
    without any call site). Drop them or use them; don't carry forward
    declarations indefinitely.
    """
    catalogue_names = set(_catalogue_constants().keys())

    referenced: set[str] = set()
    for root in _SCAN_ROOTS:
        for py in root.rglob("*.py"):
            if py.name == "errors.py":
                continue
            src = py.read_text()
            for name in catalogue_names:
                if name in src:
                    referenced.add(name)

    orphans = catalogue_names - referenced
    assert not orphans, (
        f"Catalogue constants defined but never imported: {sorted(orphans)}. "
        "Drop them or wire up the call site."
    )
