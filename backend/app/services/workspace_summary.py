"""Small, account-scoped inventory snapshot for the Home workspace.

No stored counters, resource bodies, table row counts, or per-Vault detail
requests. The directory ACL and all four totals share one read-only snapshot.
"""

from __future__ import annotations

import uuid
from datetime import timezone

from app.db.postgres import get_pool
from app.repositories.vault_files_repo import confirmed_file_predicate
from app.services.access_service import list_accessible_vaults
from app.services.document_counters import scoped_document_count_sql

MAX_SAFE_COUNT = 2**53 - 1


async def get_workspace_summary(user_id: str) -> dict:
    pool = await get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction(isolation="repeatable_read", readonly=True):
            vaults = await list_accessible_vaults(user_id, conn=conn)
            vault_ids = list(dict.fromkeys(uuid.UUID(vault["id"]) for vault in vaults))
            # Only trusted SQL fragments are interpolated; the access-scoped
            # identifiers stay bound parameters. COUNT always returns a row,
            # including for an empty directory.
            row = await conn.fetchrow(
                "SELECT CURRENT_TIMESTAMP AS observed_at, "
                f"({scoped_document_count_sql()}) AS document_count, "
                "(SELECT COUNT(*) FROM vault_tables WHERE vault_id = ANY($1::uuid[])) AS table_count, "
                "(SELECT COUNT(*) FROM vault_files vf WHERE vault_id = ANY($1::uuid[]) AND "
                f"{confirmed_file_predicate('vf')}) AS file_count",
                vault_ids,
            )

    result = {
        "version": 1,
        "scope": "accessible",
        "observed_at": row["observed_at"].astimezone(timezone.utc).isoformat(),
    }
    counts = {"vault_count": len(vault_ids), **{
        field: row[field] for field in ("document_count", "table_count", "file_count")
    }}
    for field, value in counts.items():
        # Absence is not zero. JSON consumers cannot exactly represent larger
        # integers, and an unexpected invalid value must not become a total.
        if type(value) is int and 0 <= value <= MAX_SAFE_COUNT:
            result[field] = value
    return result
