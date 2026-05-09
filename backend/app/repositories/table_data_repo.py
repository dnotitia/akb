"""Repository for vault-scoped dynamic tables (the `vt_***` PG tables
that hold actual row data).

Owns identifier sanitisation, DDL, DML, and the SQL-name rewriting used
by `execute_sql` and the `table_query` share path. The registry row in
`vault_tables` lives in `table_registry_repo`.

Module-level functions take an explicit `conn` so the caller controls
the transaction boundary.
"""

from __future__ import annotations

import re
import uuid
from datetime import date as _date, datetime, timezone


TYPE_MAP = {
    "text": "TEXT",
    "number": "NUMERIC",
    "boolean": "BOOLEAN",
    "date": "DATE",
    "json": "JSONB",
}


# ── Identifier helpers ───────────────────────────────────────────


def pg_table_name(vault_name: str, table_name: str) -> str:
    """Return the PG table name for a vault-scoped table:
    `vt_{sanitised_vault}__{sanitised_table}`."""
    v = re.sub(r"[^a-z0-9]", "_", vault_name.lower())
    t = re.sub(r"[^a-z0-9]", "_", table_name.lower().replace("-", "_"))
    return f"vt_{v}__{t}"


def safe_ident(name: str) -> str:
    """Sanitise a column / table name for use as a SQL identifier."""
    return re.sub(r"[^a-zA-Z0-9_]", "_", name)


def _coerce(v, ctype: str):
    """Apply the same string-to-typed coercions the service always did
    on insert (date ISO → date, number → float). Unknown / unparseable
    values pass through unchanged."""
    if v is None:
        return v
    if ctype == "date" and isinstance(v, str):
        try:
            return _date.fromisoformat(v)
        except ValueError:
            return None
    if ctype == "number" and isinstance(v, str):
        try:
            return float(v)
        except ValueError:
            return v
    return v


# ── DDL ──────────────────────────────────────────────────────────


async def create_dynamic_table(conn, pg_name: str, columns: list[dict]) -> None:
    """Create the data-bearing PG table for a vault table. Caller is
    responsible for sanitising `pg_name` (use `pg_table_name`)."""
    col_defs = ["id UUID PRIMARY KEY DEFAULT uuid_generate_v4()"]
    for col in columns:
        col_name = safe_ident(col["name"])
        col_type = TYPE_MAP.get(col.get("type", "text"), "TEXT")
        not_null = " NOT NULL" if col.get("required") else ""
        col_defs.append(f"{col_name} {col_type}{not_null}")
    col_defs.append("created_by TEXT")
    col_defs.append("created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()")
    col_defs.append("updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()")
    await conn.execute(f'CREATE TABLE {pg_name} ({", ".join(col_defs)})')


async def drop_dynamic_table(conn, pg_name: str) -> None:
    await conn.execute(f"DROP TABLE IF EXISTS {pg_name}")


async def count_rows(conn, pg_name: str) -> int:
    """Returns 0 if the table does not exist (used by list_tables on a
    registry row whose data table was already dropped)."""
    try:
        return int(await conn.fetchval(f"SELECT COUNT(*) FROM {pg_name}") or 0)
    except Exception:  # noqa: BLE001 — table-missing is the usual case here
        return 0


# ── DML ──────────────────────────────────────────────────────────


async def insert_row(
    conn,
    pg_name: str,
    columns_meta: list[dict],
    row_data: dict,
    created_by: str | None = None,
) -> bool:
    """Insert one row. Returns True if at least one known column was
    present in `row_data`, False if the row was skipped because nothing
    matched the schema."""
    col_names = [safe_ident(c["name"]) for c in columns_meta]
    col_types = {safe_ident(c["name"]): c.get("type", "text") for c in columns_meta}

    present_cols = [
        c for c in col_names
        if c in row_data or c.replace("_", "-") in row_data
    ]
    if not present_cols:
        return False

    vals = []
    for c in present_cols:
        v = row_data.get(c) or row_data.get(c.replace("_", "-"))
        vals.append(_coerce(v, col_types.get(c, "text")))

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
    return True


async def update_row(conn, pg_name: str, row_id: str, data: dict) -> None:
    sets: list[str] = []
    params: list = []
    idx = 1
    for key, value in data.items():
        col = safe_ident(key)
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


async def delete_rows_by_id(conn, pg_name: str, row_ids: list[str]) -> int:
    uuids = [uuid.UUID(rid) for rid in row_ids]
    result = await conn.execute(
        f"DELETE FROM {pg_name} WHERE id = ANY($1)",
        uuids,
    )
    return int(result.split(" ")[1]) if " " in result else 0


# ── Query ────────────────────────────────────────────────────────


def _build_where(where: dict | None) -> tuple[list[str], list]:
    conditions: list[str] = []
    params: list = []
    if not where:
        return conditions, params
    idx = 1
    for key, value in where.items():
        col = safe_ident(key.split("__")[0])
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
    return conditions, params


async def query_rows(
    conn,
    pg_name: str,
    columns_meta: list[dict],
    *,
    where: dict | None = None,
    order_by: str | None = None,
    order_desc: bool = False,
    limit: int = 100,
    offset: int = 0,
) -> tuple[list[dict], int]:
    conditions, params = _build_where(where)
    where_sql = " AND ".join(conditions) if conditions else "TRUE"

    if order_by:
        col = safe_ident(order_by)
        direction = "DESC" if order_desc else "ASC"
        order_sql = f"ORDER BY {col} {direction}"
    else:
        order_sql = "ORDER BY created_at DESC"

    idx = len(params) + 1
    rows = await conn.fetch(
        f"SELECT * FROM {pg_name} WHERE {where_sql} {order_sql} "
        f"LIMIT ${idx} OFFSET ${idx+1}",
        *params, limit, offset,
    )
    total = int(await conn.fetchval(
        f"SELECT COUNT(*) FROM {pg_name} WHERE {where_sql}",
        *params,
    ) or 0)

    col_names = [c["name"] for c in columns_meta]
    result_rows: list[dict] = []
    for r in rows:
        rd = dict(r)
        data = {c: rd.get(safe_ident(c)) for c in col_names if safe_ident(c) in rd}
        result_rows.append({
            "row_id": str(r["id"]),
            "data": data,
            "created_by": r.get("created_by"),
            "created_at": r["created_at"].isoformat() if r.get("created_at") else None,
        })
    return result_rows, total


async def aggregate_rows(
    conn,
    pg_name: str,
    *,
    where: dict | None,
    aggregate: dict,
) -> dict:
    conditions, params = _build_where(where)
    where_sql = " AND ".join(conditions) if conditions else "TRUE"

    agg_parts: list[str] = []
    for func, field in aggregate.items():
        if func == "count":
            agg_parts.append("COUNT(*) as count")
        elif func in ("sum", "avg", "min", "max") and field != "*":
            col = safe_ident(field)
            agg_parts.append(f"{func.upper()}({col}) as {func}_{col}")
    agg_sql = ", ".join(agg_parts) if agg_parts else "COUNT(*) as count"

    row = await conn.fetchrow(
        f"SELECT {agg_sql} FROM {pg_name} WHERE {where_sql}",
        *params,
    )
    return dict(row) if row else {}


# ── SQL rewriting (for execute_sql + table_query share path) ─────


async def build_table_name_map(conn, vault_names: list[str]) -> dict[str, str]:
    """Map friendly table aliases → real PG names.

    Single vault: bare ('pipeline') and prefixed ('sales__pipeline')
    forms both accepted. Multi-vault: only prefixed form, to avoid
    ambiguity. Raises NotFoundError on a missing vault.
    """
    from app.exceptions import NotFoundError

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
            pg_name = pg_table_name(vname, t["name"])
            sanitized_vault = re.sub(r"[^a-z0-9]", "_", vname.lower())
            sanitized_table = t["name"].replace("-", "_")
            table_map[f"{sanitized_vault}__{sanitized_table}"] = pg_name
            if len(vault_names) == 1:
                table_map[t["name"]] = pg_name
                table_map[t["name"].replace("-", "_")] = pg_name
    return table_map


def rewrite_table_names(sql: str, table_map: dict[str, str]) -> str:
    """Replace short table names in `sql` with their pg-qualified names.
    Longest match first to avoid partial collisions (e.g. 'sales' vs
    'sales_v2')."""
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
