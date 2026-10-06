"""Native authority -> public transactional outbox -> existing Redis publisher."""
import json
import os
import uuid

import pytest
import redis.asyncio as redis

from app.config import settings
from app.exceptions import ConflictError, NotFoundError
from app.services import events_publisher
from app.services.m1_pg_body_store import M1PgBodyStore
from app.services.native_revision_service import NativeRevisionService
from tests.test_event_tail_postgres import _fresh_database, _make_vault

pytestmark = pytest.mark.asyncio


async def events(pool):
    async with pool.acquire() as conn:
        rows = await conn.fetch("SELECT * FROM events ORDER BY id")
    return [{**dict(row), "payload": json.loads(row["payload"])} for row in rows]


async def lifecycle(native, vault):
    common = dict(namespace_id=vault, surface="document", actor="writer")
    current = await native.create_text(**common, path="source/first.md", payload="private body", mutation_id=uuid.uuid4())
    results = [current]
    for method, extra in [
        ("replace_text", dict(payload="private updated", notification_previous_status="active", notification_status="archived")),
        ("move_text", dict(path_to="target/nested/final.md")),
        ("delete_resource", {}),
        ("restore_text", dict(payload="private restored")),
    ]:
        current = await getattr(native, method)(
            **common, path=current.path, mutation_id=uuid.uuid4(), expected_revision_id=current.revision_id,
            expected_resource_id=current.resource_id, **extra,
        )
        results.append(current)
    return results


async def test_lifecycle_identity_and_move_scopes():
    async with _fresh_database() as pool:
        vault = await _make_vault(pool, "native-events")
        results = await lifecycle(NativeRevisionService(pool, payload_store=M1PgBodyStore(pool)), vault)
        rows = await events(pool)
        assert [row["kind"] for row in rows] == [
            "document.put", "document.update", "document.move", "document.delete", "document.restore",
        ]
        for row, result in zip(rows, results, strict=True):
            assert row["vault_id"] == vault and row["actor_id"] == "writer"
            payload = row["payload"]
            assert payload["resource_id"] == str(results[0].resource_id)
            assert payload["revision_id"] == payload["commit_hash"] == result.revision_id
            assert payload["previous_commit"] == result.parent_revision_id
            assert payload["path"] == result.path and payload["vault"] == "native-events"
            assert payload["collection"] == result.path.rpartition("/")[0]
            assert row["redis_published_at"] is None and "private" not in json.dumps(payload)
        assert rows[0]["resource_uri"] == "akb://native-events/coll/source/doc/first.md"
        assert rows[2]["resource_uri"] == "akb://native-events/coll/target/nested/doc/final.md"
        assert rows[2]["payload"]["old_uri"] == rows[0]["resource_uri"]
        assert rows[2]["payload"]["old_path"] == "source/first.md"
        assert rows[2]["payload"]["old_collection"] == "source"
        assert rows[3]["resource_uri"] == rows[4]["resource_uri"] == rows[2]["resource_uri"]
        assert rows[1]["payload"]["previous_status"] == "active" and rows[1]["payload"]["status"] == "archived"


@pytest.mark.parametrize("action", ["create", "replace", "move", "delete", "restore"])
async def test_rollback_replay_and_obsolete_head_for_each_mutation(action):
    async with _fresh_database() as pool:
        vault = await _make_vault(pool, "native-retry")
        native = NativeRevisionService(pool, payload_store=M1PgBodyStore(pool))
        common = dict(namespace_id=vault, surface="document", actor="writer")
        args = dict(**common, path="first.md", mutation_id=uuid.uuid4())
        method = {"create": "create_text", "replace": "replace_text", "move": "move_text",
                  "delete": "delete_resource", "restore": "restore_text"}[action]
        if action != "create":
            current = await native.create_text(**common, path="first.md", payload="first", mutation_id=uuid.uuid4())
            if action == "restore":
                current = await native.delete_resource(
                    **common, path=current.path, mutation_id=uuid.uuid4(),
                    expected_resource_id=current.resource_id, expected_revision_id=current.revision_id,
                )
            args.update(expected_resource_id=current.resource_id, expected_revision_id=current.revision_id)
        if action in {"create", "replace", "restore"}: args["payload"] = "changed"
        if action == "move": args["path_to"] = "destination/moved.md"
        before = await events(pool)
        def fail(boundary):
            if boundary == "authority.after_activity": raise RuntimeError("event rollback")
        broken = NativeRevisionService(pool, payload_store=M1PgBodyStore(pool), failpoint=fail)
        with pytest.raises(RuntimeError, match="event rollback"):
            await getattr(broken, method)(**args)
        assert await events(pool) == before
        result = await getattr(native, method)(**args)
        replay = await getattr(native, method)(**args)
        assert replay.idempotent_replay and result.revision_id == replay.revision_id
        assert len(await events(pool)) == len(before) + 1
        if action != "create":
            with pytest.raises((ConflictError, NotFoundError)):
                await getattr(native, method)(**{**args, "mutation_id": uuid.uuid4()})
            assert len(await events(pool)) == len(before) + 1


async def test_caller_owned_create_transaction_rollback_discards_event():
    async with _fresh_database() as pool:
        vault = await _make_vault(pool, "native-outer")
        native = NativeRevisionService(pool, payload_store=M1PgBodyStore(pool))
        async with pool.acquire() as conn:
            with pytest.raises(RuntimeError, match="outer rollback"):
                async with conn.transaction():
                    await native.create_text_in_conn(
                        conn, namespace_id=vault, surface="document", path="first.md", payload="body",
                        actor="writer", mutation_id=uuid.uuid4(),
                    )
                    assert await conn.fetchval("SELECT count(*) FROM events") == 1
                    raise RuntimeError("outer rollback")
        assert await events(pool) == []


async def test_existing_publisher_delivers_native_events_to_real_redis(monkeypatch):
    redis_url = os.environ.get("AKB_TEST_REDIS_URL")
    if not redis_url:
        if os.environ.get("REQUIRE_REAL_REDIS") == "1": pytest.fail("AKB_TEST_REDIS_URL required")
        pytest.skip("AKB_TEST_REDIS_URL not configured")
    client = redis.from_url(redis_url)
    stream = "native-event-test:" + uuid.uuid4().hex
    try:
        async with _fresh_database() as pool:
            vault = await _make_vault(pool, "native-stream")
            await lifecycle(NativeRevisionService(pool, payload_store=M1PgBodyStore(pool)), vault)
            monkeypatch.setattr(settings, "redis_url", redis_url)
            monkeypatch.setattr(settings, "redis_event_stream", stream)
            monkeypatch.setattr(events_publisher, "_redis_client", client)
            assert await events_publisher._process_once() == 5
            delivered = await client.xrange(stream)
            assert len(delivered) == 5
            for (_, fields), row in zip(delivered, await events(pool), strict=True):
                assert int(fields[b"id"]) == row["id"] and fields[b"kind"].decode() == row["kind"]
                assert fields[b"resource_uri"].decode() == row["resource_uri"]
                assert json.loads(fields[b"payload"]) == row["payload"] and row["redis_published_at"] is not None
            assert await events_publisher._process_once() == 0 and await client.xlen(stream) == 5
    finally:
        await client.delete(stream)
        await client.aclose()
