"""Pure contracts for app inventory projection and cursor safety."""

from __future__ import annotations

import base64
import uuid
from datetime import datetime, timezone

import pytest

from app.api.control_plane_models import InventoryProjection
from app.exceptions import ValidationError
from app.services import app_inventory_service as inventory


def _row(*, observed: bool = True, mismatch: bool = False) -> dict:
    app_id = uuid.uuid4()
    desired_release_id = uuid.uuid4()
    observed_release_id = uuid.uuid4() if mismatch else desired_release_id
    return {
        "installation_id": uuid.uuid4(),
        "app_id": app_id,
        "vault_id": uuid.uuid4(),
        "vault_name": "vault-a",
        "lifecycle": "active",
        "desired_release_id": desired_release_id,
        "desired_version": "1.0.0",
        "desired_manifest": {
            "steps": [],
            "expected_schema_fingerprint": "a" * 64,
        },
        "current_release_id": desired_release_id,
        "current_version": "1.0.0",
        "grant_generation": 4,
        "latest_grant_generation": 4,
        "latest_grant_status": "active",
        "latest_grant_capabilities": ["inventory:read"],
        "observed_generation": 3 if observed else None,
        "observed_at": datetime.now(timezone.utc) if observed else None,
        "observed_release_id": observed_release_id if observed else None,
        "observed_release_version": "1.0.0" if observed else None,
        "schema_fingerprint": "a" * 64 if observed else None,
        "observed_grant_generation": 4 if observed else None,
        "checkpoint": {"phase": "ready", "token": "secret-marker"},
        "recent_error": {"code": "worker_timeout", "message": "secret-marker"},
        "created_at": datetime.now(timezone.utc),
        "updated_at": datetime.now(timezone.utc),
    }


def test_cursor_is_opaque_and_bound_to_scope_and_filter():
    app_id = uuid.uuid4()
    now = datetime.now(timezone.utc)
    cursor = inventory.encode_inventory_cursor(
        app_id=app_id,
        scope="admin",
        limit=50,
        lifecycle=None,
        boundary=now,
        last_created_at=now,
        last_installation_id=uuid.uuid4(),
    )

    assert str(app_id) not in cursor
    decoded = inventory.decode_inventory_cursor(
        cursor,
        app_id=app_id,
        scope="admin",
        limit=50,
        lifecycle=None,
    )
    assert decoded["boundary"] == now

    for kwargs in (
        {"app_id": uuid.uuid4()},
        {"scope": "app"},
        {"limit": 200},
        {"lifecycle": "uninstalled"},
    ):
        with pytest.raises(ValidationError, match="Invalid inventory cursor"):
            inventory.decode_inventory_cursor(
                cursor,
                app_id=kwargs.get("app_id", app_id),
                scope=kwargs.get("scope", "admin"),
                limit=kwargs.get("limit", 50),
                lifecycle=kwargs.get("lifecycle"),
            )


def test_cursor_round_trip_when_signature_contains_separator_byte(monkeypatch):
    app_id = uuid.uuid4()
    now = datetime.now(timezone.utc)
    installation_id = uuid.uuid4()
    selected_cursor = None

    for attempt in range(4096):
        secret = f"cursor-separator-regression-{attempt}".encode()
        monkeypatch.setattr(inventory, "_cursor_secret", lambda secret=secret: secret)
        cursor = inventory.encode_inventory_cursor(
            app_id=app_id,
            scope="admin",
            limit=20,
            lifecycle=None,
            boundary=now,
            last_created_at=now,
            last_installation_id=installation_id,
        )
        decoded = base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4))
        _raw, signature = decoded.split(b".", 1)
        if b"." in signature:
            selected_cursor = cursor
            break

    assert selected_cursor is not None
    decoded = inventory.decode_inventory_cursor(
        selected_cursor,
        app_id=app_id,
        scope="admin",
        limit=20,
        lifecycle=None,
    )
    assert decoded["last_installation_id"] == installation_id


def test_drift_keeps_missing_observations_and_expected_schema_unknown():
    row = _row(observed=False)
    drift = inventory.classify_drift(row)
    assert drift["overall"] == "unknown"
    assert drift["unknown_dimensions"] == ["release", "schema", "grant"]

    row = _row(observed=True)
    drift = inventory.classify_drift(row)
    assert drift["overall"] == "in_sync"
    assert drift["release"]["status"] == "in_sync"
    assert drift["schema"]["status"] == "in_sync"
    assert drift["grant"]["status"] == "in_sync"
    assert not drift["reasons"]

    row = _row(observed=True, mismatch=True)
    drift = inventory.classify_drift(row)
    assert drift["release"]["status"] == "mismatch"
    assert drift["overall"] == "drifted"
    assert "release_mismatch" in drift["reasons"]

    row["desired_manifest"] = {"steps": []}
    drift = inventory.classify_drift(row)
    assert drift["schema"]["status"] == "unknown"
    assert drift["overall"] == "drifted"


def test_projection_redacts_unbounded_checkpoint_and_error_payloads():
    item = inventory.project_inventory_item(_row())
    InventoryProjection.model_validate({"items": [item]})
    serialized = str(item)
    assert "secret-marker" not in serialized
    assert "issuer" not in item["latest_grant"]
    assert "provenance" not in serialized
    assert item["recent_error"] == {"code": "worker_timeout"}
    assert item["checkpoint"] == {"phase": "ready"}


def test_target_state_set_is_contract_bounded():
    assert inventory.TARGET_STATES == {
        "pending",
        "running",
        "applied",
        "replayed",
        "failed",
        "skipped",
        "denied",
    }
    with pytest.raises(ValidationError):
        inventory.normalize_page_size(201)
