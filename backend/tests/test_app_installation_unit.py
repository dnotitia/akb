"""Pure contracts for app installation command normalization and projection."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

import pytest

from app.api.control_plane_models import InstallationProjection
from app.exceptions import ValidationError
from app.services import app_installation_service as installation


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
            "steps": [],
            "expected_schema_fingerprint": "a" * 64,
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
        "schema_fingerprint": "a" * 64,
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
