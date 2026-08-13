"""Durable retirement-receipt contracts for bundled standalone SSO."""

from __future__ import annotations

from dataclasses import replace

import pytest


pytestmark = pytest.mark.asyncio


def _receipt():
    from app.services.standalone_sso_bootstrap import (
        STANDALONE_SSO_RECEIPT_PROFILE,
        StandaloneSSORetirementReceipt,
    )

    return StandaloneSSORetirementReceipt(
        profile=STANDALONE_SSO_RECEIPT_PROFILE,
        issuer="https://auth.akb.example.com/realms/akb",
        realm_id="akb-realm-id",
        bootstrap_client_id="akb-bootstrap-temporary",
        management_client_uuid="management-client-uuid",
        admin_client_uuid="admin-client-uuid",
        api_client_uuid="api-client-uuid",
        product_admin_subject="00000000-0000-4000-8000-000000000001",
        akb_user_id="11111111-1111-4111-8111-111111111111",
    )


class _Transaction:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return None


class _Connection:
    def __init__(self):
        self.row: dict | None = None
        self.writes = 0

    def transaction(self):
        return _Transaction()

    async def execute(self, sql: str, *args):
        if "pg_advisory_xact_lock" in sql:
            return "SELECT 1"
        assert "INSERT INTO standalone_sso_bootstrap_retirements" in sql
        self.writes += 1
        if self.row is None:
            fields = (
                "profile",
                "issuer",
                "realm_id",
                "bootstrap_client_id",
                "management_client_uuid",
                "admin_client_uuid",
                "api_client_uuid",
                "product_admin_subject",
                "akb_user_id",
            )
            self.row = dict(zip(fields, args, strict=True))
        return "INSERT 0 1"

    async def fetchrow(self, sql: str, *_args):
        assert "FROM standalone_sso_bootstrap_retirements" in sql
        return self.row


class _Acquire:
    def __init__(self, conn: _Connection):
        self.conn = conn

    async def __aenter__(self):
        return self.conn

    async def __aexit__(self, *_args):
        return None


class _Pool:
    def __init__(self, conn: _Connection):
        self.conn = conn

    def acquire(self):
        return _Acquire(self.conn)


async def test_receipt_round_trip_is_exact_idempotent_and_conflict_safe(monkeypatch):
    from app.services import standalone_sso_receipt as service
    from app.services.standalone_sso_bootstrap import StandaloneSSOBootstrapError

    conn = _Connection()

    async def _get_pool():
        return _Pool(conn)

    monkeypatch.setattr(service, "get_pool", _get_pool)
    expected = _receipt()

    assert await service.load_standalone_sso_retirement_receipt() is None
    await service.record_standalone_sso_retirement_receipt(expected)
    assert await service.load_standalone_sso_retirement_receipt() == expected
    await service.record_standalone_sso_retirement_receipt(expected)

    with pytest.raises(StandaloneSSOBootstrapError) as captured:
        await service.record_standalone_sso_retirement_receipt(
            replace(expected, realm_id="other-realm-id")
        )

    assert captured.value.code == "keycloak_bootstrap_retirement_receipt_mismatch"
    assert await service.load_standalone_sso_retirement_receipt() == expected
    assert conn.writes == 1


async def test_receipt_schema_is_present_for_fresh_and_upgraded_databases():
    from pathlib import Path

    backend = Path(__file__).resolve().parents[1]
    init_sql = (backend / "app" / "db" / "init.sql").read_text()
    migration = backend / "app" / "db" / "migrations" / "073_sso_bootstrap_receipt.py"
    registry = (backend / "app" / "db" / "postgres.py").read_text()

    assert "CREATE TABLE IF NOT EXISTS standalone_sso_bootstrap_retirements" in init_sql
    assert migration.is_file()
    assert '"073_sso_bootstrap_receipt.py"' in registry
