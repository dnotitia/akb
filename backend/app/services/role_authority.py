"""Serialize catalog authority and its PostgreSQL role projection.

Take this transaction BEFORE user/vault/token/table row locks. All catalog
writers and role DDL share the same guard, including bootstrap reconciliation.
READ COMMITTED observes a predecessor's commit after waiting for the guard.
Do not acquire another pool connection while holding this transaction.
"""
from contextlib import asynccontextmanager


async def lock_role_authority(conn) -> None:
    await conn.execute(
        "SELECT pg_advisory_xact_lock(hashtextextended($1, 0))",
        "akb:role-sync:reconcile",
    )


@asynccontextmanager
async def role_authority_transaction(conn):
    async with conn.transaction(isolation="read_committed"):
        await lock_role_authority(conn)
        yield
