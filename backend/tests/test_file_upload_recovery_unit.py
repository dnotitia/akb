"""Lost confirmation responses must never authorize deleting a saved File."""

import asyncio
import hashlib
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from unittest.mock import AsyncMock, Mock

import pytest

from app.exceptions import ConflictError, ValidationError
from app.services import file_service as fs
from app.services import m1_file_measurement as measurement

pytestmark = pytest.mark.asyncio


@pytest.fixture
def upload(monkeypatch):
    vault_id, file_id = uuid.uuid4(), uuid.uuid4()
    data = b"saved file"
    row = {
        "id": file_id, "vault_id": vault_id, "kind": "file",
        "upload_state": "pending", "collection": "research", "name": "report.txt",
        "mime_type": "text/plain", "size_bytes": None, "content_hash": None,
        "hash_algorithm": None, "etag": None, "storage_version": None,
        "description": "", "s3_key": "team/research/legacy_report.txt",
    }
    state = {"row": row}
    lock = asyncio.Lock()

    class Conn:
        @asynccontextmanager
        async def transaction(self):
            async with lock:
                yield

        async def fetchrow(self, *_args):
            return {"name": "team"}

        async def fetchval(self, *_args):
            return "team"

    class Pool:
        @asynccontextmanager
        async def acquire(self):
            yield Conn()

    async def find(_conn, vid, fid):
        current = state["row"]
        return dict(current) if current and vid == vault_id and fid == file_id else None

    async def confirm(conn, fid, vid, **values):
        current = await find(conn, vid, fid)
        inspected_key = values.pop("expected_s3_key")
        if not current or current["upload_state"] != "pending" or current["s3_key"] != inspected_key:
            return False
        state["row"].update(values, upload_state="confirmed", hash_verified_at=datetime.now(timezone.utc))
        return True

    async def cancel(conn, vid, fid):
        current = await find(conn, vid, fid)
        if not current or current["upload_state"] != "pending":
            return None
        state["row"] = None
        return current

    monkeypatch.setattr(fs, "measurement_enabled", lambda: False)
    monkeypatch.setattr(fs, "get_pool", AsyncMock(return_value=Pool()))
    monkeypatch.setattr(fs.vault_files_repo, "lease_file_upload_confirmation", find)
    monkeypatch.setattr(fs.vault_files_repo, "find_by_id", find)
    monkeypatch.setattr(fs.vault_files_repo, "find_by_id_for_update", find)
    monkeypatch.setattr(fs.vault_files_repo, "confirm_file_upload_metadata", confirm)
    monkeypatch.setattr(fs.vault_files_repo, "delete_pending_file", cancel)
    head = Mock(return_value={"ContentLength": len(data), "ETag": '"etag"', "VersionId": "v1"})
    monkeypatch.setattr(fs.s3_adapter, "head", head)
    monkeypatch.setattr(fs.s3_adapter, "iter_chunks", lambda _key: iter([data]))
    events, projections, indexes, cleanup = (AsyncMock() for _ in range(4))
    monkeypatch.setattr(fs, "emit_event", events)
    monkeypatch.setattr(fs, "enqueue_native_file_projection", projections)
    monkeypatch.setattr(fs, "index_file_metadata", indexes)
    monkeypatch.setattr(fs, "enqueue_pending_upload_delete", cleanup)
    generic_delete = AsyncMock(side_effect=AssertionError("must never use ordinary File deletion"))
    monkeypatch.setattr(fs.vault_files_repo, "delete", generic_delete)
    return fs.FileService(), vault_id, str(file_id), state, head, events, projections, indexes, cleanup


async def test_lost_success_response_retry_returns_same_saved_file_without_side_effects(upload):
    service, vid, fid, state, head, events, projections, indexes, cleanup = upload
    original = await service.confirm_upload(vid, fid, actor_id="alice")
    # The client loses this response, then checks the same reservation.
    head.reset_mock()
    head.side_effect = AssertionError("a confirmed replay must not reread storage")
    replay = await service.confirm_upload(vid, fid, actor_id="alice")
    assert replay == original
    assert state["row"]["upload_state"] == "confirmed"
    head.assert_not_called()
    assert events.await_count == projections.await_count == indexes.await_count == 1
    cleanup.assert_not_awaited()


async def test_duplicate_inflight_confirmations_publish_once(upload, monkeypatch):
    service, vid, fid, _state, _head, events, projections, indexes, _cleanup = upload
    both_hashing = asyncio.Event()
    callers = 0

    async def hash_in_parallel(function, *args):
        nonlocal callers
        if function is not fs.compute_stream_content_hash:
            return function(*args)
        callers += 1
        if callers == 2:
            both_hashing.set()
        await asyncio.wait_for(both_hashing.wait(), 2)
        return function(*args)

    monkeypatch.setattr(fs.asyncio, "to_thread", hash_in_parallel)
    first, second = await asyncio.gather(
        service.confirm_upload(vid, fid, actor_id="alice"),
        service.confirm_upload(vid, fid, actor_id="alice"),
    )
    assert first == second
    assert events.await_count == projections.await_count == indexes.await_count == 1


async def test_pending_only_cleanup_preserves_confirmed_file(upload):
    service, vid, fid, state, _head, _events, _projections, _indexes, cleanup = upload
    await service.confirm_upload(vid, fid, actor_id="alice")
    assert await service.cancel_upload(vid, fid, actor_id="alice") == {"cancelled": False, "state": "confirmed"}
    assert state["row"]["upload_state"] == "confirmed"
    cleanup.assert_not_awaited()


async def test_pending_cleanup_is_scoped_and_idempotent_including_expired_reservation(upload):
    service, vid, fid, state, _head, _events, _projections, _indexes, cleanup = upload
    assert await service.cancel_upload(uuid.uuid4(), fid, actor_id="alice") == {"cancelled": False, "state": "absent"}
    assert state["row"] is not None
    assert await service.cancel_upload(vid, fid, actor_id="alice") == {"cancelled": True, "state": "cancelled"}
    assert await service.cancel_upload(vid, fid, actor_id="alice") == {"cancelled": False, "state": "absent"}
    cleanup.assert_awaited_once()


async def test_confirmed_retry_rejects_a_conflicting_hash_without_deletion(upload):
    service, vid, fid, state, _head, _events, _projections, _indexes, cleanup = upload
    await service.confirm_upload(vid, fid, actor_id="alice")
    with pytest.raises(ConflictError):
        await service.confirm_upload(vid, fid, actor_id="alice", content_hash=hashlib.sha256(b"other").hexdigest())
    assert state["row"]["upload_state"] == "confirmed"
    cleanup.assert_not_awaited()


async def test_pending_cleanup_rejects_malformed_identity(upload):
    service, vid, *_ = upload
    with pytest.raises(ValidationError):
        await service.cancel_upload(vid, "not-a-file", actor_id="alice")


async def test_cancellation_route_requires_the_same_write_policy_as_upload(monkeypatch):
    from fastapi import HTTPException
    from starlette.requests import Request

    from app.api.routes import files

    denied = AsyncMock(side_effect=HTTPException(403, "Vault is archived"))
    cancel = AsyncMock()
    monkeypatch.setattr(files, "_resolve_file_write_context", denied)
    monkeypatch.setattr(files.file_service, "cancel_upload", cancel)
    request = Request({"type": "http", "method": "POST", "path": "/", "headers": []})
    user = Mock()
    with pytest.raises(HTTPException) as error:
        await files.cancel_upload(request=request, vault="team", file_id=str(uuid.uuid4()), user=user)
    assert error.value.status_code == 403
    denied.assert_awaited_once_with(request, "team", user)
    cancel.assert_not_awaited()


@pytest.mark.parametrize("confirmed", [True, False])
async def test_measurement_cleanup_only_withdraws_unconfirmed_put_intents(monkeypatch, confirmed):
    vid, fid, intent = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    conn = Mock()
    conn.fetch = AsyncMock(return_value=[{"id": intent}])
    conn.execute = AsyncMock()

    @asynccontextmanager
    async def scope():
        yield conn

    conn.transaction = scope
    pool = Mock(acquire=scope)
    monkeypatch.setattr(measurement, "get_pool", AsyncMock(return_value=pool))
    monkeypatch.setattr(measurement.vault_files_repo, "find_measurement_by_id", AsyncMock(return_value={"id": fid} if confirmed else None))
    service = object.__new__(measurement.MeasurementFileService)
    result = await service.cancel_upload(vid, str(fid))
    assert "method = 'PUT' FOR UPDATE" in conn.fetch.call_args.args[0]
    if confirmed:
        assert result == {"cancelled": False, "state": "confirmed"}
        conn.execute.assert_not_awaited()
    else:
        assert result == {"cancelled": True, "state": "cancelled"}
        conn.execute.assert_awaited_once_with("DELETE FROM m1_file_transfer_intents WHERE id = $1", intent)
