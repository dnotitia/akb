"""Acceptance gates for the column logical/physical name split (#433).

Design: docs/design/proposal/2026-09-17-column-logical-physical-names/
("Acceptance gates"). This is the DB-free half. The half that needs a real
PostgreSQL — the whole lifecycle, `akb_sql`, `pg_attribute` drift, RoleSync
and an existing table left byte-identical — is
`test_table_column_logical_names_postgres.py`.

Written before the implementation, and red against 2ed799bf: every gate is
refused there by `_COLUMN_NAME_RE`, or asks for the `pg_name` the registry
does not carry yet. A gate that cannot fail proves nothing.
"""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
import uuid
from pathlib import Path

import pytest

from app.repositories import table_data_repo, table_registry_repo
from app.services import (
    app_resource_service,
    app_rollout_service,
    app_rollout_worker,
    table_schema_service,
    table_service,
)
from app.services.native_document_service import NativeDocumentService
from app.services.row_query_shape import _shape_result
from app.services.table_row_query import compile_ast_row_query, compile_row_query
from app.services.table_row_write import (
    compile_delete_rows,
    compile_insert_rows,
    compile_update_rows,
)

_VAULT = "papers"
_TABLE = "results"
_TABLE_PG = "vt_papers__results"

# What may reach PostgreSQL: an unquoted, ASCII-only identifier.
_PHYSICAL_RE = re.compile(r"^[a-z_][a-z0-9_]*$")

# Headers taken from documents (#433): Korean, parentheses, spaces, capitals.
_HEADERS = [
    {"name": "분류", "type": "text"},
    {"name": "중요도", "type": "int"},
    {"name": "TriviaQA(비과학 문헌)", "type": "numeric"},
    {"name": "w Embedding", "type": "text"},
]


def _spec(columns, *, unique_keys=None, indexes=None, table=_TABLE):
    return table_service._canonical_create_spec(
        vault_name=_VAULT,
        name=table,
        columns=[dict(c) for c in columns],
        unique_keys=unique_keys,
        indexes=indexes,
    )


def _physical(columns) -> dict[str, str]:
    return {c["name"]: c["pg_name"] for c in columns}


class _Tx:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class _Conn:
    """Enough of an asyncpg connection for create/alter to run end to end.

    Every SQL text sent is recorded with its parameters, so a gate can assert
    on exactly what would have reached PostgreSQL. `table_row` is what the
    registry answers for the table being altered.
    """

    def __init__(self, *, table_row=None):
        self.table_row = table_row
        self.sent: list[tuple[str, tuple]] = []

    def transaction(self, **_kwargs):
        return _Tx()

    async def execute(self, sql, *params):
        self.sent.append((sql, params))
        return "OK"

    async def fetch(self, sql, *params):
        self.sent.append((sql, params))
        return []

    async def fetchrow(self, sql, *params):
        if "FROM vaults" in sql:
            return {"name": _VAULT}
        if "vault_tables" in sql:
            return self.table_row
        return None

    async def fetchval(self, sql, *params):
        self.sent.append((sql, params))
        return None

    def sql(self) -> list[str]:
        return [sql for sql, _params in self.sent]


class _Pool:
    def __init__(self, conn):
        self._conn = conn

    def acquire(self):
        conn = self._conn

        class _Acquire:
            async def __aenter__(self):
                return conn

            async def __aexit__(self, *exc):
                return False

        return _Acquire()


def _wire(monkeypatch, conn) -> list[dict]:
    """Point table_service at `conn`; return the list the metadata chunk
    writer appends each call's columns to."""
    async def _get_pool():
        return _Pool(conn)

    class _RoleSync:
        async def grant_table_in_conn(self, *_a, **_k):
            return None

    indexed: list[dict] = []

    async def _index(*_a, **kwargs):
        indexed.append(kwargs)

    monkeypatch.setattr(table_service, "get_pool", _get_pool)
    monkeypatch.setattr(table_service, "get_role_sync", lambda: _RoleSync())
    monkeypatch.setattr(table_service, "index_table_metadata", _index)
    return indexed


def _registry_insert(conn: _Conn) -> list[dict]:
    for sql, params in conn.sent:
        if "INSERT INTO vault_tables" in sql:
            return json.loads(params[5])
    raise AssertionError("no registry insert was sent")


def _registry_update(conn: _Conn) -> list[dict]:
    for sql, params in conn.sent:
        if sql.startswith("UPDATE vault_tables SET columns"):
            return json.loads(params[0])
    raise AssertionError("no registry update was sent")


# ── Gate 1: Korean headers create, insert, alter, drop; reads are verbatim ──


async def test_gate1_korean_headers_create_verbatim_and_only_ascii_reaches_pg(monkeypatch):
    conn = _Conn()
    _wire(monkeypatch, conn)
    decomposed = [dict(c) for c in _HEADERS]
    # A macOS-sourced header arrives decomposed; the registry holds NFC.
    decomposed[0]["name"] = unicodedata.normalize("NFD", "분류")
    assert decomposed[0]["name"] != "분류"

    out = await table_service.create_table(
        uuid.uuid4(), _TABLE, decomposed, actor_id="tester",
        unique_keys=[{"columns": ["분류"]}],
        indexes=[{"columns": [{"name": "중요도", "order": "desc"}]}],
    )

    names = [c["name"] for c in _HEADERS]
    assert [c["name"] for c in out["columns"]] == names
    stored = _registry_insert(conn)
    assert [c["name"] for c in stored] == names
    for col in stored:
        assert _PHYSICAL_RE.fullmatch(col["pg_name"]), col
    # Identifiers are the only thing a logical name could leak into, and the
    # SQL text is where they live — values travel as parameters.
    for sql in conn.sql():
        assert sql.isascii(), sql
    phys = _physical(stored)
    ddl = next(s for s in conn.sql() if s.startswith("CREATE TABLE"))
    for pg in phys.values():
        assert f" {pg} " in ddl, (pg, ddl)
    uk = next(s for s in conn.sql() if "UNIQUE (" in s)
    assert f"UNIQUE ({phys['분류']})" in uk
    idx = next(s for s in conn.sql() if s.startswith("CREATE INDEX"))
    assert f"({phys['중요도']} DESC)" in idx
    # Search indexes the headers a reader would type (decision 10).
    assert out["unique_keys"][0]["columns"] == ["분류"]


def test_gate1_row_api_speaks_logical_names_and_sql_speaks_physical():
    cols, _, _ = _spec(_HEADERS)
    phys = _physical(cols)

    read = compile_row_query(
        vault_name=_VAULT, table_name=_TABLE, columns=cols,
        query_params=[
            ("select", "분류,중요도"),
            ("중요도", "gte.3"),
            ("w Embedding", "eq.x"),
            ("order", "중요도.desc"),
        ],
    )
    assert "error" not in read, read
    assert read["sql"].isascii(), read["sql"]
    for header in ("분류", "중요도", "w Embedding"):
        assert phys[header] in read["sql"]
    body, _ = _shape_result(
        {"items": [{phys["분류"]: "A", phys["중요도"]: 3}],
         "columns": [phys["분류"], phys["중요도"]]},
        vault_name=_VAULT, table_name=_TABLE,
        projections=read["projections"], count_exact=False, offset=0,
    )
    assert body["columns"] == ["분류", "중요도"]
    assert body["items"] == [{"분류": "A", "중요도": 3}]

    # `select=*` rows come back keyed by the logical names too.
    star = compile_row_query(
        vault_name=_VAULT, table_name=_TABLE, columns=cols, query_params=[],
    )
    body, _ = _shape_result(
        {"items": [{"id": "r1", phys["TriviaQA(비과학 문헌)"]: 0.5, "created_by": "t"}],
         "columns": ["id", phys["TriviaQA(비과학 문헌)"], "created_by"]},
        vault_name=_VAULT, table_name=_TABLE,
        projections=star["projections"], count_exact=False, offset=0,
    )
    assert body["columns"] == ["id", "TriviaQA(비과학 문헌)", "created_by"]
    assert body["items"] == [{"id": "r1", "TriviaQA(비과학 문헌)": 0.5, "created_by": "t"}]

    ast = compile_ast_row_query(
        vault_name=_VAULT, table_name=_TABLE, columns=cols,
        ast={
            "select": ["TriviaQA(비과학 문헌)"],
            "filter": {"col": "w Embedding", "op": "eq", "val": "x"},
            "order": [{"col": "중요도", "dir": "desc"}],
        },
    )
    assert "error" not in ast, ast
    assert ast["sql"].isascii(), ast["sql"]

    insert = compile_insert_rows(
        vault_name=_VAULT, table_name=_TABLE, columns=cols, actor_id="alice",
        body={"분류": "A", "w Embedding": "x"},
    )
    assert not isinstance(insert, dict), insert
    assert insert.sql == (
        f"INSERT INTO {_TABLE_PG} ({phys['분류']}, {phys['w Embedding']}, created_by) "
        "VALUES ($1, $2, $3)"
    )
    assert insert.params == ["A", "x", "alice"]

    update = compile_update_rows(
        vault_name=_VAULT, table_name=_TABLE, columns=cols,
        body={"중요도": 5}, query_params=[("분류", "eq.A")],
    )
    assert not isinstance(update, dict), update
    assert update.sql == (
        f"UPDATE {_TABLE_PG} SET {phys['중요도']} = $1, updated_at = NOW() "
        f"WHERE ({phys['분류']} = $2)"
    )


async def test_gate1_alter_adds_renames_and_drops_by_logical_name(monkeypatch):
    cols, _, _ = _spec(_HEADERS)
    phys = _physical(cols)
    conn = _Conn(table_row={
        "id": uuid.uuid4(), "name": _TABLE, "columns": cols,
        "unique_keys": [], "indexes": [], "collection": None, "description": "",
    })
    _wire(monkeypatch, conn)

    out = await table_service.alter_table(
        uuid.uuid4(), _TABLE, actor_id="tester",
        add_columns=[{"name": "비고", "type": "text"}],
        drop_columns=["w Embedding"],
        rename_columns={"TriviaQA(비과학 문헌)": "TriviaQA(과학 문헌)"},
    )

    for sql in conn.sql():
        assert sql.isascii(), sql
    stored = _registry_update(conn)
    assert [c["name"] for c in stored] == ["분류", "중요도", "TriviaQA(과학 문헌)", "비고"]
    assert [c["name"] for c in out["columns"]] == [c["name"] for c in stored]
    renamed = next(c for c in stored if c["name"] == "TriviaQA(과학 문헌)")
    # A logical rename keeps the physical column (decision 7).
    assert renamed["pg_name"] == phys["TriviaQA(비과학 문헌)"]
    assert not any("RENAME COLUMN" in sql for sql in conn.sql())
    drop = next(s for s in conn.sql() if "DROP COLUMN" in s)
    assert drop == f"ALTER TABLE {_TABLE_PG} DROP COLUMN IF EXISTS {phys['w Embedding']}"
    added = next(c for c in stored if c["name"] == "비고")
    assert _PHYSICAL_RE.fullmatch(added["pg_name"])
    assert any(f"ADD COLUMN IF NOT EXISTS {added['pg_name']} TEXT" in s for s in conn.sql())


# ── Gate 2: headers safe_ident would fuse coexist ────────────────────────────


def test_gate2_headers_safe_ident_would_fuse_get_distinct_physical_names():
    fused_a, fused_b = "분류", "모델"
    assert table_data_repo.safe_ident(fused_a) == table_data_repo.safe_ident(fused_b) == "__"

    cols, _, _ = _spec([
        {"name": fused_a, "type": "text"},
        {"name": fused_b, "type": "text"},
        {"name": "중요도", "type": "int"},
    ])

    physical = [c["pg_name"] for c in cols]
    assert len(set(physical)) == 3, physical
    assert "__" not in physical
    assert all(_PHYSICAL_RE.fullmatch(p) for p in physical), physical


# ── Gate 3: re-parsing the same document yields the identical registry row ───


def test_gate3_physical_names_are_deterministic_and_table_scoped():
    first, _, _ = _spec(_HEADERS)
    second, _, _ = _spec(_HEADERS)
    assert first == second

    # Pinned formula, so the derivation cannot drift between processes or
    # releases: c_<1-based ordinal>_<sha1(table_pg NUL logical)[:8]>.
    for ordinal, col in enumerate(first, start=1):
        digest = hashlib.sha1(
            "\x00".join([_TABLE_PG, col["name"]]).encode(), usedforsecurity=False,
        ).hexdigest()[:8]
        assert col["pg_name"] == f"c_{ordinal}_{digest}", col

    other, _, _ = _spec(_HEADERS, table="other_results")
    assert [c["pg_name"] for c in other] != [c["pg_name"] for c in first]


async def test_gate3_reparse_against_the_stored_row_is_a_match(monkeypatch):
    stored, _, _ = _spec(_HEADERS)
    conn = _Conn(table_row={
        "id": uuid.uuid4(), "vault_id": uuid.uuid4(), "collection_id": None,
        "collection": None, "name": _TABLE, "description": "",
        "columns": stored, "unique_keys": [], "indexes": [],
        "created_by": "someone", "created_at": None, "updated_at": None,
    })
    _wire(monkeypatch, conn)

    out = await table_service.create_table(
        uuid.uuid4(), _TABLE, [dict(c) for c in _HEADERS], actor_id="tester",
        if_not_exists=True, can_read_existing=True,
    )

    assert out["created"] is False
    assert out["matches_request"] is True, out["mismatches"]
    assert out["columns"] == stored


# ── Gate 4: a 21+ character Korean header fits the 63-byte physical bound ────


def test_gate4_long_korean_header_is_accepted_within_the_physical_bound():
    header = "가나다라마바사아자차카타파하거너더러머버서어"
    assert len(header) == 22
    assert len(header.encode()) == 66 > table_data_repo.PG_IDENT_MAX_LEN

    cols, _, _ = _spec([{"name": header, "type": "text"}])

    assert cols[0]["name"] == header
    assert _PHYSICAL_RE.fullmatch(cols[0]["pg_name"])
    assert len(cols[0]["pg_name"].encode()) <= table_data_repo.PG_IDENT_MAX_LEN

    # Longer than the bound even one byte per character — the case a
    # character-mapping fallback (safe_ident) could not fit.
    longer, _, _ = _spec([{"name": "가" * 70, "type": "text"}])
    assert len(longer[0]["pg_name"].encode()) <= table_data_repo.PG_IDENT_MAX_LEN

    # The logical bound is characters, not bytes: 255 is the ceiling.
    widest, _, _ = _spec([{"name": "가" * 255, "type": "text"}])
    assert widest[0]["name"] == "가" * 255
    with pytest.raises(table_service.ValidationError):
        _spec([{"name": "가" * 256, "type": "text"}])


# ── Gate 5: constraints, indexes and drift all observe the physical name ─────


def test_gate5_keys_and_indexes_are_logical_in_the_registry_physical_in_names():
    cols, uks, idxs = _spec(
        _HEADERS,
        unique_keys=[{"columns": ["분류", "w Embedding"]}],
        indexes=[{"columns": [{"name": "중요도", "order": "desc"}]}],
    )
    phys = _physical(cols)

    assert uks[0]["columns"] == ["분류", "w Embedding"]
    assert uks[0]["name"] == table_data_repo.generate_constraint_name(
        _TABLE_PG, [phys["분류"], phys["w Embedding"]], kind="uk",
    )
    assert idxs[0]["columns"] == [{"name": "중요도", "order": "desc"}]
    assert idxs[0]["name"] == table_data_repo.generate_constraint_name(
        _TABLE_PG, [phys["중요도"]], kind="idx",
    )
    assert uks[0]["name"].isascii() and idxs[0]["name"].isascii()


def test_gate5_drift_compares_the_registry_to_pg_attribute_by_physical_name():
    cols, _, _ = _spec(_HEADERS)
    phys = _physical(cols)
    pg_types = {
        "id": "uuid",
        phys["분류"]: "text",
        phys["중요도"]: "bigint",
        phys["TriviaQA(비과학 문헌)"]: "numeric",
        phys["w Embedding"]: "text",
        "created_by": "text",
        "created_at": "timestamp with time zone",
        "updated_at": "timestamp with time zone",
        "row_commit": "text",
    }

    schema = table_schema_service._build_table_schema(
        _VAULT,
        {"name": _TABLE, "columns": cols, "unique_keys": [], "indexes": []},
        pg_types,
    )

    assert schema["drift"] == {
        "has_drift": False, "missing_columns": [], "extra_columns": [], "type_mismatches": [],
    }
    assert [(c["name"], c["pg_name"]) for c in schema["columns"]] == [
        (c["name"], c["pg_name"]) for c in cols
    ]


# ── Gate 6: existing tables read back byte-identical ─────────────────────────

# Captured from 2ed799bf for the spec below: the DDL and generated names a
# table created before this change carries. Grammar-shaped names must keep
# producing exactly these bytes, which is what lets an existing table keep
# its physical names — `pg_name = name`, stored nowhere — with no DDL rewrite.
_LEGACY_SPEC = [
    {"name": "status", "type": "enum", "enum": ["draft", "active"], "default": "draft"},
    {"name": "qty", "type": "int", "required": True, "default": 1, "check": {"op": "gte", "value": 0}},
    {"name": "owner_id", "type": "uuid", "references": {"table": "users"}},
    {"name": "title", "type": "text", "unique": True},
    {"name": "score", "type": "numeric", "index": True},
]
_LEGACY_DDL = (
    "CREATE TABLE vt_papers__results (id UUID PRIMARY KEY DEFAULT uuid_generate_v4(), "
    "status TEXT DEFAULT 'draft', qty BIGINT NOT NULL DEFAULT 1, owner_id UUID, "
    "title TEXT, score NUMERIC, "
    "CONSTRAINT vt_papers__results__qty_312403a0__check CHECK (qty >= 0), "
    "CONSTRAINT vt_papers__results__status_0a1ff0b0__enum CHECK (status IN ('draft', 'active')), "
    "CONSTRAINT vt_papers__results__owner_id_dd458ac8__fkey FOREIGN KEY (owner_id) "
    "REFERENCES vt_papers__users (id) ON DELETE NO ACTION, "
    "created_by TEXT, created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(), "
    "updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(), "
    "row_commit TEXT NOT NULL DEFAULT gen_random_uuid()::TEXT)"
)
_LEGACY_UNIQUE_KEYS = [
    {"name": "vt_papers__results__title_cd83c047__uk", "columns": ["title"]},
    {"name": "vt_papers__results__title_qty_2cb37339__uk", "columns": ["title", "qty"]},
]
_LEGACY_INDEXES = [
    {"name": "vt_papers__results__score_75fb878d__idx",
     "columns": [{"name": "score", "order": "asc"}]},
    {"name": "vt_papers__results__qty_score_4d199e46__idx",
     "columns": [{"name": "qty", "order": "asc"}, {"name": "score", "order": "desc"}]},
]


async def test_gate6_grammar_shaped_names_keep_their_physical_identity():
    cols, uks, idxs = _spec(
        _LEGACY_SPEC,
        unique_keys=[{"columns": ["title", "qty"]}],
        indexes=[{"columns": ["qty", {"name": "score", "order": "desc"}]}],
    )

    assert [c["pg_name"] for c in cols] == [c["name"] for c in cols]
    assert uks == _LEGACY_UNIQUE_KEYS
    assert idxs == _LEGACY_INDEXES
    conn = _Conn()
    await table_data_repo.create_dynamic_table(
        conn, _TABLE_PG, cols, vault_name=_VAULT,
        vault_id=uuid.UUID("00000000-0000-0000-0000-000000000001"),
        resource_uri="akb://papers/table/results",
    )
    assert conn.sql()[0] == _LEGACY_DDL


def test_gate6_a_column_without_pg_name_has_the_identifier_the_legacy_rule_gives():
    # The contract for every column that stores no `pg_name`. Unquoted
    # identifiers fold to lowercase, and `safe_ident` mapped every other
    # character to `_`: that pair is the identifier the old DDL made.
    assert table_data_repo.column_pg_name({"name": "status"}) == "status"
    assert table_data_repo.column_pg_name({"name": "MyCol"}) == "mycol"
    assert table_data_repo.column_pg_name({"name": "my-col"}) == "my_col"
    assert table_data_repo.column_pg_name({"name": "분류", "pg_name": "c_1_0badf00d"}) == "c_1_0badf00d"


# The registry stores `pg_name` sparsely: only where it differs from the
# identifier the legacy rule (`safe_ident(name).lower()`) gives, which is what
# `column_pg_name` falls back to. So every table that exists keeps a registry
# row byte-identical to what it had, and older code can still use it.


def _alter_conn_for(columns):
    return _Conn(table_row={
        "id": uuid.uuid4(), "name": _TABLE, "columns": columns,
        "unique_keys": [], "indexes": [], "collection": None, "description": "",
    })


@pytest.mark.parametrize("if_not_exists", [False, True])
async def test_gate6_a_plain_column_created_now_stores_no_pg_name(monkeypatch, if_not_exists):
    conn = _Conn()
    _wire(monkeypatch, conn)

    out = await table_service.create_table(
        uuid.uuid4(), _TABLE,
        [{"name": "status", "type": "text"}, {"name": "분류", "type": "text"}],
        actor_id="tester", if_not_exists=if_not_exists,
    )

    header_pg = out["columns"][1]["pg_name"]
    assert _registry_insert(conn) == [
        {"name": "status", "type": "text"},
        {"name": "분류", "type": "text", "pg_name": header_pg},
    ]
    # Readers report both names regardless.
    assert out["columns"][0]["pg_name"] == "status"


async def test_gate6_an_alter_leaves_the_entries_it_does_not_touch_as_they_were(monkeypatch):
    legacy = [
        {"name": "status", "type": "text"},
        # Predates the column grammar: its identifier is `legacy_col`.
        {"name": "Legacy-Col", "type": "text"},
    ]
    conn = _alter_conn_for([dict(c) for c in legacy])
    _wire(monkeypatch, conn)

    await table_service.alter_table(
        uuid.uuid4(), _TABLE, actor_id="tester",
        add_columns=[{"name": "note", "type": "text"}],
    )

    assert _registry_update(conn) == [*legacy, {"name": "note", "type": "text"}]


async def test_gate6_pg_name_is_stored_only_while_a_name_needs_it(monkeypatch):
    conn = _alter_conn_for([{"name": "status", "type": "text"}])
    _wire(monkeypatch, conn)
    await table_service.alter_table(
        uuid.uuid4(), _TABLE, actor_id="tester", rename_columns={"status": "상태"},
    )
    renamed = _registry_update(conn)
    # A logical rename: the column stays `status`, which the name no longer says.
    assert renamed == [{"name": "상태", "type": "text", "pg_name": "status"}]
    assert not any("RENAME COLUMN" in sql for sql in conn.sql())

    conn = _alter_conn_for(renamed)
    _wire(monkeypatch, conn)
    await table_service.alter_table(
        uuid.uuid4(), _TABLE, actor_id="tester", rename_columns={"상태": "status"},
    )
    # Back to the name its physical name is: nothing left to record.
    assert _registry_update(conn) == [{"name": "status", "type": "text"}]


def test_gate6_readers_report_the_physical_name_a_sparse_row_implies():
    stored = json.dumps([
        {"name": "status", "type": "text"},
        {"name": "Legacy-Col", "type": "text"},
        {"name": "분류", "type": "text", "pg_name": "c_3_0badf00d"},
    ])
    physical = ["status", "legacy_col", "c_3_0badf00d"]

    assert [c["pg_name"] for c in table_registry_repo.parse_columns(stored)] == physical
    schema = table_schema_service._build_table_schema(
        _VAULT,
        {"name": _TABLE, "columns": stored, "unique_keys": [], "indexes": []},
        {"id": "uuid", **{pg: "text" for pg in physical}},
    )
    assert [c["pg_name"] for c in schema["columns"]] == physical
    assert schema["drift"]["has_drift"] is False


# ── The decisions the gates rest on ──────────────────────────────────────────
# (numbered as in the proposal's implementation notes)


@pytest.mark.parametrize("bad", [
    "", "   ", "\t", 7, None,
    "분\x00류",          # NUL separates the digest input
    "line\nbreak",       # C0 control
    "del\x7f",           # DEL
    "nel\x85",           # C1 control
    "\ud800x",           # lone surrogate: no UTF-8 form, so no digest input
    "가" * 256,
    "ID", "Created_At", "ROW_COMMIT",  # bookkeeping, in any case form
])
def test_d1_logical_names_that_are_refused(bad):
    with pytest.raises(table_service.ValidationError):
        _spec([{"name": bad, "type": "text"}])


def test_d1_logical_names_are_nfc_and_otherwise_verbatim():
    cols, _, _ = _spec([
        {"name": unicodedata.normalize("NFD", "중요도"), "type": "text"},
        {"name": " padded ", "type": "text"},
        {"name": "MixedCase", "type": "text"},
    ])
    assert [c["name"] for c in cols] == ["중요도", " padded ", "MixedCase"]
    # Not plain, so each gets a derived physical name.
    assert all(c["pg_name"].startswith("c_") for c in cols)


async def test_d2_a_caller_cannot_supply_pg_name(monkeypatch):
    with pytest.raises(table_service.ValidationError, match="pg_name"):
        _spec([{"name": "분류", "pg_name": "c_1_deadbeef", "type": "text"}])

    cols, _, _ = _spec([{"name": "분류", "type": "text"}])
    conn = _Conn(table_row={
        "id": uuid.uuid4(), "name": _TABLE, "columns": cols,
        "unique_keys": [], "indexes": [], "collection": None, "description": "",
    })
    _wire(monkeypatch, conn)
    with pytest.raises(table_service.ValidationError, match="pg_name"):
        await table_service.alter_table(
            uuid.uuid4(), _TABLE, actor_id="tester",
            add_columns=[{"name": "비고", "pg_name": "evil", "type": "text"}],
        )
    assert not any(s.startswith("ALTER TABLE") for s in conn.sql())


@pytest.mark.parametrize("pair", [
    ("Title", "title"),
    ("W Embedding", "w embedding"),
    (unicodedata.normalize("NFD", "분류"), "분류"),
])
def test_d3_logical_names_are_unique_by_nfc_casefold(pair):
    with pytest.raises(table_service.ValidationError, match="Duplicate column name"):
        _spec([{"name": pair[0], "type": "text"}, {"name": pair[1], "type": "text"}])


def test_d3_a_plain_name_equal_to_a_derived_one_is_refused():
    derived = table_data_repo.derive_column_pg_name(_TABLE_PG, "분류", 1)
    with pytest.raises(table_service.ValidationError, match="physical name"):
        _spec([{"name": "분류", "type": "text"}, {"name": derived, "type": "text"}])


async def test_d4_references_resolve_a_logical_target_to_its_physical_column(monkeypatch):
    target_cols, _, _ = _spec([{"name": "코드", "type": "text", "unique": True}], table="parents")

    async def fake_find_by_name(conn, vault_id, name):
        assert name == "parents"
        return {"columns": target_cols, "unique_keys": []}

    monkeypatch.setattr(table_service.table_registry_repo, "find_by_name", fake_find_by_name)
    source, _, _ = _spec([{
        "name": "부모 코드", "type": "text",
        "references": {"table": "parents", "column": unicodedata.normalize("NFD", "코드")},
    }])

    targets = await table_service._validate_column_references(object(), uuid.uuid4(), source)

    assert source[0]["references"] == {"table": "parents", "column": "코드"}
    assert targets == {source[0]["pg_name"]: target_cols[0]["pg_name"]}
    ddl = table_data_repo.foreign_key_constraint_definition(
        _TABLE_PG, source[0], vault_name=_VAULT, target_column=targets[source[0]["pg_name"]],
    )
    assert ddl.isascii(), ddl
    assert f"FOREIGN KEY ({source[0]['pg_name']}) REFERENCES vt_papers__parents ({target_cols[0]['pg_name']})" in ddl


def _alter_conn(columns):
    return _Conn(table_row={
        "id": uuid.uuid4(), "name": _TABLE, "columns": columns,
        "unique_keys": [], "indexes": [], "collection": None, "description": "",
    })


@pytest.mark.parametrize(("start", "new", "physical"), [
    # A column whose physical name is its logical name, renamed to a plain
    # name: the rename every table had before the split.
    ("status", "state", True),
    # ...to a header: the physical column stays.
    ("status", "상태", False),
    # A column whose physical name was derived: renaming it never moves it.
    ("상태", "status", False),
    # ...to a word PostgreSQL reserves, which cannot be a bare identifier.
    ("status", "order", False),
])
async def test_d7_rename_is_physical_only_for_a_plain_to_plain_column(monkeypatch, start, new, physical):
    cols, _, _ = _spec([
        {"name": start, "type": "enum", "enum": ["a", "b"]},
        {"name": "qty", "type": "int", "check": {"op": "gte", "value": 0}},
    ])
    before = cols[0]["pg_name"]
    conn = _alter_conn(cols)
    _wire(monkeypatch, conn)

    out = await table_service.alter_table(
        uuid.uuid4(), _TABLE, actor_id="tester", rename_columns={start: new},
    )

    col = out["columns"][0]
    assert col["name"] == new
    # Stored only while the name does not say the physical name: a logical
    # rename to a header, or from one to a plain name, records it.
    assert _registry_update(conn)[0].get("pg_name") == (None if physical else before)
    renames = [s for s in conn.sql() if "RENAME COLUMN" in s]
    if physical:
        assert renames == [f"ALTER TABLE {_TABLE_PG} RENAME COLUMN {before} TO {new}"]
        assert col["pg_name"] == new
        # The enum CHECK moves to the name derived from the new column.
        assert any(
            table_data_repo.enum_constraint_name(_TABLE_PG, new) in s for s in conn.sql()
        )
    else:
        assert renames == []
        assert col["pg_name"] == before
        assert not any(s.startswith("ALTER TABLE") for s in conn.sql())


async def test_d7_a_plain_target_held_physically_by_another_column_renames_logically(monkeypatch):
    cols, _, _ = _spec([{"name": "state", "type": "text"}, {"name": "status", "type": "text"}])
    # `state` was renamed logically earlier; its column is still `state`.
    cols[0]["name"] = "상태"
    conn = _alter_conn(cols)
    _wire(monkeypatch, conn)

    out = await table_service.alter_table(
        uuid.uuid4(), _TABLE, actor_id="tester", rename_columns={"status": "state"},
    )

    assert [(c["name"], c["pg_name"]) for c in out["columns"]] == [
        ("상태", "state"), ("state", "status"),
    ]
    assert not any("RENAME COLUMN" in s for s in conn.sql())


async def test_d7_renaming_a_column_to_the_name_it_already_has_is_a_no_op(monkeypatch):
    """`AGE` resolves to `age`, so a rename can name the column it already is;
    that must not reach RENAME COLUMN, which PostgreSQL refuses onto itself."""
    cols, _, _ = _spec([{"name": "age", "type": "int"}])
    conn = _alter_conn(cols)
    _wire(monkeypatch, conn)

    out = await table_service.alter_table(
        uuid.uuid4(), _TABLE, actor_id="tester", rename_columns={"AGE": "age"},
    )

    assert [(c["name"], c["pg_name"]) for c in out["columns"]] == [("age", "age")]
    assert not any(s.startswith("ALTER TABLE") for s in conn.sql())


async def test_d12_if_not_exists_ignores_pg_name_on_either_side(monkeypatch):
    legacy = [{"name": "title", "type": "text"}]  # stored without pg_name
    conn = _Conn(table_row={
        "id": uuid.uuid4(), "vault_id": uuid.uuid4(), "collection_id": None,
        "collection": None, "name": _TABLE, "description": "",
        "columns": legacy, "unique_keys": [], "indexes": [],
        "created_by": "someone", "created_at": None, "updated_at": None,
    })
    _wire(monkeypatch, conn)
    out = await table_service.create_table(
        uuid.uuid4(), _TABLE, [{"name": "title", "type": "text"}], actor_id="tester",
        if_not_exists=True, can_read_existing=True,
    )
    assert out["matches_request"] is True, out["mismatches"]


async def test_d4_every_boundary_resolves_by_nfc_casefold(monkeypatch):
    decomposed = unicodedata.normalize("NFD", "분류")
    cols, uks, idxs = _spec(
        _HEADERS,
        unique_keys=[{"columns": [decomposed, "W EMBEDDING"]}],
        indexes=[{"columns": [{"name": "triviaqa(비과학 문헌)", "order": "desc"}]}],
    )
    assert uks[-1]["columns"] == ["분류", "w Embedding"]
    assert idxs[-1]["columns"] == [{"name": "TriviaQA(비과학 문헌)", "order": "desc"}]
    phys = _physical(cols)

    read = compile_row_query(
        vault_name=_VAULT, table_name=_TABLE, columns=cols,
        query_params=[("select", f"{decomposed},W EMBEDDING"), ("W EMBEDDING", "eq.x")],
    )
    assert "error" not in read, read
    assert read["sql"].startswith(f"SELECT {phys['분류']}, {phys['w Embedding']} FROM")
    body, _ = _shape_result(
        {"items": [{phys["분류"]: "A", phys["w Embedding"]: "x"}], "columns": []},
        vault_name=_VAULT, table_name=_TABLE,
        projections=read["projections"], count_exact=False, offset=0,
    )
    # Reported under the declared spelling, however the caller wrote it.
    assert body["items"] == [{"분류": "A", "w Embedding": "x"}]

    upsert = compile_insert_rows(
        vault_name=_VAULT, table_name=_TABLE, columns=cols, actor_id="alice",
        unique_keys=uks, body={decomposed: "A", "w embedding": "x"},
        query_params=[("on_conflict", f"{decomposed},W EMBEDDING")],
    )
    assert not isinstance(upsert, dict), upsert
    assert f"ON CONFLICT ({phys['분류']}, {phys['w Embedding']})" in upsert.sql

    # alter: drop/alter/rename resolve the same way.
    conn = _Conn(table_row={
        "id": uuid.uuid4(), "name": _TABLE, "columns": cols,
        "unique_keys": [], "indexes": [], "collection": None, "description": "",
    })
    _wire(monkeypatch, conn)
    await table_service.alter_table(
        uuid.uuid4(), _TABLE, actor_id="tester",
        alter_columns=[{"name": "중요도".upper(), "set_default": 1}],
        drop_columns=["W EMBEDDING"],
        rename_columns={decomposed: "대분류"},
    )
    stored = _registry_update(conn)
    assert [c["name"] for c in stored] == ["대분류", "중요도", "TriviaQA(비과학 문헌)"]
    assert f"ALTER COLUMN {phys['중요도']} SET DEFAULT 1" in " ".join(conn.sql())


def test_d8_one_row_naming_one_column_twice_is_refused():
    cols, _, _ = _spec(_HEADERS)
    decomposed = unicodedata.normalize("NFD", "분류")
    insert = compile_insert_rows(
        vault_name=_VAULT, table_name=_TABLE, columns=cols, actor_id="alice",
        body={"분류": "A", decomposed: "B"},
    )
    assert isinstance(insert, dict) and insert["code"] == "invalid_argument"
    update = compile_update_rows(
        vault_name=_VAULT, table_name=_TABLE, columns=cols,
        body={"w Embedding": "a", "W EMBEDDING": "b"}, query_params=[("all", "true")],
    )
    assert isinstance(update, dict) and update["code"] == "invalid_argument"


def test_d8_a_header_with_a_comma_is_selectable_through_the_ast():
    cols, _, _ = _spec([{"name": "Revenue, 2023", "type": "numeric"}])
    ast = compile_ast_row_query(
        vault_name=_VAULT, table_name=_TABLE, columns=cols,
        ast={"select": ["Revenue, 2023"], "order": [{"col": "Revenue, 2023", "dir": "desc"}]},
    )
    assert "error" not in ast, ast
    assert [p.output_key for p in ast["projections"]] == ["Revenue, 2023"]


def test_d4_a_crafted_operand_is_refused_in_bounded_time():
    """A logical name can contain `->>`, so no grammar can say where it ends;
    the split is asked of the table. An operand built to make a regex
    backtrack must cost a bounded scan, not a quadratic one: against
    `^(.+?)(->>…)?(::…)?$` this input takes 5 s at 8,000 arrows and grows
    with the square."""
    import time

    cols, _, _ = _spec([{"name": "메타", "type": "jsonb"}])
    hostile = "->>a" * 60_000 + "::X"
    started = time.monotonic()
    out = compile_ast_row_query(
        vault_name=_VAULT, table_name=_TABLE, columns=cols,
        ast={"filter": {"col": hostile, "op": "eq", "val": "x"}},
    )
    assert time.monotonic() - started < 2
    assert out["code"] == "undefined_column"

    fine = compile_row_query(
        vault_name=_VAULT, table_name=_TABLE, columns=cols,
        query_params=[("메타->>tier", "eq.gold"), ("메타->>a:b", "eq.x")],
    )
    assert fine["code"] == "undefined_column"
    assert "메타->>a:b" in fine["error"]


def test_d4_a_header_with_edge_spaces_is_addressable_as_written():
    """Logical names are verbatim, surrounding spaces included, so the row
    API tries the name as sent before trimming it."""
    cols, _, _ = _spec([{"name": " 비고 ", "type": "text"}, {"name": "비고", "type": "text"}])
    padded, plain = cols[0]["pg_name"], cols[1]["pg_name"]
    ast = compile_ast_row_query(
        vault_name=_VAULT, table_name=_TABLE, columns=cols,
        ast={"select": [" 비고 "], "filter": {"col": " 비고 ", "op": "eq", "val": "x"}},
    )
    assert "error" not in ast, ast
    assert ast["sql"].startswith(f"SELECT {padded} FROM") and f"WHERE {padded} = $1" in ast["sql"]
    read = compile_row_query(
        vault_name=_VAULT, table_name=_TABLE, columns=cols,
        query_params=[("select", "비고"), ("비고", "eq.x")],
    )
    assert read["sql"].startswith(f"SELECT {plain} FROM"), read


# ── Drops and renames resolve a logical name, and only once ──────────────────


@pytest.mark.parametrize("scenario", ["derived", "renamed", "unregistered"])
async def test_a_drop_never_reaches_a_column_by_its_physical_name(monkeypatch, scenario):
    """A plain name can be another column's PHYSICAL name — the `c_1_…` a
    header was given, or `age` after `age` was renamed to `나이`. A drop by
    that name must not destroy the column while the registry keeps it, so a
    drop names a declared column or is refused, as rename and alter are."""
    cols, _, _ = _spec([{"name": "분류", "type": "text"}, {"name": "age", "type": "int"}])
    if scenario == "renamed":
        cols[1]["name"] = "나이"  # renamed logically: the column is still `age`
    name = {"derived": cols[0]["pg_name"], "renamed": "age", "unregistered": "ghost"}[scenario]
    conn = _alter_conn(cols)
    _wire(monkeypatch, conn)

    with pytest.raises(table_service.ValidationError, match="Cannot drop missing column"):
        await table_service.alter_table(
            uuid.uuid4(), _TABLE, actor_id="tester", drop_columns=[name],
        )

    assert not any("DROP COLUMN" in s for s in conn.sql())
    assert not any(s.startswith("UPDATE vault_tables") for s in conn.sql())


@pytest.mark.parametrize("renames", [
    {"age": "x", "AGE": "y"},
    {"분류": "a", unicodedata.normalize("NFD", "분류"): "b"},
])
async def test_a_rename_map_naming_one_column_twice_is_refused(monkeypatch, renames):
    """Two keys that resolve to one column would rename it twice and leave its
    unique key and index at the intermediate name — or, once NFC makes them
    one key, silently drop one of the two requests."""
    cols, uks, idxs = _spec(
        [{"name": "age", "type": "int", "unique": True}, {"name": "분류", "type": "text"}],
        indexes=[{"columns": ["age"]}],
    )
    conn = _Conn(table_row={
        "id": uuid.uuid4(), "name": _TABLE, "columns": cols,
        "unique_keys": uks, "indexes": idxs, "collection": None, "description": "",
    })
    _wire(monkeypatch, conn)

    with pytest.raises(table_service.ValidationError, match="more than once"):
        await table_service.alter_table(
            uuid.uuid4(), _TABLE, actor_id="tester", rename_columns=renames,
        )

    assert not any(s.startswith("ALTER TABLE") for s in conn.sql())
    assert not any(s.startswith("UPDATE vault_tables") for s in conn.sql())


# ── A header spelled like a control parameter ────────────────────────────────

# What the web UI sends for every table it lists.
_LISTING = [("limit", "50"), ("offset", "0"), ("order", "created_at.desc,id.desc")]
_ROW = "00000000-0000-0000-0000-000000000001"


def _declared(*columns) -> list[dict]:
    """Registry entries, each with a physical name its logical name is not."""
    out = []
    for ordinal, col in enumerate(columns, start=1):
        col = {"type": "text", **col} if isinstance(col, dict) else {"name": col, "type": "text"}
        digest = hashlib.sha1(col["name"].encode()).hexdigest()[:8]
        out.append({**col, "pg_name": f"c_{ordinal}_{digest}"})
    return out


@pytest.mark.parametrize("headers", [
    ["Order", "Limit", "Offset", "Select"],
    # Lowercase too: PostgreSQL reserves all four, and they are headers.
    ["order", "limit", "offset", "select"],
])
def test_a_header_spelled_like_a_read_control_leaves_the_control_working(headers):
    order, limit, _offset, select = headers
    cols = _declared(*headers)
    phys = _physical(cols)

    listing = compile_row_query(
        vault_name=_VAULT, table_name=_TABLE, columns=cols, query_params=_LISTING,
    )
    assert listing.get("sql") == (
        f"SELECT * FROM {_TABLE_PG} ORDER BY created_at DESC, id DESC LIMIT 50 OFFSET 0"
    ), listing
    picked = compile_row_query(
        vault_name=_VAULT, table_name=_TABLE, columns=cols,
        query_params=[("select", select), ("order", f"{order}.desc"), ("limit", "5")],
    )
    assert picked.get("sql") == (
        f"SELECT {phys[select]} FROM {_TABLE_PG} ORDER BY {phys[order]} DESC LIMIT 5 OFFSET 0"
    ), picked
    # A filter on the header is a filter, however it is spelled — and only a
    # filter: the control keeps its default.
    for key in (limit, limit.lower(), limit.upper()):
        filtered = compile_row_query(
            vault_name=_VAULT, table_name=_TABLE, columns=cols, query_params=[(key, "eq.x")],
        )
        assert filtered.get("sql") == (
            f"SELECT * FROM {_TABLE_PG} WHERE ({phys[limit]} = $1) LIMIT 100 OFFSET 0"
        ), (key, filtered)


@pytest.mark.parametrize("headers", [
    ["Select", "All", "Expected_Row_Commit"],
    ["select", "all", "expected_row_commit"],
])
def test_a_header_spelled_like_a_write_control_leaves_the_control_working(headers):
    cols = _declared(*headers, "분류")
    phys = _physical(cols)

    update = compile_update_rows(
        vault_name=_VAULT, table_name=_TABLE, columns=cols, body={"분류": "y"},
        query_params=[("id", f"eq.{_ROW}"), ("select", "*"), ("expected_row_commit", "tok")],
        prefer_header="return=representation",
    )
    assert not isinstance(update, dict), update
    assert update.sql == (
        f"UPDATE {_TABLE_PG} SET {phys['분류']} = $1, updated_at = NOW() "
        f"WHERE ((id = $2)) AND row_commit = $3 RETURNING *"
    )
    everything = compile_delete_rows(
        vault_name=_VAULT, table_name=_TABLE, columns=cols, query_params=[("all", "true")],
    )
    assert not isinstance(everything, dict), everything
    assert everything.sql == f"DELETE FROM {_TABLE_PG} WHERE TRUE"


def test_a_filter_on_a_header_spelled_like_a_control_is_never_dropped():
    """8d04a2aa: an UPDATE/DELETE that looks filtered must never quietly
    become broader. Names resolve case-insensitively (decision 4), so
    `count=eq.0` is a filter on a `Count` header — reading only an exact
    spelling as the column would drop it and delete every `source=test` row."""
    cols = _declared({"name": "Count", "type": "int"}, "Order", "source")
    phys = _physical(cols)

    delete = compile_delete_rows(
        vault_name=_VAULT, table_name=_TABLE, columns=cols,
        query_params=[("count", "eq.0"), ("source", "eq.test")],
    )
    assert not isinstance(delete, dict), delete
    assert delete.sql == (
        f"DELETE FROM {_TABLE_PG} WHERE ({phys['Count']} = $1) AND ({phys['source']} = $2)"
    )
    assert delete.params == [0, "test"]
    update = compile_update_rows(
        vault_name=_VAULT, table_name=_TABLE, columns=cols, body={"source": "x"},
        query_params=[("order", "eq.first")],
    )
    assert not isinstance(update, dict), update
    assert update.sql.endswith(f"WHERE ({phys['Order']} = $2)")
    # A mutation reads no `count`: a key naming a column is a filter, and a
    # malformed one is refused rather than ignored.
    refused = compile_delete_rows(
        vault_name=_VAULT, table_name=_TABLE, columns=cols,
        query_params=[("count", "exact"), ("source", "eq.test")],
    )
    assert isinstance(refused, dict) and refused["code"] == "invalid_filter", refused


# ── Words PostgreSQL reserves ────────────────────────────────────────────────


async def test_a_reserved_word_gets_a_derived_physical_name(monkeypatch):
    """`user`, `order` and `group` fit the plain grammar, but PostgreSQL
    refuses them as bare identifiers — `CREATE TABLE … (user TEXT)` is a
    syntax error — so they are not plain. Keywords it accepts as column
    names (`name`, `type`, `value`) still are."""
    conn = _Conn()
    _wire(monkeypatch, conn)
    words = ["user", "order", "group", "name", "type", "value"]

    out = await table_service.create_table(
        uuid.uuid4(), _TABLE, [{"name": w, "type": "text"} for w in words], actor_id="tester",
    )

    phys = _physical(out["columns"])
    assert [w for w in words if phys[w] != w] == ["user", "order", "group"]
    ddl = next(s for s in conn.sql() if s.startswith("CREATE TABLE"))
    for word in ("user", "order", "group"):
        assert phys[word].startswith("c_") and f" {word} " not in ddl, ddl
    assert _registry_insert(conn) == [
        {"name": w, "type": "text", "pg_name": phys[w]} for w in ("user", "order", "group")
    ] + [{"name": w, "type": "text"} for w in ("name", "type", "value")]


# ── App rollout addresses what the registry says ─────────────────────────────


class _RegistryConn(_Conn):
    """A connection the rollout worker and the Native browse can run against:
    `tables` is the registry by table name; `null_ids` the rows a backfill
    batch finds."""

    def __init__(self, tables, *, null_ids=()):
        super().__init__()
        self.tables = tables
        self.null_ids = list(null_ids)

    async def fetchrow(self, sql, *params):
        self.sent.append((sql, params))
        if "FROM vault_tables" in sql:
            return self.tables.get(params[1])
        if "FROM vaults" in sql:
            return {"name": _VAULT}
        return None

    async def fetchval(self, sql, *params):
        self.sent.append((sql, params))
        if "FROM vaults" in sql:
            return _VAULT
        if "app_owned_resources" in sql:
            return True
        return 0

    async def fetch(self, sql, *params):
        self.sent.append((sql, params))
        if "ORDER BY id LIMIT" in sql:
            rows, self.null_ids = [{"id": i} for i in self.null_ids], []
            return rows
        return []


def _registry_row(name, columns):
    return {
        "id": uuid.uuid4(), "name": name, "columns": columns,
        "unique_keys": [], "indexes": [], "collection": None, "description": "",
    }


def _rollout_target():
    return {"vault_id": uuid.uuid4(), "installation_id": uuid.uuid4(), "app_id": uuid.uuid4()}


def _no_grants(monkeypatch):
    class _RoleSync:
        async def grant_table_in_conn(self, *_a, **_k):
            return None

    monkeypatch.setattr(app_rollout_worker, "get_role_sync", lambda: _RoleSync())


async def test_rollout_steps_address_the_physical_name_the_registry_holds(monkeypatch):
    """Logical renames before adoption can leave each column's physical name
    another column's name — `z` on column `a`, `a` on `c_2_…` — and the
    fingerprint, which ignores `pg_name`, still matches a plain {a, z}
    manifest. A step's SQL must reach `a` through the registry."""
    shuffled = [
        {"name": "z", "type": "text", "pg_name": "a"},
        {"name": "a", "type": "text", "pg_name": "c_2_6530ef71"},
    ]
    orders_pg = table_data_repo.pg_table_name(_VAULT, "orders")
    conn = _RegistryConn({"orders": _registry_row("orders", shuffled)}, null_ids=[uuid.UUID(int=1)])
    target = _rollout_target()
    step = {"id": uuid.uuid4(), "step_id": "backfill_a", "checkpoint": {}}

    await app_rollout_worker._run_backfill(conn, target, step, {
        "table": "orders", "column": "a", "primary_key": "id",
        "batch_size": 10, "where_null": True, "value": "ready",
    })

    touched = [s for s in conn.sql() if orders_pg in s]
    assert len(touched) == 2 and all("c_2_6530ef71 IS NULL" in s for s in touched), touched
    assert touched[1].startswith(f"UPDATE {orders_pg} SET c_2_6530ef71=$1"), touched

    altered: list[dict] = []

    async def _alter_table(*_args, **kwargs):
        altered.append(kwargs)

    monkeypatch.setattr(app_rollout_worker, "alter_table", _alter_table)
    conn.sent.clear()
    await app_rollout_worker._execute_step(conn, target, step, {
        "operation": "set_not_null", "payload": {"table": "orders", "column": "a"},
    })

    assert [s for s in conn.sql() if orders_pg in s] == [
        f"SELECT COUNT(*) FROM {orders_pg} WHERE c_2_6530ef71 IS NULL",
    ]
    assert altered[0]["alter_columns"] == [{"name": "a", "set_not_null": True}]


def test_manifest_references_keep_the_plain_column_grammar():
    """Decision 13: a manifest names columns in the plain grammar, and a
    reference's `column` is a column name like any other."""
    column = {
        "name": "parent", "type": "text",
        "references": {"table": "parents", "column": "코드"},
    }
    with pytest.raises(table_service.ValidationError):
        app_rollout_service._normalize_column(dict(column))
    with pytest.raises(table_service.ValidationError):
        app_resource_service.canonical_table_descriptor(
            {"name": "children", "columns": [column], "unique_keys": [], "indexes": []},
        )
    plain = {**column, "references": {"table": "parents", "column": "code"}}
    assert app_rollout_service._normalize_column(plain)["references"]["column"] == "code"


async def test_rollout_create_references_the_physical_column_the_registry_holds(monkeypatch):
    """A referenced column's physical name is known only to its table's
    registry row — `code`, renamed from a header, is still `c_1_…`."""
    parents = [{"name": "code", "type": "text", "unique": True, "pg_name": "c_1_0badf00d"}]
    conn = _RegistryConn({"parents": _registry_row("parents", parents)})
    _no_grants(monkeypatch)
    column = app_rollout_service._normalize_column({
        "name": "parent", "type": "text",
        "references": {"table": "parents", "column": "code"},
    })

    await app_rollout_worker._create_table_owned(conn, _rollout_target(), {
        "table": "children", "columns": [column], "unique_keys": [], "indexes": [],
    })

    ddl = next(s for s in conn.sql() if s.startswith("CREATE TABLE"))
    parents_pg = table_data_repo.pg_table_name(_VAULT, "parents")
    assert f"REFERENCES {parents_pg} (c_1_0badf00d)" in ddl, ddl
    assert _registry_insert(conn)[0]["references"] == {"table": "parents", "column": "code"}


async def test_rollout_create_gives_a_reserved_word_a_derived_physical_name(monkeypatch):
    conn = _RegistryConn({})
    _no_grants(monkeypatch)
    columns = [
        app_rollout_service._normalize_column({"name": "order", "type": "text"}),
        app_rollout_service._normalize_column({"name": "flag", "type": "text"}),
    ]

    await app_rollout_worker._create_table_owned(conn, _rollout_target(), {
        "table": "orders", "columns": columns, "unique_keys": [], "indexes": [],
    })

    order_pg = table_data_repo.derive_column_pg_name(
        table_data_repo.pg_table_name(_VAULT, "orders"), "order", 1,
    )
    assert order_pg.startswith("c_1_")
    ddl = next(s for s in conn.sql() if s.startswith("CREATE TABLE"))
    assert f" {order_pg} TEXT" in ddl and " order " not in ddl, ddl
    assert _registry_insert(conn) == [
        {"name": "order", "type": "text", "pg_name": order_pg},
        {"name": "flag", "type": "text"},
    ]


# ── Every read surface reports both names ────────────────────────────────────


async def test_native_browse_reports_the_physical_name_a_sparse_row_implies(monkeypatch):
    stored = [
        {"name": "status", "type": "text"},
        {"name": "분류", "type": "text", "pg_name": "c_2_0badf00d"},
    ]

    async def _list_for_vault(_conn, _vault_id, **_kwargs):
        return [{
            "name": _TABLE, "columns": json.dumps(stored), "description": "",
            "collection": None, "created_at": None,
        }]

    monkeypatch.setattr(table_registry_repo, "list_for_vault", _list_for_vault)
    service = NativeDocumentService(pool=_Pool(_RegistryConn({})))

    items = await service._browse_legacy_tables(_VAULT, uuid.uuid4(), prefix="", max_depth=-1)

    assert [(c["name"], c.get("pg_name")) for c in items[0].columns] == [
        ("status", "status"), ("분류", "c_2_0badf00d"),
    ]


# ── Which tables code from before #433 can use ───────────────────────────────


async def test_older_code_needs_plain_names_and_no_stored_pg_name(monkeypatch):
    """Code from before #433 addresses a column as `safe_ident(name)` and
    accepts only plain names. Neither half of that implies the other."""
    # A header whose legacy identifier IS its physical name stores nothing...
    for old, new in (("age", "Age"), ("a_b", "A B")):
        conn = _alter_conn_for([{"name": old, "type": "text"}])
        _wire(monkeypatch, conn)
        await table_service.alter_table(
            uuid.uuid4(), _TABLE, actor_id="tester", rename_columns={old: new},
        )
        assert _registry_update(conn) == [{"name": new, "type": "text"}]
        assert not table_data_repo.is_plain_column_name(new)

    # ...and plain names store one once logical renames move them.
    columns, _, _ = _spec([{"name": "a", "type": "text"}, {"name": "비고", "type": "text"}])
    for renames in ({"a": "에이"}, {"에이": "z"}, {"비고": "a"}):
        conn = _alter_conn_for(table_registry_repo.storable_columns(columns))
        _wire(monkeypatch, conn)
        await table_service.alter_table(
            uuid.uuid4(), _TABLE, actor_id="tester", rename_columns=renames,
        )
        columns = _registry_update(conn)
    assert [c["name"] for c in columns] == ["z", "a"]
    assert all(table_data_repo.is_plain_column_name(c["name"]) and "pg_name" in c for c in columns)

    readme = (
        Path(__file__).resolve().parents[2] / "docs" / "design" / "proposal"
        / "2026-09-17-column-logical-physical-names" / "README.md"
    ).read_text()
    section = readme.split("### Rolling deploy and rollback", 1)[1].split("\n### ", 1)[0]
    text = " ".join(section.replace("`", "").split())
    assert "while every column name is plain and no column stores pg_name" in text, text
    assert "whether or not it stores pg_name" in text, text
    assert "exactly when no column stores pg_name" not in text, text
