"""Real PostgreSQL proofs for account-asset revision CAS and cleanup fencing.

Uses an isolated database, never the running application's catalog. The
optional AKB_TEST_AVATAR_S3_ENDPOINT enables an actual isolated object store.
Without it the exact storage adapter boundary is an in-memory fixture.
"""

from __future__ import annotations

import asyncio
import io
import os
import threading
import uuid
import time
from dataclasses import replace
from unittest.mock import AsyncMock

import asyncpg
import boto3
import pytest
from PIL import Image

from app.db.postgres import _load_migration
from app.exceptions import AKBError
from app.repositories import user_avatars_repo as repo, vault_files_repo
from app.services import access_service, account_self_service, auth_service, s3_delete_worker as worker
from app.services import user_avatar_service as service
from tests.test_account_self_lifecycle_postgres import PASSWORD, person as account_person
from tests.test_recovery_admin_provisioning_postgres import _fresh_database

pytestmark = pytest.mark.asyncio


def photo(color="red"):
    data = io.BytesIO()
    Image.new("RGB", (80, 60), color).save(data, format="PNG")
    return data.getvalue()


async def person(pool):
    return replace(await account_person(pool), local_session_generation=0,
                   local_session_expires_at=time.time() + 3600)


@pytest.fixture
async def runtime(monkeypatch):
    async with _fresh_database() as pool:
        for filename in ("019_s3_delete_outbox.py", "079_worker_claim_lifecycle.py", "114_user_avatars.py"):
            migration = _load_migration(filename)
            assert migration is not None
            async with pool.acquire() as conn:
                await migration.migrate(conn=conn)

        async def get_pool():
            return pool

        for module in (service, worker, access_service, account_self_service, auth_service):
            monkeypatch.setattr(module, "get_pool", get_pool)
        monkeypatch.setattr(service.settings, "s3_endpoint_url", "http://avatar-test-storage.invalid")
        monkeypatch.setattr(service.settings, "auth_mode", "local")
        monkeypatch.setattr(access_service, "get_role_sync", lambda: type("Roles", (), {"on_user_delete": AsyncMock()})())
        monkeypatch.setattr(worker, "BATCH_SIZE", 64)
        objects = {}

        def put(key, data, _mime):
            objects[key] = data

        def get(key, _limit):
            return objects[key]

        def delete(key):
            objects.pop(key, None)

        monkeypatch.setattr(service.s3_adapter, "put_private_bytes", put)
        monkeypatch.setattr(service.s3_adapter, "get_bounded_bytes", get)
        monkeypatch.setattr(service.s3_adapter, "delete", delete)
        yield pool, objects


async def save(user, expected=None, color="red"):
    await service.preflight_write(user, expected)
    return await service.save_photo(user, expected, photo(color), "image/png")


async def drain(pool):
    await pool.execute("UPDATE s3_delete_outbox SET next_attempt_at=clock_timestamp() WHERE processed_at IS NULL")
    return await worker._process_deletes_once()


async def test_create_read_replace_remove_tombstone_and_worker(runtime):
    pool, objects = runtime
    user = await person(pool)
    first = await save(user)
    assert first.width == 512 and first.url.endswith(str(first.revision))
    first_body = await service.read_photo(user, first.revision)
    assert Image.open(io.BytesIO(first_body)).size == (512, 512)
    assert len(objects) == 1
    second = await save(user, first.revision, "blue")
    assert first.revision != second.revision and len(objects) == 2
    with pytest.raises(AKBError) as old:
        await service.read_photo(user, first.revision)
    assert old.value.status_code == 404
    await drain(pool)
    assert len(objects) == 1
    await service.preflight_write(user, second.revision)
    removed = await service.remove_photo(user, second.revision)
    assert removed.url is None and removed.revision not in {first.revision, second.revision}
    assert await service.remove_photo(user, removed.revision) == removed
    await drain(pool)
    assert not objects
    assert await pool.fetchval("SELECT count(*) FROM user_avatars") == 1
    assert await pool.fetchval("SELECT count(*) FROM vault_files") == 0
    assert await pool.fetchval("SELECT count(*) FROM chunks") == 0


async def test_other_user_and_wrong_or_missing_revision_never_read_bytes(runtime):
    pool, _ = runtime
    owner, other = await person(pool), await person(pool)
    state = await save(owner)
    for user, revision in ((other, state.revision), (owner, uuid.uuid4())):
        with pytest.raises(AKBError) as caught:
            await service.read_photo(user, revision)
        assert caught.value.code == "avatar_not_found"


async def test_concurrent_first_writes_have_one_winner_and_cleanup_loser(runtime):
    pool, objects = runtime
    user = await person(pool)
    results = await asyncio.gather(
        service.save_photo(user, None, photo(), "image/png"),
        service.save_photo(user, None, photo("blue"), "image/png"), return_exceptions=True)
    winners = [value for value in results if not isinstance(value, BaseException)]
    failures = [value for value in results if isinstance(value, AKBError)]
    assert len(winners) == len(failures) == 1
    assert failures[0].code == "avatar_changed"
    await drain(pool)
    assert len(objects) == 1
    assert (await service.read_photo(user, winners[0].revision)) in objects.values()


async def test_live_photo_is_protected_from_stale_outbox(runtime):
    pool, objects = runtime
    user = await person(pool)
    state = await save(user)
    key = next(iter(objects))
    async with pool.acquire() as conn:
        await worker.enqueue_delete(conn, key)
    await drain(pool)
    assert key in objects
    assert await service.read_photo(user, state.revision)


async def test_failed_transfer_keeps_current_photo_and_durable_candidate(runtime, monkeypatch):
    pool, objects = runtime
    user = await person(pool)
    state = await save(user)

    def write_then_fail(key, data, _mime):
        objects[key] = data  # Remote acceptance, local acknowledgement lost.
        raise service.s3_adapter.StorageError("fixture transfer failure")

    monkeypatch.setattr(service.s3_adapter, "put_private_bytes", write_then_fail)
    with pytest.raises(AKBError) as caught:
        await save(user, state.revision)
    assert caught.value.code == "avatar_storage_unavailable"
    assert (await service.profile_metadata(user))["avatar"]["revision"] == state.revision
    assert len(objects) == 2
    await drain(pool)
    assert len(objects) == 1
    assert await service.read_photo(user, state.revision)


async def test_database_rollback_keeps_candidate_cleanup_and_current_photo(runtime, monkeypatch):
    pool, objects = runtime
    user = await person(pool)
    state = await save(user)
    original = repo.publish

    async def publish_then_fail(*args, **kwargs):
        await original(*args, **kwargs)
        raise RuntimeError("fixture transaction rollback")

    monkeypatch.setattr(repo, "publish", publish_then_fail)
    with pytest.raises(RuntimeError, match="rollback"):
        await save(user, state.revision)
    assert (await service.profile_metadata(user))["avatar"]["revision"] == state.revision
    await drain(pool)
    assert len(objects) == 1
    assert await service.read_photo(user, state.revision)


@pytest.mark.parametrize("action", ["suspend", "delete", "remove", "revoke", "password"])
async def test_put_revalidates_user_and_revision_after_remote_transfer(runtime, monkeypatch, action):
    pool, objects = runtime
    user = await person(pool)
    state = await save(user)
    started, finish = threading.Event(), threading.Event()

    def slow_put(key, data, _mime):
        started.set()
        assert finish.wait(5)
        objects[key] = data

    monkeypatch.setattr(service.s3_adapter, "put_private_bytes", slow_put)
    uploading = asyncio.create_task(service.save_photo(user, state.revision, photo("blue"), "image/png"))
    assert await asyncio.to_thread(started.wait, 2)
    if action == "suspend":
        await pool.execute("UPDATE users SET account_status='suspended' WHERE id=$1", uuid.UUID(user.user_id))
    elif action == "delete":
        await access_service.delete_user_account(user.user_id)
    elif action == "revoke":
        await auth_service.revoke_all_sessions(user.user_id)
    elif action == "password":
        await auth_service.change_password(user.user_id, PASSWORD, "new-photo-fixture-password-only")
    else:
        await service.remove_photo(user, state.revision)
    finish.set()
    with pytest.raises(AKBError) as caught:
        await uploading
    assert caught.value.code == ("avatar_changed" if action == "remove" else "account_session_expired")
    await drain(pool)
    assert len(objects) == (1 if action in {"suspend", "revoke", "password"} else 0)


async def test_admin_and_self_delete_both_enqueue_current_photo(runtime):
    pool, objects = runtime
    first, second = await person(pool), await person(pool)
    await save(first)
    await save(second)
    await pool.execute("INSERT INTO account_deletion_worker_state VALUES(true,clock_timestamp())")
    await account_self_service.delete_account(first, uuid.UUID(first.user_id), first.username, PASSWORD)
    await access_service.delete_user_account(second.user_id)
    assert await pool.fetchval("SELECT count(*) FROM user_avatars") == 0
    await drain(pool)
    assert not objects


async def test_write_budget_is_authoritative_and_does_not_roll_back_rejection(runtime):
    pool, _ = runtime
    user = await person(pool)
    for _ in range(10):
        await service.preflight_write(user, None)
    with pytest.raises(AKBError) as caught:
        await service.preflight_write(user, None)
    assert caught.value.code == "avatar_rate_limited"
    assert caught.value.retry_after_secs == 60
    assert await pool.fetchval("SELECT attempts FROM user_avatar_write_budget WHERE user_id=$1",
                               uuid.UUID(user.user_id)) == 11


async def test_migration_is_idempotent_and_rejects_partial_or_nonreserved_storage(runtime):
    pool, _ = runtime
    user = await person(pool)
    migration = _load_migration("114_user_avatars.py")
    async with pool.acquire() as conn:
        await migration.migrate(conn=conn)
        for key in ("vault/not-an-avatar", f"{repo.AVATAR_KEY_PREFIX}bad-metadata"):
            with pytest.raises(asyncpg.CheckViolationError):
                await conn.execute("INSERT INTO user_avatars(user_id,revision,object_key) VALUES($1,$2,$3)",
                                   uuid.UUID(user.user_id), uuid.uuid4(), key)


async def test_real_object_store_roundtrip_when_enabled(runtime, monkeypatch):
    endpoint = os.environ.get("AKB_TEST_AVATAR_S3_ENDPOINT")
    if not endpoint:
        pytest.skip("Set AKB_TEST_AVATAR_S3_ENDPOINT for isolated real S3 integration")
    pool, _ = runtime
    bucket = f"avatar-fixture-{uuid.uuid4().hex}"
    client = boto3.client("s3", endpoint_url=endpoint,
        aws_access_key_id=os.environ.get("AKB_TEST_AVATAR_S3_ACCESS_KEY", "akb-ci"),
        aws_secret_access_key=os.environ.get("AKB_TEST_AVATAR_S3_SECRET_KEY", "akb-ci-secret"))
    await asyncio.to_thread(client.create_bucket, Bucket=bucket)
    user = await person(pool)

    def put(key, body, mime):
        client.put_object(Bucket=bucket, Key=key, Body=body, ContentType=mime)

    def get(key, limit):
        stream = client.get_object(Bucket=bucket, Key=key)["Body"]
        try:
            return stream.read(limit + 1)
        finally:
            stream.close()

    def delete(key):
        client.delete_object(Bucket=bucket, Key=key)

    monkeypatch.setattr(service.s3_adapter, "put_private_bytes", put)
    monkeypatch.setattr(service.s3_adapter, "get_bounded_bytes", get)
    monkeypatch.setattr(service.s3_adapter, "delete", delete)
    try:
        first = await save(user)
        assert Image.open(io.BytesIO(await service.read_photo(user, first.revision))).size == (512, 512)
        second = await save(user, first.revision, "blue")
        await drain(pool)
        assert len(client.list_objects_v2(Bucket=bucket).get("Contents", [])) == 1
        await service.remove_photo(user, second.revision)
        await drain(pool)
        assert not client.list_objects_v2(Bucket=bucket).get("Contents")
    finally:
        contents = client.list_objects_v2(Bucket=bucket).get("Contents", [])
        for item in contents:
            client.delete_object(Bucket=bucket, Key=item["Key"])
        client.delete_bucket(Bucket=bucket)


async def sso_person(pool, monkeypatch):
    from app.services import sso_browser_session_service as sessions
    from tests.test_sso_browser_session_postgres import (
        _configure, _seed_identity, _principal, _actor, _tokens, _SSO_SESSION_EPOCH,
    )
    _configure(monkeypatch, sessions, pool)
    await pool.execute("""INSERT INTO auth_runtime_state(singleton,runtime_generation,auth_mode,sso_session_epoch)
        VALUES(true,1,'sso',$1)""", _SSO_SESSION_EPOCH)
    user_id, _ = await _seed_identity(pool)
    principal = _principal()
    issued = await sessions.create_sso_browser_session(_actor(user_id), principal, {
        "iss": principal.issuer, "sub": principal.subject, "sid": principal.claims["sid"],
        "identity_provider": "workforce"}, _tokens())
    return sessions, issued, await sessions.resolve_sso_browser_session(issued.token)


async def test_real_sso_session_resolver_upload_read_delete_http_and_csrf(runtime, monkeypatch):
    import httpx
    from fastapi import FastAPI, Request
    from fastapi.responses import JSONResponse
    from app.api import deps
    from app.api.routes.user_avatar import router

    pool, _ = runtime
    sessions, issued, user = await sso_person(pool, monkeypatch)
    assert user.oauth_scopes and user.browser_session_id and service.carrier_reason(user) is None
    monkeypatch.setattr(deps, "sso_browser_session_ready", lambda: True)
    app = FastAPI()
    app.include_router(router)

    @app.exception_handler(AKBError)
    async def error(_request: Request, exc: AKBError):
        return JSONResponse({"code": exc.code}, status_code=exc.status_code)

    cookies = {sessions.sso_browser_session_cookie_name(): issued.token,
               sessions.sso_browser_csrf_cookie_name(): issued.csrf_token}
    headers = {"X-AKB-CSRF": issued.csrf_token, "content-type": "image/png"}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="https://akb.example",
                                 cookies=cookies) as client:
        denied = await client.put("/auth/me/avatar?expected_revision=none", content=photo(),
                                  headers={"content-type": "image/png"})
        assert denied.status_code == 403
        created = await client.put("/auth/me/avatar?expected_revision=none", content=photo(), headers=headers)
        assert created.status_code == 200, created.text
        revision = created.json()["avatar"]["revision"]
        body = await client.get(f"/auth/me/avatar?v={revision}")
        assert body.status_code == 200 and body.headers["content-type"] == "image/webp"
        assert (await client.delete(f"/auth/me/avatar?expected_revision={revision}")).status_code == 403
        removed = await client.delete(f"/auth/me/avatar?expected_revision={revision}", headers=headers)
        assert removed.status_code == 200 and removed.json()["avatar"]["url"] is None


@pytest.mark.parametrize("revocation", ["single", "all", "identity"])
async def test_real_sso_carrier_is_revalidated_after_slow_put(runtime, monkeypatch, revocation):
    pool, objects = runtime
    sessions, issued, user = await sso_person(pool, monkeypatch)
    state = await save(user)
    started, finish = threading.Event(), threading.Event()

    def slow_put(key, data, _mime):
        started.set()
        assert finish.wait(5)
        objects[key] = data

    monkeypatch.setattr(service.s3_adapter, "put_private_bytes", slow_put)
    uploading = asyncio.create_task(service.save_photo(user, state.revision, photo("blue"), "image/png"))
    assert await asyncio.to_thread(started.wait, 2)
    if revocation == "single":
        await sessions.revoke_sso_browser_session(issued.token, issued.csrf_token, issued.csrf_token)
    elif revocation == "all":
        await sessions.revoke_all_sso_browser_sessions(user)
    else:
        await pool.execute("UPDATE external_identities SET subject='changed-subject' WHERE user_id=$1",
                           uuid.UUID(user.user_id))
    finish.set()
    with pytest.raises(AKBError) as caught:
        await uploading
    assert caught.value.code == "account_session_expired"
    await drain(pool)
    assert len(objects) == 1
    assert await pool.fetchval("SELECT revision FROM user_avatars WHERE user_id=$1", uuid.UUID(user.user_id)) == state.revision


@pytest.mark.parametrize("action", ["replace", "remove", "revoke", "suspend", "delete"])
async def test_delayed_read_cannot_return_obsolete_or_revoked_bytes(runtime, monkeypatch, action):
    pool, objects = runtime
    user = await person(pool)
    state = await save(user)
    started, release = threading.Event(), threading.Event()

    def slow_get(key, _limit):
        body = objects[key]
        started.set()
        assert release.wait(10)
        return body

    monkeypatch.setattr(service.s3_adapter, "get_bounded_bytes", slow_get)
    reading = asyncio.create_task(service.read_photo(user, state.revision))
    assert await asyncio.to_thread(started.wait, 5)
    try:
        if action == "replace":
            await save(user, state.revision, "blue")
        elif action == "remove":
            await service.remove_photo(user, state.revision)
        elif action == "revoke":
            await auth_service.revoke_all_sessions(user.user_id)
        elif action == "delete":
            await access_service.delete_user_account(user.user_id)
        else:
            await pool.execute("UPDATE users SET account_status='suspended' WHERE id=$1", uuid.UUID(user.user_id))
    finally:
        release.set()
    with pytest.raises(AKBError) as denied:
        await reading
    assert denied.value.status_code == (404 if action in {"replace", "remove"} else 401)


@pytest.mark.parametrize("repeat_cancel", [False, True])
async def test_cancelled_remote_put_preserves_photo_and_releases_cleanup_lock(runtime, monkeypatch, repeat_cancel):
    pool, objects = runtime
    user = await person(pool)
    state = await save(user)
    started, release = threading.Event(), threading.Event()
    candidate = []

    def slow_put(key, data, _mime):
        candidate.append(key)
        started.set()
        assert release.wait(10)
        objects[key] = data

    monkeypatch.setattr(service.s3_adapter, "put_private_bytes", slow_put)
    uploading = asyncio.create_task(service.save_photo(user, state.revision, photo("blue"), "image/png"))
    assert await asyncio.to_thread(started.wait, 5)
    try:
        uploading.cancel()
        await asyncio.sleep(0.03)
        assert not uploading.done(), "A cancelled await must still own the real PUT thread"
        if repeat_cancel:
            uploading.cancel()
            await asyncio.sleep(0.03)
            assert not uploading.done()
        assert await pool.fetchval("SELECT revision FROM user_avatars WHERE user_id=$1", uuid.UUID(user.user_id)) == state.revision
        assert await pool.fetchval("SELECT count(*) FROM s3_delete_outbox WHERE s3_key=$1 AND processed_at IS NULL", candidate[0]) == 2
    finally:
        release.set()
    with pytest.raises(asyncio.CancelledError):
        await uploading
    async with pool.acquire() as conn:
        assert await vault_files_repo.try_lock_s3_key_for_cleanup(conn, candidate[0])
        await vault_files_repo.unlock_s3_key_after_cleanup(conn, candidate[0])
    assert await pool.fetchval("SELECT revision FROM user_avatars WHERE user_id=$1", uuid.UUID(user.user_id)) == state.revision
    await drain(pool)
    assert candidate[0] not in objects
    assert len(objects) == 1
    assert await service.read_photo(user, state.revision)


async def test_tombstone_rejects_pre_delete_none_and_previous_revision(runtime):
    pool, _objects = runtime
    user = await person(pool)
    state = await save(user)
    removed = await service.remove_photo(user, state.revision)
    for stale in (None, state.revision):
        with pytest.raises(AKBError) as denied:
            await service.preflight_write(user, stale)
        assert denied.value.status_code == 409
    assert await repo.current(pool, uuid.UUID(user.user_id))
    assert await service.remove_photo(user, removed.revision) == removed
