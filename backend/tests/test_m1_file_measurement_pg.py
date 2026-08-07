"""Real-PostgreSQL contract for the guarded M1 public File protocol."""

from __future__ import annotations

import asyncio
import hashlib
import importlib.util
import os
import threading
import uuid
from pathlib import Path

import asyncpg
import pytest
import pytest_asyncio

from app.config import settings
from app.exceptions import AKBError, NotFoundError
from app.repositories import vault_files_repo
from app.services import m1_file_measurement as m1
from app.services.m1_binary_store import PreparedBinary
from app.services.m1_native_grep_service import M1NativeGrepService
from app.services.m1_pg_body_store import M1PgBodyStore
from app.services.m1_reference_payload_store import M1ReferencePayloadStore
from app.services.native_derived_worker import NativeDerivedWorker
from app.services.native_revision_service import (
    FAILPOINT_BOUNDARIES,
    NativeRevisionService,
)


_DSN = os.environ.get("AKB_M1_FILE_TEST_DSN")
_BACKEND = Path(__file__).resolve().parents[1]


def _migration(filename: str):
    path = _BACKEND / "app" / "db" / "migrations" / filename
    spec = importlib.util.spec_from_file_location(f"m1_file_{filename}_test", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest_asyncio.fixture
async def pool():
    if not _DSN:
        if os.environ.get("REQUIRE_REAL_PG") == "1":
            pytest.fail("AKB_M1_FILE_TEST_DSN is required by the real-PG gate")
        pytest.skip("AKB_M1_FILE_TEST_DSN is not configured")
    pool = await asyncpg.create_pool(_DSN, min_size=1, max_size=8)
    async with pool.acquire() as conn:
        await conn.execute((_BACKEND / "app" / "db" / "init.sql").read_text())
        for filename in (
            "015_events_outbox.py",
            "019_s3_delete_outbox.py",
            "048_native_revision_core.py",
            "053_native_revision_m1_pg_body.py",
            "054_native_revision_searchable_derived.py",
            "055_native_revision_m1_file_storage.py",
            "056_native_revision_m1_file_constraints.py",
            "057_native_revision_m1_payload_placement.py",
        ):
            await _migration(filename).migrate(conn)
    try:
        yield pool
    finally:
        await pool.close()


@pytest_asyncio.fixture
async def context(pool, tmp_path, monkeypatch):
    suffix = uuid.uuid4().hex
    async with pool.acquire() as conn:
        owner_id = await conn.fetchval(
            "INSERT INTO users (username, email, password_hash) VALUES ($1, $2, 'disabled') RETURNING id",
            f"m1-file-{suffix}", f"m1-file-{suffix}@invalid.example",
        )
        vault_id = await conn.fetchval(
            "INSERT INTO vaults (name, git_path, owner_id) VALUES ($1, '/tmp/unused.git', $2) RETURNING id",
            f"m1-file-vault-{suffix}", owner_id,
        )
        denied_vault_id = await conn.fetchval(
            "INSERT INTO vaults (name, git_path, owner_id) VALUES ($1, '/tmp/unused-denied.git', $2) RETURNING id",
            f"m1-file-denied-{suffix}", owner_id,
        )

    async def test_pool():
        return pool

    monkeypatch.setattr(m1, "get_pool", test_pool)
    monkeypatch.setattr(settings, "native_revision_m1_measurement_only", True)
    monkeypatch.setattr(settings, "db_name", "akb_revision_m1_measurement")
    monkeypatch.setattr(settings, "native_revision_m1_file_driver", "fscas")
    monkeypatch.setattr(settings, "native_revision_m1_file_fscas_root", str(tmp_path))
    monkeypatch.setattr(settings, "public_base_url", "https://akb.test")
    m1.reset_native_text_file_services_for_tests()
    try:
        yield pool, vault_id, denied_vault_id, f"m1-file-vault-{suffix}"
    finally:
        m1.reset_native_text_file_services_for_tests()
        async with pool.acquire() as conn:
            await conn.execute("DELETE FROM vaults WHERE owner_id = $1", owner_id)
            await conn.execute("DELETE FROM users WHERE id = $1", owner_id)


def _token(url: str) -> str:
    return url.rsplit("/", 1)[-1]


@pytest.mark.asyncio
async def test_measurement_file_storage_driver_is_database_constrained(context):
    pool, vault_id, _denied, _vault_name = context
    async with pool.acquire() as conn:
        with pytest.raises(asyncpg.CheckViolationError):
            await conn.execute(
                """
                INSERT INTO vault_files (
                    id, vault_id, name, s3_key, mime_type, size_bytes, created_by,
                    storage_driver, storage_locator
                ) VALUES ($1, $2, 'bad.bin', $3, 'application/octet-stream', 1,
                          'constraint-test', 'unknown', 'bad/locator')
                """,
                uuid.uuid4(), vault_id, f"m1-logical/{uuid.uuid4()}",
            )
        with pytest.raises(asyncpg.CheckViolationError):
            await conn.execute(
                """
                INSERT INTO vault_files (
                    id, vault_id, name, s3_key, mime_type, size_bytes, created_by,
                    storage_driver, storage_locator, native_resource_id,
                    native_revision_id
                ) VALUES ($1, $2, 'null-head.txt', $3, 'text/plain', 1,
                          'constraint-test', 'native_text', $4, $1, NULL)
                """,
                uuid.uuid4(), vault_id, f"m1-logical/{uuid.uuid4()}",
                f"native-text/{uuid.uuid4()}/missing-head",
            )


@pytest.mark.asyncio
async def test_pending_is_only_an_intent_and_survives_second_service_instance(context):
    pool, vault_id, denied_vault_id, vault_name = context
    data = b"durable binary\x00"
    digest = hashlib.sha256(data).hexdigest()
    first = m1.MeasurementFileService()
    initiated = await first.initiate_upload(
        vault_name=vault_name, vault_id=vault_id, collection="proof", filename="a.bin",
        actor_id="tester", mime_type="application/octet-stream", description="", content_hash=digest,
    )
    file_id = uuid.UUID(initiated["file_id"])
    async with pool.acquire() as conn:
        assert await conn.fetchval("SELECT count(*) FROM vault_files WHERE id = $1", file_id) == 0
        intent = await conn.fetchrow("SELECT declared_content_hash, body FROM m1_file_transfer_intents WHERE file_id = $1", file_id)
        assert intent["declared_content_hash"] == digest and intent["body"] is None
        assert await conn.fetchval("SELECT count(*) FROM publications WHERE resource_uri LIKE $1", f"%{file_id}%") == 0

    second = m1.MeasurementFileService()
    await second.transfer(_token(initiated["upload_url"]), method="PUT", body=data)
    with pytest.raises(NotFoundError):
        await second.confirm_upload(denied_vault_id, str(file_id), content_hash=digest)
    confirmed = await second.confirm_upload(vault_id, str(file_id), content_hash=digest)
    assert confirmed["file_id"] == str(file_id)
    assert confirmed["uri"] == initiated["uri"]
    assert confirmed["vault"] == vault_name
    assert confirmed["collection"] == "proof"
    assert confirmed["content_hash"] == digest
    assert confirmed["storage_driver"] == "fscas"
    assert [item["file_id"] for item in await second.list_files(vault_id, vault_name, None, 50)] == [str(file_id)]
    with pytest.raises(NotFoundError):
        await second.get_download_url(denied_vault_id, str(file_id))
    with pytest.raises(NotFoundError):
        await second.delete(denied_vault_id, str(file_id), actor_id="tester")
    assert await second.list_files(denied_vault_id, "denied", None, 50) == []

    # Confirm is durable/idempotent after the intent has been removed.
    assert (await first.confirm_upload(vault_id, str(file_id), content_hash=digest))["file_id"] == str(file_id)
    retry = await first.initiate_upload(
        vault_name=vault_name, vault_id=vault_id, collection="proof", filename="a.bin",
        actor_id="tester", mime_type="application/octet-stream", description="", content_hash=digest,
    )
    assert retry["deduplicated"] is True
    assert retry["file_id"] == str(file_id) and retry["uri"] == initiated["uri"]
    await second.transfer(_token(retry["upload_url"]), method="PUT", body=data)
    assert (await second.confirm_upload(vault_id, str(file_id), content_hash=digest))["uri"] == initiated["uri"]


@pytest.mark.asyncio
async def test_multiple_get_capabilities_and_shared_digest_survive_logical_delete(context):
    _pool, vault_id, _denied, vault_name = context
    service = m1.MeasurementFileService()
    data = b"same digest\x00"
    digest = hashlib.sha256(data).hexdigest()
    ids = []
    for name in ("one.bin", "two.bin"):
        initiated = await service.initiate_upload(
            vault_name=vault_name, vault_id=vault_id, collection="", filename=name,
            actor_id="tester", mime_type="application/octet-stream", description="", content_hash=digest,
        )
        await service.transfer(_token(initiated["upload_url"]), method="PUT", body=data)
        confirmed = await service.confirm_upload(vault_id, initiated["file_id"], content_hash=digest)
        ids.append(confirmed["file_id"])
    first_get = await service.get_download_url(vault_id, ids[1])
    second_get = await service.get_download_url(vault_id, ids[1])
    assert await service.transfer(_token(first_get["download_url"]), method="GET") == data
    assert await service.transfer(_token(second_get["download_url"]), method="GET") == data
    await service.delete(vault_id, ids[0], actor_id="tester")
    third_get = await service.get_download_url(vault_id, ids[1])
    assert await service.transfer(_token(third_get["download_url"]), method="GET") == data


@pytest.mark.asyncio
async def test_mismatch_expiry_and_failed_publish_leave_no_public_file(context, monkeypatch):
    pool, vault_id, _denied, vault_name = context
    service = m1.MeasurementFileService()
    bounded = await service.initiate_upload(
        vault_name=vault_name, vault_id=vault_id, collection="", filename="bounded.bin",
        actor_id="tester", mime_type="application/octet-stream", description="", content_hash=None,
    )
    monkeypatch.setattr(settings, "native_revision_m1_file_transfer_max_bytes", 2)
    with pytest.raises(AKBError) as too_large:
        await service.transfer(_token(bounded["upload_url"]), method="PUT", body=b"123")
    assert too_large.value.status_code == 413
    monkeypatch.setattr(settings, "native_revision_m1_file_transfer_max_bytes", 16 * 1024 * 1024)
    digest = hashlib.sha256(b"expected").hexdigest()
    initiated = await service.initiate_upload(
        vault_name=vault_name, vault_id=vault_id, collection="", filename="mismatch.bin",
        actor_id="tester", mime_type="application/octet-stream", description="", content_hash=digest,
    )
    with pytest.raises(AKBError) as mismatch:
        await service.transfer(_token(initiated["upload_url"]), method="PUT", body=b"wrong")
    assert mismatch.value.status_code == 409
    async with pool.acquire() as conn:
        assert await conn.fetchval("SELECT count(*) FROM m1_file_transfer_intents WHERE file_id = $1", uuid.UUID(initiated["file_id"])) == 0
        assert await conn.fetchval("SELECT count(*) FROM vault_files WHERE id = $1", uuid.UUID(initiated["file_id"])) == 0

    expiring = await service.initiate_upload(
        vault_name=vault_name, vault_id=vault_id, collection="", filename="expired.bin",
        actor_id="tester", mime_type="application/octet-stream", description="", content_hash=None,
    )
    async with pool.acquire() as conn:
        await conn.execute("UPDATE m1_file_transfer_intents SET expires_at = NOW() - INTERVAL '1 second' WHERE file_id = $1", uuid.UUID(expiring["file_id"]))
    assert await service.reap_transfer_intents() == 1

    data = b"publish failure\x00"
    failure_digest = hashlib.sha256(data).hexdigest()
    failed = await service.initiate_upload(
        vault_name=vault_name, vault_id=vault_id, collection="", filename="failed.bin",
        actor_id="tester", mime_type="application/octet-stream", description="", content_hash=failure_digest,
    )
    await service.transfer(_token(failed["upload_url"]), method="PUT", body=data)

    async def fail_publish(*_args, **_kwargs):
        raise RuntimeError("injected DB publication failure")

    monkeypatch.setattr(vault_files_repo, "insert_or_adopt_measurement_confirmed", fail_publish)
    with pytest.raises(RuntimeError, match="injected DB"):
        await service.confirm_upload(vault_id, failed["file_id"], content_hash=failure_digest)
    async with pool.acquire() as conn:
        assert await conn.fetchval("SELECT count(*) FROM vault_files WHERE id = $1", uuid.UUID(failed["file_id"])) == 0
        assert await conn.fetchval("SELECT count(*) FROM m1_file_transfer_intents WHERE file_id = $1", uuid.UUID(failed["file_id"])) == 1


@pytest.mark.parametrize("data", [b"declared text\x00body", b"declared text \xff body"])
@pytest.mark.asyncio
async def test_declared_text_rejects_non_text_bytes_without_publishing_authority(
    context, data,
):
    pool, vault_id, _denied, vault_name = context
    service = m1.MeasurementFileService()
    digest = hashlib.sha256(data).hexdigest()
    initiated = await service.initiate_upload(
        vault_name=vault_name,
        vault_id=vault_id,
        collection="notes",
        filename="invalid.txt",
        actor_id="tester",
        mime_type="text/plain",
        description="",
        content_hash=digest,
    )
    file_id = uuid.UUID(initiated["file_id"])
    await service.transfer(_token(initiated["upload_url"]), method="PUT", body=data)

    with pytest.raises(AKBError, match="valid UTF-8 text without NUL") as error:
        await service.confirm_upload(vault_id, initiated["file_id"], content_hash=digest)

    assert error.value.status_code == 422
    async with pool.acquire() as conn:
        assert await conn.fetchval(
            "SELECT count(*) FROM vault_files WHERE id = $1", file_id,
        ) == 0
        assert await conn.fetchval(
            "SELECT count(*) FROM native_resources WHERE resource_id = $1", file_id,
        ) == 0
        assert await conn.fetchval(
            "SELECT count(*) FROM native_revisions WHERE resource_id = $1", file_id,
        ) == 0
        assert await conn.fetchval(
            "SELECT count(*) FROM m1_reference_payloads WHERE digest = $1", digest,
        ) == 0
        assert await conn.fetchval(
            "SELECT count(*) FROM m1_file_transfer_intents WHERE file_id = $1", file_id,
        ) == 0


@pytest.mark.parametrize("filename", ["nested/report.txt", "/absolute.txt"])
@pytest.mark.asyncio
async def test_measurement_file_rejects_name_that_is_not_one_segment(
    context, filename,
):
    pool, vault_id, _denied, vault_name = context
    service = m1.MeasurementFileService()
    async with pool.acquire() as conn:
        before = await conn.fetchval("SELECT count(*) FROM m1_file_transfer_intents")

    with pytest.raises(AKBError, match="single path segment") as error:
        await service.initiate_upload(
            vault_name=vault_name,
            vault_id=vault_id,
            collection="notes",
            filename=filename,
            actor_id="tester",
            mime_type="text/plain",
            description="",
            content_hash=None,
        )

    assert error.value.status_code == 422
    async with pool.acquire() as conn:
        assert await conn.fetchval(
            "SELECT count(*) FROM m1_file_transfer_intents"
        ) == before


@pytest.mark.asyncio
async def test_non_text_mime_with_binary_bytes_remains_binary(context):
    _pool, vault_id, _denied, vault_name = context
    service = m1.MeasurementFileService()
    data = b"real binary\x00\xff"
    digest = hashlib.sha256(data).hexdigest()
    initiated = await service.initiate_upload(
        vault_name=vault_name,
        vault_id=vault_id,
        collection="",
        filename="real.bin",
        actor_id="tester",
        mime_type="application/octet-stream",
        description="",
        content_hash=digest,
    )
    await service.transfer(_token(initiated["upload_url"]), method="PUT", body=data)

    confirmed = await service.confirm_upload(
        vault_id, initiated["file_id"], content_hash=digest,
    )

    assert confirmed["storage_driver"] == "fscas"
    assert confirmed["native_resource_id"] is None


@pytest.mark.asyncio
async def test_binary_confirm_and_open_keep_slow_cas_io_off_the_event_loop(
    context, monkeypatch,
):
    _pool, vault_id, _denied, vault_name = context
    service = m1.MeasurementFileService()
    data = b"slow binary CAS\x00"
    digest = hashlib.sha256(data).hexdigest()
    loop_thread = threading.get_ident()

    class SlowStore:
        driver = "fscas"

        def __init__(self):
            self.prepare_started = threading.Event()
            self.prepare_release = threading.Event()
            self.open_started = threading.Event()
            self.open_release = threading.Event()
            self.prepare_thread = None
            self.open_thread = None

        def prepare_verified(self, _tenant, body, expected_digest, expected_size):
            self.prepare_thread = threading.get_ident()
            self.prepare_started.set()
            assert self.prepare_release.wait(timeout=2)
            assert body == data
            return PreparedBinary("test/slow", expected_digest, expected_size, self.driver)

        def open_verified(self, _tenant, prepared):
            self.open_thread = threading.get_ident()
            self.open_started.set()
            assert self.open_release.wait(timeout=2)
            assert prepared.digest == digest
            return data

    store = SlowStore()
    monkeypatch.setattr(m1, "_store", lambda: store)
    initiated = await service.initiate_upload(
        vault_name=vault_name, vault_id=vault_id, collection="", filename="slow.bin",
        actor_id="tester", mime_type="application/octet-stream", description="",
        content_hash=digest,
    )
    await service.transfer(_token(initiated["upload_url"]), method="PUT", body=data)

    confirm = asyncio.create_task(
        service.confirm_upload(vault_id, initiated["file_id"], content_hash=digest),
    )
    while not store.prepare_started.is_set():
        await asyncio.sleep(0)
    heartbeat = 0
    for _ in range(10):
        await asyncio.sleep(0)
        heartbeat += 1
    store.prepare_release.set()
    confirmed = await confirm

    download = await service.get_download_url(vault_id, confirmed["file_id"])
    opened = asyncio.create_task(
        service.transfer(_token(download["download_url"]), method="GET"),
    )
    while not store.open_started.is_set():
        await asyncio.sleep(0)
    for _ in range(10):
        await asyncio.sleep(0)
        heartbeat += 1
    store.open_release.set()

    assert await opened == data
    assert heartbeat == 20
    assert store.prepare_thread != loop_thread
    assert store.open_thread != loop_thread


@pytest.mark.asyncio
async def test_text_classification_runs_off_the_event_loop(context, monkeypatch):
    _pool, vault_id, _denied, vault_name = context
    service = m1.MeasurementFileService()
    data = b"classification probe"
    digest = hashlib.sha256(data).hexdigest()
    loop_thread = threading.get_ident()
    started = threading.Event()
    release = threading.Event()
    worker_thread = None

    def slow_classification(_mime_type, _data):
        nonlocal worker_thread
        worker_thread = threading.get_ident()
        started.set()
        assert release.wait(timeout=2)
        return False

    monkeypatch.setattr(m1, "_is_native_text", slow_classification)
    initiated = await service.initiate_upload(
        vault_name=vault_name,
        vault_id=vault_id,
        collection="",
        filename="classification.bin",
        actor_id="tester",
        mime_type="application/octet-stream",
        description="",
        content_hash=digest,
    )
    await service.transfer(_token(initiated["upload_url"]), method="PUT", body=data)

    confirm = asyncio.create_task(
        service.confirm_upload(vault_id, initiated["file_id"], content_hash=digest),
    )
    while not started.is_set():
        await asyncio.sleep(0)
    heartbeat = 0
    for _ in range(10):
        await asyncio.sleep(0)
        heartbeat += 1
    release.set()

    assert (await confirm)["storage_driver"] == "fscas"
    assert heartbeat == 10
    assert worker_thread != loop_thread


@pytest.mark.asyncio
async def test_periodic_reaper_removes_expired_body_without_later_upload(
    context, monkeypatch,
):
    from app.services import m1_file_transfer_reaper

    pool, vault_id, _denied, vault_name = context
    service = m1.MeasurementFileService()
    initiated = await service.initiate_upload(
        vault_name=vault_name, vault_id=vault_id, collection="", filename="idle-expired.bin",
        actor_id="tester", mime_type="application/octet-stream", description="",
        content_hash=None,
    )
    file_id = uuid.UUID(initiated["file_id"])
    await service.transfer(
        _token(initiated["upload_url"]), method="PUT", body=b"expired staging body\x00",
    )
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE m1_file_transfer_intents SET expires_at = NOW() - INTERVAL '1 second' WHERE file_id = $1",
            file_id,
        )
        assert await conn.fetchval(
            "SELECT octet_length(body) FROM m1_file_transfer_intents WHERE file_id = $1",
            file_id,
        ) > 0

    monkeypatch.setattr(m1_file_transfer_reaper._runner, "_idle_secs", 0.01)
    m1_file_transfer_reaper.start()
    try:
        async with asyncio.timeout(2):
            while True:
                async with pool.acquire() as conn:
                    remaining = await conn.fetchval(
                        "SELECT count(*) FROM m1_file_transfer_intents WHERE file_id = $1",
                        file_id,
                    )
                if remaining == 0:
                    break
                await asyncio.sleep(0.01)
    finally:
        assert await m1_file_transfer_reaper.stop(timeout=1)


@pytest.mark.asyncio
async def test_native_text_requires_concrete_atomic_result_and_verified_open_delete(context):
    pool, vault_id, _denied, vault_name = context
    service = m1.MeasurementFileService()
    data = "native text file\n".encode()
    digest = hashlib.sha256(data).hexdigest()
    bodies: dict[uuid.UUID, bytes] = {}
    deleted: list[uuid.UUID] = []

    async def publish(_conn, request):
        assert request.logical_path == "notes/text.txt"
        assert request.actor_id == "tester"
        bodies[request.file_id] = request.data
        return m1.NativeTextPublication(
            request.file_id, request.digest[:40], request.digest, len(request.data),
        )

    async def open_text(_vault_id, resource_id, _revision_id):
        data = bodies[resource_id]
        return m1.NativeTextOpenResult(data, hashlib.sha256(data).hexdigest(), len(data))

    async def delete_text(_conn, request):
        assert request.actor_id == "tester"
        assert request.logical_path == "notes/text.txt"
        deleted.append(request.resource_id)

    m1.register_native_text_file_services(publisher=publish, opener=open_text, deleter=delete_text)
    initiated = await service.initiate_upload(
        vault_name=vault_name, vault_id=vault_id, collection="notes", filename="text.txt",
        actor_id="tester", mime_type="text/plain", description="", content_hash=digest,
    )
    await service.transfer(_token(initiated["upload_url"]), method="PUT", body=data)
    confirmed = await service.confirm_upload(vault_id, initiated["file_id"], content_hash=digest)
    assert confirmed["native_resource_id"] == initiated["file_id"]
    download = await service.get_download_url(vault_id, initiated["file_id"])
    assert await service.transfer(_token(download["download_url"]), method="GET") == data
    async def corrupt_open(*_args):
        return m1.NativeTextOpenResult(data + b"corrupt", digest, len(data))

    m1.register_native_text_file_services(
        publisher=publish, opener=corrupt_open, deleter=delete_text,
    )
    corrupt_download = await service.get_download_url(vault_id, initiated["file_id"])
    with pytest.raises(AKBError, match="digest/size verification") as corrupt:
        await service.transfer(_token(corrupt_download["download_url"]), method="GET")
    assert corrupt.value.status_code == 502
    m1.register_native_text_file_services(
        publisher=publish, opener=open_text, deleter=delete_text,
    )
    await service.delete(vault_id, initiated["file_id"], actor_id="tester")
    assert deleted == [uuid.UUID(initiated["file_id"])]
    async with pool.acquire() as conn:
        assert await conn.fetchval("SELECT count(*) FROM vault_files WHERE id = $1", uuid.UUID(initiated["file_id"])) == 0
        assert await conn.fetchval(
            "SELECT count(*) FROM edges WHERE source_uri = $1 OR target_uri = $1", initiated["uri"],
        ) == 0
        assert await conn.fetchval(
            "SELECT count(*) FROM publications WHERE resource_uri = $1", initiated["uri"],
        ) == 0


@pytest.mark.asyncio
async def test_native_text_noop_publisher_cannot_confirm(context):
    pool, vault_id, _denied, vault_name = context
    service = m1.MeasurementFileService()
    data = b"no-op publisher must fail\n"
    digest = hashlib.sha256(data).hexdigest()

    async def noop_publish(*_args):
        return None

    async def noop_open(*_args):
        return m1.NativeTextOpenResult(data, digest, len(data))

    async def noop_delete(*_args):
        return None

    m1.register_native_text_file_services(
        publisher=noop_publish,  # type: ignore[arg-type]
        opener=noop_open,
        deleter=noop_delete,
    )
    initiated = await service.initiate_upload(
        vault_name=vault_name, vault_id=vault_id, collection="", filename="noop.txt",
        actor_id="tester", mime_type="text/plain", description="", content_hash=digest,
    )
    await service.transfer(_token(initiated["upload_url"]), method="PUT", body=data)
    with pytest.raises(AKBError, match="concrete publication") as error:
        await service.confirm_upload(vault_id, initiated["file_id"], content_hash=digest)
    assert error.value.status_code == 502
    async with pool.acquire() as conn:
        assert await conn.fetchval(
            "SELECT count(*) FROM vault_files WHERE id = $1", uuid.UUID(initiated["file_id"]),
        ) == 0
        assert await conn.fetchval(
            "SELECT count(*) FROM m1_file_transfer_intents WHERE file_id = $1",
            uuid.UUID(initiated["file_id"]),
        ) == 1


@pytest.mark.asyncio
async def test_concurrent_confirm_adopts_one_exact_file(context):
    pool, vault_id, _denied, vault_name = context
    service = m1.MeasurementFileService()
    data = b"concurrent\x00"
    digest = hashlib.sha256(data).hexdigest()
    initiated = await service.initiate_upload(
        vault_name=vault_name, vault_id=vault_id, collection="", filename="race.bin",
        actor_id="tester", mime_type="application/octet-stream", description="", content_hash=digest,
    )
    await service.transfer(_token(initiated["upload_url"]), method="PUT", body=data)
    results = await asyncio.gather(
        service.confirm_upload(vault_id, initiated["file_id"], content_hash=digest),
        m1.MeasurementFileService().confirm_upload(vault_id, initiated["file_id"], content_hash=digest),
    )
    assert results[0]["file_id"] == results[1]["file_id"]
    async with pool.acquire() as conn:
        assert await conn.fetchval("SELECT count(*) FROM vault_files WHERE id = $1", uuid.UUID(initiated["file_id"])) == 1


@pytest.mark.asyncio
async def test_late_dedup_resolves_exact_peer_after_its_intent_is_consumed(context, monkeypatch):
    """A late confirm must adopt the peer even when its pre-fetched intent vanished."""
    pool, vault_id, _denied, vault_name = context
    service = m1.MeasurementFileService()
    data = b"proxy style late dedup\x00"
    digest = hashlib.sha256(data).hexdigest()

    first = await service.initiate_upload(
        vault_name=vault_name, vault_id=vault_id, collection="uploads", filename="race.bin",
        actor_id="proxy-a", mime_type="application/octet-stream", description="", content_hash=None,
    )
    second = await service.initiate_upload(
        vault_name=vault_name, vault_id=vault_id, collection="uploads", filename="race.bin",
        actor_id="proxy-b", mime_type="application/octet-stream", description="", content_hash=None,
    )
    assert first["file_id"] != second["file_id"]
    await service.transfer(_token(first["upload_url"]), method="PUT", body=data)
    await service.transfer(_token(second["upload_url"]), method="PUT", body=data)
    canonical = await service.confirm_upload(vault_id, first["file_id"], content_hash=digest)

    # Pause the first late confirm after it locks the B intent.  A second B
    # confirm has therefore already fetched that intent when the first one
    # adopts A and deletes B's intent.  Its locked read then returns None.
    original_find_exact = vault_files_repo.find_measurement_exact
    original_find_by_id = vault_files_repo.find_measurement_by_id
    exact_entered = asyncio.Event()
    second_initial_read = asyncio.Event()
    allow_adoption = asyncio.Event()
    calls = 0
    by_id_calls = 0

    async def pause_first_exact(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            exact_entered.set()
            await allow_adoption.wait()
        return await original_find_exact(*args, **kwargs)

    async def observe_second_initial_read(*args, **kwargs):
        nonlocal by_id_calls
        by_id_calls += 1
        if by_id_calls == 2:
            second_initial_read.set()
        return await original_find_by_id(*args, **kwargs)

    monkeypatch.setattr(vault_files_repo, "find_measurement_exact", pause_first_exact)
    monkeypatch.setattr(vault_files_repo, "find_measurement_by_id", observe_second_initial_read)
    late_one = asyncio.create_task(
        service.confirm_upload(vault_id, second["file_id"], content_hash=digest),
    )
    await exact_entered.wait()
    late_two = asyncio.create_task(
        m1.MeasurementFileService().confirm_upload(vault_id, second["file_id"], content_hash=digest),
    )
    await second_initial_read.wait()
    allow_adoption.set()
    results = await asyncio.gather(late_one, late_two)

    assert [result["file_id"] for result in results] == [canonical["file_id"]] * 2
    assert [result["uri"] for result in results] == [canonical["uri"]] * 2
    async with pool.acquire() as conn:
        assert await conn.fetchval(
            "SELECT count(*) FROM vault_files WHERE vault_id = $1 AND name = 'race.bin'", vault_id,
        ) == 1
        assert await conn.fetchval(
            "SELECT count(*) FROM m1_file_transfer_intents WHERE vault_id = $1", vault_id,
        ) == 0


@pytest.mark.asyncio
async def test_concrete_native_text_bridge_is_atomic_searchable_and_versioned(
    context, monkeypatch,
):
    from app.services import m1_native_text_file_bridge as bridge

    pool, vault_id, _denied, vault_name = context

    async def test_pool():
        return pool

    monkeypatch.setattr(bridge, "get_pool", test_pool)
    bridge.install_m1_native_text_file_bridge()
    service = m1.MeasurementFileService()
    data = b"bridge searchable token\nsecond line\n"
    digest = hashlib.sha256(data).hexdigest()
    initiated = await service.initiate_upload(
        vault_name=vault_name,
        vault_id=vault_id,
        collection="src",
        filename="main.py",
        actor_id="bridge-test",
        mime_type="text/x-python",
        description="atomic bridge",
        content_hash=digest,
    )
    await service.transfer(_token(initiated["upload_url"]), method="PUT", body=data)
    confirmed = await service.confirm_upload(
        vault_id, initiated["file_id"], content_hash=digest,
    )
    resource_id = uuid.UUID(initiated["file_id"])
    assert confirmed["storage_driver"] == "native_text"
    assert confirmed["native_resource_id"] == initiated["file_id"]
    assert confirmed["native_revision_id"]

    async with pool.acquire() as conn:
        owner_id = await conn.fetchval("SELECT owner_id FROM vaults WHERE id = $1", vault_id)
        row = await conn.fetchrow(
            """
            SELECT r.surface, r.current_path, r.lifecycle, r.head_revision_id,
                   m.digest, m.byte_size, m.selected_placement
              FROM native_resources r
              JOIN native_revisions v
                ON v.resource_id = r.resource_id AND v.revision_id = r.head_revision_id
              JOIN native_payload_manifests m
                ON m.payload_manifest_id = v.payload_manifest_id
             WHERE r.resource_id = $1
            """,
            resource_id,
        )
    assert dict(row) == {
        "surface": "file",
        "current_path": "src/main.py",
        "lifecycle": "live",
        "head_revision_id": confirmed["native_revision_id"],
        "digest": digest,
        "byte_size": len(data),
        "selected_placement": "pg-bodystore-v1",
    }

    download = await service.get_download_url(vault_id, initiated["file_id"])
    assert await service.transfer(_token(download["download_url"]), method="GET") == data
    assert await NativeDerivedWorker(pool).process_once() == 1
    grep = await M1NativeGrepService(pool).grep(
        "searchable token",
        user_id=owner_id,
        include_text_files=True,
    )
    assert grep["total_resources"] == 1
    assert grep["results"][0]["uri"] == confirmed["uri"]
    assert grep["results"][0]["resource_type"] == "file"
    assert grep["results"][0]["revision"] == confirmed["native_revision_id"]
    assert grep["results"][0]["content_hash"] == digest

    deleted = await service.delete(vault_id, initiated["file_id"], actor_id="bridge-test")
    assert deleted["deleted"] is True
    async with pool.acquire() as conn:
        deleted_head = await conn.fetchrow(
            "SELECT lifecycle, head_revision_id FROM native_resources WHERE resource_id = $1",
            resource_id,
        )
        history = await conn.fetch(
            "SELECT action, parent_revision_id FROM native_revisions WHERE resource_id = $1",
            resource_id,
        )
        assert await conn.fetchval(
            "SELECT count(*) FROM vault_files WHERE id = $1", resource_id,
        ) == 0
    assert deleted_head["lifecycle"] == "deleted"
    assert deleted_head["head_revision_id"] != confirmed["native_revision_id"]
    assert {item["action"] for item in history} == {"create", "delete"}
    delete_row = next(item for item in history if item["action"] == "delete")
    assert delete_row["parent_revision_id"] == confirmed["native_revision_id"]

    failed_data = b"rolled back native body\n"
    failed_digest = hashlib.sha256(failed_data).hexdigest()
    failed = await service.initiate_upload(
        vault_name=vault_name,
        vault_id=vault_id,
        collection="src",
        filename="rollback.py",
        actor_id="bridge-test",
        mime_type="text/x-python",
        description="rollback",
        content_hash=failed_digest,
    )
    await service.transfer(_token(failed["upload_url"]), method="PUT", body=failed_data)

    async def fail_file_row(*_args, **_kwargs):
        raise RuntimeError("file row publication failed")

    monkeypatch.setattr(
        vault_files_repo,
        "insert_or_adopt_measurement_confirmed",
        fail_file_row,
    )
    with pytest.raises(RuntimeError, match="file row publication failed"):
        await service.confirm_upload(
            vault_id, failed["file_id"], content_hash=failed_digest,
        )
    async with pool.acquire() as conn:
        assert await conn.fetchval(
            "SELECT count(*) FROM native_resources WHERE resource_id = $1",
            uuid.UUID(failed["file_id"]),
        ) == 0
        assert await conn.fetchval(
            "SELECT count(*) FROM m1_reference_payloads WHERE digest = $1",
            failed_digest,
        ) == 0
        assert await conn.fetchval(
            "SELECT count(*) FROM vault_files WHERE id = $1",
            uuid.UUID(failed["file_id"]),
        ) == 0


@pytest.mark.asyncio
async def test_reference_document_and_native_text_file_deduplicate_per_placement(
    context, monkeypatch,
):
    """The same text can have one verified body per selected placement."""
    from app.services import m1_native_text_file_bridge as bridge

    pool, vault_id, _denied, vault_name = context

    async def test_pool():
        return pool

    monkeypatch.setattr(bridge, "get_pool", test_pool)
    bridge.install_m1_native_text_file_bridge()

    data = b"same canonical text across placements\n"
    digest = hashlib.sha256(data).hexdigest()
    reference_store = M1ReferencePayloadStore(pool)
    document_service = NativeRevisionService(pool, payload_store=reference_store)
    document = await document_service.create_text(
        namespace_id=vault_id,
        surface="document",
        path="notes/shared.md",
        payload=data,
        actor="placement-test",
        mutation_id=uuid.uuid4(),
        expected_digest=digest,
        expected_size=len(data),
    )
    reference = await reference_store.prepare_text(
        namespace_id=vault_id,
        payload=data,
    )

    file_service = m1.MeasurementFileService()
    initiated = await file_service.initiate_upload(
        vault_name=vault_name,
        vault_id=vault_id,
        collection="notes",
        filename="shared.txt",
        actor_id="placement-test",
        mime_type="text/plain",
        description="same canonical body",
        content_hash=digest,
    )
    await file_service.transfer(_token(initiated["upload_url"]), method="PUT", body=data)
    confirmed = await file_service.confirm_upload(
        vault_id, initiated["file_id"], content_hash=digest,
    )
    pg_store = M1PgBodyStore(pool)
    pg_body = await pg_store.prepare_text(namespace_id=vault_id, payload=data)

    assert reference.payload_id != pg_body.payload_id
    assert reference.payload_id == (
        await reference_store.prepare_text(namespace_id=vault_id, payload=data)
    ).payload_id
    assert pg_body.payload_id == (
        await pg_store.prepare_text(namespace_id=vault_id, payload=data)
    ).payload_id

    document_snapshot = await document_service.get_resource_revision(
        namespace_id=vault_id,
        surface="document",
        resource_id=document.resource_id,
        revision_id=document.revision_id,
    )
    file_resource_id = uuid.UUID(confirmed["native_resource_id"])
    file_snapshot = await NativeRevisionService(
        pool, payload_store=pg_store,
    ).get_resource_revision(
        namespace_id=vault_id,
        surface="file",
        resource_id=file_resource_id,
        revision_id=confirmed["native_revision_id"],
    )
    assert (document_snapshot.payload_bytes, document_snapshot.selected_placement) == (
        data,
        M1ReferencePayloadStore.selected_placement,
    )
    assert (file_snapshot.payload_bytes, file_snapshot.selected_placement) == (
        data,
        M1PgBodyStore.selected_placement,
    )

    async with pool.acquire() as conn:
        payload_rows = await conn.fetch(
            """
            SELECT selected_placement, count(*)::int AS count
              FROM m1_reference_payloads
             WHERE namespace_id = $1 AND digest = $2 AND byte_size = $3
             GROUP BY selected_placement
            """,
            vault_id,
            digest,
            len(data),
        )
        manifest_rows = await conn.fetch(
            """
            SELECT r.surface, m.selected_placement, m.private_locator
              FROM native_resources r
              JOIN native_revisions v
                ON v.resource_id = r.resource_id AND v.revision_id = r.head_revision_id
              JOIN native_payload_manifests m
                ON m.payload_manifest_id = v.payload_manifest_id
             WHERE r.resource_id = ANY($1::uuid[])
            """,
            [document.resource_id, file_resource_id],
        )
    assert {
        row["selected_placement"]: row["count"] for row in payload_rows
    } == {
        M1ReferencePayloadStore.selected_placement: 1,
        M1PgBodyStore.selected_placement: 1,
    }
    assert {
        row["surface"]: (row["selected_placement"], row["private_locator"])
        for row in manifest_rows
    } == {
        "document": (M1ReferencePayloadStore.selected_placement, reference.payload_id),
        "file": (M1PgBodyStore.selected_placement, pg_body.payload_id),
    }


async def _confirmed_native_text_file(
    *, vault_id: uuid.UUID, vault_name: str, collection: str, filename: str, body: bytes,
) -> dict:
    service = m1.MeasurementFileService()
    digest = hashlib.sha256(body).hexdigest()
    initiated = await service.initiate_upload(
        vault_name=vault_name,
        vault_id=vault_id,
        collection=collection,
        filename=filename,
        actor_id="collection-delete-test",
        mime_type="text/plain",
        description="recursive collection delete",
        content_hash=digest,
    )
    await service.transfer(_token(initiated["upload_url"]), method="PUT", body=body)
    return await service.confirm_upload(
        vault_id, initiated["file_id"], content_hash=digest,
    )


async def _confirmed_binary_file(
    *, vault_id: uuid.UUID, vault_name: str, collection: str, filename: str, body: bytes,
) -> dict:
    service = m1.MeasurementFileService()
    digest = hashlib.sha256(body).hexdigest()
    initiated = await service.initiate_upload(
        vault_name=vault_name,
        vault_id=vault_id,
        collection=collection,
        filename=filename,
        actor_id="collection-delete-test",
        mime_type="application/octet-stream",
        description="recursive collection delete",
        content_hash=digest,
    )
    await service.transfer(_token(initiated["upload_url"]), method="PUT", body=body)
    return await service.confirm_upload(
        vault_id, initiated["file_id"], content_hash=digest,
    )


@pytest.mark.asyncio
async def test_recursive_collection_delete_retains_binary_cas_without_legacy_s3_outbox(
    context, monkeypatch,
):
    from app.services import collection_service as collections

    pool, vault_id, _denied, vault_name = context

    async def test_pool():
        return pool

    monkeypatch.setattr(collections, "get_pool", test_pool)
    confirmed = await _confirmed_binary_file(
        vault_id=vault_id,
        vault_name=vault_name,
        collection="binary/doomed",
        filename="proof.bin",
        body=b"retained binary CAS\x00",
    )
    file_id = uuid.UUID(confirmed["file_id"])
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT s3_key, storage_driver, storage_locator FROM vault_files WHERE id = $1",
            file_id,
        )
    assert row["storage_driver"] == "fscas"

    deleted = await collections.CollectionService().delete(
        vault=vault_name,
        path="binary",
        recursive=True,
        agent_id="collection-delete-test",
    )
    assert deleted["deleted_files"] == 1

    async with pool.acquire() as conn:
        assert await conn.fetchval(
            "SELECT count(*) FROM vault_files WHERE id = $1", file_id,
        ) == 0
        assert await conn.fetchval(
            "SELECT count(*) FROM s3_delete_outbox WHERE s3_key = $1", row["s3_key"],
        ) == 0

    # Logical deletion does not erase immutable shared CAS content.
    retained = Path(settings.native_revision_m1_file_fscas_root) / row["storage_locator"]
    assert retained.is_file()


@pytest.mark.asyncio
async def test_recursive_collection_delete_tombstones_native_text_lineage_and_exact_grep(
    context, monkeypatch,
):
    from app.services import collection_service as collections
    from app.services import m1_native_text_file_bridge as bridge

    pool, vault_id, _denied, vault_name = context

    async def test_pool():
        return pool

    monkeypatch.setattr(collections, "get_pool", test_pool)
    bridge.install_m1_native_text_file_bridge()
    confirmed = await _confirmed_native_text_file(
        vault_id=vault_id,
        vault_name=vault_name,
        collection="doomed/nested",
        filename="proof.txt",
        body=b"collection tombstone exact grep token\n",
    )
    resource_id = uuid.UUID(confirmed["file_id"])
    create_revision_id = confirmed["native_revision_id"]
    async with pool.acquire() as conn:
        owner_id = await conn.fetchval("SELECT owner_id FROM vaults WHERE id = $1", vault_id)
        related_publication = f"deleted-file-{uuid.uuid4().hex}"
        unrelated_publication = f"unrelated-file-{uuid.uuid4().hex}"
        await conn.executemany(
            """
            INSERT INTO publications (slug, vault_id, resource_type, resource_uri)
            VALUES ($1, $2, 'file', $3)
            """,
            [
                (related_publication, vault_id, confirmed["uri"]),
                (
                    unrelated_publication,
                    vault_id,
                    f"akb://{vault_name}/coll/kept/file/{uuid.uuid4()}",
                ),
            ],
        )

    before = await M1NativeGrepService(pool).grep(
        "collection tombstone exact grep token",
        user_id=owner_id,
        resource_id=resource_id,
        include_text_files=True,
    )
    assert before["total_resources"] == 1

    deleted = await collections.CollectionService().delete(
        vault=vault_name,
        path="doomed",
        recursive=True,
        agent_id="collection-delete-test",
    )
    assert deleted["deleted_files"] == 1

    async with pool.acquire() as conn:
        head = await conn.fetchrow(
            "SELECT lifecycle, head_revision_id FROM native_resources WHERE resource_id = $1",
            resource_id,
        )
        history = await conn.fetch(
            """
            SELECT revision_id, parent_revision_id, action
              FROM native_revisions
             WHERE resource_id = $1
             ORDER BY occurred_at, revision_id
            """,
            resource_id,
        )
        assert await conn.fetchval(
            "SELECT count(*) FROM vault_files WHERE id = $1", resource_id,
        ) == 0
        assert await conn.fetchval(
            "SELECT count(*) FROM publications WHERE slug = $1", related_publication,
        ) == 0
        assert await conn.fetchval(
            "SELECT count(*) FROM publications WHERE slug = $1", unrelated_publication,
        ) == 1
    assert head["lifecycle"] == "deleted"
    assert head["head_revision_id"] != create_revision_id
    assert [row["action"] for row in history] == ["create", "delete"]
    assert history[-1]["parent_revision_id"] == create_revision_id

    after = await M1NativeGrepService(pool).grep(
        "collection tombstone exact grep token",
        user_id=owner_id,
        resource_id=resource_id,
        include_text_files=True,
    )
    assert after["searched_resources"] == 0
    assert after["total_resources"] == 0
    assert after["results"] == []


@pytest.mark.asyncio
async def test_recursive_collection_delete_rolls_back_native_tombstone_on_later_failure(
    context, monkeypatch,
):
    from app.services import collection_service as collections
    from app.services import m1_native_text_file_bridge as bridge

    pool, vault_id, _denied, vault_name = context

    async def test_pool():
        return pool

    monkeypatch.setattr(collections, "get_pool", test_pool)
    bridge.install_m1_native_text_file_bridge()
    confirmed = await _confirmed_native_text_file(
        vault_id=vault_id,
        vault_name=vault_name,
        collection="rollback/nested",
        filename="proof.txt",
        body=b"collection rollback exact grep token\n",
    )
    resource_id = uuid.UUID(confirmed["file_id"])
    create_revision_id = confirmed["native_revision_id"]
    publication_slug = f"rollback-file-{uuid.uuid4().hex}"
    async with pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO publications (slug, vault_id, resource_type, resource_uri)
            VALUES ($1, $2, 'file', $3)
            """,
            publication_slug, vault_id, confirmed["uri"],
        )

    async def fail_event(*_args, **_kwargs):
        raise RuntimeError("injected downstream collection failure")

    monkeypatch.setattr(collections, "emit_event", fail_event)
    with pytest.raises(RuntimeError, match="injected downstream collection failure"):
        await collections.CollectionService().delete(
            vault=vault_name,
            path="rollback",
            recursive=True,
            agent_id="collection-delete-test",
        )

    async with pool.acquire() as conn:
        head = await conn.fetchrow(
            "SELECT lifecycle, head_revision_id FROM native_resources WHERE resource_id = $1",
            resource_id,
        )
        assert await conn.fetchval(
            "SELECT count(*) FROM native_revisions WHERE resource_id = $1", resource_id,
        ) == 1
        assert await conn.fetchval(
            "SELECT count(*) FROM vault_files WHERE id = $1", resource_id,
        ) == 1
        assert await conn.fetchval(
            "SELECT count(*) FROM collections WHERE vault_id = $1 AND path = 'rollback/nested'",
            vault_id,
        ) == 1
        assert await conn.fetchval(
            "SELECT count(*) FROM publications WHERE slug = $1", publication_slug,
        ) == 1
    assert dict(head) == {
        "lifecycle": "live",
        "head_revision_id": create_revision_id,
    }


_NATIVE_AUTHORITY_TABLES = (
    "native_resources",
    "native_payload_manifests",
    "native_revisions",
    "native_revision_activity",
    "native_invalidation_intents",
)


async def _native_authority_counts(pool, resource_id: uuid.UUID) -> dict[str, int]:
    async with pool.acquire() as conn:
        return {
            table: await conn.fetchval(
                f"SELECT count(*) FROM {table} WHERE resource_id = $1", resource_id,
            )
            for table in _NATIVE_AUTHORITY_TABLES
        }


@pytest.mark.parametrize(
    "boundary",
    [
        # Prepared *inside* the caller's File transaction, unlike the
        # standalone-pool arm where the verified body survives a rollback.
        "payload.after_prepare_before_tx",
        "authority.after_resource",
        "authority.after_manifest",
        "authority.after_revision",
        "authority.after_head",
        "authority.after_path",
        "authority.after_alias",
        "authority.after_activity",
        "authority.after_invalidation",
        "authority.before_commit",
    ],
)
@pytest.mark.asyncio
async def test_composite_file_confirm_failpoint_leaves_no_partial_authority_and_retries_once(
    context, monkeypatch, boundary,
):
    """C5 at the riskiest boundary AKB has: a composite File confirm.

    ``MeasurementFileService.confirm_upload`` owns the outer ``vault_files``
    transaction and the bridged ledger publish runs inside it as a savepoint.
    A failure injected into the nested publish must therefore unwind *both*:
    no public File row, no rows in any of the five native authority tables,
    no path alias and no verified body.  The transfer intent is the one thing
    that must survive, because it is what makes the retry possible — and the
    clean retry must land exactly one activity event and one durable
    invalidation intent, not two.
    """
    from app.services import m1_native_text_file_bridge as bridge

    pool, vault_id, _denied, vault_name = context
    assert boundary in FAILPOINT_BOUNDARIES

    async def test_pool():
        return pool

    monkeypatch.setattr(bridge, "get_pool", test_pool)

    fired: list[str] = []

    async def failpoint(name: str) -> None:
        fired.append(name)
        if name == boundary:
            raise RuntimeError(f"injected:{name}")

    bridge.install_m1_native_text_file_bridge(failpoint=failpoint)

    service = m1.MeasurementFileService()
    data = b"composite confirm body\nsecond line\n"
    digest = hashlib.sha256(data).hexdigest()
    initiated = await service.initiate_upload(
        vault_name=vault_name,
        vault_id=vault_id,
        collection="src",
        filename="composite.py",
        actor_id="c5-test",
        mime_type="text/x-python",
        description="composite failpoint",
        content_hash=digest,
    )
    file_id = uuid.UUID(initiated["file_id"])
    await service.transfer(_token(initiated["upload_url"]), method="PUT", body=data)

    with pytest.raises(RuntimeError, match=f"injected:{boundary}"):
        await service.confirm_upload(vault_id, initiated["file_id"], content_hash=digest)

    assert fired[-1] == boundary
    assert await _native_authority_counts(pool, file_id) == dict.fromkeys(
        _NATIVE_AUTHORITY_TABLES, 0,
    )
    async with pool.acquire() as conn:
        assert await conn.fetchval(
            "SELECT count(*) FROM vault_files WHERE id = $1", file_id,
        ) == 0
        assert await conn.fetchval(
            "SELECT count(*) FROM native_resource_path_aliases WHERE namespace_id = $1",
            vault_id,
        ) == 0
        assert await conn.fetchval(
            "SELECT count(*) FROM m1_reference_payloads WHERE digest = $1", digest,
        ) == 0
        # The only durable residue is the transfer intent the retry needs.
        assert await conn.fetchval(
            "SELECT count(*) FROM m1_file_transfer_intents WHERE file_id = $1", file_id,
        ) == 1
    assert await service.list_files(vault_id, vault_name, None, 50) == []
    with pytest.raises(NotFoundError):
        await service.get_download_url(vault_id, initiated["file_id"])

    # Reinstall production-shaped (no failpoint) and retry the same confirm.
    bridge.install_m1_native_text_file_bridge()
    confirmed = await service.confirm_upload(
        vault_id, initiated["file_id"], content_hash=digest,
    )
    assert confirmed["file_id"] == initiated["file_id"]
    assert confirmed["uri"] == initiated["uri"]
    assert confirmed["storage_driver"] == "native_text"
    assert confirmed["native_resource_id"] == initiated["file_id"]
    assert confirmed["content_hash"] == digest

    assert await _native_authority_counts(pool, file_id) == dict.fromkeys(
        _NATIVE_AUTHORITY_TABLES, 1,
    )
    async with pool.acquire() as conn:
        activity = await conn.fetchrow(
            "SELECT action, revision_id, actor FROM native_revision_activity WHERE resource_id = $1",
            file_id,
        )
        intent = await conn.fetchrow(
            """
            SELECT reason, revision_id, claimed_at, completed_at, last_error
              FROM native_invalidation_intents
             WHERE resource_id = $1
            """,
            file_id,
        )
        assert await conn.fetchval(
            "SELECT count(*) FROM vault_files WHERE id = $1", file_id,
        ) == 1
        assert await conn.fetchval(
            "SELECT count(*) FROM m1_file_transfer_intents WHERE file_id = $1", file_id,
        ) == 0
    assert dict(activity) == {
        "action": "create",
        "revision_id": confirmed["native_revision_id"],
        "actor": "c5-test",
    }
    # Durable means still owed: the retry left a pending intent for the
    # invalidation worker, not a completed or already-claimed one.
    assert dict(intent) == {
        "reason": "create",
        "revision_id": confirmed["native_revision_id"],
        "claimed_at": None,
        "completed_at": None,
        "last_error": None,
    }

    download = await service.get_download_url(vault_id, initiated["file_id"])
    assert await service.transfer(_token(download["download_url"]), method="GET") == data
    assert [item["file_id"] for item in await service.list_files(vault_id, vault_name, None, 50)] == [
        initiated["file_id"],
    ]


@pytest.mark.asyncio
async def test_composite_confirm_discards_a_ledger_publish_that_already_reported_commit(
    context, monkeypatch,
):
    """In a composite transaction the ledger's own "commit" is not one.

    ``authority.after_commit_before_response`` fires once the nested publish
    has released its savepoint.  On the standalone pool that boundary models
    a lost response over a *durable* Revision, and the retry replays it
    idempotently.  Bridged into a File confirm there is no such Revision: the
    outer ``vault_files`` transaction is still the only commit boundary, so
    the whole publication is discarded.  What this test asserts is exactly
    that non-durability — zero rows after the failure, one row after the
    retry.  It does *not* assert that the retry took the fresh-lineage path
    rather than the replay path: the aborted revision id never reached the
    database, so the exactly-one counts cannot tell the two apart, and
    fresh-lineage is an inference from the rollback rather than an observed
    fact.  Pinning the non-durability keeps the boundary's name from being
    read as a durability claim on this path.
    """
    from app.services import m1_native_text_file_bridge as bridge

    pool, vault_id, _denied, vault_name = context

    async def test_pool():
        return pool

    monkeypatch.setattr(bridge, "get_pool", test_pool)

    fired = False

    async def failpoint(name: str) -> None:
        nonlocal fired
        if name == "authority.after_commit_before_response" and not fired:
            fired = True
            raise RuntimeError("injected:response-lost")

    bridge.install_m1_native_text_file_bridge(failpoint=failpoint)

    service = m1.MeasurementFileService()
    data = b"savepoint release is not a commit\n"
    digest = hashlib.sha256(data).hexdigest()
    initiated = await service.initiate_upload(
        vault_name=vault_name,
        vault_id=vault_id,
        collection="src",
        filename="after_commit.py",
        actor_id="c5-test",
        mime_type="text/x-python",
        description="composite after-commit",
        content_hash=digest,
    )
    file_id = uuid.UUID(initiated["file_id"])
    await service.transfer(_token(initiated["upload_url"]), method="PUT", body=data)

    with pytest.raises(RuntimeError, match="injected:response-lost"):
        await service.confirm_upload(vault_id, initiated["file_id"], content_hash=digest)

    assert fired is True
    assert await _native_authority_counts(pool, file_id) == dict.fromkeys(
        _NATIVE_AUTHORITY_TABLES, 0,
    )
    async with pool.acquire() as conn:
        assert await conn.fetchval(
            "SELECT count(*) FROM vault_files WHERE id = $1", file_id,
        ) == 0

    confirmed = await service.confirm_upload(
        vault_id, initiated["file_id"], content_hash=digest,
    )
    assert confirmed["storage_driver"] == "native_text"
    assert await _native_authority_counts(pool, file_id) == dict.fromkeys(
        _NATIVE_AUTHORITY_TABLES, 1,
    )
