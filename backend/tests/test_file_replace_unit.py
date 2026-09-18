"""File replacement contract and optimistic-concurrency regression tests."""

from __future__ import annotations

import hashlib
import uuid

import pytest

from app.exceptions import ConflictError, NotFoundError
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


def _row(
    *,
    content_hash=_OLD_HASH,
    etag="etag-old",
    s3_key="team/original.bin",
    kind="file",
):
    return {
        "id": uuid.UUID("11111111-2222-3333-4444-555555555555"),
        "vault_id": uuid.UUID("aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"),
        "collection_id": None,
        "collection": "proof",
        "kind": kind,
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


def _never(message: str):
    """A stand-in that fails the test if the step it replaces ever runs."""

    async def _fail(*_args, **_kwargs):
        pytest.fail(message)

    return _fail


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
        fs,
        "_issue_write_capability",
        _never("stale request must not receive an upload URL"),
    )

    with pytest.raises(ConflictError, match="content_hash moved") as exc:
        await service.initiate_replace(
            "team",
            _row()["vault_id"],
            str(_row()["id"]),
            actor_id="tester",
            content_hash=_NEW_HASH,
            expected_content_hash="0" * 64,
        )
    assert exc.value.status_code == 409


async def test_replace_routes_do_not_treat_document_images_as_files(monkeypatch):
    service, _pool = await _service(monkeypatch)

    async def _find(*_args):
        return _row(kind="attachment")

    async def _discard(*_args):
        return None

    monkeypatch.setattr(fs.vault_files_repo, "find_by_id", _find)
    monkeypatch.setattr(fs, "_discard_replacement_objects", _discard)
    monkeypatch.setattr(
        fs,
        "_issue_write_capability",
        _never("a document image must not receive a File replacement URL"),
    )
    monkeypatch.setattr(
        fs.s3_adapter,
        "copy",
        lambda *_args, **_kwargs: pytest.fail(
            "a document image must not reach object replacement"
        ),
    )

    with pytest.raises(NotFoundError):
        await service.initiate_replace(
            "team",
            _row()["vault_id"],
            str(_row()["id"]),
            actor_id="tester",
            content_hash=_NEW_HASH,
        )
    with pytest.raises(NotFoundError):
        await service.confirm_replace(
            "team",
            _row()["vault_id"],
            str(_row()["id"]),
            str(uuid.uuid4()),
            actor_id="tester",
            content_hash=_NEW_HASH,
        )


async def test_initiate_replace_rejects_stale_version(monkeypatch):
    service, _pool = await _service(monkeypatch)

    async def _find(*_args):
        return _row()

    monkeypatch.setattr(fs.vault_files_repo, "find_by_id", _find)
    monkeypatch.setattr(
        fs,
        "_issue_write_capability",
        _never("stale request must not receive an upload URL"),
    )

    with pytest.raises(ConflictError, match="file version moved") as exc:
        await service.initiate_replace(
            "team",
            _row()["vault_id"],
            str(_row()["id"]),
            actor_id="tester",
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
        fs,
        "_issue_write_capability",
        _never("identical content must not be uploaded"),
    )

    result = await service.initiate_replace(
        "team",
        _row()["vault_id"],
        str(_row()["id"]),
        actor_id="tester",
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

    granted: list[dict] = []

    async def _issue(_conn, **kwargs):
        granted.append(kwargs)
        return "T" * 43

    monkeypatch.setattr(fs.vault_files_repo, "find_by_id", _find)
    monkeypatch.setattr(fs.s3_adapter, "ensure_bucket", lambda _bucket: None)
    monkeypatch.setattr(fs, "_issue_write_capability", _issue)
    monkeypatch.setattr(fs, "_enqueue_s3_delete", _enqueue)

    result = await service.initiate_replace(
        "team",
        _row()["vault_id"],
        str(_row()["id"]),
        actor_id="tester",
        content_hash=_NEW_HASH,
    )

    staging_key = fs._replacement_staging_key(
        "team", _row()["id"], uuid.UUID(result["replacement_id"]),
    )
    assert result["unchanged"] is False
    assert result["upload_url"].endswith("/api/v1/files/upload/" + "T" * 43)
    assert result["expires_in"] == fs._PRESIGN_UPLOAD_TTL
    assert scheduled == [(staging_key, fs._REPLACEMENT_STAGING_DELETE_DELAY)]
    # The capability must name the staging key. Bound to the live key it would
    # let a caller overwrite the object the replacement is meant to supersede
    # without ever passing the optimistic-concurrency recheck.
    assert granted[0]["object_key"] == staging_key
    assert granted[0]["object_key"] != _row()["s3_key"]


async def test_upload_url_carries_no_locator_and_never_signs(monkeypatch):
    """`upload_url` names this service and an opaque token, nothing else.

    A presigned PUT URL carried the object store's endpoint, the bucket, the
    key and a signature, and the response repeated the key in `s3_key`. What a
    caller does is unchanged — PUT the bytes to an absolute URL with no
    Authorization header — but the storage topology is no longer part of the
    public contract, and `s3_key` is gone because nothing ever read it."""
    service, pool = await _service(monkeypatch)
    written: list[tuple] = []

    async def allowed(*_args, **_kwargs):
        return True

    async def inserted(_conn, **kwargs):
        return kwargs["file_id"]

    async def _execute(*args):
        written.append(args)

    monkeypatch.setattr(pool.conn, "execute", _execute)
    monkeypatch.setattr(fs, "lock_vault_for_child_write", allowed)
    monkeypatch.setattr(fs.vault_files_repo, "s3_key_available_for_registration", allowed)
    monkeypatch.setattr(fs.vault_files_repo, "insert_or_adopt", inserted)
    monkeypatch.setattr(fs.s3_adapter, "ensure_bucket", lambda _bucket: None)

    result = await service.initiate_upload(
        "team", _row()["vault_id"], "", "test.bin", actor_id="tester",
    )

    assert "s3_key" not in result
    assert "/api/v1/files/upload/" in result["upload_url"]
    for leaked in ("X-Amz-", "akb-files", "?", "team/"):
        assert leaked not in result["upload_url"], leaked
    assert result["expires_in"] == fs._PRESIGN_UPLOAD_TTL
    # The grant is written in the same transaction as the reservation, and it
    # names the key — that binding is what the route resolves.
    grants = [a for a in written if "m1_file_transfer_intents" in a[0]]
    assert grants, "an upload grant must be recorded"
    assert "'PUT'" in grants[0][0]


async def test_download_reports_the_capability_lifetime_and_never_signs(monkeypatch):
    """`expires_in` must equal how long the URL actually works.

    A presigned URL could have its life cut short by the S3 session's own
    expiry, so the response had to report the shortened figure. A capability's
    life is the row this call writes, so the two cannot drift — but the
    contract a caller reads is unchanged, and one consumer treats
    `expires_in <= 0` as already-expired.

    The stronger assertion here is the negative one: a download must not reach
    the object store at all. That is the whole point of the change."""
    service, pool = await _service(monkeypatch)
    written: list[tuple] = []

    async def _execute(*args):
        written.append(args)

    monkeypatch.setattr(pool.conn, "execute", _execute)

    async def find(*_args):
        return {**_row(), "upload_state": "confirmed", "hash_algorithm": "sha256"}

    monkeypatch.setattr(fs.vault_files_repo, "find_by_id", find)
    # The guard that used to stand here monkeypatched `presign_get` to fail.
    # That function no longer exists, which is a stronger statement than any
    # stub could make: there is no way to sign a download for a client.
    assert not hasattr(fs.s3_adapter, "presign_get")

    result = await service.get_download_url(_row()["vault_id"], str(_row()["id"]))

    assert result["expires_in"] == fs._PRESIGN_DOWNLOAD_TTL
    assert result["expires_in"] > 0
    # The lifetime written to the grant is the one reported back. Asserted by
    # meaning rather than by position — the parameter list grows.
    assert written, "a grant must be recorded"
    sql, *params = written[0]
    assert "INSERT INTO m1_file_transfer_intents" in sql
    assert result["expires_in"] in params
    # No locator, no signature — only this service and an opaque token.
    assert "/api/v1/files/download/" in result["download_url"]
    for leaked in ("X-Amz-", "akb-files", "?"):
        assert leaked not in result["download_url"], leaked


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
