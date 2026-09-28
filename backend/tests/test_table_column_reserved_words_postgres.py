"""Live PostgreSQL proof that a grammatical column name PostgreSQL will not
take is refused before any DDL, and that the refused set is exactly the one
the server refuses.

`user` passes `^[a-z][a-z0-9_]*$` and used to reach `CREATE TABLE`, where it is
a syntax error: a 500. `xmin` reached `ALTER TABLE … ADD COLUMN` and failed
with 42701: also a 500. The frozen word list is compared with the running
server's `pg_get_keywords()`, so a PostgreSQL upgrade that reserves a new word
turns this red instead of turning a create into a 500.
"""

from __future__ import annotations

import os
import uuid
from contextlib import asynccontextmanager
from pathlib import Path

import asyncpg
import pytest

from app.db import postgres
from app.exceptions import ValidationError
from app.repositories import table_data_repo
from app.services import table_service
from app.services.role_sync import RoleSync, user_role_name, vault_group_role_name

pytestmark = pytest.mark.asyncio

_INIT_SQL = (Path(__file__).resolve().parents[1] / "app" / "db" / "init.sql").read_text()
_DSN = os.environ.get(
    "AKB_TEST_DSN",
    "postgresql://akb:akb@localhost:15432/akb",  # pragma: allowlist secret
)


def _database_dsn(name: str) -> str:
    base, _ = _DSN.rsplit("/", 1)
    return f"{base}/{name}"


async def _can_connect() -> bool:
    try:
        conn = await asyncpg.connect(_DSN, timeout=2)
    except (OSError, asyncpg.PostgresError):
        return False
    await conn.close()
    return True


@asynccontextmanager
async def _live(monkeypatch):
    if not await _can_connect():
        if os.environ.get("REQUIRE_REAL_PG") == "1":
            pytest.fail(f"Required PostgreSQL is not reachable at {_DSN}")
        pytest.skip(f"Postgres not reachable at {_DSN}")
    admin = await asyncpg.connect(_DSN)
    name = f"akb_colwords_{uuid.uuid4().hex[:12]}"
    await admin.execute(f'CREATE DATABASE "{name}"')
    pool = await asyncpg.create_pool(_database_dsn(name), min_size=1, max_size=4)
    roles: list[str] = []
    try:
        async with pool.acquire() as conn:
            await postgres._run_boot_schema(conn, init_sql=_INIT_SQL)
        role_sync = RoleSync(pool)
        monkeypatch.setattr(postgres, "_pool", pool)
        monkeypatch.setattr(table_service, "get_role_sync", lambda: role_sync)
        owner = await pool.fetchval(
            "INSERT INTO users (username, email, password_hash) "
            "VALUES ($1, $2, 'fixture') RETURNING id",
            f"owner-{name}", f"{name}@example.invalid",
        )
        roles.append(user_role_name(owner))
        await role_sync.on_user_create(owner)
        vault_name = f"words-{name[-8:]}"
        async with pool.acquire() as conn:
            async with conn.transaction():
                vault_id = await conn.fetchval(
                    "INSERT INTO vaults (name, git_path, owner_id) "
                    "VALUES ($1, $2, $3) RETURNING id",
                    vault_name, f"/tmp/{name}.git", owner,
                )
                await role_sync.on_vault_create_in_conn(conn, vault_id, owner_user_id=owner)
        roles.extend(vault_group_role_name(vault_id, s) for s in ("reader", "writer", "admin"))
        yield pool, vault_id, vault_name
    finally:
        await pool.close()
        await admin.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
        for role in roles:
            try:
                await admin.execute(f'DROP ROLE IF EXISTS "{role}"')
            except asyncpg.PostgresError:
                pass
        await admin.close()


async def test_a_reserved_word_is_refused_before_any_ddl(monkeypatch):
    async with _live(monkeypatch) as (pool, vault_id, _vault):
        with pytest.raises(ValidationError) as caught:
            await table_service.create_table(
                vault_id, "sheet",
                [{"name": "user", "type": "text"}, {"name": "order", "type": "int"}],
                actor_id="owner",
            )
        assert "'user' is a word PostgreSQL reserves" in str(caught.value)
        assert await pool.fetchval(
            "SELECT count(*) FROM vault_tables WHERE vault_id = $1", vault_id) == 0


async def test_a_system_column_name_is_refused_on_create_and_on_add(monkeypatch):
    async with _live(monkeypatch) as (pool, vault_id, _vault):
        with pytest.raises(ValidationError):
            await table_service.create_table(
                vault_id, "sheet", [{"name": "xmin", "type": "text"}], actor_id="owner",
            )
        await table_service.create_table(
            vault_id, "sheet", [{"name": "title", "type": "text"}], actor_id="owner",
        )
        for name in ("ctid", "tableoid"):
            with pytest.raises(ValidationError) as caught:
                await table_service.alter_table(
                    vault_id, "sheet", actor_id="owner",
                    add_columns=[{"name": name, "type": "text"}],
                )
            assert f"{name!r} is a PostgreSQL system column" in str(caught.value)


async def test_the_refused_words_are_the_ones_the_server_refuses(monkeypatch):
    async with _live(monkeypatch) as (pool, vault_id, vault_name):
        keywords = await pool.fetch("SELECT word, catcode::text AS catcode FROM pg_get_keywords()")
        refused = {r["word"] for r in keywords if r["catcode"] in ("R", "T")}
        assert table_service._PG_RESERVED_COLUMN_WORDS == refused

        # And the words it takes stay usable: a table named with them is
        # created, written and read back.
        accepted = sorted(r["word"] for r in keywords if r["catcode"] in ("U", "C")
                          and r["word"] not in table_service._RESERVED)[:40]
        await table_service.create_table(
            vault_id, "kw", [{"name": word, "type": "text"} for word in accepted],
            actor_id="owner",
        )
        pg_name = table_data_repo.pg_table_name(vault_name, "kw")
        attnames = await pool.fetch(
            "SELECT attname FROM pg_attribute WHERE attrelid = to_regclass($1) "
            "AND attnum > 0 AND NOT attisdropped", f"public.{pg_name}")
        assert set(accepted) <= {r["attname"] for r in attnames}
