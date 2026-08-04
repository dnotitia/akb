"""Pure contracts for installation lifecycle normalization and projection."""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone

import pytest

from app.exceptions import ValidationError
from app.services import app_lifecycle_service as lifecycle


def _row(*, observed: bool = True, lifecycle_name: str = "installing") -> dict:
    app_id = uuid.uuid4()
    vault_id = uuid.uuid4()
    release_id = uuid.uuid4()
    grant_id = uuid.uuid4()
    now = datetime.now(timezone.utc)
    return {
        "installation_id": uuid.uuid4(),
        "app_id": app_id,
        "vault_id": vault_id,
        "lifecycle": lifecycle_name,
        "blocked_reason": None,
        "desired_release_id": release_id,
        "desired_version": "1.0.0",
        "desired_manifest": {
            "steps": [{"id": "prepare"}],
            "expected_schema_fingerprint": "a" * 64,
        },
        "current_release_id": None if lifecycle_name == "installing" else release_id,
        "current_version": None if lifecycle_name == "installing" else "1.0.0",
        "grant_generation": 1,
        "latest_grant_id": grant_id,
        "latest_grant_generation": 1,
        "latest_grant_status": "active",
        "latest_grant_capabilities": ["inventory:read", "installation:read"],
        "latest_active_grant_id": grant_id,
        "latest_active_grant_generation": 1,
        "latest_active_grant_status": "active",
        "latest_active_grant_capabilities": [
            "inventory:read",
            "installation:read",
        ],
        "observed_generation": 2 if observed else None,
        "observed_at": now if observed else None,
        "observed_release_id": release_id if observed else None,
        "observed_release_version": "1.0.0" if observed else None,
        "schema_fingerprint": "a" * 64 if observed else None,
        "observed_grant_generation": 1 if observed else None,
        "checkpoint": {"phase": "ready", "token": "secret-marker"},
        "recent_error": {
            "code": "worker_timeout",
            "message": "secret-marker",
        },
        "resources": [
            {"kind": "collection", "key": "owned-key", "status": "owned"},
            {"kind": "table", "key": "retained-key", "status": "retained"},
        ],
        "created_at": now,
        "updated_at": now,
    }


def test_capabilities_are_exactly_sorted_and_deduplicated():
    assert lifecycle.normalize_capabilities(
        ["inventory:read", "installation:read", "inventory:read"]
    ) == ["installation:read", "inventory:read"]

    with pytest.raises(ValidationError):
        lifecycle.normalize_capabilities([])
    with pytest.raises(ValidationError):
        lifecycle.normalize_capabilities(["documents:write"])


def test_mode_normalization_is_closed_and_defaults_to_install():
    assert lifecycle.normalize_mode(None) == "install"
    assert lifecycle.normalize_mode("fresh") == "fresh"
    with pytest.raises(ValidationError):
        lifecycle.normalize_mode("upgrade")


def test_status_projection_redacts_payloads_and_unrelated_metadata():
    projected = lifecycle.project_installation_status(_row())
    serialized = json.dumps(projected, sort_keys=True)

    assert "secret-marker" not in serialized
    assert "worker_timeout" in serialized
    assert "vault_name" not in projected
    assert "provenance" not in serialized
    assert projected["resources"] == [
        {"kind": "collection", "key": "owned-key", "status": "owned"},
        {"kind": "table", "key": "retained-key", "status": "retained"},
    ]
    assert projected["checkpoint"] == {"phase": "ready"}
    assert projected["recent_error"] == {"code": "worker_timeout"}
    assert projected["lifecycle"] == "installing"
    assert projected["current_release"] is None
    assert projected["latest_grant"]["id"] == str(projected["latest_active_grant"]["id"])


def test_status_projection_decodes_asyncpg_jsonb_resources():
    row = _row()
    row["resources"] = json.dumps(row["resources"])

    projected = lifecycle.project_installation_status(row)

    assert projected["latest_grant"]["id"] == str(row["latest_grant_id"])
    assert projected["latest_active_grant"]["id"] == str(row["latest_active_grant_id"])
    assert projected["resources"] == [
        {"kind": "collection", "key": "owned-key", "status": "owned"},
        {"kind": "table", "key": "retained-key", "status": "retained"},
    ]


def test_replay_requires_the_same_desired_release_and_active_grant():
    row = _row()
    grant = {
        "generation": 1,
        "status": "active",
        "capabilities": ["installation:read", "inventory:read"],
    }
    assert lifecycle._is_replay_state(
        row,
        grant,
        row["desired_release_id"],
        ["installation:read", "inventory:read"],
    )
    assert not lifecycle._is_replay_state(
        row,
        grant,
        uuid.uuid4(),
        ["installation:read", "inventory:read"],
    )
    grant["capabilities"] = ["installation:read"]
    assert not lifecycle._is_replay_state(
        row,
        grant,
        row["desired_release_id"],
        ["installation:read", "inventory:read"],
    )
