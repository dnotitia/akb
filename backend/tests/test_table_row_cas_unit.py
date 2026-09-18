"""RED-first: row CAS contention — lost update must 409, not silently win.

T2 gate: this test FAILS on current main (no expected_row_commit
anywhere in the write path) and must go GREEN after the row_commit
column + trigger + 409 implementation.

Contract under test (T0: row_commit text + trigger bump, equality-only):

1. UPDATE with a matching expected_row_commit applies and bumps row_commit.
2. UPDATE with a stale expected_row_commit gets 409 (row untouched).
3. Concurrent writers A (read rev R) and B (read rev R, writes first):
   A's write with rev R then gets 409; A re-reads (rev R+1) and retries OK.
4. DELETE with stale expected_row_commit gets 409 (row survives).
"""

from __future__ import annotations

from app.services.table_row_write import (
    compile_delete_rows,
    compile_update_rows,
)

COLUMNS = [
    {"name": "title", "type": "text"},
    {"name": "severity", "type": "text"},
]


def test_update_with_matching_row_commit_applies() -> None:
    compiled = compile_update_rows(
        vault_name="eng",
        table_name="incidents",
        columns=COLUMNS,
        body={"title": "fixed"},
        query_params=[("id", "eq.00000000-0000-0000-0000-000000000001"),
                      ("expected_row_commit", "eq.aaa")],
    )
    assert not isinstance(compiled, dict)
    assert "row_commit" in compiled.sql
    assert compiled.params[0] == "fixed"
    assert "aaa" in compiled.params


def test_update_without_a_token_compiles_unguarded() -> None:
    """Opt-in: the guard is what the caller asks for, not what it must supply.

    A caller that sends no token gets the statement this endpoint has always
    produced — its own filter, no `row_commit` predicate, and no 409 on a
    zero-row match. The token buys lost-update detection; its absence costs
    only that.
    """
    compiled = compile_update_rows(
        vault_name="eng",
        table_name="incidents",
        columns=COLUMNS,
        body={"title": "fixed"},
        query_params=[("id", "eq.00000000-0000-0000-0000-000000000001")],
    )
    assert not isinstance(compiled, dict), compiled
    assert compiled.cas_guarded is False
    assert "row_commit" not in compiled.sql


def test_delete_without_a_token_compiles_unguarded() -> None:
    compiled = compile_delete_rows(
        vault_name="eng",
        table_name="incidents",
        columns=COLUMNS,
        query_params=[("id", "eq.00000000-0000-0000-0000-000000000001")],
    )
    assert not isinstance(compiled, dict), compiled
    assert compiled.cas_guarded is False
    assert "row_commit" not in compiled.sql
