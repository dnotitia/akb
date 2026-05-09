"""Repository for vault-scoped dynamic tables (the `vt_***` PG tables
that hold actual row data).

Owns identifier sanitisation, DDL primitives, and the SQL-name
rewriting used by `execute_sql` and the `table_query` share path.
The registry row in `vault_tables` lives in `table_registry_repo`.

Module-level functions take an explicit `conn` so the caller controls
the transaction boundary.

Row-level DML (INSERT/UPDATE/DELETE on a single row) is intentionally
not exposed here: all row-level mutations happen through
`execute_sql`, which gives operators raw SQL with proper read-only /
write enforcement at the PG transaction level. If a structured
row-CRUD API is added later it will live in this module.
"""

from __future__ import annotations

import re


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
