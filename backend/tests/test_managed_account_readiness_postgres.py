"""PostgreSQL proof for the managed active-human inventory query."""

from __future__ import annotations

import os
import uuid

import asyncpg
import pytest


pytestmark = pytest.mark.asyncio

_DSN = os.environ.get("AKB_TEST_DSN", "postgresql://akb:akb@localhost:15432/akb")
_ISSUER = "https://id.example.com/realms/akb-platform"


async def test_managed_account_query_uses_one_exact_database_snapshot(monkeypatch):
    try:
        admin = await asyncpg.connect(_DSN)
    except Exception:
        pytest.skip("Postgres unreachable at AKB_TEST_DSN")

    schema = "managed_readiness_" + uuid.uuid4().hex
    await admin.execute(f'CREATE SCHEMA "{schema}"')
    pool = None
    try:
        pool = await asyncpg.create_pool(
            _DSN,
            min_size=1,
            max_size=2,
            server_settings={"search_path": schema},
        )
        async with pool.acquire() as conn:
            await conn.execute(
                """
                CREATE TABLE users (
                    id UUID PRIMARY KEY,
                    auth_provider TEXT NOT NULL,
                    account_status TEXT NOT NULL,
                    account_kind TEXT NOT NULL
                );
                CREATE TABLE external_identities (
                    user_id UUID NOT NULL REFERENCES users(id),
                    issuer TEXT NOT NULL,
                    subject TEXT NOT NULL,
                    UNIQUE (issuer, subject)
                );
                """
            )
            expected_id = uuid.uuid4()
            await conn.execute(
                """
                INSERT INTO users VALUES
                  ($1, 'keycloak', 'active', 'human'),
                  ($2, 'local', 'suspended', 'human'),
                  ($3, 'service', 'active', 'service')
                """,
                expected_id,
                uuid.uuid4(),
                uuid.uuid4(),
            )
            await conn.execute(
                "INSERT INTO external_identities VALUES ($1, $2, 'subject-1')",
                expected_id,
                _ISSUER,
            )

        from app.services import account_service

        async def _get_pool():
            return pool

        monkeypatch.setattr(account_service, "get_pool", _get_pool)
        settings = account_service.settings
        for field, value in {
            "auth_mode": "sso",
            "keycloak_enabled": True,
            "keycloak_enrollment_mode": "invite_only",
            "keycloak_link_by_email": False,
            "keycloak_require_verified_email": True,
            "keycloak_server_url": "https://id.example.com",
            "keycloak_realm": "akb-platform",
        }.items():
            monkeypatch.setattr(settings, field, value, raising=False)

        state = await account_service.get_managed_account_state(
            issuer=_ISSUER,
            expected_humans=[
                {"user_id": str(expected_id), "subject": "subject-1"}
            ],
        )
        assert state["ready"] is True
        assert state["observed_active_humans"] == 1

        async with pool.acquire() as conn:
            await conn.execute(
                "INSERT INTO users VALUES ($1, 'local', 'active', 'human')",
                uuid.uuid4(),
            )
        drifted = await account_service.get_managed_account_state(
            issuer=_ISSUER,
            expected_humans=[
                {"user_id": str(expected_id), "subject": "subject-1"}
            ],
        )
        assert drifted["account_inventory_ready"] is False
        assert "active_human_set_mismatch" in drifted["issues"]
    finally:
        if pool is not None:
            await pool.close()
        await admin.execute(f'DROP SCHEMA "{schema}" CASCADE')
        await admin.close()
