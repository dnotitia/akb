"""Unit tests for vault-table identifier sanitisation.

Covers the single sanitiser used by ``pg_table_name`` (DDL),
``pg_short_name`` (what ``akb_browse`` advertises as ``sql_name``),
and ``build_table_name_map`` (what the rewriter uses). These three
*must* agree, or non-ASCII / hyphenated table names become
unreachable via ``akb_sql`` (issues #110 + #111).

The tests here are dependency-light — no DB, no pool. They exist
specifically to lock down the sanitiser shape so a future regex tweak
can't silently re-introduce the all-underscore drop-out.
"""
from __future__ import annotations

from app.repositories.table_data_repo import (
    pg_short_name,
    pg_table_name,
    rewrite_table_names,
)


# ── pg_short_name + pg_table_name agree on every case ──────────


def test_short_name_is_the_right_half_of_pg_table_name():
    """Whatever `pg_short_name` produces must match the part after
    `vt_<vault>__` in `pg_table_name` — otherwise the `sql_name`
    surfaced via `akb_browse` won't match what the rewriter expects."""
    cases = [
        ("vault", "pipeline"),                # plain ascii
        ("vault", "pipeline-snapshots"),      # hyphen
        ("vault", "공공사업기획"),             # all non-ascii → all-underscore
        ("vault", "table.with.dots"),         # non-letter punctuation
        ("vault", "MixedCASE"),               # case-fold
        ("vault", "한글-mixed-2026"),          # ascii + non-ascii + digits
    ]
    for vault, name in cases:
        full = pg_table_name(vault, name)
        assert full.startswith(f"vt_{vault}__"), full
        assert full == f"vt_{vault}__{pg_short_name(name)}", (
            f"pg_table_name and pg_short_name disagree for {name!r}: "
            f"{full} vs vt_{vault}__{pg_short_name(name)}"
        )


def test_short_name_idempotent():
    """Sanitising the output a second time is a no-op — protects
    callers that round-trip the value through display→short→display."""
    for raw in ["pipeline", "pipeline-snapshots", "공공사업기획", "한글-mixed-2026"]:
        once = pg_short_name(raw)
        twice = pg_short_name(once)
        assert once == twice, f"{raw!r} → {once!r} → {twice!r}"


def test_short_name_korean_collapses_to_underscores():
    """Regression guard for issue #111. The whole-byte sanitiser must
    keep collapsing non-ASCII to underscores; if someone "improves" it
    to preserve Korean, ``akb_sql`` breaks because the tokenizer only
    accepts ``[A-Za-z_][A-Za-z0-9_]*``."""
    assert pg_short_name("공공사업기획") == "______"


# ── rewriter resolves the sanitised short name ─────────────────


def test_rewriter_resolves_all_underscore_identifier():
    """Issue #111: the rewriter previously skipped all-underscore
    tokens. Build a map keyed by the same sanitised form
    ``build_table_name_map`` now emits and confirm the rewriter
    qualifies it."""
    table_map = {"______": "vt_demo__________"}
    rewritten = rewrite_table_names(
        "SELECT * FROM ______ LIMIT 1", table_map,
    )
    assert "vt_demo__________" in rewritten
    assert "______" not in rewritten.replace("vt_demo__________", "")


def test_rewriter_leaves_quoted_identifier_alone():
    """The all-underscore fix must not also start rewriting quoted
    forms — those are PG case-sensitive references and the user has
    explicitly opted out of the bareword rewriter."""
    table_map = {"______": "vt_demo__________"}
    sql = 'SELECT * FROM "______" LIMIT 1'
    assert rewrite_table_names(sql, table_map) == sql
