"""Real PostgreSQL proofs for safe self-delete, including races and durable cleanup."""
from __future__ import annotations

import asyncio
import unicodedata
import uuid
from dataclasses import replace

import bcrypt
import asyncpg
import httpx
import pytest
from fastapi import FastAPI
from fastapi.responses import JSONResponse

from app.exceptions import AKBError
from app.services import account_self_service as service, account_deletion_worker as worker, auth_service
from app.services.auth_service import AuthenticatedUser
from tests.test_recovery_admin_provisioning_postgres import _fresh_database

pytestmark = pytest.mark.asyncio
PASSWORD = "fixture-confirmation-only"  # pragma: allowlist secret


@pytest.fixture
async def pool(monkeypatch):
    async with _fresh_database() as pool:
        async def get_pool():
            return pool
        for module in (service, worker, auth_service):
            monkeypatch.setattr(module, "get_pool", get_pool)
        monkeypatch.setattr(service.settings, "auth_mode", "local")
        monkeypatch.setattr(service.settings, "account_self_service_enabled", True)
        await pool.execute("INSERT INTO account_deletion_worker_state VALUES(true,clock_timestamp())")
        yield pool


async def person(pool, *, admin=False, recovery=False) -> AuthenticatedUser:
    uid = uuid.uuid4()
    name = f"person-{uid.hex[:10]}"
    hashed = bcrypt.hashpw(PASSWORD.encode(), bcrypt.gensalt(rounds=4)).decode()
    await pool.execute("""INSERT INTO users(id,username,email,password_hash,is_admin,is_recovery_admin)
        VALUES($1,$2,$3,$4,$5,$6)""", uid, name, f"{name}@example.test", hashed, admin, recovery)
    return AuthenticatedUser(str(uid), name, f"{name}@example.test", None, admin, "jwt")


async def vault(pool, user, *, archived=False):
    uid = uuid.uuid4()
    await pool.execute("""INSERT INTO vaults(id,name,owner_id,git_path,status)
        VALUES($1,$2,$3,$4,$5)""", uid, f"vault-{uid.hex[:10]}", uuid.UUID(user.user_id), "/unused-test",
        "archived" if archived else "active")
    return uid


async def delete(user):
    return await service.delete_account(user, uuid.UUID(user.user_id), user.username, PASSWORD)


async def test_owned_archived_vault_is_blocker_and_pagination_is_authoritative(pool):
    user = await person(pool)
    ids = [await vault(pool, user, archived=True), await vault(pool, user)]
    view = await service.lifecycle(user)
    assert view["deletion"]["supported"] and not view["deletion"]["allowed"]
    assert {"code": "owned_vaults", "count": 2} in view["deletion"]["blockers"]
    page = await service.deletion_blockers(user, cursor=None, limit=1)
    assert page["total"] == 2 and page["next_cursor"]
    second = await service.deletion_blockers(user, cursor=uuid.UUID(page["next_cursor"]), limit=1)
    assert second["next_cursor"] is None
    assert page["owned_vaults"] != second["owned_vaults"]
    with pytest.raises(AKBError) as exc:
        await delete(user)
    assert exc.value.code == "account_deletion_blocked"
    assert await pool.fetchval("SELECT count(*) FROM vaults WHERE id=ANY($1::uuid[])", ids) == 2
    assert await pool.fetchval("SELECT count(*) FROM account_deletion_cleanup") == 0


async def test_deletion_preserves_shared_publication_revokes_tokens_and_jobs_survive(pool):
    user, owner = await person(pool), await person(pool)
    vid = await vault(pool, owner)
    token_id = uuid.uuid4()
    await pool.execute("INSERT INTO tokens(id,user_id,name,token_hash,token_prefix) VALUES($1,$2,'fixture','fixture-hash','fixture')",
                       token_id, uuid.UUID(user.user_id))
    # Existing publication belongs to the other vault, even though this user published it.
    await pool.execute("""INSERT INTO publications(slug,resource_type,vault_id,resource_uri,created_by)
        VALUES('fixture-publication','document',$1,'fixture.md',$2)""", vid, uuid.UUID(user.user_id))
    view = await service.lifecycle(user)
    assert view["deletion"]["effects"] == {"active_pats_revoked": 1, "shared_publications_preserved": 1}
    assert await delete(user) == {"deleted": True, "user_id": user.user_id}
    assert not await pool.fetchval("SELECT EXISTS(SELECT 1 FROM users WHERE id=$1)", uuid.UUID(user.user_id))
    assert not await pool.fetchval("SELECT EXISTS(SELECT 1 FROM tokens WHERE id=$1)", token_id)
    pub = await pool.fetchrow("SELECT * FROM publications WHERE slug='fixture-publication'")
    assert pub is not None and pub["created_by"] is None
    assert await pool.fetchval("SELECT count(*) FROM account_deletion_cleanup WHERE completed_at IS NULL") == 2
    assert await pool.fetchval("SELECT count(*) FROM account_lifecycle_attempts") == 0


async def test_confirmation_and_carrier_guards_fail_before_mutation(pool):
    user = await person(pool)
    for carrier in ("pat", "oauth", "browser_session"):
        with pytest.raises(AKBError) as exc:
            await delete(replace(user, auth_method=carrier))
        assert exc.value.code == "human_session_required"
    with pytest.raises(AKBError) as exc:
        await service.delete_account(user, uuid.uuid4(), user.username, PASSWORD)
    assert exc.value.code == "account_identity_changed"
    with pytest.raises(AKBError):
        await service.delete_account(user, uuid.UUID(user.user_id), "wrong-name", PASSWORD)
    with pytest.raises(AKBError) as exc:
        await service.delete_account(user, uuid.UUID(user.user_id), user.username, "wrong-password")
    assert exc.value.code == "reauthentication_failed" and exc.value.status_code == 403
    assert await pool.fetchval("SELECT count(*) FROM account_deletion_cleanup") == 0


async def test_confirmation_budget_is_not_rolled_back_with_failure(pool):
    user = await person(pool)
    for _ in range(5):
        with pytest.raises(AKBError) as exc:
            await service.delete_account(user, uuid.UUID(user.user_id), user.username, "wrong")
        assert exc.value.status_code == 403
    with pytest.raises(AKBError) as exc:
        await delete(user)
    assert exc.value.status_code == 429


async def test_last_admin_and_recovery_guards(pool):
    admin = await person(pool, admin=True)
    with pytest.raises(AKBError) as exc:
        await delete(admin)
    assert exc.value.details == {"blockers": [{"code": "last_active_admin"}]}
    recovery = await person(pool, admin=True, recovery=True)
    with pytest.raises(AKBError) as exc:
        await delete(recovery)
    assert exc.value.code == "recovery_admin_protected"
    other = await person(pool, admin=True)
    results = await asyncio.gather(delete(admin), delete(other), return_exceptions=True)
    assert sum(isinstance(r, dict) for r in results) == 1
    assert await pool.fetchval("SELECT count(*) FROM users WHERE is_admin AND NOT is_recovery_admin") == 1


async def test_cleanup_unavailable_and_sso_are_explicit(pool, monkeypatch):
    user = await person(pool)
    await pool.execute("UPDATE account_deletion_worker_state SET last_seen_at=now()-interval '2 minutes'")
    view = await service.lifecycle(user)
    assert view["deletion"]["reason"] == "cleanup_unavailable"
    assert view["revoke_sessions"]["supported"]
    with pytest.raises(AKBError) as exc:
        await delete(user)
    assert exc.value.code == "cleanup_unavailable"
    monkeypatch.setattr(service.settings, "auth_mode", "sso")
    view = await service.lifecycle(user)
    assert view["deletion"]["reason"] == "managed_account"
    with pytest.raises(AKBError):
        await service.revoke_sessions(user, uuid.UUID(user.user_id))


async def test_all_detach_and_jobs_rollback_if_final_delete_fails(pool):
    user, owner = await person(pool), await person(pool)
    vid = await vault(pool, owner)
    await pool.execute("""INSERT INTO publications(slug,resource_type,vault_id,resource_uri,created_by)
        VALUES('rollback-publication','document',$1,'fixture.md',$2)""", vid, uuid.UUID(user.user_id))
    await pool.execute("""CREATE FUNCTION fail_fixture_delete() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN RAISE EXCEPTION 'injected fixture failure'; END $$;
        CREATE TRIGGER fixture_delete BEFORE DELETE ON users FOR EACH ROW EXECUTE FUNCTION fail_fixture_delete();""")
    with pytest.raises(Exception, match="injected fixture failure"):
        await delete(user)
    assert await pool.fetchval("SELECT created_by FROM publications WHERE slug='rollback-publication'") == uuid.UUID(user.user_id)
    assert await pool.fetchval("SELECT count(*) FROM account_deletion_cleanup") == 0


async def test_new_owned_vault_after_preview_is_rechecked(pool):
    user = await person(pool)
    assert (await service.lifecycle(user))["deletion"]["allowed"]
    vid = await vault(pool, user)
    with pytest.raises(AKBError) as exc:
        await delete(user)
    assert exc.value.code == "account_deletion_blocked"
    assert await pool.fetchval("SELECT owner_id FROM vaults WHERE id=$1", vid) == uuid.UUID(user.user_id)


async def test_concurrent_vault_creation_cannot_slip_past_locked_owner(pool, monkeypatch):
    user = await person(pool)
    original = service._blockers
    vid = uuid.uuid4()
    insert_task = None
    async with pool.acquire() as writer:
        writer_pid = await writer.fetchval("SELECT pg_backend_pid()")

        async def race_insert(conn, row, **kwargs):
            nonlocal insert_task
            insert_task = asyncio.create_task(writer.execute(
                """INSERT INTO vaults(id,name,owner_id,git_path)
                   VALUES($1,$2,$3,'/unused-test')""",
                vid, f"race-{vid.hex[:10]}", uuid.UUID(user.user_id)))
            # Observe a real FK lock wait, rather than relying on a timing sleep.
            for _ in range(200):
                if await conn.fetchval("SELECT cardinality(pg_blocking_pids($1)) > 0", writer_pid):
                    break
                await asyncio.sleep(0.01)
            else:
                pytest.fail("Concurrent owner FK did not wait for the deletion lock")
            return await original(conn, row, **kwargs)

        monkeypatch.setattr(service, "_blockers", race_insert)
        try:
            assert (await delete(user))["deleted"]
            with pytest.raises(asyncpg.ForeignKeyViolationError):
                await insert_task
        finally:
            if insert_task is not None and not insert_task.done():
                insert_task.cancel()
                await asyncio.gather(insert_task, return_exceptions=True)
    assert not await pool.fetchval("SELECT EXISTS(SELECT 1 FROM vaults WHERE id=$1)", vid)


async def test_cleanup_worker_retries_independently_after_account_is_gone(pool, monkeypatch):
    user = await person(pool)
    await delete(user)
    class Roles:
        fail = True
        async def _drop_role_if_present(self, conn, name):
            if self.fail:
                raise RuntimeError("fixture failure")
    roles = Roles()
    monkeypatch.setattr(worker, "get_role_sync", lambda: roles)
    assert await worker.process_once() == 1
    row = await pool.fetchrow("SELECT * FROM account_deletion_cleanup")
    assert row["completed_at"] is None and row["attempts"] == 1 and row["last_error_code"] == "RuntimeError"
    roles.fail = False
    await pool.execute("UPDATE account_deletion_cleanup SET next_attempt_at=now()")
    assert await worker.process_once() == 1
    assert await worker.process_once() == 0
    assert (await worker.pending_stats())["pending"] == 0


async def test_http_contract_identity_and_retired_endpoint(pool, monkeypatch, tmp_path):
    monkeypatch.setattr(service.settings, "git_storage_path", str(tmp_path / "vaults"))
    from fastapi.exceptions import RequestValidationError
    from app.main import validation_error_handler
    from app.api.routes.access import router
    from app.api.deps import get_current_user
    from app.services.access_service import delete_other_user_account
    user = await person(pool, admin=True)
    for variant in (user.user_id.upper(), user.user_id.replace("-", "")):
        with pytest.raises(AKBError) as exc:
            await delete_other_user_account(variant, actor_id=user.user_id)
        assert exc.value.code == "self_delete_required"
    app = FastAPI()
    app.add_exception_handler(RequestValidationError, validation_error_handler)
    app.include_router(router, prefix="/api/v1")
    app.dependency_overrides[get_current_user] = lambda: user
    @app.exception_handler(AKBError)
    async def errors(request, exc):
        return JSONResponse({"detail": {"code": exc.code}}, status_code=exc.status_code)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/api/v1/my/account/lifecycle")
        assert response.status_code == 200 and response.headers["cache-control"] == "no-store"
        assert (await client.delete("/api/v1/my/account")).status_code == 410
        assert (await client.post("/api/v1/my/account/deletion", json={})).status_code == 422
        invalid_secret = "fixture-private-input-" * 60  # pragma: allowlist secret
        response = await client.post("/api/v1/my/account/deletion", json={
            "expected_user_id": user.user_id, "confirm_username": user.username,
            "current_password": invalid_secret,
        })
        assert response.status_code == 422 and invalid_secret not in response.text
        assert "fixture-private-input" not in response.text
        response = await client.post("/api/v1/my/account/session-revocations", json={"expected_user_id": str(uuid.uuid4())})
        assert response.status_code == 409
        response = await client.post("/api/v1/my/account/session-revocations", json={"expected_user_id": user.user_id})
        assert response.status_code == 200
        assert await pool.fetchval("SELECT session_generation FROM users WHERE id=$1", uuid.UUID(user.user_id)) == 1


@pytest.mark.parametrize("password_form", ["NFC", "NFD"])
async def test_http_deletion_password_matches_registration_normalization(pool, password_form):
    from app.api.deps import get_current_user
    from app.api.routes.access import router
    from app.api.routes.auth import LoginRequest, RegisterRequest

    user = await person(pool)
    password = "fixture-caf\u00e9-\ube44\ubc00"  # pragma: allowlist secret
    registration = RegisterRequest(
        username=user.username, email=user.email,
        password=unicodedata.normalize("NFD", password),
    )
    login = LoginRequest(username=user.username, password=unicodedata.normalize(password_form, password))
    assert login.password == registration.password
    hashed = bcrypt.hashpw(registration.password.encode(), bcrypt.gensalt(rounds=4)).decode()
    await pool.execute("UPDATE users SET password_hash=$1 WHERE id=$2", hashed, uuid.UUID(user.user_id))
    app = FastAPI()
    app.include_router(router, prefix="/api/v1")
    app.dependency_overrides[get_current_user] = lambda: user

    @app.exception_handler(AKBError)
    async def errors(request, exc):
        return JSONResponse({"detail": {"code": exc.code}}, status_code=exc.status_code)

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        body = {
            "expected_user_id": user.user_id, "confirm_username": user.username,
            "current_password": unicodedata.normalize(password_form, password),
        }
        assert (await client.post("/api/v1/my/account/deletion", json={**body, "unexpected": True})).status_code == 422
        response = await client.post("/api/v1/my/account/deletion", json=body)
        assert response.status_code == 200, response.text
        assert response.json() == {"deleted": True, "user_id": user.user_id}
    assert not await pool.fetchval("SELECT EXISTS(SELECT 1 FROM users WHERE id=$1)", uuid.UUID(user.user_id))


async def test_admin_promoted_after_lock_snapshot_cannot_satisfy_last_admin_guard(pool, monkeypatch):
    """A new eligible row is a phantom: its concurrent demotion is not fenced."""
    deleting = await person(pool, admin=True)
    promoted = await person(pool)
    promoted_id = uuid.UUID(promoted.user_id)
    original_blockers = service._blockers
    observed = []

    async def promote_then_demote_around_count(conn, row, **kwargs):
        # delete_account has locked its eligible-admin snapshot already. This
        # independent connection can still change the previously nonadmin row.
        await pool.execute("UPDATE users SET is_admin=true WHERE id=$1", promoted_id)
        try:
            result = await original_blockers(conn, row, **kwargs)
            observed.extend(result)
            return result
        finally:
            # Demotion commits before the self-delete transaction can commit.
            await pool.execute("UPDATE users SET is_admin=false WHERE id=$1", promoted_id)

    monkeypatch.setattr(service, "_blockers", promote_then_demote_around_count)
    with pytest.raises(AKBError) as exc:
        await delete(deleting)
    assert exc.value.code == "account_deletion_blocked"
    assert {"code": "last_active_admin"} in observed
    assert await pool.fetchval("SELECT count(*) FROM users WHERE is_admin") == 1
    assert await pool.fetchval("SELECT count(*) FROM account_deletion_cleanup") == 0
