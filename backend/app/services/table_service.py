"""Vault table service — real PostgreSQL tables per vault.

Each vault's tables are created as actual PG tables with naming convention:
  vt_{sanitized_vault_name}__{sanitized_table_name}

vault_tables registry tracks metadata (column definitions, description).
Data lives in real PG tables with proper types, indexes, and full SQL support.
"""

from __future__ import annotations

import json
import logging
import re
import uuid
from datetime import datetime, timezone

from app.db.postgres import get_pool
from app.exceptions import ConflictError, NotFoundError
from app.services.index_service import (
    build_table_chunk, delete_table_chunks, write_source_chunks,
)
from app.utils import ensure_list

logger = logging.getLogger("akb.tables")

TYPE_MAP = {
    "text": "TEXT",
    "number": "NUMERIC",
    "boolean": "BOOLEAN",
    "date": "DATE",
    "json": "JSONB",
}


def _pg_table_name(vault_name: str, table_name: str) -> str:
    """Generate safe PG table name: vt_{vault}__{table}."""
    v = re.sub(r"[^a-z0-9]", "_", vault_name.lower())
    t = re.sub(r"[^a-z0-9]", "_", table_name.lower().replace("-", "_"))
    return f"vt_{v}__{t}"


def _safe_ident(name: str) -> str:
    """Sanitize column/table name for SQL identifier."""
    return re.sub(r"[^a-zA-Z0-9_]", "_", name)


async def build_table_name_map(conn, vault_names: list[str]) -> dict[str, str]:
    """Build a mapping of friendly table names → real PG table names.

    For a single vault, both bare names ('pipeline') and prefixed names
    ('sales__pipeline') are accepted. For multi-vault queries only the
    prefixed form is accepted to avoid ambiguity.

    Used by both akb_sql and table_query shares.
    """
    table_map: dict[str, str] = {}
    for vname in vault_names:
        vault_row = await conn.fetchrow("SELECT id FROM vaults WHERE name = $1", vname)
        if not vault_row:
            raise NotFoundError("Vault", vname)
        tables = await conn.fetch(
            "SELECT name FROM vault_tables WHERE vault_id = $1",
            vault_row["id"],
        )
        for t in tables:
            pg_name = _pg_table_name(vname, t["name"])
            sanitized_vault = re.sub(r"[^a-z0-9]", "_", vname.lower())
            sanitized_table = t["name"].replace("-", "_")
            table_map[f"{sanitized_vault}__{sanitized_table}"] = pg_name
            if len(vault_names) == 1:
                table_map[t["name"]] = pg_name
                table_map[t["name"].replace("-", "_")] = pg_name
    return table_map


def rewrite_table_names(sql: str, table_map: dict[str, str]) -> str:
    """Replace short table names in `sql` with their pg-qualified names.

    Longest match first to avoid partial collisions (e.g. 'sales' vs 'sales_v2').
    """
    rewritten = sql
    for short_name in sorted(table_map.keys(), key=len, reverse=True):
        pg_name = table_map[short_name]
        rewritten = re.sub(
            rf"\b{re.escape(short_name)}\b",
            pg_name,
            rewritten,
            flags=re.IGNORECASE,
        )
    return rewritten


async def index_table_metadata(
    table_id: str,
    vault_id: uuid.UUID,
    vault_name: str,
    name: str,
    description: str | None,
    columns: list[dict],
) -> None:
    """Build + upsert the metadata chunk for a table so hybrid search
    can surface it alongside documents. Safe to call repeatedly — the
    underlying write_source_chunks replaces all prior chunks for this
    table first."""
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


async def create_table(
    vault_id: uuid.UUID,
    name: str,
    columns: list[dict],
    description: str = "",
    created_by: str | None = None,
) -> dict:
    pool = await get_pool()
    tid = uuid.uuid4()
    now = datetime.now(timezone.utc)

    async with pool.acquire() as conn:
        # Check vault name
        vault = await conn.fetchrow("SELECT name FROM vaults WHERE id = $1", vault_id)
        if not vault:
            raise NotFoundError("Vault", str(vault_id))

        existing = await conn.fetchrow(
            "SELECT id FROM vault_tables WHERE vault_id = $1 AND name = $2",
            vault_id, name,
        )
        if existing:
            raise ConflictError(f"Table already exists: {name}")

        # Reject reserved column names that conflict with auto-added columns
        RESERVED = {"id", "created_at", "updated_at", "created_by"}
        for col in columns:
            col_name_lower = col["name"].lower()
            if col_name_lower in RESERVED:
                raise ValueError(
                    f"Column name '{col['name']}' is reserved (auto-added by AKB). "
                    f"Reserved names: {sorted(RESERVED)}. Choose a different name."
                )

        # Build CREATE TABLE DDL
        pg_name = _pg_table_name(vault["name"], name)
        col_defs = ["id UUID PRIMARY KEY DEFAULT uuid_generate_v4()"]
        for col in columns:
            col_name = _safe_ident(col["name"])
            col_type = TYPE_MAP.get(col.get("type", "text"), "TEXT")
            not_null = " NOT NULL" if col.get("required") else ""
            col_defs.append(f"{col_name} {col_type}{not_null}")
        col_defs.append("created_by TEXT")
        col_defs.append("created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()")
        col_defs.append("updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()")

        ddl = f'CREATE TABLE {pg_name} ({", ".join(col_defs)})'
        await conn.execute(ddl)

        # Register in vault_tables
        await conn.execute(
            """
            INSERT INTO vault_tables (id, vault_id, name, description, columns, created_by, created_at, updated_at)
            VALUES ($1, $2, $3, $4, $5, $6, $7, $7)
            """,
            tid, vault_id, name, description, json.dumps(columns), created_by, now,
        )

    # Index metadata chunk so the table is discoverable via hybrid search.
    # Outside the vault-creation transaction on purpose — embedding call
    # can be slow and we'd rather not hold a DB connection on it.
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
    return {"table_id": str(tid), "name": name, "columns": columns}


async def list_tables(vault_id: uuid.UUID) -> list[dict]:
    pool = await get_pool()
    async with pool.acquire() as conn:
        vault = await conn.fetchrow("SELECT name FROM vaults WHERE id = $1", vault_id)
        if not vault:
            return []

        rows = await conn.fetch(
            "SELECT id, name, description, columns, created_at FROM vault_tables WHERE vault_id = $1 ORDER BY name",
            vault_id,
        )

        results = []
        for r in rows:
            pg_name = _pg_table_name(vault["name"], r["name"])
            # Get row count from actual table
            try:
                count = await conn.fetchval(f"SELECT COUNT(*) FROM {pg_name}")
            except Exception:
                count = 0
            results.append({
                "table_id": str(r["id"]),
                "name": r["name"],
                "description": r["description"],
                "columns": ensure_list(r["columns"]) if isinstance(r["columns"], str) else r["columns"],
                "row_count": count,
                "created_at": r["created_at"].isoformat(),
            })
    return results


async def insert_rows(
    vault_id: uuid.UUID,
    table_name: str,
    rows: list[dict],
    created_by: str | None = None,
) -> dict:
    pool = await get_pool()
    async with pool.acquire() as conn:
        vault = await conn.fetchrow("SELECT name FROM vaults WHERE id = $1", vault_id)
        table = await conn.fetchrow(
            "SELECT id, columns FROM vault_tables WHERE vault_id = $1 AND name = $2",
            vault_id, table_name,
        )
        if not table:
            raise NotFoundError("Table", table_name)

        pg_name = _pg_table_name(vault["name"], table_name)
        columns_meta = ensure_list(table["columns"]) if isinstance(table["columns"], str) else list(table["columns"])
        col_names = [_safe_ident(c["name"]) for c in columns_meta]

        inserted = 0
        for row_data in rows:
            # Build INSERT with only columns that exist in row_data
            present_cols = [c for c in col_names if c in row_data or c.replace("_", "-") in row_data]
            if not present_cols:
                continue

            vals = []
            col_types = {_safe_ident(c["name"]): c.get("type", "text") for c in columns_meta}
            for c in present_cols:
                v = row_data.get(c) or row_data.get(c.replace("_", "-"))
                # Type conversion
                ctype = col_types.get(c, "text")
                if ctype == "date" and isinstance(v, str):
                    from datetime import date as _date
                    try:
                        v = _date.fromisoformat(v)
                    except ValueError:
                        v = None
                elif ctype == "number" and isinstance(v, str):
                    try:
                        v = float(v)
                    except ValueError:
                        pass
                vals.append(v)

            placeholders = ", ".join(f"${i+1}" for i in range(len(present_cols)))
            col_list = ", ".join(present_cols)

            if created_by:
                col_list += ", created_by"
                placeholders += f", ${len(present_cols)+1}"
                vals.append(created_by)

            await conn.execute(
                f"INSERT INTO {pg_name} ({col_list}) VALUES ({placeholders})",
                *vals,
            )
            inserted += 1

    return {"inserted": inserted, "table": table_name}


async def query_table(
    vault_id: uuid.UUID,
    table_name: str,
    where: dict | None = None,
    order_by: str | None = None,
    order_desc: bool = False,
    limit: int = 100,
    offset: int = 0,
    aggregate: dict | None = None,
) -> dict:
    pool = await get_pool()
    async with pool.acquire() as conn:
        vault = await conn.fetchrow("SELECT name FROM vaults WHERE id = $1", vault_id)
        table = await conn.fetchrow(
            "SELECT id, columns FROM vault_tables WHERE vault_id = $1 AND name = $2",
            vault_id, table_name,
        )
        if not table:
            raise NotFoundError("Table", table_name)

        pg_name = _pg_table_name(vault["name"], table_name)

        # Build WHERE
        conditions = []
        params: list = []
        idx = 1

        if where:
            for key, value in where.items():
                col = _safe_ident(key.split("__")[0])
                if key.endswith("__gte"):
                    conditions.append(f"{col} >= ${idx}")
                    params.append(value)
                elif key.endswith("__lte"):
                    conditions.append(f"{col} <= ${idx}")
                    params.append(value)
                elif key.endswith("__gt"):
                    conditions.append(f"{col} > ${idx}")
                    params.append(value)
                elif key.endswith("__lt"):
                    conditions.append(f"{col} < ${idx}")
                    params.append(value)
                elif key.endswith("__like"):
                    conditions.append(f"{col}::text ILIKE ${idx}")
                    params.append(f"%{value}%")
                else:
                    conditions.append(f"{col}::text = ${idx}")
                    params.append(str(value))
                idx += 1

        where_sql = " AND ".join(conditions) if conditions else "TRUE"

        # Aggregation
        if aggregate:
            agg_parts = []
            for func, field in aggregate.items():
                if func == "count":
                    agg_parts.append("COUNT(*) as count")
                elif func in ("sum", "avg", "min", "max") and field != "*":
                    col = _safe_ident(field)
                    agg_parts.append(f"{func.upper()}({col}) as {func}_{col}")
            agg_sql = ", ".join(agg_parts) if agg_parts else "COUNT(*) as count"

            row = await conn.fetchrow(
                f"SELECT {agg_sql} FROM {pg_name} WHERE {where_sql}",
                *params,
            )
            return {"aggregate": dict(row), "table": table_name}

        # Regular query
        order_sql = ""
        if order_by:
            col = _safe_ident(order_by)
            direction = "DESC" if order_desc else "ASC"
            order_sql = f"ORDER BY {col} {direction}"
        else:
            order_sql = "ORDER BY created_at DESC"

        params.extend([limit, offset])
        rows = await conn.fetch(
            f"SELECT * FROM {pg_name} WHERE {where_sql} {order_sql} LIMIT ${idx} OFFSET ${idx+1}",
            *params,
        )

        total = await conn.fetchval(
            f"SELECT COUNT(*) FROM {pg_name} WHERE {where_sql}",
            *params[:-2],
        )

        # Convert rows to dicts
        columns_meta = ensure_list(table["columns"]) if isinstance(table["columns"], str) else list(table["columns"])
        col_names = [c["name"] for c in columns_meta]

        result_rows = []
        for r in rows:
            data = {c: r.get(_safe_ident(c)) for c in col_names if _safe_ident(c) in dict(r)}
            result_rows.append({
                "row_id": str(r["id"]),
                "data": data,
                "created_by": r.get("created_by"),
                "created_at": r["created_at"].isoformat() if r.get("created_at") else None,
            })

    return {"table": table_name, "total": total, "rows": result_rows}


async def update_row(
    vault_id: uuid.UUID,
    table_name: str,
    row_id: str,
    data: dict,
) -> dict:
    pool = await get_pool()
    async with pool.acquire() as conn:
        vault = await conn.fetchrow("SELECT name FROM vaults WHERE id = $1", vault_id)
        table = await conn.fetchrow(
            "SELECT id FROM vault_tables WHERE vault_id = $1 AND name = $2",
            vault_id, table_name,
        )
        if not table:
            raise NotFoundError("Table", table_name)

        pg_name = _pg_table_name(vault["name"], table_name)

        sets = []
        params = []
        idx = 1
        for key, value in data.items():
            col = _safe_ident(key)
            sets.append(f"{col} = ${idx}")
            params.append(value)
            idx += 1

        sets.append(f"updated_at = ${idx}")
        params.append(datetime.now(timezone.utc))
        idx += 1

        params.append(uuid.UUID(row_id))
        await conn.execute(
            f"UPDATE {pg_name} SET {', '.join(sets)} WHERE id = ${idx}",
            *params,
        )

    return {"updated": True, "row_id": row_id}


async def delete_rows(
    vault_id: uuid.UUID,
    table_name: str,
    row_ids: list[str] | None = None,
) -> dict:
    pool = await get_pool()
    async with pool.acquire() as conn:
        vault = await conn.fetchrow("SELECT name FROM vaults WHERE id = $1", vault_id)
        table = await conn.fetchrow(
            "SELECT id FROM vault_tables WHERE vault_id = $1 AND name = $2",
            vault_id, table_name,
        )
        if not table:
            raise NotFoundError("Table", table_name)

        pg_name = _pg_table_name(vault["name"], table_name)

        if row_ids:
            uuids = [uuid.UUID(rid) for rid in row_ids]
            result = await conn.execute(
                f"DELETE FROM {pg_name} WHERE id = ANY($1)",
                uuids,
            )
        else:
            return {"deleted": 0}

        count = int(result.split(" ")[1]) if " " in result else 0
    return {"deleted": count, "table": table_name}


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
        table_map = await build_table_name_map(conn, vault_names)
        rewritten = rewrite_table_names(sql, table_map)

        # Reject multi-statement SQL (semicolon between statements)
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
                    # Convert special types to serializable
                    result_rows = []
                    for r in rows:
                        row = {}
                        for k, v in dict(r).items():
                            if isinstance(v, uuid.UUID):
                                row[k] = str(v)
                            elif hasattr(v, 'isoformat'):
                                row[k] = v.isoformat()
                            elif isinstance(v, (int, float, str, bool, type(None))):
                                row[k] = v
                            else:
                                row[k] = str(v)
                        result_rows.append(row)
                    return {
                        "columns": list(dict(rows[0]).keys()) if rows else [],
                        "rows": result_rows,
                        "total": len(result_rows),
                    }
                else:
                    result = await conn.execute(rewritten)
                    return {"result": result}
        except Exception as e:
            msg = str(e)
            if read_only and "read-only transaction" in msg:
                return {"error": "Write operation denied. You have read-only access to this vault."}
            return {"error": msg}
