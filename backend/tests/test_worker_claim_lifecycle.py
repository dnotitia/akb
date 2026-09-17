"""Crash-safe retry accounting contracts shared by durable workers."""

from __future__ import annotations

import inspect
from pathlib import Path

import pytest

from app.services import (
    delete_worker,
    embed_worker,
    events_publisher,
    metadata_worker,
    queue_rescuer,
    s3_delete_worker,
)
from app.services._backfill import MAX_RETRIES


@pytest.mark.parametrize(
    ("claim", "counter", "claimed", "abandoned"),
    [
        (embed_worker._claim_batch, "vector_retry_count", "vector_claimed_at", "vector_abandoned_at"),
        (delete_worker._claim_delete_batch, "retry_count", "claimed_at", "abandoned_at"),
        (s3_delete_worker._claim_batch, "retry_count", "claimed_at", "abandoned_at"),
        (metadata_worker._claim_batch, "llm_retry_count", "llm_claimed_at", "llm_abandoned_at"),
        (events_publisher._claim_batch, "attempts", "claimed_at", "abandoned_at"),
    ],
)
def test_claims_burn_attempt_before_external_work(claim, counter, claimed, abandoned):
    source = inspect.getsource(claim)

    assert f"{counter} = {counter} + 1" in source
    assert f"{claimed} = NOW()" in source
    assert f"{abandoned} IS NULL" in source
    assert counter in source.split("RETURNING", 1)[1]
    assert claimed in source.split("RETURNING", 1)[1]


class _RecordingConn:
    def __init__(self):
        self.calls: list[tuple[str, tuple]] = []

    async def execute(self, sql: str, *args):
        self.calls.append((sql, args))
        return "UPDATE 1"


async def test_failure_does_not_increment_twice_and_stamps_terminal(monkeypatch):
    conn = _RecordingConn()
    delays: list[int] = []

    def delay(index: int) -> int:
        delays.append(index)
        return 123

    monkeypatch.setattr(delete_worker, "next_attempt_delay", delay)
    await delete_worker._mark_delete_failure(
        conn, 7, MAX_RETRIES, "terminal",
    )

    sql, args = conn.calls[0]
    assert "retry_count = retry_count + 1" not in sql
    assert delays == [MAX_RETRIES - 1]
    assert args[2] is None
    assert args[3] is True
    assert "abandoned_at" in sql


async def test_events_client_init_failure_credits_unattempted_batch(monkeypatch):
    batch = [
        {"id": index, "attempts": 1, "claimed_at": "same-claim"}
        for index in range(1, 4)
    ]

    class Context:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        def transaction(self):
            return self

    class Pool:
        def acquire(self):
            return Context()

    async def get_pool():
        return Pool()

    async def claim(_conn):
        return batch

    async def unavailable():
        raise OSError("redis unavailable")

    failed: list[int] = []
    released: list[list[int]] = []

    async def mark_failure(_conn, event_id, _attempts, _error):
        failed.append(event_id)

    async def release(_conn, rows):
        released.append([row["id"] for row in rows])

    monkeypatch.setattr(events_publisher.settings, "redis_url", "redis://test")
    monkeypatch.setattr(events_publisher, "get_pool", get_pool)
    monkeypatch.setattr(events_publisher, "_claim_batch", claim)
    monkeypatch.setattr(events_publisher, "_client", unavailable)
    monkeypatch.setattr(events_publisher, "_mark_failure", mark_failure)
    monkeypatch.setattr(events_publisher, "_release_unattempted", release)

    assert await events_publisher._process_once() == 0
    assert failed == [1]
    assert released == [[2, 3]]


def test_rescuer_covers_every_attempt_at_claim_queue():
    sql = "\n".join(queue_rescuer._RESCUE_STATEMENTS)

    for table in (
        "chunks",
        "vector_delete_outbox",
        "s3_delete_outbox",
        "events",
        "documents",
        "native_invalidation_intents",
        "native_file_projection_outbox",
    ):
        assert f"UPDATE {table}" in sql
    assert sql.count(">= $1") == 7
    assert "delivery_outcome = 'abandoned'" in sql
    assert "outcome = 'abandoned'" in sql

    projection_rescue = next(
        statement for statement in queue_rescuer._RESCUE_STATEMENTS
        if "UPDATE native_file_projection_outbox" in statement
    )
    assert "completed_at = NOW(), outcome = 'abandoned'" in projection_rescue
    assert "claimed_at = NULL, next_attempt_at = NULL" in projection_rescue
    assert "retry_count >= $1" in projection_rescue
    assert "next_attempt_at <= NOW()" in projection_rescue


def test_migration_is_registered_and_adds_claim_lifecycle_columns():
    backend = Path(__file__).resolve().parents[1]
    postgres = (backend / "app/db/postgres.py").read_text()
    migration = (backend / "app/db/migrations/079_worker_claim_lifecycle.py").read_text()

    assert '"079_worker_claim_lifecycle.py"' in postgres
    for column in (
        "vector_claimed_at",
        "vector_abandoned_at",
        "claimed_at",
        "abandoned_at",
        "llm_claimed_at",
        "llm_abandoned_at",
    ):
        assert column in migration
    assert "native_invalidation_intents" in inspect.getsource(
        __import__("app.services.native_derived_worker", fromlist=["NativeDerivedWorker"])
        .NativeDerivedWorker._claim_one
    )
