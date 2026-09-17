"""Migration 109: case-insensitive uniqueness on users.email (#551).

`users.email` is a case-sensitive TEXT UNIQUE, but the authoritative-adoption
lookup matches on `lower(email)`. A case variant pre-registered through local
signup (`victim@corp.example` vs `Victim@corp.example`) makes that lookup find
two rows and refuse with `identity_conflict` — blocking the victim's sign-in
until an admin approves them. Availability-only (approval cannot be bypassed),
but a denial-of-signin anyone can stage.

This adds the race-proof backstop: a UNIQUE index on `lower(email)`. The
application-level check in `register()` gives the friendly 409; this index is
what holds under concurrency.

Existing variant rows would violate the index, so they are resolved first:
within each lower-cased group, keep the earliest-created row untouched and
rename the rest by appending `+dup<N>` before the `@` (deterministic, reversible
by an admin, and still a deliverable-looking address rather than garbage). The
adopt lookup then finds exactly one row per address again.

Idempotent: CREATE UNIQUE INDEX IF NOT EXISTS; the rename loop only touches
groups that still collide.
"""

from __future__ import annotations

import logging

logger = logging.getLogger("akb.migration.109")


async def migrate(conn=None):
    if conn is None:
        from app.db.postgres import get_pool

        pool = await get_pool()
        async with pool.acquire() as new_conn:
            await _run(new_conn)
    else:
        await _run(conn)


async def _run(conn):
    async with conn.transaction():
        # Resolve pre-existing case-variant groups before the index lands.
        # Keep the earliest-created row per lower(email); rename the rest.
        rows = await conn.fetch(
            """
            SELECT id, email,
                   ROW_NUMBER() OVER (
                       PARTITION BY lower(email) ORDER BY created_at, id
                   ) AS rn
              FROM users
            """
        )
        for row in rows:
            if int(row["rn"]) <= 1:
                continue
            local, _, domain = row["email"].partition("@")
            suffix = int(row["rn"]) - 1
            new_email = f"{local}+dup{suffix}@{domain}" if domain else f"{row['email']}+dup{suffix}"
            # The renamed address must itself be collision-free; bump the
            # suffix until it is (bounded: at most the group size + a few).
            for _ in range(100):
                clash = await conn.fetchval(
                    "SELECT 1 FROM users WHERE lower(email) = lower($1) AND id <> $2",
                    new_email,
                    row["id"],
                )
                if clash is None:
                    break
                suffix += 1
                new_email = f"{local}+dup{suffix}@{domain}" if domain else f"{row['email']}+dup{suffix}"
            await conn.execute(
                "UPDATE users SET email = $1, updated_at = NOW() WHERE id = $2",
                new_email,
                row["id"],
            )
            logger.info(
                "Migration 109: renamed case-variant email %r -> %r",
                row["email"],
                new_email,
            )

        await conn.execute(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS users_email_lower_uniq
                ON users (lower(email))
            """
        )
    logger.info("Migration 109: users_email_lower_uniq ready")
