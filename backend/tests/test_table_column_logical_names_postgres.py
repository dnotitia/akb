"""Acceptance gates for the column logical/physical name split (#433) — live PG.

Design: docs/design/proposal/2026-09-17-column-logical-physical-names/
("Acceptance gates"). The DB-free half is
`test_table_column_logical_names_unit.py`.

Live PostgreSQL, because every claim here is about what the database holds:
the physical columns `pg_attribute` reports, the columns a UNIQUE constraint
and an index are really built on, what a reader's own role may SELECT through
`akb_sql` before and after its grants are torn down and reconciled, and an
existing table whose registry row must not change by a byte when this code
boots over it and uses it. A fake connection would be asserting the test's own
model.

Written before the implementation and red against 2ed799bf, where every gate
is refused by `_COLUMN_NAME_RE`. Gate 6 was rewritten for sparse `pg_name`
storage and was red against the eager implementation that preceded it.

Skips when PostgreSQL is unreachable unless REQUIRE_REAL_PG=1, which the
live-PG CI lane sets — a gate that green-skips there is not a gate.
"""

from __future__ import annotations

import json
import os
import re
import uuid
from contextlib import asynccontextmanager
from pathlib import Path

import asyncpg
import pytest

from app.db import postgres
from app.exceptions import ValidationError
from app.repositories import table_data_repo
from app.services import (
    access_service,
    table_row_query,
    table_row_write,
    table_schema_service,
    table_service,
    user_sql_executor,
)
from app.services.document_service import DocumentService
from app.services.native_document_service import NativeDocumentService
from app.services.role_sync import RoleSync, user_role_name, vault_group_role_name

pytestmark = pytest.mark.asyncio

_BACKEND = Path(__file__).resolve().parents[1]
_INIT_SQL = (_BACKEND / "app" / "db" / "init.sql").read_text()
# The last migration the registry held before #433. A database booted through
# it is one this code has never touched.
_BEFORE_433 = "112_edges_resource_identity.py"
_DSN = os.environ.get(
    "AKB_TEST_DSN",
    "postgresql://akb:akb@localhost:15432/akb",  # pragma: allowlist secret
)

# Headers taken from documents (#433): Korean, parentheses, spaces, capitals.
_HEADERS = [
    {"name": "분류", "type": "text"},
    {"name": "중요도", "type": "int"},
    {"name": "TriviaQA(비과학 문헌)", "type": "numeric"},
    {"name": "w Embedding", "type": "text"},
]
_BOOKKEEPING = {"id", "created_by", "created_at", "updated_at", "row_commit"}


def _database_dsn(name: str) -> str:
    base, _ = _DSN.rsplit("/", 1)
    return f"{base}/{name}"


def _registered_migrations() -> list[str]:
    """Every migration, in the order `postgres.py` registers them."""
    registry = (_BACKEND / "app" / "db" / "postgres.py").read_text()
    ordered: list[str] = []
    for name in re.findall(r'"(\d{3}_[a-z0-9_]+\.py)"', registry):
        if name not in ordered:
            ordered.append(name)
    return ordered


async def _boot_through(conn, last: str) -> None:
    """Shape the database as a release whose migration registry ended at
    `last` left it: init.sql, then each migration through `last`, recorded in
    the ledger so a later boot applies only what came after."""
    await conn.execute(_INIT_SQL)
    for filename in _registered_migrations():
        module = postgres._load_migration(filename)
        if module is not None:
            await postgres._run_one_migration(conn, filename, module)
        if filename == last:
            return
    raise AssertionError(f"{last} is not in the migration registry")


async def _can_connect() -> bool:
    try:
        conn = await asyncpg.connect(_DSN, timeout=2)
    except (OSError, asyncpg.PostgresError):
        return False
    await conn.close()
    return True


@asynccontextmanager
async def _fresh_database(*, through: str | None = None):
    if not await _can_connect():
        if os.environ.get("REQUIRE_REAL_PG") == "1":
            pytest.fail(f"Required PostgreSQL is not reachable at {_DSN}")
        pytest.skip(f"Postgres not reachable at {_DSN}")
    admin = await asyncpg.connect(_DSN)
    name = f"akb_colnames_{uuid.uuid4().hex[:12]}"
    await admin.execute(f'CREATE DATABASE "{name}"')
    pool = await asyncpg.create_pool(_database_dsn(name), min_size=1, max_size=8)
    roles: list[str] = []
    try:
        # The boot path itself — init.sql, then every registered migration
        # through the ledger — so this database is shaped like a real one.
        async with pool.acquire() as conn:
            if through is None:
                await postgres._run_boot_schema(conn, init_sql=_INIT_SQL)
            else:
                await _boot_through(conn, through)
        yield pool, roles
    finally:
        await pool.close()
        await admin.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
        # Roles are cluster-global; the database going away does not take
        # them with it.
        for role in roles:
            try:
                await admin.execute(f'DROP ROLE IF EXISTS "{role}"')
            except asyncpg.PostgresError:
                pass
        await admin.close()


class _Live:
    def __init__(self, pool, role_sync: RoleSync, roles: list[str]):
        self.pool = pool
        self.role_sync = role_sync
        self._roles = roles

    async def _user(self, label: str) -> uuid.UUID:
        uid = await self.pool.fetchval(
            "INSERT INTO users (username, email, password_hash) "
            "VALUES ($1, $2, 'fixture') RETURNING id",
            label, f"{label}@example.invalid",
        )
        self._roles.append(user_role_name(uid))
        await self.role_sync.on_user_create(uid)
        return uid

    async def vault(self, label: str) -> tuple[uuid.UUID, str, uuid.UUID]:
        suffix = uuid.uuid4().hex[:8]
        owner = await self._user(f"{label}-owner-{suffix}")
        name = f"{label}-{suffix}"
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                vault_id = await conn.fetchval(
                    "INSERT INTO vaults (name, git_path, owner_id) "
                    "VALUES ($1, $2, $3) RETURNING id",
                    name, f"/tmp/{name}.git", owner,
                )
                await self.role_sync.on_vault_create_in_conn(
                    conn, vault_id, owner_user_id=owner,
                )
        self._roles.extend(
            vault_group_role_name(vault_id, scope) for scope in ("reader", "writer", "admin")
        )
        return vault_id, name, owner

    async def member(self, vault_id: uuid.UUID, role: str) -> uuid.UUID:
        uid = await self._user(f"member-{uuid.uuid4().hex[:8]}")
        await self.pool.execute(
            "INSERT INTO vault_access (vault_id, user_id, role) VALUES ($1, $2, $3)",
            vault_id, uid, role,
        )
        await self.role_sync.on_grant(vault_id, uid, role)
        return uid

    async def attnames(self, table_pg: str) -> list[str]:
        rows = await self.pool.fetch(
            """
            SELECT attname FROM pg_attribute
             WHERE attrelid = to_regclass($1) AND attnum > 0 AND NOT attisdropped
             ORDER BY attnum
            """,
            f"public.{table_pg}",
        )
        return [r["attname"] for r in rows]

    async def stored_columns(self, vault_id: uuid.UUID, table: str) -> str:
        """The registry's `columns` exactly as stored: the jsonb text."""
        return await self.pool.fetchval(
            "SELECT columns::text FROM vault_tables WHERE vault_id = $1 AND name = $2",
            vault_id, table,
        )

    async def stored_entries(self, vault_id: uuid.UUID, table: str) -> list[str]:
        """Each stored column entry's jsonb text, in order."""
        rows = await self.pool.fetch(
            """
            SELECT e.value::text AS entry
              FROM vault_tables t, jsonb_array_elements(t.columns) WITH ORDINALITY AS e(value, n)
             WHERE t.vault_id = $1 AND t.name = $2
             ORDER BY e.n
            """,
            vault_id, table,
        )
        return [r["entry"] for r in rows]


@asynccontextmanager
async def _live(monkeypatch, **fresh):
    async with _fresh_database(**fresh) as (pool, roles):
        role_sync = RoleSync(pool)
        # Every service reaches the database through `get_pool()`; pinning the
        # module pool points all of them — table, row, schema, access, browse —
        # at this throwaway database.
        monkeypatch.setattr(postgres, "_pool", pool)
        monkeypatch.setattr(table_service, "get_role_sync", lambda: role_sync)
        monkeypatch.setattr(
            user_sql_executor, "_executor", user_sql_executor.UserSqlExecutor(pool),
        )
        yield _Live(pool, role_sync, roles)


@pytest.fixture
async def live(monkeypatch):
    async with _live(monkeypatch) as env:
        yield env


@pytest.fixture
async def live_before_433(monkeypatch):
    """A database as the last release before #433 left it."""
    async with _live(monkeypatch, through=_BEFORE_433) as env:
        yield env


def _items(result) -> list[dict]:
    assert not isinstance(result, dict), result
    assert result.body is not None, result
    return result.body["items"]


async def _read(vault, vault_id, table, user, *, is_admin=True, **kwargs):
    return await table_row_query.select_rows(
        vault_name=vault, vault_id=vault_id, table_name=table,
        user_id=user, actor_id="tester", is_admin=is_admin, **kwargs,
    )


# ── Gate 1: a Korean-header table lives a whole life ─────────────────────────


async def test_gate1_korean_header_table_creates_inserts_alters_drops(live):
    vault_id, vault, owner = await live.vault("gate1")
    names = [c["name"] for c in _HEADERS]

    created = await table_service.create_table(
        vault_id, "results", [dict(c) for c in _HEADERS], actor_id="owner",
        description="parsed from a paper",
    )
    assert [c["name"] for c in created["columns"]] == names
    phys = {c["name"]: c["pg_name"] for c in created["columns"]}
    table_pg = table_data_repo.pg_table_name(vault, "results")
    assert await live.attnames(table_pg) == [
        "id", *phys.values(), "created_by", "created_at", "updated_at", "row_commit",
    ]

    inserted = await table_row_write.insert_rows(
        vault_name=vault, vault_id=vault_id, table_name="results",
        user_id=owner, actor_id="owner", is_admin=True,
        body=[
            {"분류": "A", "중요도": 3, "TriviaQA(비과학 문헌)": 0.5, "w Embedding": "x"},
            {"분류": "B", "중요도": 1},
        ],
        prefer_header="return=representation",
    )
    rows = _items(inserted)
    assert [r["분류"] for r in rows] == ["A", "B"]
    assert set(names) <= set(inserted.body["columns"])
    assert not set(phys.values()) & set(inserted.body["columns"])

    selected = await _read(
        vault, vault_id, "results", owner,
        query_params=[("select", "분류,중요도"), ("중요도", "gte.2"), ("order", "중요도.desc")],
    )
    assert _items(selected) == [{"분류": "A", "중요도": 3}]
    assert selected.body["columns"] == ["분류", "중요도"]

    queried = await table_row_query.query_rows(
        vault_name=vault, vault_id=vault_id, table_name="results",
        user_id=owner, actor_id="owner", is_admin=True,
        ast={"select": ["w Embedding"], "filter": {"col": "분류", "op": "eq", "val": "A"}},
    )
    assert _items(queried) == [{"w Embedding": "x"}]

    updated = await table_row_write.update_rows(
        vault_name=vault, vault_id=vault_id, table_name="results",
        user_id=owner, actor_id="owner", is_admin=True,
        body={"중요도": 5}, query_params=[("분류", "eq.B"), ("select", "분류,중요도")],
        prefer_header="return=representation",
    )
    assert _items(updated) == [{"분류": "B", "중요도": 5}]

    altered = await table_service.alter_table(
        vault_id, "results", actor_id="owner",
        add_columns=[{"name": "비고", "type": "text", "default": "없음"}],
        rename_columns={"w Embedding": "W Embedding"},
        alter_columns=[{"name": "중요도", "set_default": 0, "set_not_null": True}],
    )
    assert [c["name"] for c in altered["columns"]] == [
        "분류", "중요도", "TriviaQA(비과학 문헌)", "W Embedding", "비고",
    ]
    renamed = next(c for c in altered["columns"] if c["name"] == "W Embedding")
    assert renamed["pg_name"] == phys["w Embedding"]
    note_pg = next(c["pg_name"] for c in altered["columns"] if c["name"] == "비고")

    await table_row_write.insert_rows(
        vault_name=vault, vault_id=vault_id, table_name="results",
        user_id=owner, actor_id="owner", is_admin=True, body={"분류": "C"},
    )
    third = await _read(
        vault, vault_id, "results", owner,
        query_params=[("분류", "eq.C"), ("select", "중요도,비고,W Embedding")],
    )
    assert _items(third) == [{"중요도": 0, "비고": "없음", "W Embedding": None}]

    await table_service.alter_table(vault_id, "results", actor_id="owner", drop_columns=["비고"])
    assert note_pg not in await live.attnames(table_pg)

    deleted = await table_row_write.delete_rows(
        vault_name=vault, vault_id=vault_id, table_name="results",
        user_id=owner, actor_id="owner", is_admin=True,
        query_params=[("분류", "eq.C")],
    )
    assert deleted.content_range == "*/1"

    # Every read surface hands back the headers exactly as the document had them.
    final = ["분류", "중요도", "TriviaQA(비과학 문헌)", "W Embedding"]
    listed = await table_service.list_tables(vault_id)
    assert [c["name"] for c in listed[0]["columns"]] == final
    schema = await table_schema_service.get_table_schema(vault_id, "results")
    assert [c["name"] for c in schema["columns"]] == final
    assert all(c["pg_name"] for c in schema["columns"])
    assert schema["drift"]["has_drift"] is False, schema["drift"]
    vault_schema = await table_schema_service.get_vault_schema(vault_id)
    assert [c["name"] for c in vault_schema["tables"][0]["columns"]] == final
    browse = await DocumentService.__new__(DocumentService)._browse_tables_by_depth(
        vault, vault_id, prefix="", max_depth=-1,
    )
    assert [c["name"] for c in browse[0].columns] == final
    info = await access_service._list_tables_with_schema(vault, vault_id)
    info_names = [c["name"] for c in info[0]["columns"] if c["name"] not in _BOOKKEEPING]
    assert info_names == final
    # Search indexes the headers a reader would type, not their SQL spelling.
    chunk = await live.pool.fetchval(
        """
        SELECT c.content FROM chunks c
          JOIN vault_tables t ON t.id = c.source_id
         WHERE c.source_type = 'table' AND t.vault_id = $1
        """,
        vault_id,
    )
    for header in final:
        assert header in chunk, chunk

    await table_service.drop_table(vault_id, "results", actor_id="owner")
    assert await live.attnames(table_pg) == []
    assert await live.pool.fetchval(
        "SELECT count(*) FROM vault_tables WHERE vault_id = $1", vault_id,
    ) == 0


# ── Gate 2: headers safe_ident would fuse coexist ────────────────────────────


async def test_gate2_headers_safe_ident_would_fuse_are_distinct_columns(live):
    vault_id, vault, owner = await live.vault("gate2")
    assert table_data_repo.safe_ident("분류") == table_data_repo.safe_ident("모델") == "__"

    created = await table_service.create_table(
        vault_id, "models",
        [{"name": "분류", "type": "text"}, {"name": "모델", "type": "text"}],
        actor_id="owner",
    )
    physical = [c["pg_name"] for c in created["columns"]]
    assert len(set(physical)) == 2 and "__" not in physical
    table_pg = table_data_repo.pg_table_name(vault, "models")
    assert set(physical) <= set(await live.attnames(table_pg))

    await table_row_write.insert_rows(
        vault_name=vault, vault_id=vault_id, table_name="models",
        user_id=owner, actor_id="owner", is_admin=True,
        body={"분류": "추론", "모델": "gpt"},
    )
    read = await _read(vault, vault_id, "models", owner, query_params=[("select", "분류,모델")])
    assert _items(read) == [{"분류": "추론", "모델": "gpt"}]


# ── Gate 3: re-parsing the same document yields the identical registry row ───


async def test_gate3_recreating_the_same_table_yields_the_same_registry_row(live):
    vault_id, _vault, _owner = await live.vault("gate3")

    await table_service.create_table(
        vault_id, "results", [dict(c) for c in _HEADERS], actor_id="owner",
    )
    first = await live.stored_columns(vault_id, "results")

    again = await table_service.create_table(
        vault_id, "results", [dict(c) for c in _HEADERS], actor_id="owner",
        if_not_exists=True, can_read_existing=True,
    )
    assert again["created"] is False
    assert again["matches_request"] is True, again["mismatches"]

    await table_service.drop_table(vault_id, "results", actor_id="owner")
    await table_service.create_table(
        vault_id, "results", [dict(c) for c in _HEADERS], actor_id="owner",
    )
    assert await live.stored_columns(vault_id, "results") == first


# ── Gate 4: a 21+ character Korean header ────────────────────────────────────


async def test_gate4_long_korean_header_round_trips_within_the_physical_bound(live):
    vault_id, vault, owner = await live.vault("gate4")
    header = "가나다라마바사아자차카타파하거너더러머버서어"
    assert len(header) == 22 and len(header.encode()) > table_data_repo.PG_IDENT_MAX_LEN
    # Past the bound even at one byte per character: PostgreSQL would
    # silently truncate an identifier this long.
    longer = "가" * 70

    created = await table_service.create_table(
        vault_id, "wide",
        [{"name": header, "type": "text"}, {"name": longer, "type": "text"}],
        actor_id="owner",
    )
    attnames = await live.attnames(table_data_repo.pg_table_name(vault, "wide"))
    for col in created["columns"]:
        assert len(col["pg_name"].encode()) <= table_data_repo.PG_IDENT_MAX_LEN
        assert col["pg_name"] in attnames

    await table_row_write.insert_rows(
        vault_name=vault, vault_id=vault_id, table_name="wide",
        user_id=owner, actor_id="owner", is_admin=True, body={header: "값", longer: "긴 값"},
    )
    read = await _read(
        vault, vault_id, "wide", owner, query_params=[("select", f"{header},{longer}")],
    )
    assert _items(read) == [{header: "값", longer: "긴 값"}]


# ── Gate 5: every enforcement surface agrees on the physical name ────────────


async def _sql(vault, user, sql):
    return await table_service.execute_sql(
        vault_names=[vault], user_id=str(user), actor_id="tester", sql=sql, is_admin=False,
    )


async def test_gate5_sql_drift_rolesync_and_keys_agree_on_the_physical_name(live):
    vault_id, vault, owner = await live.vault("gate5")
    created = await table_service.create_table(
        vault_id, "results",
        [{"name": "분류", "type": "text"}, {"name": "중요도", "type": "int"}],
        actor_id="owner",
        unique_keys=[{"columns": ["분류"]}],
        indexes=[{"columns": [{"name": "중요도", "order": "desc"}]}],
    )
    phys = {c["name"]: c["pg_name"] for c in created["columns"]}
    table_pg = table_data_repo.pg_table_name(vault, "results")

    # The UNIQUE constraint and the index are built on the physical columns.
    uk_name = created["unique_keys"][0]["name"]
    uk_columns = await live.pool.fetch(
        """
        SELECT a.attname FROM pg_constraint c
          JOIN pg_attribute a ON a.attrelid = c.conrelid AND a.attnum = ANY(c.conkey)
         WHERE c.conname = $1
        """,
        uk_name,
    )
    assert [r["attname"] for r in uk_columns] == [phys["분류"]]
    index_def = await live.pool.fetchval(
        "SELECT indexdef FROM pg_indexes WHERE indexname = $1",
        created["indexes"][0]["name"],
    )
    assert f"({phys['중요도']} DESC)" in index_def

    # pg_attribute drift detection reads the same physical names.
    schema = await table_schema_service.get_table_schema(vault_id, "results")
    assert schema["drift"]["has_drift"] is False, schema["drift"]

    # Writes through the owner's own role; the key is enforced by PostgreSQL.
    for row in ({"분류": "A", "중요도": 1}, {"분류": "B", "중요도": 1}):
        ok = await table_row_write.insert_rows(
            vault_name=vault, vault_id=vault_id, table_name="results",
            user_id=owner, actor_id="owner", is_admin=False, body=row,
        )
        assert not isinstance(ok, dict), ok
    duplicate = await table_row_write.insert_rows(
        vault_name=vault, vault_id=vault_id, table_name="results",
        user_id=owner, actor_id="owner", is_admin=False, body={"분류": "A"},
    )
    assert isinstance(duplicate, dict) and duplicate["code"] == "unique_violation"
    with pytest.raises(ValidationError) as preflight:
        await table_service.alter_table(
            vault_id, "results", actor_id="owner",
            add_unique_keys=[{"columns": ["중요도"]}],
        )
    assert "'중요도': 1" in str(preflight.value)

    # A reader sees physical names in akb_sql; a logical name is pointed at one.
    reader = await live.member(vault_id, "reader")
    physical_select = f"SELECT {phys['분류']} FROM results ORDER BY {phys['분류']}"
    seen = await _sql(vault, reader, physical_select)
    assert seen.get("items") == [{phys["분류"]: "A"}, {phys["분류"]: "B"}], seen
    logical = await _sql(vault, reader, 'SELECT "분류" FROM results')
    assert logical["code"] == "undefined_column", logical
    assert phys["분류"] in logical["hint"], logical

    # Revoke-equivalent: the membership and the table's grants both go.
    await live.role_sync.on_revoke(vault_id, reader)
    async with live.pool.acquire() as conn:
        groups = ", ".join(
            f'"{vault_group_role_name(vault_id, scope)}"' for scope in ("reader", "writer", "admin")
        )
        await conn.execute(f"REVOKE ALL ON {table_pg} FROM {groups}")
    denied = await _sql(vault, reader, physical_select)
    assert denied["code"] == "permission_denied", denied

    # Reconciled from the catalog, every surface agrees again.
    await live.role_sync.reconcile_from_catalog()
    assert await _sql(vault, reader, physical_select) == seen
    logical_again = await _sql(vault, reader, 'SELECT "분류" FROM results')
    assert phys["분류"] in logical_again["hint"], logical_again
    duplicate_again = await table_row_write.insert_rows(
        vault_name=vault, vault_id=vault_id, table_name="results",
        user_id=owner, actor_id="owner", is_admin=False, body={"분류": "B"},
    )
    assert isinstance(duplicate_again, dict) and duplicate_again["code"] == "unique_violation"
    schema_again = await table_schema_service.get_table_schema(vault_id, "results")
    assert schema_again["drift"]["has_drift"] is False


# ── Gate 6: existing tables read back byte-identical ─────────────────────────

# A table as 2ed799bf created it: the registry carries no `pg_name`. The last
# column predates the column grammar (0.3.6): its registry name was never a
# PostgreSQL identifier, and unquoted DDL folded `safe_ident` of it to
# `legacy_col`.
_LEGACY_COLUMNS = [
    {"name": "title", "type": "text"},
    {"name": "status", "type": "enum", "enum": ["draft", "active"], "default": "draft",
     "check": {"op": "in", "values": ["draft", "active"]}},
    {"name": "qty", "type": "int", "check": {"op": "gte", "value": 0}},
    {"name": "Legacy-Col", "type": "text"},
]


async def _physical_snapshot(conn, table_pg: str):
    regclass = f"public.{table_pg}"
    return {
        "relfilenode": await conn.fetchval(
            "SELECT relfilenode FROM pg_class WHERE oid = to_regclass($1)", regclass,
        ),
        "columns": [tuple(r) for r in await conn.fetch(
            """
            SELECT a.attname, format_type(a.atttypid, a.atttypmod), a.attnotnull,
                   pg_get_expr(d.adbin, d.adrelid)
              FROM pg_attribute a
              LEFT JOIN pg_attrdef d ON d.adrelid = a.attrelid AND d.adnum = a.attnum
             WHERE a.attrelid = to_regclass($1) AND a.attnum > 0 AND NOT a.attisdropped
             ORDER BY a.attnum
            """,
            regclass,
        )],
        "constraints": [tuple(r) for r in await conn.fetch(
            "SELECT conname, pg_get_constraintdef(oid) FROM pg_constraint "
            "WHERE conrelid = to_regclass($1) ORDER BY conname",
            regclass,
        )],
        "indexes": [tuple(r) for r in await conn.fetch(
            "SELECT indexname, indexdef FROM pg_indexes WHERE tablename = $1 ORDER BY indexname",
            table_pg,
        )],
        "triggers": [r["tgname"] for r in await conn.fetch(
            "SELECT tgname FROM pg_trigger WHERE tgrelid = to_regclass($1) "
            "AND NOT tgisinternal ORDER BY tgname",
            regclass,
        )],
    }


async def test_gate6_an_existing_table_is_byte_identical_under_this_code(live_before_433):
    """A table created before #433 carries no `pg_name`, and must not gain one:
    booting this code over it and using it through every surface leaves its
    registry row byte-identical, its physical table untouched, and every
    column's physical name the one it always had (`column_pg_name`'s fallback,
    which is the contract, not a shim)."""
    live = live_before_433
    vault_id, vault, owner = await live.vault("gate6")
    table_pg = table_data_repo.pg_table_name(vault, "legacy")
    uk_name = table_data_repo.generate_constraint_name(table_pg, ["title"], kind="uk")
    idx_name = table_data_repo.generate_constraint_name(table_pg, ["qty"], kind="idx")
    async with live.pool.acquire() as conn:
        async with conn.transaction():
            await table_data_repo.create_dynamic_table(
                conn, table_pg, [dict(c) for c in _LEGACY_COLUMNS], vault_name=vault,
                vault_id=vault_id, resource_uri=f"akb://{vault}/table/legacy",
            )
            await conn.execute(f"ALTER TABLE {table_pg} ADD CONSTRAINT {uk_name} UNIQUE (title)")
            await conn.execute(f"CREATE INDEX {idx_name} ON {table_pg} (qty ASC)")
            await conn.execute(
                """
                INSERT INTO vault_tables (vault_id, name, description, columns,
                                          unique_keys, indexes, created_by)
                VALUES ($1, 'legacy', '', $2::jsonb, $3::jsonb, $4::jsonb, 'owner')
                """,
                vault_id,
                json.dumps(_LEGACY_COLUMNS),
                json.dumps([{"name": uk_name, "columns": ["title"]}]),
                json.dumps([{"name": idx_name, "columns": [{"name": "qty", "order": "asc"}]}]),
            )
            await live.role_sync.grant_table_in_conn(conn, vault_id, table_pg)
            await conn.execute(
                f"INSERT INTO {table_pg} (title, status, qty, legacy_col) "
                "VALUES ('a', 'active', 2, 'old'), ('b', 'draft', 0, NULL)"
            )
        physical_before = await _physical_snapshot(conn, table_pg)
    stored_before = await live.stored_columns(vault_id, "legacy")
    entries_before = await live.stored_entries(vault_id, "legacy")
    assert "pg_name" not in stored_before
    read_before = await _read(vault, vault_id, "legacy", owner, query_params=[("order", "title.asc")])
    sql = "SELECT * FROM legacy ORDER BY title"
    sql_before = await table_service.execute_sql(
        vault_names=[vault], user_id=str(owner), actor_id="owner", sql=sql, is_admin=True,
    )

    # Deploy this code: its boot applies every migration registered after
    # the last release before #433.
    async with live.pool.acquire() as conn:
        await postgres._run_boot_schema(conn, init_sql=_INIT_SQL)

    # ...and use the table through every surface.
    read_after = await _read(vault, vault_id, "legacy", owner, query_params=[("order", "title.asc")])
    assert read_after.body == read_before.body
    assert await table_service.execute_sql(
        vault_names=[vault], user_id=str(owner), actor_id="owner", sql=sql, is_admin=True,
    ) == sql_before
    legacy_read = await _read(
        vault, vault_id, "legacy", owner,
        query_params=[("select", "title,Legacy-Col"), ("Legacy-Col", "eq.old")],
    )
    assert _items(legacy_read) == [{"title": "a", "Legacy-Col": "old"}]
    await table_row_write.insert_rows(
        vault_name=vault, vault_id=vault_id, table_name="legacy",
        user_id=owner, actor_id="owner", is_admin=True, body={"title": "c", "qty": 1},
    )
    schema = await table_schema_service.get_table_schema(vault_id, "legacy")
    assert schema["drift"]["has_drift"] is False, schema["drift"]
    # Readers report both names, computing the physical one.
    expected = {c["name"]: c["name"] for c in _LEGACY_COLUMNS} | {"Legacy-Col": "legacy_col"}
    assert {c["name"]: c["pg_name"] for c in schema["columns"]} == expected
    listed = await table_service.list_tables(vault_id)
    assert {c["name"]: c["pg_name"] for c in listed[0]["columns"]} == expected
    browse = await DocumentService.__new__(DocumentService)._browse_tables_by_depth(
        vault, vault_id, prefix="", max_depth=-1,
    )
    assert {c["name"]: c["pg_name"] for c in browse[0].columns} == expected
    native = await NativeDocumentService(pool=live.pool)._browse_legacy_tables(
        vault, vault_id, prefix="", max_depth=-1,
    )
    assert {c["name"]: c.get("pg_name") for c in native[0].columns} == expected

    # Nothing of that wrote a byte into the registry or touched the table.
    assert await live.stored_columns(vault_id, "legacy") == stored_before
    async with live.pool.acquire() as conn:
        assert await _physical_snapshot(conn, table_pg) == physical_before
    stored = json.loads(stored_before)
    assert [table_data_repo.column_pg_name(c) for c in stored] == [
        "title", "status", "qty", "legacy_col",
    ]
    assert [c["name"] for c in stored[:3]] == ["title", "status", "qty"]
    assert set(expected.values()) <= set(await live.attnames(table_pg))

    # An alter that does not touch those columns leaves their stored entries
    # as they were, and a plain column it adds stores no `pg_name` either.
    await table_service.alter_table(
        vault_id, "legacy", actor_id="owner", add_columns=[{"name": "note", "type": "text"}],
    )
    entries_after = await live.stored_entries(vault_id, "legacy")
    assert entries_after[:4] == entries_before
    assert json.loads(entries_after[4]) == {"name": "note", "type": "text"}
    assert "pg_name" not in await live.stored_columns(vault_id, "legacy")

    # Generated constraint names still derive exactly as they did: widening the
    # enum drops the CHECK it created and nothing is left behind.
    await table_service.alter_table(
        vault_id, "legacy", actor_id="owner",
        alter_columns=[{"name": "status", "set_enum": ["draft", "active", "archived"]}],
    )
    async with live.pool.acquire() as conn:
        await conn.execute(f"INSERT INTO {table_pg} (title, status) VALUES ('d', 'archived')")
        status_checks = await conn.fetchval(
            """
            SELECT count(*) FROM pg_constraint c
              JOIN pg_attribute a ON a.attrelid = c.conrelid AND a.attnum = ANY(c.conkey)
             WHERE c.conrelid = to_regclass($1) AND c.contype = 'c' AND a.attname = 'status'
            """,
            f"public.{table_pg}",
        )
    assert status_checks == 1
    assert "pg_name" not in await live.stored_columns(vault_id, "legacy")


async def test_gate6_pg_name_is_stored_only_while_a_name_needs_it(live):
    """A plain column created now stores exactly what one created before #433
    did. A rename that leaves a column's physical name behind its logical one
    records `pg_name`; a rename back to the name that physical name is drops
    it again."""
    vault_id, vault, owner = await live.vault("gate6b")
    created = await table_service.create_table(
        vault_id, "states",
        [{"name": "status", "type": "text"}, {"name": "분류", "type": "text"}],
        actor_id="owner",
    )
    header_pg = created["columns"][1]["pg_name"]
    # Both names are reported, whether or not they are stored.
    assert [c["pg_name"] for c in created["columns"]] == ["status", header_pg]
    entries = [json.loads(e) for e in await live.stored_entries(vault_id, "states")]
    assert entries == [
        {"name": "status", "type": "text"},
        {"name": "분류", "type": "text", "pg_name": header_pg},
    ]
    await table_row_write.insert_rows(
        vault_name=vault, vault_id=vault_id, table_name="states",
        user_id=owner, actor_id="owner", is_admin=True, body={"status": "open", "분류": "A"},
    )

    await table_service.alter_table(
        vault_id, "states", actor_id="owner", rename_columns={"status": "상태"},
    )
    entries = [json.loads(e) for e in await live.stored_entries(vault_id, "states")]
    assert entries[0] == {"name": "상태", "type": "text", "pg_name": "status"}
    assert "status" in await live.attnames(table_data_repo.pg_table_name(vault, "states"))
    read = await _read(vault, vault_id, "states", owner, query_params=[("select", "상태")])
    assert _items(read) == [{"상태": "open"}]

    await table_service.alter_table(
        vault_id, "states", actor_id="owner", rename_columns={"상태": "status"},
    )
    entries = [json.loads(e) for e in await live.stored_entries(vault_id, "states")]
    assert entries[0] == {"name": "status", "type": "text"}
    read = await _read(vault, vault_id, "states", owner, query_params=[("select", "status")])
    assert _items(read) == [{"status": "open"}]


# ── A drop names a declared column; reserved words are headers too ───────────


async def test_a_drop_never_reaches_a_column_by_its_physical_name(live):
    """A plain name can be another column's physical name: the `c_1_…` a
    header was given, or `age` after a logical rename to `나이`. Dropping by
    it is refused, and the column and its data stay."""
    vault_id, vault, owner = await live.vault("dropguard")
    created = await table_service.create_table(
        vault_id, "results",
        [{"name": "분류", "type": "text"}, {"name": "age", "type": "int"}],
        actor_id="owner",
    )
    await table_service.alter_table(
        vault_id, "results", actor_id="owner", rename_columns={"age": "나이"},
    )
    await table_row_write.insert_rows(
        vault_name=vault, vault_id=vault_id, table_name="results",
        user_id=owner, actor_id="owner", is_admin=True, body={"분류": "A", "나이": 7},
    )
    table_pg = table_data_repo.pg_table_name(vault, "results")
    before = await live.attnames(table_pg)
    stored = await live.stored_columns(vault_id, "results")

    for name in (created["columns"][0]["pg_name"], "age"):
        with pytest.raises(ValidationError, match="Cannot drop missing column"):
            await table_service.alter_table(
                vault_id, "results", actor_id="owner", drop_columns=[name],
            )

    assert await live.attnames(table_pg) == before
    assert await live.stored_columns(vault_id, "results") == stored
    read = await _read(vault, vault_id, "results", owner, query_params=[("select", "분류,나이")])
    assert _items(read) == [{"분류": "A", "나이": 7}]


async def test_reserved_words_are_headers_like_any_other(live):
    """`CREATE TABLE … (user TEXT)` is a syntax error, so a reserved word gets
    a derived physical name; the row API still speaks the word, including
    one spelled like a query control the web UI sends on every listing."""
    vault_id, vault, owner = await live.vault("reserved")
    words = ["user", "order", "group", "limit", "select"]
    created = await table_service.create_table(
        vault_id, "sheet",
        [{"name": w, "type": "text"} for w in words] + [{"name": "name", "type": "text"}],
        actor_id="owner",
    )
    phys = {c["name"]: c["pg_name"] for c in created["columns"]}
    assert phys["name"] == "name"
    assert set(phys[w] for w in words) <= set(
        await live.attnames(table_data_repo.pg_table_name(vault, "sheet"))
    )

    await table_row_write.insert_rows(
        vault_name=vault, vault_id=vault_id, table_name="sheet",
        user_id=owner, actor_id="owner", is_admin=True,
        body=[{w: f"{w}-{n}" for w in [*words, "name"]} for n in (1, 2)],
    )
    listing = await _read(
        vault, vault_id, "sheet", owner,
        query_params=[("limit", "50"), ("offset", "0"), ("order", "created_at.desc,id.desc")],
    )
    assert len(_items(listing)) == 2
    picked = await _read(
        vault, vault_id, "sheet", owner,
        query_params=[("select", "user,order"), ("group", "eq.group-2"), ("order", "user.asc")],
    )
    assert _items(picked) == [{"user": "user-2", "order": "order-2"}]

    # Among PostgreSQL's keywords, exactly those it refuses as a column name
    # are not plain; the ones it accepts (`name`, `type`, …) keep themselves.
    keywords = await live.pool.fetch("SELECT word, catcode::text AS catcode FROM pg_get_keywords()")
    refused = {r["word"] for r in keywords if r["catcode"] in ("R", "T")}
    assert {
        r["word"] for r in keywords if not table_data_repo.is_plain_column_name(r["word"])
    } == refused
