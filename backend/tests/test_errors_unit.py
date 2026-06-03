"""Unit tests for the error-envelope builder."""
from __future__ import annotations

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
