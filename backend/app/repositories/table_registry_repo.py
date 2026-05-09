"""Repository for `vault_tables` — the registry of vault-scoped tables.

Module-level functions take an explicit `conn` so the caller controls
the transaction boundary. Same shape as `events_repo` — service code
composes repo calls under one `async with conn.transaction():` block.

Data tables (the `vt_***` PG tables that hold actual rows) live in
`table_data_repo`. This module only deals with the registry row.
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime
from typing import Any

from app.utils import ensure_list


async def find_by_name(conn, vault_id: uuid.UUID, name: str) -> dict | None:
    row = await conn.fetchrow(
        """
        SELECT id, vault_id, name, description, columns, created_by,
               created_at, updated_at
          FROM vault_tables
         WHERE vault_id = $1 AND name = $2
        """,
        vault_id, name,
    )
    return dict(row) if row else None


async def list_for_vault(conn, vault_id: uuid.UUID) -> list[dict]:
    rows = await conn.fetch(
        """
        SELECT id, name, description, columns, created_at
          FROM vault_tables
         WHERE vault_id = $1
         ORDER BY name
        """,
        vault_id,
    )
    return [dict(r) for r in rows]


async def insert(
    conn,
    *,
    table_id: uuid.UUID,
    vault_id: uuid.UUID,
    name: str,
    description: str,
    columns: list[dict],
    created_by: str | None,
    now: datetime,
) -> None:
    await conn.execute(
        """
        INSERT INTO vault_tables
            (id, vault_id, name, description, columns, created_by, created_at, updated_at)
        VALUES ($1, $2, $3, $4, $5, $6, $7, $7)
        """,
        table_id, vault_id, name, description, json.dumps(columns), created_by, now,
    )


async def delete(conn, table_id: uuid.UUID) -> None:
    await conn.execute("DELETE FROM vault_tables WHERE id = $1", table_id)


async def update_columns(conn, table_id: uuid.UUID, columns: list[dict]) -> None:
    await conn.execute(
        "UPDATE vault_tables SET columns = $1, updated_at = NOW() WHERE id = $2",
        json.dumps(columns), table_id,
    )


def parse_columns(raw: Any) -> list[dict]:
    """Normalise the `columns` jsonb to list[dict]. asyncpg returns it as
    a pre-parsed list normally, but legacy rows inserted as JSON string
    literals come back as `str` — handle both."""
    if isinstance(raw, str):
        return ensure_list(raw)
    return list(raw) if raw else []
