#!/usr/bin/env python3
"""Audit or remove graph edges outside their owning vault.

The default mode is read-only and reports aggregate counts only. Full resource
URIs are intentionally not printed because vault and path names may be
sensitive. Cleanup is explicit, confirmation-gated, transactional, and
idempotent.
"""

from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path
import sys

# Operators run this file directly from ``backend/`` (and the container does
# the same), which otherwise places only ``scripts/`` on Python's import path.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.db.postgres import close_pool, get_pool
from app.services.edge_boundary import invalid_edge_predicate


_CONFIRMATION = "DELETE_CROSS_VAULT_EDGES"
def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Report knowledge-graph edges outside their owning vault",
    )
    parser.add_argument(
        "--delete",
        action="store_true",
        help="Delete every reported row in one transaction",
    )
    parser.add_argument(
        "--confirm",
        help=f"Required with --delete; must equal {_CONFIRMATION!r}",
    )
    return parser


async def _audit(*, delete: bool) -> dict:
    pool = await get_pool()
    invalid = invalid_edge_predicate()
    async with pool.acquire() as conn:
        async with conn.transaction(readonly=not delete):
            rows = await conn.fetch(
                f"""
                SELECT e.kind, e.relation_type, count(*) AS count
                FROM edges e
                JOIN vaults v ON v.id = e.vault_id
                WHERE {invalid}
                GROUP BY e.kind, e.relation_type
                ORDER BY e.kind, e.relation_type
                """
            )
            groups = [
                {
                    "kind": row["kind"],
                    "relation_type": row["relation_type"],
                    "count": row["count"],
                }
                for row in rows
            ]
            total = sum(group["count"] for group in groups)
            deleted = 0
            if delete and total:
                result = await conn.execute(
                    f"""
                    DELETE FROM edges e
                    USING vaults v
                    WHERE v.id = e.vault_id
                      AND ({invalid})
                    """
                )
                deleted = int(result.rsplit(" ", 1)[-1])
    return {"invalid_edges": total, "groups": groups, "deleted": deleted}


async def _main() -> int:
    args = _parser().parse_args()
    if args.delete and args.confirm != _CONFIRMATION:
        raise SystemExit(
            f"--delete requires --confirm {_CONFIRMATION}"
        )
    try:
        print(json.dumps(await _audit(delete=args.delete), sort_keys=True))
    finally:
        await close_pool()
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(_main()))
