"""Managed Pod readiness contract for exact platform-owned human accounts."""

from __future__ import annotations

import uuid

import pytest


pytestmark = pytest.mark.asyncio

ISSUER = "https://id.example.com/realms/akb-platform"
USER_ID = "11111111-1111-4111-8111-111111111111"


class _Acquire:
    def __init__(self, rows):
        self.connection = _Connection(rows)

    async def __aenter__(self):
        return self.connection

    async def __aexit__(self, *_args):
        return False


class _Connection:
    def __init__(self, rows):
        self.rows = rows
        self.query = ""
        self.args = ()

    async def fetch(self, query, *args):
        self.query = query
        self.args = args
        return self.rows


class _Pool:
    def __init__(self, rows):
        self.acquire_context = _Acquire(rows)

    def acquire(self):
        return self.acquire_context


def _managed_settings(monkeypatch, account_service, *, enabled: bool = True):
    values = {
        "auth_mode": "sso" if enabled else "local",
        "keycloak_enabled": enabled,
        "keycloak_enrollment_mode": "invite_only" if enabled else "open",
        "keycloak_link_by_email": False if enabled else True,
        "keycloak_require_verified_email": True,
        "keycloak_server_url": "https://id.example.com",
        "keycloak_realm": "akb-platform",
    }
    for field, value in values.items():
        monkeypatch.setattr(account_service.settings, field, value, raising=False)


async def test_exact_managed_account_state_is_ready_without_sensitive_query_fields(monkeypatch):
    from app.services import account_service

    rows = [
        {
            "id": uuid.UUID(USER_ID),
            "auth_provider": "keycloak",
            "subjects": ["subject-1"],
        }
    ]
    pool = _Pool(rows)

    async def _get_pool():
        return pool

    monkeypatch.setattr(account_service, "get_pool", _get_pool)
    _managed_settings(monkeypatch, account_service)

    state = await account_service.get_managed_account_state(
        issuer=ISSUER,
        expected_humans=[{"user_id": USER_ID, "subject": "subject-1"}],
    )

    assert state == {
        "ready": True,
        "account_inventory_ready": True,
        "managed_auth_profile_ready": True,
        "expected_active_humans": 1,
        "observed_active_humans": 1,
        "issues": [],
    }
    query = pool.acquire_context.connection.query.lower()
    assert "email" not in query
    assert "password" not in query
    assert "tokens" not in query
    assert pool.acquire_context.connection.args == (ISSUER,)


async def test_account_and_profile_divergence_are_independent_fail_closed_signals(monkeypatch):
    from app.services import account_service

    rows = [
        {
            "id": uuid.UUID(USER_ID),
            "auth_provider": "local",
            "subjects": ["different-subject"],
        },
        {
            "id": uuid.UUID("22222222-2222-4222-8222-222222222222"),
            "auth_provider": "local",
            "subjects": [],
        },
    ]

    async def _get_pool():
        return _Pool(rows)

    monkeypatch.setattr(account_service, "get_pool", _get_pool)
    _managed_settings(monkeypatch, account_service, enabled=False)

    state = await account_service.get_managed_account_state(
        issuer=ISSUER,
        expected_humans=[{"user_id": USER_ID, "subject": "subject-1"}],
    )

    assert state["ready"] is False
    assert state["account_inventory_ready"] is False
    assert state["managed_auth_profile_ready"] is False
    assert state["expected_active_humans"] == 1
    assert state["observed_active_humans"] == 2
    assert state["issues"] == [
        "active_human_set_mismatch",
        "expected_identity_mismatch",
        "human_auth_provider_mismatch",
        "managed_auth_profile_mismatch",
    ]


async def test_exact_inventory_can_preflight_before_managed_profile_rollout(monkeypatch):
    from app.services import account_service

    rows = [
        {
            "id": uuid.UUID(USER_ID),
            "auth_provider": "keycloak",
            "subjects": ["subject-1"],
        }
    ]

    async def _get_pool():
        return _Pool(rows)

    monkeypatch.setattr(account_service, "get_pool", _get_pool)
    _managed_settings(monkeypatch, account_service, enabled=False)

    state = await account_service.get_managed_account_state(
        issuer=ISSUER,
        expected_humans=[{"user_id": USER_ID, "subject": "subject-1"}],
    )

    assert state["account_inventory_ready"] is True
    assert state["managed_auth_profile_ready"] is False
    assert state["ready"] is False
    assert state["issues"] == ["managed_auth_profile_mismatch"]


async def test_invalid_or_ambiguous_expected_accounts_are_rejected_before_db(monkeypatch):
    from app.exceptions import ValidationError
    from app.services import account_service

    async def _must_not_get_pool():
        raise AssertionError("invalid expected accounts must fail before DB access")

    monkeypatch.setattr(account_service, "get_pool", _must_not_get_pool)
    _managed_settings(monkeypatch, account_service)

    cases = [
        [],
        [
            {"user_id": USER_ID, "subject": "subject-1"},
            {"user_id": USER_ID, "subject": "subject-2"},
        ],
        [
            {"user_id": USER_ID, "subject": "subject-1"},
            {
                "user_id": "22222222-2222-4222-8222-222222222222",
                "subject": "subject-1",
            },
        ],
        [{"user_id": "not-a-uuid", "subject": "subject-1"}],
    ]
    for expected in cases:
        with pytest.raises(ValidationError):
            await account_service.get_managed_account_state(
                issuer=ISSUER,
                expected_humans=expected,
            )


async def test_requested_issuer_must_equal_the_running_akb_issuer(monkeypatch):
    from app.exceptions import ValidationError
    from app.services import account_service

    async def _must_not_get_pool():
        raise AssertionError("issuer mismatch must fail before DB access")

    monkeypatch.setattr(account_service, "get_pool", _must_not_get_pool)
    _managed_settings(monkeypatch, account_service)

    with pytest.raises(ValidationError):
        await account_service.get_managed_account_state(
            issuer="https://other.example.com/realms/akb-platform",
            expected_humans=[{"user_id": USER_ID, "subject": "subject-1"}],
        )
