#!/usr/bin/env python3
"""Machine-check the stop-the-world SSO epoch upgrade/rollback contract."""

from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path
import sys

# Kubernetes and the documented rollback procedure execute this file directly.
# Python then puts only ``scripts/`` on sys.path, so make the backend package
# root explicit instead of relying on a caller-provided PYTHONPATH.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import asyncpg

from app.config import settings
from app.db.postgres import close_pool, init_db
from app.services.sso_session_epoch import (
    prepare_sso_session_epoch_rollback,
    reconcile_sso_session_epoch,
)


async def _status(conn) -> dict[str, object]:
    marker_exists = await conn.fetchval("SELECT to_regclass('auth_runtime_epoch_upgrade') IS NOT NULL")
    if not marker_exists:
        return {
            "contract": "stop-the-world-v1",
            "state": "migration_pending",
            "legacy_rows_present": None,
        }
    state = await conn.fetchval("SELECT state FROM auth_runtime_epoch_upgrade WHERE singleton = TRUE")
    legacy_rows_present = await conn.fetchval(
        """
        SELECT EXISTS (
            SELECT 1 FROM admin_browser_sessions WHERE session_epoch IS NULL
            UNION ALL
            SELECT 1 FROM sso_browser_sessions WHERE session_epoch IS NULL
            UNION ALL
            SELECT 1 FROM sso_browser_logout_fences WHERE session_epoch IS NULL
        )
        """
    )
    return {
        "contract": "stop-the-world-v1",
        "state": state,
        "legacy_rows_present": legacy_rows_present,
    }


async def _connect():
    return await asyncpg.connect(
        settings.asyncpg_dsn,
        server_settings={"application_name": "akb-auth-runtime-preflight"},
    )


async def _assert_quiescent(conn) -> None:
    others = await conn.fetchval(
        """
        SELECT COUNT(*)
          FROM pg_stat_activity
         WHERE datname = current_database()
           AND pid <> pg_backend_pid()
           AND backend_type = 'client backend'
        """
    )
    if others:
        raise RuntimeError("stop-the-world-v1 requires every backend and database client to be stopped")


async def _run(command: str) -> dict[str, object]:
    conn = await _connect()
    try:
        if command == "status":
            return await _status(conn)
        await _assert_quiescent(conn)
    finally:
        await conn.close()

    try:
        if command == "prepare-upgrade":
            await init_db()
            await reconcile_sso_session_epoch(upgrade_preflight=True)
        elif command == "prepare-rollback":
            await prepare_sso_session_epoch_rollback()
        else:
            raise ValueError("unknown command")
    finally:
        await close_pool()

    conn = await _connect()
    try:
        return await _status(conn)
    finally:
        await conn.close()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "command",
        choices=("status", "prepare-upgrade", "prepare-rollback"),
    )
    args = parser.parse_args()
    result = asyncio.run(_run(args.command))
    print(json.dumps(result, sort_keys=True))


if __name__ == "__main__":
    main()
