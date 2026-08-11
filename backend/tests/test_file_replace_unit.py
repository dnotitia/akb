"""File replacement contract and optimistic-concurrency regression tests."""

from __future__ import annotations

import hashlib
import uuid

import pytest

from app.exceptions import ConflictError
from app.services import file_service as fs


pytestmark = pytest.mark.asyncio

_OLD_BYTES = b"old file bytes"
_NEW_BYTES = b"new file bytes"
_OLD_HASH = hashlib.sha256(_OLD_BYTES).hexdigest()
_NEW_HASH = hashlib.sha256(_NEW_BYTES).hexdigest()


class _Context:
    def __init__(self, value):
        self.value = value

    async def __aenter__(self):
        return self.value

    async def __aexit__(self, *_args):
        return None


class _Conn:
    def transaction(self):
        return _Context(self)

    async def fetchval(self, *_args):
        return 1

    async def fetchrow(self, *_args):
        return None

    async def execute(self, *_args):
        return None


class _Pool:
    def __init__(self):
        self.conn = _Conn()

    def acquire(self):
        return _Context(self.conn)


def _row(*, content_hash=_OLD_HASH, etag="etag-old", s3_key="team/original.bin"):
    return {
        "id": uuid.UUID("11111111-2222-3333-4444-555555555555"),
        "vault_id": uuid.UUID("aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"),
        "collection_id": None,
        "collection": "proof",
        "name": "original.bin",
        "s3_key": s3_key,
        "mime_type": "application/octet-stream",
        "size_bytes": len(_OLD_BYTES),
        "description": "fixture",
        "created_by": "tester",
        "content_hash": content_hash,
        "hash_algorithm": "sha256",
        "etag": etag,
        "storage_version": None,
    }


async def _service(monkeypatch):
    monkeypatch.setattr(fs, "measurement_enabled", lambda: False)
    pool = _Pool()

    async def _pool():
        return pool

    monkeypatch.setattr(fs, "get_pool", _pool)
    return fs.FileService(), pool


async def test_initiate_replace_rejects_stale_hash_before_issuing_upload(monkeypatch):
    service, _pool = await _service(monkeypatch)

    async def _find(*_args):
        return _row()

    monkeypatch.setattr(fs.vault_files_repo, "find_by_id", _find)
    monkeypatch.setattr(
        fs.s3_adapter,
        "presign_put",
        lambda *_args, **_kwargs: pytest.fail("stale request must not receive an upload URL"),
    )

    with pytest.raises(ConflictError, match="content_hash moved") as exc:
        await service.initiate_replace(
            "team",
            _row()["vault_id"],
            str(_row()["id"]),
            content_hash=_NEW_HASH,
            expected_content_hash="0" * 64,
        )
    assert exc.value.status_code == 409


async def test_initiate_replace_rejects_stale_version(monkeypatch):
    service, _pool = await _service(monkeypatch)

    async def _find(*_args):
        return _row()

    monkeypatch.setattr(fs.vault_files_repo, "find_by_id", _find)
    monkeypatch.setattr(
        fs.s3_adapter,
        "presign_put",
        lambda *_args, **_kwargs: pytest.fail("stale request must not receive an upload URL"),
    )

    with pytest.raises(ConflictError, match="file version moved") as exc:
        await service.initiate_replace(
            "team",
            _row()["vault_id"],
            str(_row()["id"]),
            content_hash=_NEW_HASH,
            expected_content_hash=_OLD_HASH,
            expected_version="etag-stale",
        )
    assert exc.value.status_code == 409


async def test_initiate_replace_skips_identical_content(monkeypatch):
    service, _pool = await _service(monkeypatch)

    async def _find(*_args):
        return _row()

    monkeypatch.setattr(fs.vault_files_repo, "find_by_id", _find)
    monkeypatch.setattr(
        fs.s3_adapter,
        "presign_put",
        lambda *_args, **_kwargs: pytest.fail("identical content must not be uploaded"),
    )

    result = await service.initiate_replace(
        "team",
        _row()["vault_id"],
        str(_row()["id"]),
        content_hash=_OLD_HASH,
        expected_content_hash=_OLD_HASH,
        expected_version="etag-old",
    )

    assert result["unchanged"] is True
    assert result["content_hash"] == _OLD_HASH
    assert result["version"] == "etag-old"


async def test_initiate_replace_schedules_abandoned_staging_cleanup(monkeypatch):
    service, _pool = await _service(monkeypatch)
    scheduled: list[tuple[str, int]] = []

    async def _find(*_args):
        return _row()

    async def _enqueue(_conn, key, *, delay_seconds=0):
        scheduled.append((key, delay_seconds))
        return len(scheduled)

    monkeypatch.setattr(fs.vault_files_repo, "find_by_id", _find)
    monkeypatch.setattr(fs.s3_adapter, "ensure_bucket", lambda _bucket: None)
    monkeypatch.setattr(fs.s3_adapter, "presign_put", lambda *_args, **_kwargs: "https://upload.test")
    monkeypatch.setattr(fs, "_enqueue_s3_delete", _enqueue)

    result = await service.initiate_replace(
        "team",
        _row()["vault_id"],
        str(_row()["id"]),
        content_hash=_NEW_HASH,
    )

    assert result["unchanged"] is False
    assert result["upload_url"] == "https://upload.test"
    assert scheduled == [
        (
            fs._replacement_staging_key(
                "team",
                _row()["id"],
                uuid.UUID(result["replacement_id"]),
            ),
            fs._REPLACEMENT_STAGING_DELETE_DELAY,
        )
    ]


async def test_confirm_replace_switches_metadata_only_after_locked_recheck(monkeypatch):
    service, _pool = await _service(monkeypatch)
    old = _row()
    copied: list[tuple[str, str]] = []
    replaced: list[dict] = []
    enqueued: list[tuple[str, int]] = []
    cancelled: list[int] = []
    events: list[tuple] = []

    async def _find(*_args):
        return dict(old)

    async def _locked(*_args):
        return dict(old)

    async def _replace(_conn, _fid, **kwargs):
        replaced.append(kwargs)

    async def _enqueue(_conn, key, *, delay_seconds=0):
        enqueued.append((key, delay_seconds))
        return len(enqueued)

    async def _cancel(_conn, outbox_id):
        cancelled.append(outbox_id)

    async def _event(*args, **kwargs):
        events.append((args, kwargs))

    async def _index(*_args, **_kwargs):
        return None

    def _copy(source, destination):
        copied.append((source, destination))
        return {
            "ContentLength": len(_NEW_BYTES),
            "ContentType": "application/octet-stream",
            "ETag": '"etag-new"',
        }

    monkeypatch.setattr(fs.vault_files_repo, "find_by_id", _find)
    monkeypatch.setattr(fs.vault_files_repo, "find_by_id_for_update", _locked)
    monkeypatch.setattr(fs.vault_files_repo, "replace_confirmed_metadata", _replace)
    monkeypatch.setattr(fs, "_enqueue_s3_delete", _enqueue)
    monkeypatch.setattr(fs, "_cancel_s3_delete", _cancel)
    monkeypatch.setattr(fs, "emit_event", _event)
    monkeypatch.setattr(fs, "index_file_metadata", _index)
    monkeypatch.setattr(fs.s3_adapter, "copy", _copy)
    monkeypatch.setattr(fs.s3_adapter, "iter_chunks", lambda key: iter([_NEW_BYTES]))

    result = await service.confirm_replace(
        "team",
        old["vault_id"],
        str(old["id"]),
        str(uuid.UUID("99999999-8888-7777-6666-555555555555")),
        actor_id="tester",
        content_hash=_NEW_HASH,
        expected_content_hash=_OLD_HASH,
        expected_version="etag-old",
    )

    assert result["uri"] == f"akb://team/coll/proof/file/{old['id']}"
    assert result["content_hash"] == _NEW_HASH
    assert result["previous_content_hash"] == _OLD_HASH
    assert result["version"] == "etag-new"
    assert result["previous_version"] == "etag-old"
    assert result["unchanged"] is False
    assert len(copied) == 1
    staging_key, final_key = copied[0]
    assert staging_key != old["s3_key"]
    assert final_key != old["s3_key"]
    assert final_key != staging_key
    assert final_key.split("/")[-1].startswith(
        "99999999888877776666555555555555_"
    )
    assert replaced[0]["s3_key"] == final_key
    assert replaced[0]["content_hash"] == _NEW_HASH
    assert enqueued == [
        (final_key, fs._REPLACEMENT_CANDIDATE_DELETE_DELAY),
        (old["s3_key"], 0),
        (staging_key, 0),
    ]
    assert cancelled == [1]
    assert events[0][0][1] == "file.update"


async def test_confirm_replace_detects_race_under_lock_without_touching_live_key(monkeypatch):
    service, _pool = await _service(monkeypatch)
    old = _row()
    raced = _row(content_hash=hashlib.sha256(b"concurrent").hexdigest(), etag="etag-raced")
    copied: list[tuple[str, str]] = []
    deleted: list[str] = []
    enqueued: list[tuple[str, int]] = []

    async def _find(*_args):
        return dict(old)

    async def _locked(*_args):
        return dict(raced)

    def _copy(source, destination):
        copied.append((source, destination))
        return {
            "ContentLength": len(_NEW_BYTES),
            "ContentType": "application/octet-stream",
            "ETag": '"etag-new"',
        }

    async def _enqueue(_conn, key, *, delay_seconds=0):
        enqueued.append((key, delay_seconds))
        return len(enqueued)

    monkeypatch.setattr(fs.vault_files_repo, "find_by_id", _find)
    monkeypatch.setattr(fs.vault_files_repo, "find_by_id_for_update", _locked)
    monkeypatch.setattr(
        fs.vault_files_repo,
        "replace_confirmed_metadata",
        lambda *_args, **_kwargs: pytest.fail("stale replacement must not publish metadata"),
    )
    monkeypatch.setattr(fs.s3_adapter, "copy", _copy)
    monkeypatch.setattr(fs.s3_adapter, "iter_chunks", lambda key: iter([_NEW_BYTES]))
    monkeypatch.setattr(fs.s3_adapter, "delete", lambda key: deleted.append(key))
    monkeypatch.setattr(fs, "_enqueue_s3_delete", _enqueue)

    with pytest.raises(ConflictError, match="content_hash moved") as exc:
        await service.confirm_replace(
            "team",
            old["vault_id"],
            str(old["id"]),
            str(uuid.uuid4()),
            actor_id="tester",
            content_hash=_NEW_HASH,
            expected_content_hash=_OLD_HASH,
            expected_version="etag-old",
        )

    assert exc.value.status_code == 409
    assert copied[0][1] != old["s3_key"], "the candidate copy must never target the live key"
    assert set(deleted) == set(copied[0])
    assert (copied[0][1], fs._REPLACEMENT_CANDIDATE_DELETE_DELAY) in enqueued


async def test_confirm_replace_rejects_mismatched_uploaded_bytes(monkeypatch):
    service, _pool = await _service(monkeypatch)
    old = _row()
    copied: list[tuple[str, str]] = []
    deleted: list[str] = []

    async def _find(*_args):
        return dict(old)

    def _copy(source, destination):
        copied.append((source, destination))
        return {
            "ContentLength": len(b"wrong"),
            "ContentType": "application/octet-stream",
            "ETag": '"etag-wrong"',
        }

    monkeypatch.setattr(fs.vault_files_repo, "find_by_id", _find)
    monkeypatch.setattr(fs.s3_adapter, "copy", _copy)
    monkeypatch.setattr(fs.s3_adapter, "iter_chunks", lambda key: iter([b"wrong"]))
    monkeypatch.setattr(fs.s3_adapter, "delete", lambda key: deleted.append(key))

    with pytest.raises(ConflictError, match="hash mismatch"):
        await service.confirm_replace(
            "team",
            old["vault_id"],
            str(old["id"]),
            str(uuid.uuid4()),
            actor_id="tester",
            content_hash=_NEW_HASH,
        )

    assert set(deleted) == set(copied[0])


async def test_confirm_replace_never_deletes_candidate_on_ambiguous_commit(monkeypatch):
    """A successful COMMIT can still surface a transport error to the caller.

    The final object must stay intact in that case; its delayed cleanup is
    cancelled atomically by the same transaction if publication committed.
    """
    service, pool = await _service(monkeypatch)
    old = _row()
    deleted: list[str] = []
    enqueued: list[tuple[str, int]] = []

    class _AmbiguousTransaction(_Context):
        async def __aexit__(self, exc_type, *_args):
            if exc_type is None:
                raise RuntimeError("commit outcome unknown")
            return None

    class _AmbiguousConn(_Conn):
        def transaction(self):
            return _AmbiguousTransaction(self)

    pool.conn = _AmbiguousConn()

    async def _find(*_args):
        return dict(old)

    async def _replace(*_args, **_kwargs):
        return None

    async def _enqueue(_conn, key, *, delay_seconds=0):
        enqueued.append((key, delay_seconds))
        return len(enqueued)

    async def _noop(*_args, **_kwargs):
        return None

    monkeypatch.setattr(fs.vault_files_repo, "find_by_id", _find)
    monkeypatch.setattr(fs.vault_files_repo, "find_by_id_for_update", _find)
    monkeypatch.setattr(fs.vault_files_repo, "replace_confirmed_metadata", _replace)
    monkeypatch.setattr(fs, "_enqueue_s3_delete", _enqueue)
    monkeypatch.setattr(fs, "_cancel_s3_delete", _noop)
    monkeypatch.setattr(fs, "emit_event", _noop)
    monkeypatch.setattr(
        fs.s3_adapter,
        "copy",
        lambda _source, _destination: {
            "ContentLength": len(_NEW_BYTES),
            "ContentType": "application/octet-stream",
            "ETag": '"etag-new"',
        },
    )
    monkeypatch.setattr(fs.s3_adapter, "iter_chunks", lambda _key: iter([_NEW_BYTES]))
    monkeypatch.setattr(fs.s3_adapter, "delete", lambda key: deleted.append(key))

    replacement_id = uuid.uuid4()
    with pytest.raises(RuntimeError, match="commit outcome unknown"):
        await service.confirm_replace(
            "team",
            old["vault_id"],
            str(old["id"]),
            str(replacement_id),
            actor_id="tester",
            content_hash=_NEW_HASH,
        )

    staging_key = fs._replacement_staging_key("team", old["id"], replacement_id)
    final_key = fs._replacement_final_key(
        "team", old["collection"], old["name"], replacement_id,
    )
    assert staging_key in deleted
    assert final_key not in deleted
    assert (final_key, fs._REPLACEMENT_CANDIDATE_DELETE_DELAY) in enqueued


async def test_delete_locks_row_before_selecting_object_for_cleanup(monkeypatch):
    """Delete must enqueue the key observed after serializing with replace."""
    service, _pool = await _service(monkeypatch)
    current = _row(s3_key="team/newly-published.bin")
    enqueued: list[str] = []

    async def _locked(*_args):
        return dict(current)

    async def _legacy_read(*_args):
        pytest.fail("delete must not read file metadata outside the row lock")

    async def _noop(*_args, **_kwargs):
        return None

    async def _enqueue(_conn, key, *, delay_seconds=0):
        assert delay_seconds == 0
        enqueued.append(key)
        return len(enqueued)

    monkeypatch.setattr(fs.vault_files_repo, "find_by_id", _legacy_read)
    monkeypatch.setattr(fs.vault_files_repo, "find_by_id_for_update", _locked)
    monkeypatch.setattr(fs.vault_files_repo, "delete", _noop)
    monkeypatch.setattr(fs, "delete_file_chunks", _noop)
    monkeypatch.setattr(fs, "_enqueue_s3_delete", _enqueue)
    monkeypatch.setattr(fs, "emit_event", _noop)

    result = await service.delete(
        current["vault_id"], str(current["id"]), actor_id="tester",
    )

    assert result["deleted"] is True
    assert enqueued == [current["s3_key"]]


async def test_storage_copy_uses_boto_managed_copy_for_large_file_compatibility(monkeypatch):
    from app.services.adapters import s3_adapter

    calls: list[tuple] = []

    class _S3:
        def copy(self, source, bucket, destination):
            calls.append((source, bucket, destination))

    monkeypatch.setattr(s3_adapter, "client", lambda: _S3())
    monkeypatch.setattr(
        s3_adapter,
        "head",
        lambda key: {"ContentLength": 12, "ETag": '"copied"', "Key": key},
    )

    result = s3_adapter.copy("staging/source", "published/destination")

    assert calls == [
        (
            {"Bucket": fs.settings.s3_bucket, "Key": "staging/source"},
            fs.settings.s3_bucket,
            "published/destination",
        )
    ]
    assert result["Key"] == "published/destination"
