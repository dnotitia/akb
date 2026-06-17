"""Repository for `vault_tables` — the registry of vault-scoped tables.

Module-level functions take an explicit `conn` so the caller controls
the transaction boundary. Same shape as `events_repo` — service code
composes repo calls under one `async with conn.transaction():` block.

Data tables (the `vt_***` PG tables that hold actual rows) live in
`table_data_repo`. This module only deals with the registry row.

Collection membership is normalized via FK `vault_tables.collection_id`
referencing `collections.id` (NULL == vault root). Table names remain
unique within a vault (NOT per-collection), so the PG-side
`pg_table_name(vault, name)` mapping is unchanged and a table can be
moved between collections without renaming.
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime
from typing import Any

from app.utils import ensure_list


async def find_by_name(conn, vault_id: uuid.UUID, name: str) -> dict | None:
    """Look up a table by (vault, name). Name is unique within a vault
    (across all collections) so no collection scoping is needed here.
    The returned row carries `collection_id` for callers that need to
    know which collection the table currently belongs to."""
    row = await conn.fetchrow(
        """
        SELECT vt.id, vt.vault_id, vt.collection_id, c.path AS collection,
               vt.name, vt.description, vt.columns,
               vt.unique_keys, vt.indexes, vt.created_by,
               vt.created_at, vt.updated_at
          FROM vault_tables vt
          LEFT JOIN collections c ON c.id = vt.collection_id
         WHERE vt.vault_id = $1 AND vt.name = $2
        """,
        vault_id, name,
    )
    return dict(row) if row else None


async def list_for_vault(
    conn,
    vault_id: uuid.UUID,
    *,
    collection_id: uuid.UUID | None = None,
    scoped: bool = False,
    max_depth: int | None = None,
    prefix: str = "",
) -> list[dict]:
    """List tables in a vault.

    Two filtering modes — pick one:

    * ``scoped=True`` — equality on ``collection_id``
      (``None`` ⇒ ``IS NULL``). Returns tables sitting directly in
      that collection (or vault root). Used by ``collection=X, depth=0``
      browse and analogous internal paths.

    * ``max_depth`` is not ``None`` — tree-depth filter from ``prefix``.
      A table at collection ``X/Y`` has depth 2 from vault root; depth 1
      from prefix ``X``. Tables with ``collection_id IS NULL`` are at
      depth 0. ``max_depth < 0`` disables the depth filter
      (entire subtree). Used by the unified vault browse to honor
      ``depth=N``.

    Default (no flags) returns every table regardless of collection
    — preserved so legacy callers see no behaviour change.
    """
    if scoped:
        if collection_id is None:
            rows = await conn.fetch(
                """
                SELECT vt.id, vt.collection_id, c.path AS collection,
                       vt.name, vt.description, vt.columns,
                       vt.unique_keys, vt.indexes, vt.created_at
                  FROM vault_tables vt
                  LEFT JOIN collections c ON c.id = vt.collection_id
                 WHERE vt.vault_id = $1 AND vt.collection_id IS NULL
                 ORDER BY vt.name
                """,
                vault_id,
            )
        else:
            rows = await conn.fetch(
                """
                SELECT vt.id, vt.collection_id, c.path AS collection,
                       vt.name, vt.description, vt.columns,
                       vt.unique_keys, vt.indexes, vt.created_at
                  FROM vault_tables vt
                  LEFT JOIN collections c ON c.id = vt.collection_id
                 WHERE vt.vault_id = $1 AND vt.collection_id = $2
                 ORDER BY vt.name
                """,
                vault_id, collection_id,
            )
    elif max_depth is not None:
        params: list = [vault_id]
        prefix_clause = ""
        if prefix:
            from app.util.text import like_escape
            safe_prefix = like_escape(prefix)
            params.append(safe_prefix)
            params.append(safe_prefix + "/%")
            prefix_clause = (
                f" AND (c.path = ${len(params)-1} "
                f"OR c.path LIKE ${len(params)} ESCAPE '\\')"
            )
            depth_offset = prefix.count("/") + 1
        else:
            depth_offset = 0

        if max_depth < 0:
            depth_clause = ""
        else:
            params.append(max_depth + depth_offset)
            # Depth of a table = number of segments in its collection
            # path. NULL collection ⇒ depth 0. For non-NULL, segments
            # = slashes + 1.
            depth_clause = (
                f" AND COALESCE("
                f"length(c.path) - length(replace(c.path, '/', '')) + 1, 0"
                f") <= ${len(params)}"
            )
        # Without a prefix the NULL-collection tables (vault root,
        # depth 0) are always included if depth allows; the LEFT JOIN
        # preserves them. With a prefix they're excluded by the
        # prefix_clause (NULL fails both equality and LIKE), which is
        # the desired scoping behaviour.
        sql = (
            "SELECT vt.id, vt.collection_id, c.path AS collection, "
            "       vt.name, vt.description, vt.columns, "
            "       vt.unique_keys, vt.indexes, vt.created_at "
            "  FROM vault_tables vt "
            "  LEFT JOIN collections c ON c.id = vt.collection_id "
            " WHERE vt.vault_id = $1"
            + prefix_clause
            + depth_clause
            + " ORDER BY vt.name"
        )
        rows = await conn.fetch(sql, *params)
    else:
        rows = await conn.fetch(
            """
            SELECT vt.id, vt.collection_id, c.path AS collection,
                   vt.name, vt.description, vt.columns,
                   vt.unique_keys, vt.indexes, vt.created_at
              FROM vault_tables vt
              LEFT JOIN collections c ON c.id = vt.collection_id
             WHERE vt.vault_id = $1
             ORDER BY vt.name
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
    collection_id: uuid.UUID | None = None,
    unique_keys: list[dict] | None = None,
    indexes: list[dict] | None = None,
) -> None:
    await conn.execute(
        """
        INSERT INTO vault_tables
            (id, vault_id, collection_id, name, description, columns,
             unique_keys, indexes,
             created_by, created_at, updated_at)
        VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $10)
        """,
        table_id, vault_id, collection_id, name, description,
        json.dumps(columns),
        json.dumps(unique_keys or []), json.dumps(indexes or []),
        created_by, now,
    )


async def delete(conn, table_id: uuid.UUID) -> None:
    await conn.execute("DELETE FROM vault_tables WHERE id = $1", table_id)


async def update_columns(conn, table_id: uuid.UUID, columns: list[dict]) -> None:
    await conn.execute(
        "UPDATE vault_tables SET columns = $1, updated_at = NOW() WHERE id = $2",
        json.dumps(columns), table_id,
    )


async def update_schema_meta(
    conn,
    table_id: uuid.UUID,
    *,
    unique_keys: list[dict],
    indexes: list[dict],
) -> None:
    """Persist the resolved ``unique_keys`` / ``indexes`` metadata.

    Called inside the same TX as the physical DDL so the registry can
    never drift from the live schema (AKB #215 AC #10)."""
    await conn.execute(
        "UPDATE vault_tables "
        "   SET unique_keys = $1, indexes = $2, updated_at = NOW() "
        " WHERE id = $3",
        json.dumps(unique_keys), json.dumps(indexes), table_id,
    )


def parse_columns(raw: Any) -> list[dict]:
    """Normalise the `columns` jsonb to list[dict]. asyncpg returns it as
    a pre-parsed list normally, but legacy rows inserted as JSON string
    literals come back as `str` — handle both."""
    if isinstance(raw, str):
        return ensure_list(raw)
    return list(raw) if raw else []


def parse_json_list(raw: Any) -> list[dict]:
    """Normalise a JSONB list column (``unique_keys`` / ``indexes``) to
    ``list[dict]``. Mirrors :func:`parse_columns`: asyncpg usually
    returns a pre-parsed list, but legacy rows inserted as JSON string
    literals come back as ``str`` — handle both, and ``None`` → ``[]``."""
    if isinstance(raw, str):
        return ensure_list(raw)
    return list(raw) if raw else []
