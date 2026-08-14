"""Durable retirement-receipt contracts for bundled standalone SSO."""

from __future__ import annotations

from dataclasses import replace

import pytest


pytestmark = pytest.mark.asyncio


def _receipt(
    *,
    profile: str | None = None,
    backchannel_logout_uri: str | None = None,
):
    from app.services.standalone_sso_bootstrap import (
        STANDALONE_SSO_RECEIPT_PROFILE,
        StandaloneSSORetirementReceipt,
    )

    selected_profile = profile or STANDALONE_SSO_RECEIPT_PROFILE
    return StandaloneSSORetirementReceipt(
        profile=selected_profile,
        issuer="https://auth.akb.example.com/realms/akb",
        realm_id="akb-realm-id",
        bootstrap_client_id="akb-bootstrap-temporary",
        management_client_uuid="management-client-uuid",
        admin_client_uuid="admin-client-uuid",
        api_client_uuid="api-client-uuid",
        product_admin_subject="00000000-0000-4000-8000-000000000001",
        akb_user_id="11111111-1111-4111-8111-111111111111",
        backchannel_logout_uri=(
            backchannel_logout_uri
            if backchannel_logout_uri is not None
            else (
                "https://akb.example.com/api/v1/auth/keycloak/backchannel-logout"
                if selected_profile == STANDALONE_SSO_RECEIPT_PROFILE
                else None
            )
        ),
    )


class _Transaction:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return None


class _Connection:
    def __init__(self):
        self.rows: dict[str, dict] = {}
        self.writes = 0

    def transaction(self):
        return _Transaction()

    async def execute(self, sql: str, *args):
        if "pg_advisory_xact_lock" in sql:
            return "SELECT 1"
        assert (
            "INSERT INTO standalone_sso_bootstrap_retirements" in sql
            or "UPDATE standalone_sso_bootstrap_retirements" in sql
        )
        self.writes += 1
        profile = args[0]
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
            "backchannel_logout_uri",
        )
        if "UPDATE" in sql:
            assert self.rows[profile]["backchannel_logout_uri"] == args[-1]
            self.rows[profile] = dict(zip(fields, args[: len(fields)], strict=True))
        elif profile not in self.rows:
            self.rows[profile] = dict(zip(fields, args, strict=True))
        return "UPDATE 1" if "UPDATE" in sql else "INSERT 0 1"

    async def fetchrow(self, sql: str, profile: str):
        assert "FROM standalone_sso_bootstrap_retirements" in sql
        return self.rows.get(profile)


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
        await service.record_standalone_sso_retirement_receipt(replace(expected, realm_id="other-realm-id"))

    assert captured.value.code == "keycloak_bootstrap_retirement_receipt_mismatch"
    assert await service.load_standalone_sso_retirement_receipt() == expected
    assert conn.writes == 1


async def test_current_callback_receipt_replacement_is_an_exact_compare_and_swap(monkeypatch):
    from app.services import standalone_sso_receipt as service
    from app.services.standalone_sso_bootstrap import StandaloneSSOBootstrapError

    conn = _Connection()

    async def _get_pool():
        return _Pool(conn)

    monkeypatch.setattr(service, "get_pool", _get_pool)
    source = _receipt()
    target = replace(
        source,
        bootstrap_client_id="akb-bootstrap-upgrade-v2",
        backchannel_logout_uri=("http://backend:8000/api/v1/auth/keycloak/backchannel-logout"),
    )
    await service.record_standalone_sso_retirement_receipt(source)
    await service.record_standalone_sso_retirement_receipt(
        target,
        previous_receipt=source,
    )

    assert await service.load_standalone_sso_retirement_receipt() == target

    with pytest.raises(StandaloneSSOBootstrapError) as captured:
        await service.record_standalone_sso_retirement_receipt(
            replace(target, backchannel_logout_uri=source.backchannel_logout_uri),
            previous_receipt=source,
        )

    assert captured.value.code == "keycloak_bootstrap_retirement_receipt_mismatch"
    assert await service.load_standalone_sso_retirement_receipt() == target
    assert conn.writes == 2


async def test_loader_prefers_current_v3_then_v2_then_v1(monkeypatch):
    from app.services import standalone_sso_receipt as service
    from app.services.standalone_sso_bootstrap import (
        STANDALONE_SSO_RECEIPT_PROFILE_V1,
        STANDALONE_SSO_RECEIPT_PROFILE_V2,
    )

    conn = _Connection()
    legacy = _receipt(profile=STANDALONE_SSO_RECEIPT_PROFILE_V1)
    v2 = _receipt(profile=STANDALONE_SSO_RECEIPT_PROFILE_V2)
    current = _receipt()

    def _row(receipt):
        return {
            "profile": receipt.profile,
            "issuer": receipt.issuer,
            "realm_id": receipt.realm_id,
            "bootstrap_client_id": receipt.bootstrap_client_id,
            "management_client_uuid": receipt.management_client_uuid,
            "admin_client_uuid": receipt.admin_client_uuid,
            "api_client_uuid": receipt.api_client_uuid,
            "product_admin_subject": receipt.product_admin_subject,
            "akb_user_id": receipt.akb_user_id,
            "backchannel_logout_uri": receipt.backchannel_logout_uri,
        }

    for receipt in (legacy, v2, current):
        conn.rows[receipt.profile] = _row(receipt)

    async def _get_pool():
        return _Pool(conn)

    monkeypatch.setattr(service, "get_pool", _get_pool)
    del conn.rows[current.profile]
    assert await service.load_standalone_sso_retirement_receipt() == v2

    del conn.rows[v2.profile]
    assert await service.load_standalone_sso_retirement_receipt() == legacy

    conn.rows[current.profile] = _row(current)
    assert await service.load_standalone_sso_retirement_receipt() == current


async def test_receipt_schema_is_present_for_fresh_and_upgraded_databases():
    from pathlib import Path

    backend = Path(__file__).resolve().parents[1]
    init_sql = (backend / "app" / "db" / "init.sql").read_text()
    migration = backend / "app" / "db" / "migrations" / "073_sso_bootstrap_receipt.py"
    callback_migration = backend / "app" / "db" / "migrations" / "075_sso_callback_receipt.py"
    registry = (backend / "app" / "db" / "postgres.py").read_text()

    assert "CREATE TABLE IF NOT EXISTS standalone_sso_bootstrap_retirements" in init_sql
    assert "backchannel_logout_uri TEXT" in init_sql
    assert migration.is_file()
    assert callback_migration.is_file()
    assert '"073_sso_bootstrap_receipt.py"' in registry
    assert '"075_sso_callback_receipt.py"' in registry
