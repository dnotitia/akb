#!/usr/bin/env python3
"""Backfill existing violations of the overview/skill reservation.

Thin runner around `app.services.skill_reservation_backfill.run` — all logic
(and the service-layer-only rule) lives there. Mirror vaults are excluded, and
so are ARCHIVED vaults unless `--include-archived` is passed; the dry run's
`archived_excluded` count shows what that exclusion leaves behind.

DEFAULT IS A DRY RUN: it only scans and prints per-class counts. Pass
`--execute` to apply the repairs.

Files/tables under overview are report-only because they have no move API.
Legacy subcollections are removed when empty; non-empty ones are reported as
errors. An execute run exits non-zero while either class remains.

The stdout summary is COUNTS ONLY, deliberately: it gets pasted into a public
repo's PRs/issues, so no vault name or document path may appear there. Per-item
detail goes to logging.

Usage:
    python -m scripts.backfill_skill_reservation             # dry run (counts)
    python -m scripts.backfill_skill_reservation --execute   # apply
    python -m scripts.backfill_skill_reservation --execute --include-archived
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from app.db.postgres import init_db, close_pool
from app.services import skill_reservation_backfill
from app.services.revision_backend import get_document_service


async def main() -> int:
    ap = argparse.ArgumentParser(
        description="Backfill overview/skill reservation violations."
    )
    ap.add_argument(
        "--execute", action="store_true",
        help="apply the repairs (default: dry run, counts only)",
    )
    ap.add_argument(
        "--include-archived", action="store_true",
        help="also repair archived (read-only) vaults; requires a PM decision, "
             "since it rewrites a frozen vault's git history",
    )
    args = ap.parse_args()

    logging.basicConfig(level=logging.INFO, stream=sys.stderr)

    await init_db()
    try:
        result = await skill_reservation_backfill.run(
            get_document_service(),
            execute=args.execute,
            include_archived=args.include_archived,
        )
    finally:
        await close_pool()

    if result["dry_run"]:
        print(result)
        return 0
    errors = result["errors"]
    resources = result["resource_violations"]
    # Redacted summary: counts + an error COUNT. The error entries carry
    # vault/path detail and were already logged; they never go to stdout.
    print({
        "dry_run": False,
        "done": result["done"],
        "errors": len(errors),
        "resource_violations": resources,
        "reserved_subcollections": result["reserved_subcollections"],
    })
    # Files/tables inside the reserved namespace have no automated treatment
    # (no move operation exists for either), so they are counted, not fixed.
    # A non-zero count therefore has to fail the run: exiting 0 here would let
    # a document-only repair be read as "the namespace is now compliant".
    return 1 if (
        errors or any(resources.values()) or result["reserved_subcollections"]
    ) else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
