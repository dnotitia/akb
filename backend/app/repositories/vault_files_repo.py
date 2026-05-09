"""Repository for `vault_files` — metadata for binary files stored in S3.

The actual file bytes never touch this layer; S3 access lives in
`services/adapters/s3_adapter.py`. Module-level functions take an
explicit `conn` so the caller controls the transaction boundary.
"""

from __future__ import annotations

import uuid


async def insert(
    conn,
    *,
    file_id: uuid.UUID,
    vault_id: uuid.UUID,
    collection: str,
    name: str,
    s3_key: str,
    mime_type: str,
    size_bytes: int,
    description: str,
    created_by: str,
) -> None:
    await conn.execute(
        """
        INSERT INTO vault_files
            (id, vault_id, collection, name, s3_key, mime_type, size_bytes, description, created_by)
        VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)
        """,
        file_id, vault_id, collection, name, s3_key,
        mime_type, size_bytes, description, created_by,
    )


async def find_by_id(
    conn,
    vault_id: uuid.UUID,
    file_id: uuid.UUID,
) -> dict | None:
    row = await conn.fetchrow(
        """
        SELECT id, vault_id, collection, name, s3_key, mime_type,
               size_bytes, description, created_by, created_at, updated_at
          FROM vault_files
         WHERE id = $1 AND vault_id = $2
        """,
        file_id, vault_id,
    )
    return dict(row) if row else None


async def update_size(conn, file_id: uuid.UUID, size_bytes: int) -> None:
    await conn.execute(
        "UPDATE vault_files SET size_bytes = $1, updated_at = NOW() WHERE id = $2",
        size_bytes, file_id,
    )


async def delete(conn, file_id: uuid.UUID) -> None:
    """Delete the metadata row only. Caller is responsible for S3 object
    lifecycle (commit 6 introduces the s3_delete_outbox so the worker
    drains S3 deletions atomically with the DB DELETE)."""
    await conn.execute("DELETE FROM vault_files WHERE id = $1", file_id)


async def list_for_vault(
    conn,
    vault_id: uuid.UUID,
    *,
    collection: str | None = None,
    limit: int = 50,
) -> list[dict]:
    if collection:
        rows = await conn.fetch(
            """
            SELECT id, collection, name, mime_type, size_bytes, description,
                   created_by, created_at
              FROM vault_files
             WHERE vault_id = $1 AND collection = $2
             ORDER BY created_at DESC
             LIMIT $3
            """,
            vault_id, collection, limit,
        )
    else:
        rows = await conn.fetch(
            """
            SELECT id, collection, name, mime_type, size_bytes, description,
                   created_by, created_at
              FROM vault_files
             WHERE vault_id = $1
             ORDER BY created_at DESC
             LIMIT $2
            """,
            vault_id, limit,
        )
    return [dict(r) for r in rows]
