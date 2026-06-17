"""Unit tests for AKB #215 declarative `unique_keys` + `indexes`.

DB-free. Covers the pure logic added for PR-1:

* generated-name strategy — deterministic, schema-global-namespaced,
  collision-safe, 63-byte truncation→sha1 hash (table_data_repo);
* validation — column-existence (case-insensitive), reserved-column
  rejection, index `order` enum, non-empty columns, duplicate names
  (table_service._resolve_unique_keys / _resolve_indexes);
* preflight duplicate-detection query SHAPE (table_data_repo.unique_key_duplicates);
* registry JSON round-trip (table_registry_repo.parse_json_list).

Style mirrors tests/test_table_identifier_unit.py — dependency-light,
no pool, no live PG.
"""
from __future__ import annotations

import hashlib

import pytest

from app.exceptions import ValidationError
from app.repositories import table_data_repo
from app.repositories.table_registry_repo import parse_json_list


# ── generated-name strategy ───────────────────────────────────────


def test_unique_key_name_is_deterministic_and_namespaced():
    """A generated UNIQUE-key name namespaces by the pg table (index
    names are schema-global) and is stable across repeated calls."""
    pg = "vt_demo__events"
    a = table_data_repo.generate_constraint_name(pg, ["actor", "ts"], kind="uk")
    b = table_data_repo.generate_constraint_name(pg, ["actor", "ts"], kind="uk")
    assert a == b, "name generation must be deterministic"
    assert a.startswith("vt_demo__events"), a
    assert a.endswith("__uk"), a
    assert "actor" in a and "ts" in a


def test_index_name_uses_idx_suffix():
    pg = "vt_demo__events"
    n = table_data_repo.generate_constraint_name(pg, ["ts"], kind="idx")
    assert n.endswith("__idx"), n
    assert n.startswith("vt_demo__events"), n


def test_generated_names_differ_by_table_and_columns():
    """Schema-global namespace: same columns on two different pg tables
    must NOT collide; different columns on the same table differ too."""
    n1 = table_data_repo.generate_constraint_name("vt_a__t", ["x"], kind="uk")
    n2 = table_data_repo.generate_constraint_name("vt_b__t", ["x"], kind="uk")
    n3 = table_data_repo.generate_constraint_name("vt_a__t", ["y"], kind="uk")
    assert n1 != n2
    assert n1 != n3


def test_generated_names_disambiguate_underscore_flattened_columns():
    """Distinct column lists whose `_`-joined forms collide (["a","b"] vs
    ["a_b"]) must still get DISTINCT names — the structured sha1 digest is
    always present so the underscore flattening can never alias them."""
    pg = "vt_demo__t"
    n_two = table_data_repo.generate_constraint_name(pg, ["a", "b"], kind="uk")
    n_one = table_data_repo.generate_constraint_name(pg, ["a_b"], kind="uk")
    assert n_two != n_one, (n_two, n_one)
    # order is part of an index's identity → distinct names too
    i_ab = table_data_repo.generate_constraint_name(pg, ["a", "b"], kind="idx")
    i_ba = table_data_repo.generate_constraint_name(pg, ["b", "a"], kind="idx")
    assert i_ab != i_ba, (i_ab, i_ba)


def test_generated_name_fits_namedatalen_and_is_stable_for_long_inputs():
    """When the readable name would exceed 63 bytes, it is truncated and a
    deterministic sha1[:8] of the full logical name is appended. The
    result must (a) fit, (b) be stable, (c) stay collision-safe across
    distinct long inputs that share a truncated prefix."""
    pg = "vt_" + "a" * 40 + "__" + "b" * 10
    cols_1 = ["c" * 30, "d" * 30]
    cols_2 = ["c" * 30, "e" * 30]  # shares the truncated prefix
    n1 = table_data_repo.generate_constraint_name(pg, cols_1, kind="uk")
    n1b = table_data_repo.generate_constraint_name(pg, cols_1, kind="uk")
    n2 = table_data_repo.generate_constraint_name(pg, cols_2, kind="uk")
    assert len(n1.encode()) <= table_data_repo.PG_IDENT_MAX_LEN
    assert len(n2.encode()) <= table_data_repo.PG_IDENT_MAX_LEN
    assert n1 == n1b, "hashed name must be stable"
    assert n1 != n2, "distinct inputs sharing a prefix must not collide"


def test_generated_name_hash_is_pure_hashlib():
    """No randomness: recomputing the sha1 of the logical name yields the
    suffix actually appended (proves stability across processes)."""
    pg = "vt_" + "a" * 40 + "__tbl"
    cols = ["x" * 40]
    name = table_data_repo.generate_constraint_name(pg, cols, kind="uk")
    # digest is sha1 over the NUL-joined (pg, kind, *columns) tuple — recompute
    # it the same way to prove there is no randomness (stable across processes).
    expected = hashlib.sha1("\x00".join([pg, "uk", *cols]).encode()).hexdigest()[:8]
    # hash is embedded before the kind suffix: ..._{sha1[:8]}__uk
    assert expected in name, (name, expected)
    assert name.endswith("__uk")


# ── preflight duplicate-detection query shape ─────────────────────


class _RecordingConn:
    """Captures the SQL + params a repo primitive would send to PG."""

    def __init__(self, rows=None):
        self.sql = None
        self.params = None
        self._rows = rows or []

    async def fetch(self, sql, *params):
        self.sql = sql
        self.params = params
        return self._rows


@pytest.mark.asyncio
async def test_unique_key_duplicates_query_shape():
    conn = _RecordingConn(rows=[])
    await table_data_repo.unique_key_duplicates(
        conn, "vt_demo__events", ["actor", "ts"], limit=5,
    )
    sql = conn.sql.upper()
    assert "FROM VT_DEMO__EVENTS" in sql
    assert "GROUP BY" in sql
    assert "HAVING COUNT(*) > 1" in sql
    # only the key columns (+ the COUNT) appear — no SELECT * that could
    # leak other columns of the table into the duplicate sample.
    select_clause = conn.sql.split("FROM")[0]
    assert "actor" in select_clause and "ts" in select_clause
    assert "COUNT(*)" in select_clause.upper()
    assert "ACTOR" in sql and "TS" in sql
    assert "LIMIT 5" in sql
    # NULLS DISTINCT parity: rows with any NULL key value are excluded so the
    # preflight is not STRICTER than the UNIQUE constraint it guards (#220 review).
    assert "WHERE" in sql
    assert sql.count("IS NOT NULL") == 2  # one per key column (actor, ts)


@pytest.mark.asyncio
async def test_unique_key_duplicates_sanitizes_identifiers():
    """Columns flow through safe_ident — a punctuation column can't
    inject raw SQL into the preflight GROUP BY."""
    conn = _RecordingConn(rows=[])
    await table_data_repo.unique_key_duplicates(
        conn, "vt_demo__t", ["a;DROP", "b"], limit=1,
    )
    assert ";DROP" not in conn.sql
    assert "a_DROP" in conn.sql


# ── DDL primitive identifier safety ───────────────────────────────


class _ExecConn:
    def __init__(self):
        self.sql = None

    async def execute(self, sql, *params):
        self.sql = sql


@pytest.mark.asyncio
async def test_create_index_order_enum_only():
    """`order` is a closed {ASC,DESC} enum — never interpolated raw."""
    conn = _ExecConn()
    await table_data_repo.create_index(
        conn, "vt_d__t", "vt_d__t__a__idx",
        [("a", "asc"), ("b", "desc")],
    )
    assert "ASC" in conn.sql and "DESC" in conn.sql
    assert "CREATE INDEX" in conn.sql

    with pytest.raises(ValidationError):
        await table_data_repo.create_index(
            conn, "vt_d__t", "n", [("a", "sideways")],
        )


@pytest.mark.asyncio
async def test_create_unique_constraint_shape():
    conn = _ExecConn()
    await table_data_repo.create_unique_constraint(
        conn, "vt_d__t", "vt_d__t__a_b__uk", ["a", "b"],
    )
    assert "ADD CONSTRAINT" in conn.sql.upper()
    assert "UNIQUE" in conn.sql.upper()


@pytest.mark.asyncio
async def test_drop_constraint_and_index_are_if_exists():
    conn = _ExecConn()
    await table_data_repo.drop_constraint(conn, "vt_d__t", "vt_d__t__a__uk")
    assert "DROP CONSTRAINT IF EXISTS" in conn.sql.upper()
    await table_data_repo.drop_index(conn, "vt_d__t__a__idx")
    assert "DROP INDEX IF EXISTS" in conn.sql.upper()


# ── service-layer validation/resolution ───────────────────────────

from app.services import table_service  # noqa: E402


_DECLARED = [{"name": "actor", "type": "text"}, {"name": "ts", "type": "date"}]


def test_resolve_unique_keys_generates_stable_name():
    resolved = table_service._resolve_unique_keys(
        [{"columns": ["actor", "ts"]}], _DECLARED, "vt_demo__events",
    )
    assert len(resolved) == 1
    assert resolved[0]["columns"] == ["actor", "ts"]
    assert resolved[0]["name"].endswith("__uk")


def test_resolve_unique_keys_uses_caller_name_verbatim():
    resolved = table_service._resolve_unique_keys(
        [{"name": "uq_event", "columns": ["actor"]}], _DECLARED, "vt_demo__events",
    )
    assert resolved[0]["name"] == "uq_event"


def test_resolve_unique_keys_rejects_unknown_column():
    with pytest.raises(ValidationError):
        table_service._resolve_unique_keys(
            [{"columns": ["nope"]}], _DECLARED, "vt_demo__events",
        )


def test_resolve_unique_keys_column_match_is_case_insensitive():
    # A mixed-case reference matches the declared column case-insensitively AND
    # is normalised to the CANONICAL declared name — so the stored metadata and
    # the duplicate-preflight sample (row[safe_ident(name)]) use the real PG
    # identifier, never the caller's casing (which would KeyError). (#220 review)
    resolved = table_service._resolve_unique_keys(
        [{"columns": ["ACTOR"]}], _DECLARED, "vt_demo__events",
    )
    assert resolved[0]["columns"] == ["actor"]


def test_resolve_rejects_duplicate_column_within_one_key():
    # columns:["actor","actor"] must be a clean 422, not a DDL 500 (42701/42P16).
    with pytest.raises(ValidationError):
        table_service._resolve_unique_keys(
            [{"columns": ["actor", "actor"]}], _DECLARED, "vt_demo__events",
        )
    # case-insensitive duplicate too
    with pytest.raises(ValidationError):
        table_service._resolve_unique_keys(
            [{"columns": ["actor", "ACTOR"]}], _DECLARED, "vt_demo__events",
        )
    with pytest.raises(ValidationError):
        table_service._resolve_indexes(
            [{"columns": ["ts", "ts"]}], _DECLARED, "vt_demo__events",
        )


def test_resolve_indexes_canonicalises_column_case():
    resolved = table_service._resolve_indexes(
        [{"columns": [{"name": "TS", "order": "desc"}]}], _DECLARED, "vt_demo__events",
    )
    assert resolved[0]["columns"][0]["name"] == "ts"
    assert resolved[0]["columns"][0]["order"] == "desc"


def test_resolve_unique_keys_rejects_reserved_column():
    for reserved in ("id", "created_at", "updated_at", "created_by"):
        with pytest.raises(ValidationError):
            table_service._resolve_unique_keys(
                [{"columns": [reserved]}], _DECLARED, "vt_demo__events",
            )


def test_resolve_unique_keys_rejects_empty_columns():
    with pytest.raises(ValidationError):
        table_service._resolve_unique_keys(
            [{"columns": []}], _DECLARED, "vt_demo__events",
        )


def test_resolve_unique_keys_rejects_duplicate_names():
    with pytest.raises(ValidationError):
        table_service._resolve_unique_keys(
            [
                {"name": "dup", "columns": ["actor"]},
                {"name": "dup", "columns": ["ts"]},
            ],
            _DECLARED, "vt_demo__events",
        )


def test_resolve_indexes_accepts_bare_string_and_obj_columns():
    resolved = table_service._resolve_indexes(
        [{"columns": ["actor", {"name": "ts", "order": "desc"}]}],
        _DECLARED, "vt_demo__events",
    )
    assert resolved[0]["columns"] == [
        {"name": "actor", "order": "asc"},
        {"name": "ts", "order": "desc"},
    ]
    assert resolved[0]["name"].endswith("__idx")


def test_resolve_indexes_rejects_bad_order():
    with pytest.raises(ValidationError):
        table_service._resolve_indexes(
            [{"columns": [{"name": "ts", "order": "upwards"}]}],
            _DECLARED, "vt_demo__events",
        )


def test_resolve_indexes_rejects_unknown_column():
    with pytest.raises(ValidationError):
        table_service._resolve_indexes(
            [{"columns": ["ghost"]}], _DECLARED, "vt_demo__events",
        )


def test_resolve_indexes_rejects_reserved_column():
    with pytest.raises(ValidationError):
        table_service._resolve_indexes(
            [{"columns": ["id"]}], _DECLARED, "vt_demo__events",
        )


# ── registry JSON round-trip ──────────────────────────────────────


def test_parse_json_list_handles_str_list_and_none():
    payload = [{"name": "uq", "columns": ["a", "b"]}]
    import json
    assert parse_json_list(json.dumps(payload)) == payload  # legacy str row
    assert parse_json_list(payload) == payload               # asyncpg pre-parsed
    assert parse_json_list(None) == []                       # NULL/empty
    assert parse_json_list("[]") == []


# ── stable unique-violation contract (AC#2) ───────────────────────
#
# A duplicate INSERT against a declared unique key raises asyncpg
# UniqueViolationError (SQLSTATE 23505). The executor must translate
# that into our own UniqueViolationError carrying pg_sqlstate='23505',
# and execute_sql must surface it under the dedicated stable
# UNIQUE_VIOLATION code (NOT the generic SQL_ERROR catch-all) with
# pg_sqlstate threaded into the envelope — mirroring PERMISSION_DENIED.


class _FakeTxn:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class _FakeConn:
    """Minimal asyncpg-conn stand-in whose .execute(SQL) raises 23505."""

    def __init__(self, raise_on_sql: bool):
        self._raise_on_sql = raise_on_sql

    def transaction(self):
        return _FakeTxn()

    async def execute(self, sql, *args):
        # SET LOCAL ... statements succeed; the user INSERT raises.
        if self._raise_on_sql and not sql.upper().startswith("SET LOCAL"):
            import asyncpg
            raise asyncpg.exceptions.UniqueViolationError(
                'duplicate key value violates unique constraint '
                '"vt_demo__events__actor_ts__uk"'
            )
        return "INSERT 0 1"


class _FakeAcquire:
    def __init__(self, conn):
        self._conn = conn

    async def __aenter__(self):
        return self._conn

    async def __aexit__(self, *exc):
        return False


class _FakePool:
    def __init__(self, conn):
        self._conn = conn

    def acquire(self):
        return _FakeAcquire(self._conn)


async def test_executor_translates_pg_23505_to_stable_unique_violation():
    from app.services.user_sql_executor import (
        UniqueViolationError,
        UserSqlExecutor,
    )

    pool = _FakePool(_FakeConn(raise_on_sql=True))
    ex = UserSqlExecutor(pool)
    with pytest.raises(UniqueViolationError) as ei:
        await ex.execute(
            user_id="00000000-0000-0000-0000-000000000001",
            sql="INSERT INTO vt_demo__events (actor) VALUES ('x')",
            is_admin=True,  # skip SET LOCAL ROLE (needs no real role)
        )
    # The contract: stable sqlstate is attached and the constraint name
    # (here the generated name) rides along in the verbatim PG message.
    assert ei.value.pg_sqlstate == "23505"
    assert "unique constraint" in str(ei.value)


async def test_execute_sql_surfaces_unique_violation_with_stable_code(monkeypatch):
    """The service maps UniqueViolationError → UNIQUE_VIOLATION code with
    pg_sqlstate, not the generic SQL_ERROR catch-all."""
    from app.services import table_service
    from app.services.user_sql_executor import UniqueViolationError
    from app.util.errors import UNIQUE_VIOLATION

    class _Executor:
        async def execute(self, **kw):
            raise UniqueViolationError("duplicate key value", pg_sqlstate="23505")

    # Stub the pool/rewriter so we reach the executor call without a live DB.
    class _Conn:
        async def fetch(self, *a, **k):
            return []

    class _Acq:
        async def __aenter__(self):
            return _Conn()

        async def __aexit__(self, *e):
            return False

    class _Pool:
        def acquire(self):
            return _Acq()

    async def _fake_pool():
        return _Pool()

    monkeypatch.setattr(table_service, "get_pool", _fake_pool)
    monkeypatch.setattr(
        table_service.table_data_repo, "build_table_name_map",
        lambda conn, vaults: _async_return({"events": "vt_demo__events"}),
    )
    monkeypatch.setattr(
        table_service.table_data_repo, "rewrite_table_names",
        lambda sql, m: sql,
    )
    monkeypatch.setattr(table_service, "get_user_sql_executor", lambda: _Executor())

    out = await table_service.execute_sql(
        vault_names=["demo"],
        user_id="u1",
        sql="INSERT INTO events (actor) VALUES ('x')",
        is_admin=True,
    )
    assert out["code"] == UNIQUE_VIOLATION
    assert out["code"] != "sql_error"
    assert out["details"]["pg_sqlstate"] == "23505"


def _async_return(value):
    async def _coro():
        return value
    return _coro()


# ── REST create surface accepts unique_keys + indexes (#215) ──────


def test_rest_create_table_request_accepts_unique_keys_and_indexes():
    """CreateTableRequest must carry the declarative fields so a REST/web
    POST is not silently dropped (asymmetric read/write contract bug)."""
    from app.api.routes.tables import CreateTableRequest

    req = CreateTableRequest(
        name="events",
        columns=[{"name": "actor", "type": "text"}, {"name": "ts", "type": "text"}],
        unique_keys=[{"columns": ["actor", "ts"]}],
        indexes=[{"columns": ["actor"]}],
    )
    assert req.unique_keys == [{"columns": ["actor", "ts"]}]
    assert req.indexes == [{"columns": ["actor"]}]
    # Defaults stay None (omitted) so create_table's own defaults apply.
    req2 = CreateTableRequest(name="t", columns=[{"name": "a", "type": "text"}])
    assert req2.unique_keys is None and req2.indexes is None


# ── AC#11 discovery surface: declared guarantees reach the search chunk ──


def test_table_chunk_exposes_unique_keys_and_indexes():
    """The hybrid-search metadata chunk must carry the declared unique_keys
    + indexes (names AND columns) so an agent can DISCOVER a table's
    guarantees by searching — AC#11's discovery surface (#220 review)."""
    from app.services.index_service import build_table_chunk
    c = build_table_chunk(
        vault_name="v", name="customers", description="Customer records",
        columns=[{"name": "email", "type": "text"}, {"name": "tenant", "type": "text"}],
        unique_keys=[{"name": "uk_customers_email", "columns": ["email"]}],
        indexes=[{"name": "idx_customers_tenant",
                  "columns": [{"name": "tenant", "order": "desc"}]}],
    )
    assert "uk_customers_email" in c.content
    assert "idx_customers_tenant" in c.content
    assert "email" in c.content and "tenant" in c.content
    # the index's non-default order is surfaced too
    assert "desc" in c.content


def test_table_chunk_backcompat_no_constraints():
    """Omitting unique_keys/indexes must not crash and must not emit the
    sections (older callers / tables without declared guarantees)."""
    from app.services.index_service import build_table_chunk
    c = build_table_chunk(
        vault_name="v", name="t", description=None,
        columns=[{"name": "a", "type": "text"}],
    )
    assert "UNIQUE_KEYS" not in c.content
    assert "INDEXES" not in c.content
