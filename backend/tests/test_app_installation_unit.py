"""Pure contracts for app installation command normalization and projection."""

from __future__ import annotations

import uuid
import traceback
from datetime import datetime, timezone

import asyncpg
import pytest

from app.api.control_plane_models import InstallationActiveStatus, InstallationProjection
from app.exceptions import AKBError, ForbiddenError, ValidationError
from app.services import app_installation_service as installation
from app.services import app_resource_service as resources
from app.services.auth_service import AuthenticatedUser


def _row() -> dict:
    release_id = uuid.uuid4()
    app_id = uuid.uuid4()
    return {
        "installation_id": uuid.uuid4(),
        "app_id": app_id,
        "vault_id": uuid.uuid4(),
        "lifecycle": "blocked",
        "blocked_reason": "worker_timeout",
        "desired_release_id": release_id,
        "desired_version": "1.2.3",
        "desired_manifest": {
            "manifest_version": 2,
            "app_key": "installation-test",
            "source_revision": "a" * 40,
            "image_digest": "sha256:" + "b" * 64,
            "schema_version": 3,
            "schema": {"tables": []},
            "transition_plans": [{"source": "fresh", "steps": []}],
        },
        "current_release_id": release_id,
        "current_version": "1.2.3",
        "desired_grant_generation": 4,
        "grant_generation": 4,
        "grant_status": "revoked",
        "grant_capabilities": ["installation:read"],
        "active_grant_generation": None,
        "active_grant_status": None,
        "active_grant_capabilities": None,
        "observed_generation": 3,
        "observed_at": datetime.now(timezone.utc),
        "observed_release_id": release_id,
        "observed_release_version": "1.2.3",
        "schema_fingerprint": resources.canonical_table_fingerprint([]),
        "observed_grant_generation": 4,
        "checkpoint": {
            "phase": "ready",
            "message": "private-worker-payload",
        },
        "recent_error": {
            "code": "worker_timeout",
            "message": "private-worker-payload",
        },
        "created_at": datetime.now(timezone.utc),
        "updated_at": datetime.now(timezone.utc),
    }


def test_capabilities_are_exact_sorted_and_deduplicated():
    assert installation.normalize_capabilities(
        ["inventory:read", "installation:read", "inventory:read"]
    ) == ["installation:read", "inventory:read"]

    with pytest.raises(ValidationError):
        installation.normalize_capabilities([])
    with pytest.raises(ValidationError):
        installation.normalize_capabilities(["document:read"])
    with pytest.raises(ValidationError):
        installation.normalize_capabilities([" installation:read"])


def test_mode_defaults_and_rejects_unknown_values():
    assert installation.normalize_mode(None) == "install"
    assert installation.normalize_mode("fresh") == "fresh"
    with pytest.raises(ValidationError):
        installation.normalize_mode("upgrade")


def test_projection_keeps_truthful_state_and_redacts_payloads():
    row = _row()
    projection = installation.project_installation(
        row,
        [
            {
                "resource_kind": "table",
                "resource_key": "owned-table",
                "status": "retained",
                "metadata": {"private": "private-worker-payload"},
            }
        ],
    )
    InstallationProjection.model_validate(projection)
    assert projection["drift"]["release"]["status"] == "in_sync"
    assert projection["drift"]["schema"]["status"] == "in_sync"
    assert projection["drift"]["grant"]["status"] == "in_sync"

    serialized = str(projection)
    assert projection["lifecycle"] == "blocked"
    assert projection["desired_grant_generation"] == 4
    assert projection["latest_grant"] == {
        "generation": 4,
        "status": "revoked",
        "capabilities": ["installation:read"],
    }
    assert projection["active_grant"] is None
    assert projection["owned_resources"] == [
        {"kind": "table", "key": "owned-table", "status": "retained"}
    ]
    assert projection["checkpoint"] == {"phase": "ready"}
    assert projection["recent_error"] == {"code": "worker_timeout"}
    assert "private-worker-payload" not in serialized
    assert "provenance" not in serialized
    assert "issuer" not in serialized


def test_member_installation_active_status_is_an_allowlisted_boolean():
    assert InstallationActiveStatus.model_validate({"active": True}).model_dump() == {
        "active": True
    }
    with pytest.raises(ValueError):
        InstallationActiveStatus.model_validate(
            {"active": True, "lifecycle": "active", "recent_error": "private"}
        )


@pytest.mark.asyncio
async def test_member_installation_status_rejects_app_credentials_before_database(monkeypatch):
    async def unexpected_pool():
        raise AssertionError("application credentials must be rejected before database access")

    monkeypatch.setattr(installation, "get_pool", unexpected_pool)
    app_user = AuthenticatedUser(
        user_id=str(uuid.uuid4()),
        username="app-principal",
        email="app@example.invalid",
        display_name=None,
        is_admin=False,
        auth_method="pat",
        account_kind="app",
    )

    with pytest.raises(ForbiddenError, match="Installation request denied"):
        await installation.get_member_installation_active_status(
            uuid.uuid4(),
            uuid.uuid4(),
            user=app_user,
            correlation_id="test",
        )


@pytest.mark.asyncio
async def test_member_installation_unavailable_is_not_reported_as_inactive(monkeypatch):
    user = AuthenticatedUser(
        user_id=str(uuid.uuid4()),
        username="member",
        email="member@example.invalid",
        display_name=None,
        is_admin=False,
        auth_method="jwt",
    )
    vault_id = uuid.uuid4()
    raw_error_marker = "RAW_MEMBER_INSTALLATION_QUERY_ERROR"
    raw_credential = "synthetic-fault-credential-abcdef"
    raw_error = f"{raw_error_marker}; Authorization: Bearer {raw_credential}"

    class FakeConnection:
        async def fetchrow(self, _query, requested_vault_id):
            assert requested_vault_id == vault_id
            return {
                "id": vault_id,
                "name": "member-status-vault",
                "owner_id": uuid.UUID(user.user_id),
            }

        async def fetchval(self, query, *_args):
            assert "FROM vault_app_installations" in query
            raise asyncpg.exceptions.UndefinedTableError(raw_error)

    class FakeAcquire:
        async def __aenter__(self):
            return FakeConnection()

        async def __aexit__(self, *_args):
            return False

    class FakePool:
        def acquire(self):
            return FakeAcquire()

    async def fake_get_pool():
        return FakePool()

    async def allow_owner_access(*_args, **_kwargs):
        return None

    monkeypatch.setattr(installation, "get_pool", fake_get_pool)
    monkeypatch.setattr(installation, "check_vault_access", allow_owner_access)

    with pytest.raises(AKBError) as unavailable:
        await installation.get_member_installation_active_status(
            uuid.uuid4(), vault_id, user=user, correlation_id="test"
        )

    assert unavailable.value.status_code == 503
    assert unavailable.value.code == "member_installation_status_unavailable"
    assert unavailable.value.message == "Installation status is temporarily unavailable"
    assert raw_error_marker not in str(unavailable.value)
    assert raw_credential not in str(unavailable.value)
    assert raw_error_marker not in "".join(traceback.format_exception(unavailable.value))
    assert unavailable.value.__suppress_context__
