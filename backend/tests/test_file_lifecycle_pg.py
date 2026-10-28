"""File confirmation, cascade, and late-staging races against real PostgreSQL.

Storage bytes are an in-memory adapter; SQL, transactions, services, upload
route and delete worker are real. HTTP/MinIO coverage lives in the HTTP lane.
"""
from __future__ import annotations

import asyncio
import hashlib
import threading
import uuid
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock
from urllib.parse import urlsplit

import pytest
from starlette.requests import Request

from app.exceptions import ConflictError, NotFoundError
from app.repositories import vault_files_repo
from app.repositories.document_repo import CollectionRepository
from app.services import collection_service as cs
from app.services import file_service as fs
from app.services import publication_service, s3_delete_worker as worker
from tests import test_file_capability_resolution_pg as capability_fixtures
from tests.test_file_upload_capability_unit import _FakeStore

pool = capability_fixtures.pool
service = capability_fixtures.service
vault_id = capability_fixtures.vault_id

pytestmark = pytest.mark.asyncio
OLD, NEW = b"original file", b"replacement with different size"


def digest(body):
    return hashlib.sha256(body).hexdigest()


@pytest.fixture
def objects(monkeypatch, pool):
    store = _FakeStore()
    blobs = store.objects

    def head(key):
        if key not in blobs:
            raise NotFoundError("File", key)
        return {"ContentLength": len(blobs[key]), "ETag": digest(blobs[key]),
                "ContentType": "application/octet-stream"}

    def copy(source, target):
        blobs[target] = blobs[source]
        return head(target)

    async def get_pool():
        return pool

    monkeypatch.setattr(fs.s3_adapter, "ensure_bucket", lambda *_: None)
    monkeypatch.setattr(fs.s3_adapter, "head", head)
    monkeypatch.setattr(fs.s3_adapter, "copy", copy)
    monkeypatch.setattr(fs.s3_adapter, "iter_chunks", lambda key: iter([blobs[key]]))
    monkeypatch.setattr(fs.s3_adapter, "put_bytes", lambda key, body, **kw: blobs.__setitem__(key, body))
    monkeypatch.setattr(fs.s3_adapter, "delete", lambda key: blobs.pop(key, None))
    for method in ("multipart_create", "multipart_part", "multipart_complete", "multipart_abort"):
        monkeypatch.setattr(fs.s3_adapter, method, getattr(store, method))
    monkeypatch.setattr(fs, "index_file_metadata", AsyncMock())
    monkeypatch.setattr(fs, "enqueue_native_file_projection", AsyncMock())
    monkeypatch.setattr(fs, "_delete_file_publications", AsyncMock())
    monkeypatch.setattr(publication_service, "delete_publications_for_file", AsyncMock())
    monkeypatch.setattr(cs, "get_pool", get_pool)
    monkeypatch.setattr(cs, "delete_file_chunks", AsyncMock())
    monkeypatch.setattr("app.services.document_counters.native_documents_are_authoritative", lambda: False)
    monkeypatch.setattr(worker, "get_pool", get_pool)
    monkeypatch.setattr(worker, "BATCH_SIZE", 256)
    monkeypatch.setattr(worker, "_last_staging_scan_at", 0)
    monkeypatch.setattr(worker, "STAGING_SCAN_INTERVAL_SECONDS", 0)
    monkeypatch.setattr(worker, "_staging_scan_cursor", None)
    return blobs


async def seed(
    pool, vault_id, objects, *, state="confirmed", collection_id=None,
    certified=True, content_addressed=False,
):
    fid = uuid.uuid4()
    async with pool.acquire() as conn:
        name = await conn.fetchval("SELECT name FROM vaults WHERE id=$1", vault_id)
        key = (fs._s3_key(name, "", "original.bin", content_hash=digest(OLD)) if content_addressed
               else f"{name}/original-{fid}.bin")
        await conn.execute(
            """INSERT INTO vault_files
                (id,vault_id,collection_id,kind,upload_state,name,s3_key,mime_type,
                 size_bytes,content_hash,hash_algorithm,hash_verified_at,etag,created_by)
               VALUES($1,$2,$3,'file',$4,'original.bin',$5,'application/octet-stream',
                      $6,$7,'sha256',CASE WHEN $8 THEN NOW() END,$7,'tester')""",
            fid, vault_id, collection_id, state, key, len(OLD),
            digest(OLD) if certified and state == "confirmed" else None,
            certified and state == "confirmed",
        )
    objects[key] = OLD
    return name, fid, key


async def replacement(service, name, vault_id, fid, objects):
    prepared = await service.initiate_replace(
        name, vault_id, str(fid), actor_id="tester", content_hash=digest(NEW),
    )
    rid = prepared["replacement_id"]
    objects[fs._replacement_staging_key(name, fid, uuid.UUID(rid))] = NEW
    return rid


async def publish(service, name, vault_id, fid, rid):
    return await service.confirm_replace(
        name, vault_id, str(fid), rid, actor_id="tester", content_hash=digest(NEW),
    )


async def test_old_confirmation_preserves_replacement(pool, vault_id, service, objects, monkeypatch):
    name, fid, _ = await seed(pool, vault_id, objects)
    rid = await replacement(service, name, vault_id, fid, objects)
    published = await publish(service, name, vault_id, fid, rid)
    fs.enqueue_native_file_projection.reset_mock()
    fs.index_file_metadata.reset_mock()

    def no_io(*args):
        pytest.fail("confirmed replays must not inspect storage")

    monkeypatch.setattr(fs.s3_adapter, "head", no_io)
    with pytest.raises(ConflictError):
        await service.confirm_upload(vault_id, str(fid), actor_id="tester", content_hash=digest(OLD))
    for content_hash in (None, digest(NEW)):
        current = await service.confirm_upload(vault_id, str(fid), actor_id="tester", content_hash=content_hash)
        assert current["content_hash"] == published["content_hash"]
        assert current["size_bytes"] == len(NEW)
    async with pool.acquire() as conn:
        row = await vault_files_repo.find_by_id(conn, vault_id, fid)
        assert objects[row["s3_key"]] == NEW
        assert row["content_hash"] == digest(NEW)
        assert await conn.fetchval("SELECT COUNT(*) FROM events WHERE vault_id=$1 AND kind='file.put'", vault_id) == 0
    fs._delete_file_publications.assert_not_awaited()
    fs.enqueue_native_file_projection.assert_not_awaited()
    fs.index_file_metadata.assert_not_awaited()


async def test_legacy_confirmed_metadata_is_not_recertified(pool, vault_id, service, objects, monkeypatch):
    _, fid, _ = await seed(pool, vault_id, objects, certified=False)
    monkeypatch.setattr(fs.s3_adapter, "head", lambda *_: pytest.fail("legacy replay is not repair"))
    result = await service.confirm_upload(vault_id, str(fid), actor_id="tester")
    assert result["content_hash"] is None
    with pytest.raises(ConflictError):
        await service.confirm_upload(vault_id, str(fid), actor_id="tester", content_hash=digest(OLD))


@pytest.mark.parametrize("inspection", ["valid", "missing", "mismatch"])
async def test_confirmation_rechecks_after_storage_io(
    pool, vault_id, service, objects, monkeypatch, inspection,
):
    name, fid, old_key = await seed(
        pool, vault_id, objects, state="pending", content_addressed=inspection == "mismatch",
    )
    rid = await replacement(service, name, vault_id, fid, objects)
    started, release = threading.Event(), threading.Event()
    original_head = fs.s3_adapter.head

    def head(key):
        if key == old_key:
            started.set()
            assert release.wait(5)
            if inspection == "missing":
                raise NotFoundError("File", key)
        return original_head(key)

    monkeypatch.setattr(fs.s3_adapter, "head", head)
    if inspection == "mismatch":
        objects[old_key] = b"wrong pending bytes"
    task = asyncio.create_task(service.confirm_upload(vault_id, str(fid), actor_id="tester"))
    try:
        assert await asyncio.to_thread(started.wait, 3)
        await publish(service, name, vault_id, fid, rid)
    finally:
        release.set()
    result = await asyncio.wait_for(task, 5)
    assert result["content_hash"] == digest(NEW)
    async with pool.acquire() as conn:
        row = await vault_files_repo.find_by_id(conn, vault_id, fid)
    assert objects[row["s3_key"]] == NEW
    assert row["content_hash"] == digest(NEW)
    fs._delete_file_publications.assert_not_awaited()


async def test_duplicate_pending_confirmations_publish_once(pool, vault_id, service, objects, monkeypatch):
    _, fid, key = await seed(pool, vault_id, objects, state="pending")
    ready, release = threading.Event(), threading.Event()
    count = 0
    count_lock = threading.Lock()

    def read(_key):
        nonlocal count
        with count_lock:
            count += 1
            if count == 2:
                ready.set()
        assert release.wait(5)
        yield objects[key]

    monkeypatch.setattr(fs.s3_adapter, "iter_chunks", read)
    tasks = [asyncio.create_task(service.confirm_upload(
        vault_id, str(fid), actor_id="tester", content_hash=digest(OLD),
    )) for _ in range(2)]
    try:
        assert await asyncio.to_thread(ready.wait, 3)
    finally:
        release.set()
    results = await asyncio.wait_for(asyncio.gather(*tasks), 5)
    assert all(result["content_hash"] == digest(OLD) for result in results)
    fs.enqueue_native_file_projection.assert_awaited_once()
    fs.index_file_metadata.assert_awaited_once()
    async with pool.acquire() as conn:
        assert await conn.fetchval("SELECT COUNT(*) FROM events WHERE vault_id=$1 AND kind='file.put'", vault_id) == 1


@pytest.mark.parametrize("state", ["pending", "confirmed"])
async def test_repository_mutations_require_pending_and_inspected_key(pool, vault_id, objects, state):
    _, fid, key = await seed(pool, vault_id, objects, state=state)
    async with pool.acquire() as conn:
        for checked_key in (("wrong-key", key) if state == "confirmed" else ("wrong-key",)):
            assert not await vault_files_repo.confirm_file_upload_metadata(
                conn, fid, vault_id, expected_s3_key=checked_key, size_bytes=1,
                content_hash=digest(NEW), hash_algorithm="sha256", etag=None, storage_version=None,
            )
            assert not await vault_files_repo.delete_failed_pending_file(
                conn, vault_id=vault_id, file_id=fid, expected_s3_key=checked_key,
            )


@pytest.mark.parametrize("first", ["delete", "replace"])
async def test_collection_delete_serializes_with_replacement(
    pool, vault_id, service, objects, monkeypatch, first,
):
    coll = CollectionRepository(pool)
    cid = await coll.get_or_create(vault_id, "folder/nested")
    name, fid, old_key = await seed(pool, vault_id, objects, collection_id=cid)
    rid = await replacement(service, name, vault_id, fid, objects)
    locked, release = asyncio.Event(), asyncio.Event()
    original = (CollectionRepository.list_files_under if first == "delete"
                else vault_files_repo.find_by_id_for_update)

    async def pause(*args, **kwargs):
        row = await original(*args, **kwargs)
        locked.set()
        await release.wait()
        return row

    if first == "delete":
        monkeypatch.setattr(CollectionRepository, "list_files_under", pause)
    else:
        monkeypatch.setattr(vault_files_repo, "find_by_id_for_update", pause)

    async def delete():
        return await cs.CollectionService().delete(vault=name, path="folder", recursive=True, agent_id="tester")

    a = asyncio.create_task(delete() if first == "delete" else publish(service, name, vault_id, fid, rid))
    b = None
    try:
        await asyncio.wait_for(locked.wait(), 3)
        b = asyncio.create_task(publish(service, name, vault_id, fid, rid) if first == "delete" else delete())
        await asyncio.sleep(0.05)
        assert not b.done()
    finally:
        release.set()
    results = await asyncio.wait_for(asyncio.gather(a, b, return_exceptions=True), 5)
    if first == "delete":
        assert isinstance(results[1], NotFoundError)
    else:
        assert isinstance(results[0], dict)
        assert results[1]["deleted_files"] == 1
    async with pool.acquire() as conn:
        assert not await conn.fetchval("SELECT EXISTS(SELECT 1 FROM vault_files WHERE id=$1)", fid)
        if first == "replace":
            # The actual published candidate, not the old snapshot key.
            keys = await conn.fetch("SELECT s3_key FROM s3_delete_outbox WHERE processed_at IS NULL")
            assert any(row["s3_key"] in objects and row["s3_key"] != old_key
                       and not row["s3_key"].startswith(worker.REPLACEMENT_STAGING_PREFIX) for row in keys)


@pytest.mark.parametrize("multipart", [False, True])
async def test_late_put_is_rediscovered_without_request_side_cleanup(
    pool, vault_id, service, objects, monkeypatch, multipart,
):
    from app.api.routes.files import upload_by_capability

    name, fid, _ = await seed(pool, vault_id, objects)
    prepared = await service.initiate_replace(name, vault_id, str(fid), actor_id="tester", content_hash=digest(NEW))
    rid = uuid.UUID(prepared["replacement_id"])
    key = fs._replacement_staging_key(name, fid, rid)
    token = urlsplit(prepared["upload_url"]).path.rsplit("/", 1)[1]
    admitted, release = asyncio.Event(), asyncio.Event()
    body = NEW * (17 * 1024 * 1024 // len(NEW) + 1) if multipart else NEW

    async def receive():
        admitted.set()
        await release.wait()
        return {"type": "http.request", "body": body, "more_body": False}

    request = Request({"type": "http", "method": "PUT", "path": "/files/upload/test",
                       "headers": [(b"content-length", str(len(body)).encode())]}, receive=receive)
    upload = asyncio.create_task(upload_by_capability(token, request))
    try:
        await asyncio.wait_for(admitted.wait(), 3)
        async with pool.acquire() as conn:
            await conn.execute("UPDATE m1_file_transfer_intents SET expires_at=NOW()-INTERVAL '1 second' WHERE id=$1", rid)
            await conn.execute("UPDATE s3_delete_outbox SET next_attempt_at=NOW() WHERE s3_key=$1", key)
        await worker._process_deletes_once()
        assert key not in objects
    finally:
        release.set()
    assert (await asyncio.wait_for(upload, 3)).status_code == 200
    assert objects[key] == body
    # Inventory is independent of the upload request, including after restart.
    monkeypatch.setattr(fs.s3_adapter, "list_objects_page", lambda *a, **kw: ([{
        "Key": key, "LastModified": datetime.now(timezone.utc) - timedelta(days=1),
    }], None))
    assert await worker._reconcile_staging_once() == 1
    assert await worker._reconcile_staging_once() == 0  # no duplicate pending job
    await worker._process_deletes_once()
    assert key not in objects
    objects[key] = NEW  # a still-later storage completion, after that deletion
    assert await worker._reconcile_staging_once() == 1
    await worker._process_deletes_once()
    assert key not in objects


async def test_inventory_wraps_and_preserves_valid_or_live_keys(pool, vault_id, service, objects, monkeypatch):
    name, fid, _ = await seed(pool, vault_id, objects)
    rid = await replacement(service, name, vault_id, fid, objects)
    valid = fs._replacement_staging_key(name, fid, uuid.UUID(rid))
    live = fs._replacement_staging_key(name, fid, uuid.uuid4())
    expired = fs._replacement_staging_key(name, fid, uuid.uuid4())
    async with pool.acquire() as conn:
        await conn.execute("UPDATE vault_files SET s3_key=$2 WHERE id=$1", fid, live)
    seen = []

    def page(prefix, *, continuation_token, limit):
        assert prefix == worker.REPLACEMENT_STAGING_PREFIX
        assert limit == worker.STAGING_SCAN_PAGE_SIZE
        seen.append(continuation_token)
        keys = [valid, live, prefix + "not-a-staging-key"] if continuation_token is None else [expired]
        return [{"Key": k, "LastModified": datetime.now(timezone.utc)-timedelta(days=1)} for k in keys], (
            "page-two" if continuation_token is None else None
        )

    monkeypatch.setattr(fs.s3_adapter, "list_objects_page", page)
    assert await worker._reconcile_staging_once() == 0
    assert await worker._reconcile_staging_once() == 1
    assert await worker._reconcile_staging_once() == 0
    assert seen == [None, "page-two", None]


@pytest.mark.parametrize("failure", ["missing", "mismatch", "transient"])
async def test_failed_inspection_only_cleans_failed_pending_upload(
    pool, vault_id, service, objects, monkeypatch, failure,
):
    _, fid, key = await seed(pool, vault_id, objects, state="pending", content_addressed=True)
    if failure == "missing":
        objects.pop(key)
    elif failure == "mismatch":
        objects[key] = NEW
    else:
        def unavailable(*args):
            raise fs.StorageError("temporary outage")
        monkeypatch.setattr(fs.s3_adapter, "head", unavailable)
    with pytest.raises(fs.AKBError) as error:
        await service.confirm_upload(vault_id, str(fid), actor_id="tester")
    assert error.value.status_code == {"missing": 404, "mismatch": 409, "transient": 502}[failure]
    async with pool.acquire() as conn:
        row = await vault_files_repo.find_by_id(conn, vault_id, fid)
        if failure == "transient":
            assert row["upload_state"] == "pending"
        else:
            assert row is None
            cleanup = await conn.fetch(
                "SELECT next_attempt_at > NOW() AS delayed FROM s3_delete_outbox "
                "WHERE s3_key=$1 AND processed_at IS NULL ORDER BY next_attempt_at", key,
            )
            assert [job["delayed"] for job in cleanup] == [False, True]


async def test_confirmation_event_failure_rolls_back_transition(pool, vault_id, service, objects, monkeypatch):
    _, fid, _ = await seed(pool, vault_id, objects, state="pending")
    monkeypatch.setattr(fs, "emit_event", AsyncMock(side_effect=RuntimeError("event write failed")))
    with pytest.raises(RuntimeError, match="event write failed"):
        await service.confirm_upload(vault_id, str(fid), actor_id="tester")
    async with pool.acquire() as conn:
        row = await vault_files_repo.find_by_id(conn, vault_id, fid)
    assert row["upload_state"] == "pending"
    assert row["content_hash"] is None
    fs.enqueue_native_file_projection.assert_not_awaited()
    fs.index_file_metadata.assert_not_awaited()


@pytest.mark.parametrize("failure", ["listing", "database"])
async def test_inventory_failure_does_not_advance_or_lose_page(
    pool, vault_id, objects, monkeypatch, failure,
):
    name, fid, _ = await seed(pool, vault_id, objects)
    key = fs._replacement_staging_key(name, fid, uuid.uuid4())

    def page(*args, **kwargs):
        return [{"Key": key, "LastModified": datetime.now(timezone.utc)-timedelta(days=1)}], "next-page"

    async def get_pool():
        return pool

    if failure == "listing":
        def unavailable(*args, **kwargs):
            raise fs.StorageError("list denied")
        monkeypatch.setattr(fs.s3_adapter, "list_objects_page", unavailable)
    else:
        monkeypatch.setattr(fs.s3_adapter, "list_objects_page", page)
        monkeypatch.setattr(worker, "get_pool", AsyncMock(side_effect=RuntimeError("DB unavailable")))
    with pytest.raises((RuntimeError, fs.StorageError)):
        await worker._reconcile_staging_once()
    assert worker._staging_scan_cursor is None
    monkeypatch.setattr(fs.s3_adapter, "list_objects_page", page)
    monkeypatch.setattr(worker, "get_pool", get_pool)
    assert await worker._reconcile_staging_once() == 1
    assert worker._staging_scan_cursor == "next-page"


async def test_collection_delete_rollback_preserves_file_and_cleanup_ownership(
    pool, vault_id, objects, monkeypatch,
):
    coll = CollectionRepository(pool)
    cid = await coll.get_or_create(vault_id, "folder/nested")
    name, fid, key = await seed(pool, vault_id, objects, collection_id=cid)
    monkeypatch.setattr(cs, "emit_event", AsyncMock(side_effect=RuntimeError("event write failed")))
    with pytest.raises(RuntimeError, match="event write failed"):
        await cs.CollectionService().delete(vault=name, path="folder", recursive=True, agent_id="tester")
    async with pool.acquire() as conn:
        row = await vault_files_repo.find_by_id(conn, vault_id, fid)
        assert row["s3_key"] == key
        assert not await conn.fetchval("SELECT EXISTS(SELECT 1 FROM s3_delete_outbox WHERE s3_key=$1)", key)
        assert await conn.fetchval("SELECT EXISTS(SELECT 1 FROM collections WHERE id=$1)", cid)
    assert objects[key] == OLD
