"""Vault table service — real PostgreSQL tables per vault.

Service composes `table_registry_repo` (the `vault_tables` row) and
`table_data_repo` (the dynamic `vt_***` PG tables) under transaction
boundaries managed here. SQL lives in the repos; this module is
business logic + orchestration only.

Identifier helpers and the SQL-name rewriting used by the
`table_query` share path (publication_service) live in
`table_data_repo`; we re-export them at the historical names so that
caller keeps working. Later commits switch the imports to the public
names.
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone

from app.db.postgres import get_pool
from app.exceptions import ConflictError, NotFoundError
from app.repositories import table_data_repo, table_registry_repo
from app.repositories.events_repo import emit_event
from app.services.index_service import (
    build_table_chunk, delete_table_chunks, write_source_chunks,
)

# Re-exported helpers used by publication_service for the
# `table_query` share path. Other modules import directly from
# `table_data_repo`.
from app.repositories.table_data_repo import (  # noqa: F401
    build_table_name_map,
    rewrite_table_names,
)

logger = logging.getLogger("akb.tables")

# Reserved column names that conflict with auto-added bookkeeping columns.
_RESERVED = {"id", "created_at", "updated_at", "created_by"}


# ── Indexing helpers ─────────────────────────────────────────────


async def index_table_metadata(
    table_id: str,
    vault_id: uuid.UUID,
    vault_name: str,
    name: str,
    description: str | None,
    columns: list[dict],
) -> None:
    """Build + upsert the metadata chunk for a table so hybrid search can
    surface it. Safe to call repeatedly — write_source_chunks replaces
    all prior chunks for this table first."""
    chunk = build_table_chunk(
        vault_name=vault_name, name=name,
        description=description, columns=columns,
    )
    pool = await get_pool()
    async with pool.acquire() as conn:
        await write_source_chunks(
            conn, "table", table_id,
            vault_id=vault_id,
            chunks=[chunk],
        )


async def delete_table_index(table_id: str) -> None:
    """Drop the metadata chunk for a table (outbox-driven vector-store delete)."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        await delete_table_chunks(conn, table_id)


# ── CRUD ─────────────────────────────────────────────────────────


async def create_table(
    vault_id: uuid.UUID,
    name: str,
    columns: list[dict],
    *,
    actor_id: str,
    description: str = "",
) -> dict:
    pool = await get_pool()
    tid = uuid.uuid4()
    now = datetime.now(timezone.utc)

    async with pool.acquire() as conn:
        async with conn.transaction():
            vault = await conn.fetchrow("SELECT name FROM vaults WHERE id = $1", vault_id)
            if not vault:
                raise NotFoundError("Vault", str(vault_id))

            existing = await table_registry_repo.find_by_name(conn, vault_id, name)
            if existing:
                raise ConflictError(f"Table already exists: {name}")

            for col in columns:
                if col["name"].lower() in _RESERVED:
                    raise ValueError(
                        f"Column name '{col['name']}' is reserved (auto-added by AKB). "
                        f"Reserved names: {sorted(_RESERVED)}. Choose a different name."
                    )

            pg_name = table_data_repo.pg_table_name(vault["name"], name)
            await table_data_repo.create_dynamic_table(conn, pg_name, columns)
            await table_registry_repo.insert(
                conn,
                table_id=tid, vault_id=vault_id, name=name,
                description=description, columns=columns,
                created_by=actor_id, now=now,
            )
            await emit_event(
                conn, "table.create",
                vault_id=vault_id, ref_type="table", ref_id=str(tid),
                actor_id=actor_id,
                payload={
                    "vault": vault["name"],
                    "table_name": name,
                    "columns_count": len(columns),
                    "description": description,
                },
            )

    # Outside the create transaction on purpose: the embedding call can
    # be slow and we'd rather not hold a DB connection on it.
    try:
        await index_table_metadata(
            str(tid),
            vault_id=vault_id,
            vault_name=vault["name"],
            name=name,
            description=description,
            columns=columns,
        )
    except Exception as e:  # noqa: BLE001
        logger.warning("table metadata indexing failed for %s: %s", name, e)

    logger.info("Table created: %s → %s", name, pg_name)
    return {
        "kind": "table",
        "id": str(tid),
        "vault": vault["name"],
        "name": name,
        "columns": columns,
    }


async def list_tables(vault_id: uuid.UUID) -> list[dict]:
    pool = await get_pool()
    async with pool.acquire() as conn:
        vault = await conn.fetchrow("SELECT name FROM vaults WHERE id = $1", vault_id)
        if not vault:
            return []

        rows = await table_registry_repo.list_for_vault(conn, vault_id)

        results: list[dict] = []
        for r in rows:
            pg_name = table_data_repo.pg_table_name(vault["name"], r["name"])
            count = await table_data_repo.count_rows(conn, pg_name)
            results.append({
                "kind": "table",
                "id": str(r["id"]),
                "vault": vault["name"],
                "name": r["name"],
                "description": r["description"],
                "columns": table_registry_repo.parse_columns(r["columns"]),
                "row_count": count,
                "created_at": r["created_at"].isoformat(),
            })
    return results


async def drop_table(
    vault_id: uuid.UUID,
    table_name: str,
    *,
    actor_id: str,
) -> dict:
    """Drop a vault-scoped table: registry row + dynamic PG table +
    edges referencing the table URI + metadata chunk."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            vault = await conn.fetchrow("SELECT name FROM vaults WHERE id = $1", vault_id)
            if not vault:
                raise NotFoundError("Vault", str(vault_id))

            table = await table_registry_repo.find_by_name(conn, vault_id, table_name)
            if not table:
                raise NotFoundError("Table", table_name)

            table_id = table["id"]
            pg_name = table_data_repo.pg_table_name(vault["name"], table_name)
            await table_data_repo.drop_dynamic_table(conn, pg_name)
            await table_registry_repo.delete(conn, table_id)

            # Clean up edges referencing this table.
            t_uri = f"akb://{vault['name']}/table/{table_name}"
            await conn.execute(
                "DELETE FROM edges WHERE source_uri = $1 OR target_uri = $1",
                t_uri,
            )

            await emit_event(
                conn, "table.drop",
                vault_id=vault_id, ref_type="table", ref_id=str(table_id),
                actor_id=actor_id,
                payload={
                    "vault": vault["name"],
                    "table_name": table_name,
                },
            )

    # Outside the TX: drop the metadata chunk via the vector-store
    # outbox (same pattern as file deletion).
    try:
        await delete_table_index(str(table_id))
    except Exception as e:  # noqa: BLE001
        logger.warning("table chunk delete failed for %s: %s", table_name, e)

    logger.info("Table dropped: %s (%s)", table_name, pg_name)
    return {
        "kind": "table",
        "id": str(table_id),
        "vault": vault["name"],
        "name": table_name,
        "deleted": True,
    }


async def alter_table(
    vault_id: uuid.UUID,
    table_name: str,
    *,
    actor_id: str,
    add_columns: list[dict] | None = None,
    drop_columns: list[str] | None = None,
    rename_columns: dict[str, str] | None = None,
) -> dict:
    """Apply schema changes to a vault table:
       - add_columns: [{name, type}, ...]
       - drop_columns: ["name", ...]
       - rename_columns: {"old": "new", ...}

    All three are optional and combine in one TX. Emits `table.alter`
    after the writes commit.
    """
    pool = await get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            vault = await conn.fetchrow("SELECT name FROM vaults WHERE id = $1", vault_id)
            if not vault:
                raise NotFoundError("Vault", str(vault_id))

            table = await table_registry_repo.find_by_name(conn, vault_id, table_name)
            if not table:
                raise NotFoundError("Table", table_name)

            columns = table_registry_repo.parse_columns(table["columns"])
            pg_name = table_data_repo.pg_table_name(vault["name"], table_name)

            added: list[str] = []
            dropped: list[str] = []
            renamed: dict[str, str] = {}

            if add_columns:
                for col in add_columns:
                    if any(c["name"] == col["name"] for c in columns):
                        continue
                    col_type = table_data_repo.TYPE_MAP.get(col.get("type", "text"), "TEXT")
                    safe_name = table_data_repo.safe_ident(col["name"])
                    await table_data_repo.add_column(conn, pg_name, safe_name, col_type)
                    columns.append(col)
                    added.append(col["name"])

            if drop_columns:
                for col_name in drop_columns:
                    safe_name = table_data_repo.safe_ident(col_name)
                    await table_data_repo.drop_column(conn, pg_name, safe_name)
                    dropped.append(col_name)
                columns = [c for c in columns if c["name"] not in drop_columns]

            if rename_columns:
                for old_name, new_name in rename_columns.items():
                    old_safe = table_data_repo.safe_ident(old_name)
                    new_safe = table_data_repo.safe_ident(new_name)
                    await table_data_repo.rename_column(conn, pg_name, old_safe, new_safe)
                    for c in columns:
                        if c["name"] == old_name:
                            c["name"] = new_name
                    renamed[old_name] = new_name

            await table_registry_repo.update_columns(conn, table["id"], columns)

            await emit_event(
                conn, "table.alter",
                vault_id=vault_id, ref_type="table", ref_id=str(table["id"]),
                actor_id=actor_id,
                payload={
                    "vault": vault["name"],
                    "table_name": table_name,
                    "added": added,
                    "dropped": dropped,
                    "renamed": renamed,
                },
            )

    return {
        "kind": "table",
        "id": str(table["id"]),
        "vault": vault["name"],
        "name": table_name,
        "columns": columns,
    }


async def execute_sql(
    vault_names: list[str],
    sql: str,
    read_only: bool = False,
) -> dict:
    """Execute raw SQL scoped to vault tables.

    Table references are rewritten:
      - Single vault: 'pipeline' → 'vt_sales__pipeline'
      - Cross-vault: 'sales__pipeline' → 'vt_sales__pipeline'
    Only tables registered in vault_tables are accessible.
    """
    pool = await get_pool()
    async with pool.acquire() as conn:
        table_map = await table_data_repo.build_table_name_map(conn, vault_names)
        rewritten = table_data_repo.rewrite_table_names(sql, table_map)

        sql_check = rewritten.rstrip(";").strip()
        if ";" in sql_check:
            return {"error": "Multi-statement SQL is not allowed. Send one statement at a time."}

        is_select = rewritten.strip().upper().startswith(("SELECT", "WITH"))

        try:
            async with conn.transaction():
                # PostgreSQL enforces read-only: blocks INSERT/UPDATE/DELETE/
                # TRUNCATE/DROP/ALTER/CREATE regardless of SQL tricks
                if read_only:
                    await conn.execute("SET TRANSACTION READ ONLY")

                if is_select:
                    rows = await conn.fetch(rewritten)
                    result_rows = []
                    for r in rows:
                        row = {}
                        for k, v in dict(r).items():
                            if isinstance(v, uuid.UUID):
                                row[k] = str(v)
                            elif hasattr(v, "isoformat"):
                                row[k] = v.isoformat()
                            elif isinstance(v, (int, float, str, bool, type(None))):
                                row[k] = v
                            else:
                                row[k] = str(v)
                        result_rows.append(row)
                    return {
                        "kind": "table_query",
                        "vaults": vault_names,
                        "columns": list(dict(rows[0]).keys()) if rows else [],
                        "items": result_rows,
                        "total": len(result_rows),
                    }
                else:
                    result = await conn.execute(rewritten)
                    return {
                        "kind": "table_sql",
                        "vaults": vault_names,
                        "result": result,
                    }
        except Exception as e:
            msg = str(e)
            if read_only and "read-only transaction" in msg:
                return {"error": "Write operation denied. You have read-only access to this vault."}
            return {"error": msg}
