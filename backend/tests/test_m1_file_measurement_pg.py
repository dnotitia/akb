"""Real-PostgreSQL contract for the guarded M1 public File protocol."""

from __future__ import annotations

import asyncio
import hashlib
import importlib.util
import os
import uuid
from pathlib import Path

import asyncpg
import pytest
import pytest_asyncio

from app.config import settings
from app.exceptions import AKBError, NotFoundError
from app.repositories import vault_files_repo
from app.services import m1_file_measurement as m1
from app.services.m1_native_grep_service import M1NativeGrepService
from app.services.native_derived_worker import NativeDerivedWorker


_DSN = os.environ.get(
    "AKB_M1_FILE_TEST_DSN",
    "postgresql://akb:akb-r5-local@127.0.0.1:55433/akb_revision_m1_measurement",  # pragma: allowlist secret
)
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
    pool = await asyncpg.create_pool(_DSN, min_size=1, max_size=8)
    async with pool.acquire() as conn:
        await conn.execute((_BACKEND / "app" / "db" / "init.sql").read_text())
        for filename in (
            "048_native_revision_core.py",
            "049_native_revision_m1_pg_body.py",
            "050_native_revision_searchable_derived.py",
            "051_native_revision_m1_file_storage.py",
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
        return bodies[resource_id]

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
        return data + b"corrupt"

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
        return data

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
