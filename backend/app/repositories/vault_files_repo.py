"""Repository for `vault_files` — metadata for binary files stored in S3.

The actual file bytes never touch this layer; S3 access lives in
`services/adapters/s3_adapter.py`. Module-level functions take an
explicit `conn` so the caller controls the transaction boundary.

Collection membership is normalized via the FK `vault_files.collection_id`
referencing `collections.id`. NULL == vault root. The legacy free-form
`vault_files.collection` TEXT column was removed in migration 020.
"""

from __future__ import annotations

import uuid


async def insert(
    conn,
    *,
    file_id: uuid.UUID,
    vault_id: uuid.UUID,
    name: str,
    s3_key: str,
    mime_type: str,
    size_bytes: int,
    description: str,
    created_by: str,
    collection_id: uuid.UUID | None = None,
) -> None:
    await conn.execute(
        """
        INSERT INTO vault_files
            (id, vault_id, collection_id, name, s3_key, mime_type,
             size_bytes, description, created_by)
        VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)
        """,
        file_id, vault_id, collection_id, name, s3_key,
        mime_type, size_bytes, description, created_by,
    )


async def find_by_id(
    conn,
    vault_id: uuid.UUID,
    file_id: uuid.UUID,
) -> dict | None:
    """Returns the file row joined with its collection.path. The
    `collection` field on the result dict is the human-readable path
    (or None for vault root), used by event payloads + browse renderers
    that need the path string."""
    row = await conn.fetchrow(
        """
        SELECT vf.id, vf.vault_id, vf.collection_id, c.path AS collection,
               vf.name, vf.s3_key, vf.mime_type, vf.size_bytes,
               vf.description, vf.created_by, vf.created_at, vf.updated_at
          FROM vault_files vf
          LEFT JOIN collections c ON c.id = vf.collection_id
         WHERE vf.id = $1 AND vf.vault_id = $2
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
    lifecycle (s3_delete_outbox is enqueued by file_service in the same
    TX so the worker drains S3 deletions atomically with the DB write)."""
    await conn.execute("DELETE FROM vault_files WHERE id = $1", file_id)


async def list_for_vault(
    conn,
    vault_id: uuid.UUID,
    *,
    collection_id: uuid.UUID | None = None,
    scoped: bool = False,
    limit: int = 50,
) -> list[dict]:
    """List files in a vault. When `scoped=True`, only rows whose
    `collection_id` matches `collection_id` (NULL == vault root) are
    returned. Default `scoped=False` returns every file regardless
    of collection (used by the top-level vault file list page)."""
    if scoped:
        if collection_id is None:
            rows = await conn.fetch(
                """
                SELECT vf.id, vf.collection_id, c.path AS collection, vf.name,
                       vf.mime_type, vf.size_bytes, vf.description,
                       vf.created_by, vf.created_at
                  FROM vault_files vf
                  LEFT JOIN collections c ON c.id = vf.collection_id
                 WHERE vf.vault_id = $1 AND vf.collection_id IS NULL
                 ORDER BY vf.created_at DESC
                 LIMIT $2
                """,
                vault_id, limit,
            )
        else:
            rows = await conn.fetch(
                """
                SELECT vf.id, vf.collection_id, c.path AS collection, vf.name,
                       vf.mime_type, vf.size_bytes, vf.description,
                       vf.created_by, vf.created_at
                  FROM vault_files vf
                  LEFT JOIN collections c ON c.id = vf.collection_id
                 WHERE vf.vault_id = $1 AND vf.collection_id = $2
                 ORDER BY vf.created_at DESC
                 LIMIT $3
                """,
                vault_id, collection_id, limit,
            )
    else:
        rows = await conn.fetch(
            """
            SELECT vf.id, vf.collection_id, c.path AS collection, vf.name,
                   vf.mime_type, vf.size_bytes, vf.description,
                   vf.created_by, vf.created_at
              FROM vault_files vf
              LEFT JOIN collections c ON c.id = vf.collection_id
             WHERE vf.vault_id = $1
             ORDER BY vf.created_at DESC
             LIMIT $2
            """,
            vault_id, limit,
        )
    return [dict(r) for r in rows]
