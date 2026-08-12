"""Single source of truth for error-response shape.

Background — until 0.5.5, error returns across the backend had ~6
distinct shapes: bare `{error}`, `{error, code}`, `{error, code,
pg_sqlstate}`, `{error, hint, available_*}`, `{error, message,
hint}`, etc. Every new handler that wanted to surface an auxiliary
hint reinvented a slightly different envelope. Agents that wanted to
auto-recover had to learn each case's field names.

0.5.6 collapses everything to one shape::

    {
      "error":   <human-readable message>,         # always
      "code":    <stable enum>,                    # always
      "hint":    <self-correction guidance>,       # optional
      "details": { ... case-specific metadata }    # optional
    }

The frontend never read auxiliary fields, the akb-mcp stdio proxy
just passes the envelope through, and the only e2e assertion on a
top-level meta field was `test_mcp_e2e`'s `available_columns` check
(now `details.available_columns`). Beyond that, callers that
inspected `pg_sqlstate`, `available_tables`, `available_arguments`,
or `message` need to look one level deeper.

Use ``err(...)`` everywhere. Don't hand-craft error dicts; the
flat-vs-nested rule has to be enforced in one place or it drifts
back to ~6 shapes within a quarter.
"""
from __future__ import annotations

from typing import Any

from app.exceptions import (
    ConflictError,
    ForbiddenError,
    InvalidColumnTypeError,
    NotFoundError,
    ValidationError,
    WriteBusyError,
)


# ── Stable error codes ────────────────────────────────────────
#
# Code strings are part of the contract — callers (agents,
# scripts) branch on them. Add to this list before using a new
# one; do not coin codes inline at the call site.

# Resource lookup
NOT_FOUND = "not_found"

# Authorization / permission
PERMISSION_DENIED = "permission_denied"
VAULT_ARCHIVED = "vault_archived"
# A `vault_write_policy`-marked vault rejected a write because the
# caller's token isn't on its `vault_write_grants` allowlist (P0 S3).
# Mirrors VAULT_ARCHIVED's specificity — the akb_sql dual-enforcement
# block in `table_service.execute_sql` returns this; the
# `access_service.check_vault_access` guard raises a plain
# `ForbiddenError` instead (like every other guard in that function),
# which maps to the generic PERMISSION_DENIED at the HTTP/MCP envelope.
VAULT_WRITE_MANAGED = "vault_write_managed"
# RFC 6750 §3.1 — OAuth scope check failed at MCP tool dispatch
# (caller is authenticated but the access token lacks the scope the
# tool requires). PERMISSION_DENIED is for AKB-internal access (vault
# role, public visibility); this is the OAuth-layer counterpart.
INSUFFICIENT_SCOPE = "insufficient_scope"

# Caller-side input
INVALID_ARGUMENT = "invalid_argument"      # generic argument shape problem
INVALID_COLUMN_TYPE = "invalid_column_type"
INVALID_URI = "invalid_uri"                # akb:// URI parse failure
INVALID_PATH = "invalid_path"              # collection / file path failure
UNKNOWN_ARGUMENT = "unknown_argument"      # arg key not in tool schema (0.5.4)
UNKNOWN_TOOL = "unknown_tool"
CONFLICT = "conflict"                      # version / expected-state mismatch
NATIVE_REVISION_SELECTOR_AMBIGUOUS = "native_revision_selector_ambiguous"
WRITE_BUSY = "write_busy"                  # write-lane admission timed out — retry after backoff
UNIQUE_VIOLATION = "unique_violation"      # PG 23505 — INSERT/UPDATE breaks a unique key
EDIT_FAILED = "edit_failed"                # akb_edit: old_string match / uniqueness failure

# SQL surface — `akb_sql`
MULTI_STATEMENT = "multi_statement"
METHOD_NOT_ALLOWED = "method_not_allowed"  # DDL via akb_sql, etc.
SQL_ERROR = "sql_error"                    # generic PG error after enrichment
UNDEFINED_COLUMN = "undefined_column"
UNDEFINED_TABLE = "undefined_table"
UNFILTERED_MUTATION = "unfiltered_mutation"
BULK_TOO_LARGE = "bulk_too_large"
NO_UNIQUE_CONSTRAINT = "no_unique_constraint"

# Row-read / row-write REST surface — PostgREST-style URL/AST compiler
# (`table_row_query`). These sit alongside the generic INVALID_ARGUMENT
# for shape errors, but carry a more specific code so SDK/codegen
# clients can branch on the exact compiler rejection.
INVALID_FILTER = "invalid_filter"          # malformed filter operand / op.value grammar
INVALID_OPERATOR = "invalid_operator"      # unknown or unsupported row-read operator
INVALID_CAST = "invalid_cast"              # unsupported / malformed JSON-path cast
FILTER_TOO_DEEP = "filter_too_deep"        # boolean filter nesting past MAX_BOOL_DEPTH
NOT_IMPLEMENTED = "not_implemented"        # recognized-but-unsupported request shape

# Knowledge-graph linking
SELF_LINK = "self_link"

# Server-side — anything the caller can't fix. Reach for this only
# when the failure is genuinely "our problem", not a caller-side
# argument shape or lookup issue. Backed by the MCP dispatch's
# last-resort catch (unhandled exceptions land here) and by handlers
# that explicitly want to surface a 5xx-class failure (e.g. upstream
# storage write failure in akb_publication_snapshot).
INTERNAL = "internal"

# Manual-vault Git history resolution. These are deliberately catalogue
# entries rather than values copied from an exception at dispatch time: the
# MCP wire contract must remain statically auditable by the catalogue test.
GIT_HISTORY_FAILED = "git_history_failed"
GIT_HISTORY_TIMEOUT = "git_history_timeout"
GIT_HISTORY_OUTPUT_CAPPED = "git_history_output_capped"
GIT_HISTORY_ENTRY_CAPPED = "git_history_entry_capped"


# ── Builder ───────────────────────────────────────────────────


def err(
    message: str,
    code: str,
    *,
    hint: str | None = None,
    **details: Any,
) -> dict:
    """Build the canonical error response envelope.

    >>> err("Vault is archived", code=VAULT_ARCHIVED)
    {'error': 'Vault is archived', 'code': 'vault_archived'}

    >>> err("Unknown argument 'user'", code=UNKNOWN_ARGUMENT,
    ...     hint="Did you mean: author?",
    ...     available_arguments=['author', 'vault'])
    {'error': "Unknown argument 'user'", 'code': 'unknown_argument',
     'hint': 'Did you mean: author?',
     'details': {'available_arguments': ['author', 'vault']}}
    """
    out: dict[str, Any] = {"error": message, "code": code}
    if hint is not None:
        out["hint"] = hint
    if details:
        out["details"] = details
    return out


def exception_envelope(e: Exception) -> dict:
    """Map a bubbled-up exception to the canonical ``err()`` envelope.

    Access guards (``check_vault_access``) raise ``AKBError`` subclasses
    *outside* the per-handler try/except blocks, so a permission denial
    (``ForbiddenError``) or a missing vault (``NotFoundError``) would otherwise
    fall through the MCP dispatch's generic ``code=internal`` catch-all — which
    reads as a 500 to clients rather than the 4xx it is. Known ``AKBError``
    subclasses get their canonical, stable code; anything else is a genuine
    internal error. Lives here (no import-time side effects) so it stays unit-
    testable without importing the MCP server. See dnotitia/akb#221.
    """
    if isinstance(e, ForbiddenError):
        return err(str(e), code=PERMISSION_DENIED)
    if isinstance(e, NotFoundError):
        return err(str(e), code=NOT_FOUND)
    if isinstance(e, ConflictError):
        if e.code == NATIVE_REVISION_SELECTOR_AMBIGUOUS:
            return err(str(e), code=NATIVE_REVISION_SELECTOR_AMBIGUOUS)
        return err(str(e), code=CONFLICT)
    if isinstance(e, InvalidColumnTypeError):
        return err(
            str(e),
            code=INVALID_COLUMN_TYPE,
            hint=e.hint,
            **(e.details or {}),
        )
    if isinstance(e, WriteBusyError):
        return err(
            str(e), code=WRITE_BUSY,
            hint="The vault is under heavy write load. Wait a few seconds and retry; no partial write occurred.",
            retry_after_secs=e.retry_after_secs,
        )
    if getattr(e, "code", None) == GIT_HISTORY_FAILED:
        return err(str(e), code=GIT_HISTORY_FAILED)
    if getattr(e, "code", None) == GIT_HISTORY_TIMEOUT:
        return err(str(e), code=GIT_HISTORY_TIMEOUT)
    if getattr(e, "code", None) == GIT_HISTORY_OUTPUT_CAPPED:
        return err(str(e), code=GIT_HISTORY_OUTPUT_CAPPED)
    if getattr(e, "code", None) == GIT_HISTORY_ENTRY_CAPPED:
        return err(str(e), code=GIT_HISTORY_ENTRY_CAPPED)
    if isinstance(e, ValidationError):
        return err(str(e), code=INVALID_ARGUMENT)
    return err(str(e), code=INTERNAL)
