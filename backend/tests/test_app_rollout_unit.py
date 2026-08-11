"""Pure AKB-126 manifest and public projection contracts."""

from __future__ import annotations

import hashlib
import json
import uuid

import pytest

from app.exceptions import ValidationError
from app.services import app_rollout_service as rollout


def _manifest(*, operation: str = "add_column", phase: str = "expand") -> dict:
    step = {
        "id": "expand_flag",
        "phase": phase,
        "operation": operation,
        "payload": {
            "table": "orders",
            "column": {"name": "flag", "type": "text"},
        },
    }
    step["checksum"] = hashlib.sha256(
        json.dumps(step, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
    ).hexdigest()
    body = {"manifest_version": 1, "steps": [step]}
    body["manifest_checksum"] = hashlib.sha256(
        json.dumps(
            {"manifest_version": 1, "steps": [{k: v for k, v in step.items() if k != "checksum"}]},
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        ).encode()
    ).hexdigest()
    return body


def test_manifest_canonical_checksums_and_normalization():
    body = rollout.validate_manifest(_manifest())
    assert body["manifest_version"] == 1
    assert body["steps"][0]["operation"] == "add_column"
    assert body["steps"][0]["step_order"] == 0


@pytest.mark.parametrize(
    "mutate",
    [
        lambda body: body["steps"].append(body["steps"][0].copy()),
        lambda body: body["steps"][0].update({"phase": "contract"}),
        lambda body: body["steps"][0].update({"operation": "drop_table"}),
    ],
)
def test_manifest_rejects_unsupported_or_duplicate_steps(mutate):
    body = _manifest()
    mutate(body)
    with pytest.raises(ValidationError):
        rollout.validate_manifest(body)


def test_manifest_rejects_unbounded_backfill_and_forbidden_defaults():
    body = _manifest(operation="backfill_column", phase="backfill")
    payload = body["steps"][0]["payload"]
    payload.update({"column": "flag", "where_null": True, "batch_size": 1001, "value": "x"})
    with pytest.raises(ValidationError):
        rollout.validate_manifest(body)

    body = _manifest()
    body["steps"][0]["payload"]["column"]["default"] = "now()"
    with pytest.raises(ValidationError):
        rollout.validate_manifest(body)


def test_public_projection_has_no_manifest_or_raw_error_payload():
    now = __import__("datetime").datetime.now(__import__("datetime").timezone.utc)
    row = {
        "id": uuid.uuid4(),
        "app_id": uuid.uuid4(),
        "release_id": uuid.uuid4(),
        "manifest_checksum": "a" * 64,
        "snapshot_id": uuid.uuid4(),
        "status": "blocked",
        "blocked_reason": "step_failed",
        "created_at": now,
        "updated_at": now,
        "completed_at": now,
    }
    target = {
        "id": uuid.uuid4(),
        "installation_id": uuid.uuid4(),
        "vault_id": uuid.uuid4(),
        "ordinal": 0,
        "batch_no": 0,
        "is_canary": True,
        "state": "failed",
        "reason_code": "step_failed",
    }
    step = {
        "target_id": target["id"],
        "step_id": "expand_flag",
        "operation": "add_column",
        "state": "failed",
        "checkpoint": {"cursor": "private", "phase": "expand"},
        "reason_code": "step_failed",
    }
    result = rollout._public_job(row, [target], [step])
    assert "private" not in json.dumps(result)
    assert result["targets"][0]["steps"][0]["checkpoint"] == {"phase": "expand"}
