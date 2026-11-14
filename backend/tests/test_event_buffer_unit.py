"""Contract of the buffer that lets a transaction insert its events last."""

from __future__ import annotations

import uuid

import pytest

from app.repositories import events_repo

pytestmark = pytest.mark.asyncio


@pytest.fixture
def inserted(monkeypatch):
    rows: list[tuple[object, str, dict]] = []

    async def emit_event(conn, kind, **fields):
        rows.append((conn, kind, fields))
        return len(rows)

    monkeypatch.setattr(events_repo, "emit_event", emit_event)
    return rows


async def test_flush_inserts_in_collection_order_once(inserted):
    vault = uuid.uuid4()
    events = events_repo.EventBuffer()
    events.add("first", vault_id=vault, payload={"n": 1})
    events.add("second", vault_id=vault, resource_uri="akb://v/table/t", actor_id="a")

    assert len(events) == 2
    assert await events.flush("conn") == [1, 2]
    assert [kind for _, kind, _ in inserted] == ["first", "second"]
    assert inserted[1][2] == {
        "vault_id": vault, "resource_uri": "akb://v/table/t", "actor_id": "a", "payload": None,
    }
    assert len(events) == 0
    assert await events.flush("conn") == []


async def test_savepoint_drops_only_the_failed_block(inserted):
    events = events_repo.EventBuffer()
    events.add("kept")
    with pytest.raises(RuntimeError):
        with events.savepoint():
            events.add("rolled-back")
            raise RuntimeError("step failed")
    with events.savepoint():
        events.add("later")

    await events.flush("conn")
    assert [kind for _, kind, _ in inserted] == ["kept", "later"]


async def test_deferred_events_insert_on_every_normal_exit_only(inserted):
    async def body(stop_early: bool) -> str:
        before = len(inserted)
        async with events_repo.deferred_events("conn") as events:
            events.add("collected")
            if stop_early:
                return "returned"
            assert len(inserted) == before
        return "completed"

    assert await body(stop_early=True) == "returned"
    assert await body(stop_early=False) == "completed"
    assert [kind for _, kind, _ in inserted] == ["collected", "collected"]

    with pytest.raises(RuntimeError):
        async with events_repo.deferred_events("conn") as events:
            events.add("discarded")
            raise RuntimeError("rolled back")
    assert len(inserted) == 2
