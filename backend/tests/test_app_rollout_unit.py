"""Pure AKB-126 manifest and public projection contracts."""

from __future__ import annotations

import hashlib
import json
import uuid

import pytest

from app.exceptions import ValidationError
from app.services import app_rollout_service as rollout
from app.services import app_rollout_worker as worker


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


@pytest.mark.asyncio
async def test_backfill_checkpoint_is_cumulative_across_restart_resume():
    """A restarted worker must never publish a smaller completed count."""

    class FakeConnection:
        def __init__(self, first_batch, second_batch):
            self._batches = [first_batch, second_batch]
            self.checkpoint_writes = []

        async def fetchval(self, query, *_args):
            assert "SELECT name FROM vaults" in query
            return "fixture-vault"

        async def fetch(self, query, *_args):
            assert "ORDER BY id LIMIT" in query
            return self._batches.pop(0)

        async def execute(self, query, *_args):
            if "SET checkpoint" in query:
                checkpoint = json.loads(_args[1])
                self.checkpoint_writes.append(checkpoint)

    first_ids = [{"id": uuid.UUID(int=100 + index)} for index in range(10)]
    second_ids = [{"id": uuid.UUID(int=200 + index)} for index in range(5)]
    connection = FakeConnection(first_ids, second_ids)
    target = {"vault_id": uuid.uuid4()}
    payload = {
        "table": "orders",
        "column": "flag",
        "primary_key": "id",
        "batch_size": 10,
        "where_null": True,
        "value": "ready",
    }
    step = {"id": uuid.uuid4(), "step_id": "backfill_flag", "checkpoint": {}}

    assert await worker._run_backfill(connection, target, step, payload) is False
    persisted = connection.checkpoint_writes[-1]
    assert persisted["completed"] == 10
    assert persisted["total"] == 10

    # A process restart reloads the persisted checkpoint from PostgreSQL.
    resumed_step = {"id": step["id"], "step_id": step["step_id"], "checkpoint": persisted}
    assert await worker._run_backfill(connection, target, resumed_step, payload) is False
    resumed = connection.checkpoint_writes[-1]
    assert resumed["completed"] >= persisted["completed"]
    assert resumed["completed"] == 15
    assert resumed["total"] == 15
    assert resumed["cursor"] != persisted["cursor"]

    # A checkpoint written by the pre-fix worker may already carry the full
    # total while its completed count only describes the last batch.
    legacy_connection = FakeConnection(second_ids, [])
    legacy_step = {
        "id": step["id"],
        "step_id": step["step_id"],
        "checkpoint": {
            "cursor": str(first_ids[-1]["id"]),
            "completed": 10,
            "total": 25,
        },
    }
    assert await worker._run_backfill(legacy_connection, target, legacy_step, payload) is False
    legacy_resumed = legacy_connection.checkpoint_writes[-1]
    assert legacy_resumed["completed"] == 15
    assert legacy_resumed["total"] == 25


@pytest.mark.asyncio
async def test_blocked_rollout_marks_pending_snapshot_targets_by_id():
    """A failed target must finish the bounded snapshot projection update."""

    class FakeConnection:
        def __init__(self):
            self.queries: list[str] = []

        async def execute(self, query, *_args):
            self.queries.append(query)

    connection = FakeConnection()
    target = {
        "id": uuid.uuid4(),
        "job_id": uuid.uuid4(),
        "app_id": uuid.uuid4(),
        "installation_id": uuid.uuid4(),
    }

    await worker._mark_job_blocked(connection, target, "step_failed")

    assert any(
        "UPDATE app_rollout_snapshot_targets" in query
        and "WHERE id IN" in query
        and "snapshot_target_id" in query
        for query in connection.queries
    )
