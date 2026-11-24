"""Additive workspace-account schema migration tests."""

from __future__ import annotations

import os
import uuid

import asyncpg
import pytest
import pytest_asyncio

from tests.helpers.pg_database import database_template, temporary_database

from app.db.postgres import _load_migration


pytestmark = pytest.mark.asyncio
_DSN = os.environ.get("AKB_TEST_DSN", "postgresql://akb:akb@localhost:15432/akb")


async def _initialize(conn: asyncpg.Connection) -> None:
    await conn.execute("""
        CREATE EXTENSION "uuid-ossp";
        CREATE TABLE users (
            id UUID PRIMARY KEY,
            username TEXT NOT NULL,
            email TEXT NOT NULL,
            password_hash TEXT NOT NULL
        );
    """)


@pytest_asyncio.fixture(scope="module", loop_scope="module")
async def schema_template():
    async with database_template(_DSN, "akb_legacy_account_template_", _initialize) as template:
        yield template


@pytest.fixture
async def conn(schema_template):
    async with temporary_database(_DSN, "akb_legacy_account_", template=schema_template) as database:
        connection = await asyncpg.connect(database.dsn)
        try:
            yield connection
        finally:
            await connection.close()


async def test_migration_is_additive_idempotent_and_preserves_legacy_user(conn):
    user_id = uuid.uuid4()
    username = f"legacy-{uuid.uuid4().hex[:10]}"
    await conn.execute(
        "INSERT INTO users (id, username, email, password_hash) VALUES ($1, $2, $3, $4)",
        user_id,
        username,
        f"{username}@example.com",
        "legacy-hash",
    )

    migration = _load_migration("043_workspace_account_governance.py")
    assert migration is not None
    await migration.migrate(conn=conn)
    await migration.migrate(conn=conn)

    row = await conn.fetchrow(
        "SELECT id, account_status, account_kind FROM users WHERE id = $1",
        user_id,
    )
    assert dict(row) == {
        "id": user_id,
        "account_status": "active",
        "account_kind": "human",
    }

    identity_id = uuid.uuid4()
    await conn.execute(
        """
        INSERT INTO external_identities (id, user_id, issuer, subject, email_snapshot)
        VALUES ($1, $2, $3, $4, $5)
        """,
        identity_id,
        user_id,
        "https://id.example.com/realms/akb",
        "subject-1",
        f"{username}@example.com",
    )
    with pytest.raises(asyncpg.UniqueViolationError):
        await conn.execute(
            """
            INSERT INTO external_identities (id, user_id, issuer, subject)
            VALUES ($1, $2, $3, $4)
            """,
            uuid.uuid4(),
            user_id,
            "https://id.example.com/realms/akb",
            "subject-1",
        )

    await conn.execute("DELETE FROM users WHERE id = $1", user_id)


async def test_account_status_and_kind_constraints_reject_unknown_values(conn):
    migration = _load_migration("043_workspace_account_governance.py")
    assert migration is not None
    await migration.migrate(conn=conn)

    user_id = uuid.uuid4()
    with pytest.raises(asyncpg.CheckViolationError):
        await conn.execute(
            """
            INSERT INTO users (
                id, username, email, password_hash, account_status, account_kind
            ) VALUES ($1, $2, $3, $4, 'ghost', 'human')
            """,
            user_id,
            f"bad-{user_id.hex[:10]}",
            f"bad-{user_id.hex[:10]}@example.com",
            "hash",
        )

    with pytest.raises(asyncpg.CheckViolationError):
        await conn.execute(
            """
            INSERT INTO users (
                id, username, email, password_hash, account_status, account_kind
            ) VALUES ($1, $2, $3, $4, 'active', 'robot')
            """,
            user_id,
            f"bad-{user_id.hex[:10]}",
            f"bad-{user_id.hex[:10]}@example.com",
            "hash",
        )
