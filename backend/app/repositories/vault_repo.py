"""Repository for vault operations."""

from __future__ import annotations

import uuid

import asyncpg


class VaultRepository:
    def __init__(self, pool: asyncpg.Pool):
        self.pool = pool

    async def get_by_name(self, name: str) -> dict | None:
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow("SELECT * FROM vaults WHERE name = $1", name)
            return dict(row) if row else None

    async def get_id_by_name(self, name: str) -> uuid.UUID | None:
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow("SELECT id FROM vaults WHERE name = $1", name)
            return row["id"] if row else None

    async def list_all(self) -> list[dict]:
        async with self.pool.acquire() as conn:
            rows = await conn.fetch("SELECT id, name, description, created_at FROM vaults ORDER BY name")
            return [
                {
                    "id": str(r["id"]),
                    "name": r["name"],
                    "description": r["description"],
                    "created_at": r["created_at"].isoformat() if r["created_at"] else None,
                }
                for r in rows
            ]

    async def create(
        self,
        name: str,
        description: str,
        git_path: str,
        owner_id: uuid.UUID | None = None,
        public_access: str = "none",
        conn=None,
    ) -> uuid.UUID:
        vault_id = uuid.uuid4()
        sql = "INSERT INTO vaults (id, name, description, git_path, owner_id, public_access) VALUES ($1, $2, $3, $4, $5, $6)"
        args = (vault_id, name, description, git_path, owner_id, public_access)
        if conn is not None:
            await conn.execute(sql, *args)
        else:
            async with self.pool.acquire() as acq:
                await acq.execute(sql, *args)
        return vault_id
