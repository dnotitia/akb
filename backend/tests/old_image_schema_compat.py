#!/usr/bin/env python3
"""Run the pre-governance auth flow against an already-expanded database."""

from __future__ import annotations

import argparse
import asyncio
import sys
import uuid
from pathlib import Path
from urllib.parse import unquote, urlparse


def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--backend", required=True, type=Path)
    parser.add_argument("--dsn", required=True)
    return parser.parse_args()


def _configure_old_backend(backend: Path, dsn: str):
    sys.path.insert(0, str(backend.resolve()))
    from app.config import settings

    parsed = urlparse(dsn)
    if parsed.scheme not in {"postgres", "postgresql"} or not parsed.hostname:
        raise RuntimeError("AKB_TEST_DSN must be a PostgreSQL URL")
    settings.db_host = parsed.hostname
    settings.db_port = parsed.port or 5432
    settings.db_name = parsed.path.lstrip("/")
    settings.db_user = unquote(parsed.username or "")
    settings.db_password = unquote(parsed.password or "")
    settings.jwt_secret = (  # pragma: allowlist secret
        "old-image-compatibility-secret-32-bytes"
    )
    return settings


class _RoleSyncStub:
    async def on_user_create(self, _user_id) -> None:
        return None


async def _run(backend: Path, dsn: str) -> None:
    _configure_old_backend(backend, dsn)
    from app.db.postgres import close_pool, init_db
    from app.services import auth_service as auth

    auth.get_role_sync = lambda: _RoleSyncStub()
    await init_db()
    username = f"old-image-{uuid.uuid4().hex[:12]}"
    registered_id: str | None = None
    try:
        registered = await auth.register(
            username,
            f"{username}@example.com",
            "known-password",
        )
        registered_id = registered["user_id"]
        logged_in = await auth.login(username, "known-password")
        minted = await auth.create_pat(registered_id, "old-image-compat")

        assert logged_in["user"]["id"] == registered_id
        assert minted["token"].startswith("akb_")
        pool = await auth.get_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT account_status, account_kind
                  FROM users WHERE id = $1
                """,
                uuid.UUID(registered_id),
            )
        assert dict(row) == {
            "account_status": "active",
            "account_kind": "human",
        }
    finally:
        if registered_id is not None:
            pool = await auth.get_pool()
            async with pool.acquire() as conn:
                await conn.execute(
                    "DELETE FROM events WHERE actor_id = $1",
                    registered_id,
                )
                await conn.execute(
                    "DELETE FROM users WHERE id = $1",
                    uuid.UUID(registered_id),
                )
        await close_pool()

    print("old-image/new-schema compatibility: ok")


def main() -> None:
    args = _arguments()
    asyncio.run(_run(args.backend, args.dsn))


if __name__ == "__main__":
    main()
