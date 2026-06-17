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
    PG_IDENT_MAX_LEN,
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


# ── rewriter skips PG reserved keywords (issue #180-family, #182) ──
#
# The table map is vault-wide, so a single table named like a SQL
# keyword used to clobber that keyword in EVERY statement in the vault
# (`INSERT ... VALUES (...)` → `INSERT ... vt_<vault>__values (...)`).
# Because the rewriter is tokenizer-based rather than grammar-based, every
# PostgreSQL keyword is unsafe as a bare table alias. Keyword-shaped tables
# remain reachable through their vault-prefixed alias.


def test_rewriter_keyword_decoy_does_not_break_values():
    """Issue #182's motivating case: a vault containing a table named
    ``values`` must not break unrelated INSERTs."""
    table_map = {
        "source_github_issues": "vt_demo__source_github_issues",
        "values": "vt_demo__values",
        "demo__values": "vt_demo__values",
    }
    rewritten = rewrite_table_names(
        "INSERT INTO source_github_issues (title) VALUES ('x')", table_map,
    )
    assert rewritten == (
        "INSERT INTO vt_demo__source_github_issues (title) VALUES ('x')"
    )


def test_rewriter_keyword_decoys_leave_select_order_group_alone():
    """Same hazard for the other common keyword-shaped names; the skip
    is case-insensitive like the rest of the rewriter."""
    table_map = {
        "pipeline": "vt_demo__pipeline",
        "select": "vt_demo__select",
        "order": "vt_demo__order",
        "group": "vt_demo__group",
    }
    rewritten = rewrite_table_names(
        "SELECT id FROM pipeline GROUP BY id ORDER BY id", table_map,
    )
    assert rewritten == (
        "SELECT id FROM vt_demo__pipeline GROUP BY id ORDER BY id"
    )


def test_rewriter_keyword_decoys_leave_between_exists_over_alone():
    """Expression/window keywords are not reserved table-name keywords, but
    rewriting them still corrupts valid SQL when the vault also has tables
    named ``between``, ``exists``, or ``over``."""
    table_map = {
        "scores": "vt_demo__scores",
        "between": "vt_demo__between",
        "exists": "vt_demo__exists",
        "over": "vt_demo__over",
    }
    rewritten = rewrite_table_names(
        "SELECT avg(score) OVER () FROM scores "
        "WHERE score BETWEEN 1 AND 10 "
        "AND EXISTS (SELECT 1)",
        table_map,
    )
    assert rewritten == (
        "SELECT avg(score) OVER () FROM vt_demo__scores "
        "WHERE score BETWEEN 1 AND 10 "
        "AND EXISTS (SELECT 1)"
    )


def test_rewriter_keyword_named_table_is_unreachable_via_bare_ident():
    """DOCUMENTED TRADE-OFF: a table literally named ``values`` can no
    longer be referenced as a bare ``FROM values`` — the rewriter leaves
    the keyword alone and PG rejects it with a syntax error (VALUES is
    fully reserved). That is the acceptable cost: before this fix the
    same map entry silently corrupted every OTHER statement in the
    vault, so keyword-named tables were already breaking the vault. The
    supported route remains the vault-prefixed alias (next test), which
    is never a keyword."""
    table_map = {"values": "vt_demo__values"}
    sql = "SELECT * FROM values"
    assert rewrite_table_names(sql, table_map) == sql


def test_rewriter_prefixed_alias_still_reaches_keyword_named_tables():
    """The ``<vault>__<table>`` alias is not a keyword, so a
    keyword-named table stays reachable through it."""
    table_map = {
        "demo__between": "vt_demo__between",
        "demo__exists": "vt_demo__exists",
        "demo__over": "vt_demo__over",
        "demo__values": "vt_demo__values",
    }
    rewritten = rewrite_table_names(
        "SELECT * FROM demo__values "
        "JOIN demo__between ON true "
        "JOIN demo__exists ON true "
        "JOIN demo__over ON true",
        table_map,
    )
    assert rewritten == (
        "SELECT * FROM vt_demo__values "
        "JOIN vt_demo__between ON true "
        "JOIN vt_demo__exists ON true "
        "JOIN vt_demo__over ON true"
    )


def test_rewriter_non_reserved_pg_keyword_named_table_needs_prefixed_alias():
    """Even a non-reserved PG keyword is context-sensitive SQL syntax, so
    the bare form is left alone and the prefixed alias is the supported
    route."""
    table_map = {
        "comment": "vt_demo__comment",
        "demo__comment": "vt_demo__comment",
    }
    rewritten = rewrite_table_names("SELECT * FROM comment", table_map)
    assert rewritten == "SELECT * FROM comment"

    rewritten = rewrite_table_names("SELECT * FROM demo__comment", table_map)
    assert rewritten == "SELECT * FROM vt_demo__comment"


# ── identifier length boundary (E08) ───────────────────────────


def test_pg_ident_max_len_is_namedatalen_minus_one():
    """PG's default NAMEDATALEN is 64; usable identifier length is 63.
    Lock the constant so a refactor can't silently change the bound
    `table_service` and `role_sync` both rely on."""
    assert PG_IDENT_MAX_LEN == 63


def test_pg_table_name_length_boundary():
    """E08 regression: a long vault + long table name can push
    `vt_<vault>__<table>` exactly one byte over the limit, where PG
    would silently truncate (risking a GRANT collision). Pin the math
    that `create_table`'s pre-check guards against."""
    fits = pg_table_name("a" * 27, "b" * 31)  # 3 + 27 + 2 + 31 = 63
    over = pg_table_name("a" * 27, "b" * 32)  # 3 + 27 + 2 + 32 = 64
    assert len(fits) == PG_IDENT_MAX_LEN
    assert len(over) == PG_IDENT_MAX_LEN + 1
