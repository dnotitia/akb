"""Migration: relations → edges.

Copies all existing document-to-document relations into the new unified edges table,
converting doc IDs to akb:// URIs. The old relations table is preserved (not dropped).

Run:  python -m app.db.migrations.001_relations_to_edges
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent.parent))

from app.db.postgres import get_pool, init_db, close_pool


async def migrate():
    await init_db()
    pool = await get_pool()

    async with pool.acquire() as conn:
        # Check if edges table exists
        exists = await conn.fetchval(
            "SELECT EXISTS(SELECT 1 FROM information_schema.tables WHERE table_name = 'edges')"
        )
        if not exists:
            print("edges table does not exist. Run init.sql first.")
            return

        # Check if migration already ran
        edge_count = await conn.fetchval("SELECT COUNT(*) FROM edges")
        if edge_count > 0:
            print(f"edges table already has {edge_count} rows — skipping migration.")
            return

        rows = await conn.fetch("""
            SELECT r.id, r.source_doc_id, r.target_doc_id, r.relation_type, r.created_at,
                   sd.path AS source_path, td.path AS target_path,
                   v.name AS vault_name, sd.vault_id
            FROM relations r
            JOIN documents sd ON r.source_doc_id = sd.id
            JOIN documents td ON r.target_doc_id = td.id
            JOIN vaults v ON sd.vault_id = v.id
        """)

        migrated = 0
        for r in rows:
            source_uri = f"akb://{r['vault_name']}/doc/{r['source_path']}"
            target_uri = f"akb://{r['vault_name']}/doc/{r['target_path']}"
            await conn.execute("""
                INSERT INTO edges (vault_id, source_uri, target_uri, relation_type,
                                   source_type, target_type, created_at)
                VALUES ($1, $2, $3, $4, 'doc', 'doc', $5)
                ON CONFLICT DO NOTHING
            """, r["vault_id"], source_uri, target_uri, r["relation_type"], r["created_at"])
            migrated += 1

        print(f"Migrated {migrated} relations → edges")

    await close_pool()


if __name__ == "__main__":
    asyncio.run(migrate())
