"""`pg_table_name` fits a vault+table pair into one PG identifier.

Property tests rather than golden strings: a golden string pins the
scheme, and what has to hold is the four things below. The one golden
assertion that IS worth having is that a pair which already fits is
byte-identical, because that is the whole reason this change needs no
migration — every table that exists was created under the old rule, so
it fits by construction.

The service-level behaviour that used to refuse an over-long pair lives
in ``test_table_name_length_unit.py``.
"""
from __future__ import annotations

from app.repositories import table_data_repo
from app.services import role_sync

_LIMIT = table_data_repo.PG_IDENT_MAX_LEN




def test_a_pair_that_fits_is_untouched():
    """Every table that exists was created under the old rule, so it fits
    by construction. If any of these changed, this would be a migration."""
    for vault, table in [
        ("manager", "teams"),
        ("manager", "team_resource_assignments"),
        ("internal-s3-source-c9c29c38490ff905", "s3_akb_collector_dev"),
        ("gdn-state", "slack_distill_runs"),
        ("a", "b"),
    ]:
        composed = f"vt_{vault.lower().replace('-', '_')}__{table.lower().replace('-', '_')}"
        assert len(composed) <= _LIMIT, "fixture is not a fitting pair"
        assert table_data_repo.pg_table_name(vault, table) == composed


def test_an_overlong_pair_fits_and_keeps_the_grammar():
    """`role_sync` interpolates this name into raw SQL behind a grammar
    that needs both sides and the separator. Truncating the composed
    string blindly would cut before the `__` for a long enough vault."""
    for vault, table in [
        ("internal-confluence-source-d6d4b0950261786c", "confluence_akbe2e"),
        ("v" * 70, "t"),
        ("v", "t" * 70),
        ("v" * 70, "t" * 70),
    ]:
        name = table_data_repo.pg_table_name(vault, table)
        assert len(name) <= _LIMIT, name
        assert role_sync._is_safe_pg_table_name(name), name


def test_the_rule_is_deterministic():
    """The name is derived at every call site rather than stored —
    including role_sync's GRANTs. A name that differed between calls
    would grant on one relation and query another."""
    vault, table = "internal-confluence-source-d6d4b0950261786c", "confluence_akbe2e"
    first = table_data_repo.pg_table_name(vault, table)
    assert all(table_data_repo.pg_table_name(vault, table) == first for _ in range(5))


def test_two_pairs_that_truncate_alike_stay_distinct():
    """The digest is over the ORIGINAL pair, not the truncated form.

    The pairs below are chosen so that EVERYTHING kept is identical — the
    same vault prefix, the same table — and they differ only in bytes the
    truncation throws away. That is the only shape that can tell the two
    digests apart: a first version of this test used vaults that already
    differed near the front, so a digest over the truncated string passed
    it, and the mutation that introduced exactly that bug survived."""
    table = "t" * 10
    a = table_data_repo.pg_table_name("v" * 60 + "aaa", table)
    b = table_data_repo.pg_table_name("v" * 60 + "bbb", table)
    assert a[: -8] == b[: -8], "fixture no longer shares the kept prefix"
    assert a != b, "two vaults differing only past the truncation became one name"

    long_vault = "v" * 60 + "aaa"
    c = table_data_repo.pg_table_name(long_vault, "t" * 9 + "c")
    d = table_data_repo.pg_table_name(long_vault, "t" * 9 + "d")
    assert c != d, "two tables composed to one physical name"


def test_the_boundary_is_where_it_says_it_is():
    """63 is untouched and 64 is fitted — an off-by-one either way would
    rename an existing table or leave the failing case failing."""
    vault = "prod-conc-1780908249-8ml717"  # 27
    at_limit = "report_metrics_1780908249_8ml71"  # 31 → 63
    over = "report_metrics_1780908249_8ml717"  # 32 → 64

    assert table_data_repo.pg_table_name(vault, at_limit) == f"vt_{vault.replace('-', '_')}__{at_limit}"
    assert len(table_data_repo.pg_table_name(vault, at_limit)) == 63

    fitted = table_data_repo.pg_table_name(vault, over)
    assert fitted != f"vt_{vault.replace('-', '_')}__{over}"
    assert len(fitted) <= _LIMIT
