"""Isolated PostgreSQL proof for Native-only approval requester provisioning."""

import hashlib
import os
import uuid

import pytest

from app.services import approval_service_bootstrap as bootstrap, auth_service
from tests.test_admin_auth_postgres import _fresh_database

pytestmark = pytest.mark.asyncio


@pytest.fixture
async def pool(monkeypatch):
    async with _fresh_database() as database:
        async def get_pool():
            return database
        monkeypatch.setattr(bootstrap, "get_pool", get_pool)
        monkeypatch.setattr(auth_service, "get_pool", get_pool)
        yield database


async def test_bootstrap_replay_rotation_and_retired_tombstone(pool):
    key, _, _ = auth_service.generate_pat("service")
    token_id = uuid.uuid4()
    first = await bootstrap.provision(token_id, key)
    replay = await bootstrap.provision(token_id, key)
    assert first["status"] == "issued" and replay == {**first, "status": "replayed"}
    user_id = uuid.UUID(first["requester_user_id"])
    assert first["requester_token_id"] == str(token_id)
    assert first["scopes"] == ["sso_operation_requests"] and first["vault_scope"] is None
    actor = await auth_service.resolve_rest_user_authorization("Bearer " + key)
    assert actor is not None and actor.user_id == str(user_id) and actor.token_id == str(token_id)
    assert actor.account_kind == "service" and not actor.is_admin
    assert actor.token_scopes == frozenset({"sso_operation_requests"})
    row = await pool.fetchrow("SELECT * FROM users WHERE id=$1", user_id)
    assert (row["account_kind"], row["account_status"], row["is_admin"], row["auth_provider"]) == (
        "service", "active", False, "service"
    )
    token = await pool.fetchrow("SELECT * FROM tokens WHERE id=$1", token_id)
    assert token["key_class"] == "service" and token["scopes"] == first["scopes"]
    assert token["vault_scope"] is None and token["token_hash"] == hashlib.sha256(key.encode()).hexdigest()

    new_key, _, _ = auth_service.generate_pat("service")
    with pytest.raises(bootstrap.BootstrapRefused, match="approval_bootstrap_credential_conflict"):
        await bootstrap.provision(token_id, new_key)
    next_id = uuid.uuid4()
    rotated = await bootstrap.provision(next_id, new_key)
    assert rotated["requester_user_id"] == first["requester_user_id"]
    assert rotated["requester_token_id"] == str(next_id) and rotated["status"] == "issued"
    assert await pool.fetchval("SELECT COUNT(*) FROM tokens WHERE user_id=$1", user_id) == 2
    assert await pool.fetchval("SELECT COUNT(*) FROM approval_service_bootstrap") == 2
    assert await pool.fetchval("SELECT COUNT(*) FROM events WHERE kind='auth.sso_approval_service_credential_issued'") == 2
    assert key not in repr(first) + repr(await pool.fetch("SELECT * FROM approval_service_bootstrap")) + repr(
        await pool.fetch("SELECT * FROM events")
    )

    await pool.execute("DELETE FROM tokens WHERE id=$1", token_id)
    with pytest.raises(bootstrap.BootstrapRefused, match="approval_bootstrap_credential_retired"):
        await bootstrap.provision(token_id, key)
    assert await pool.fetchval("SELECT COUNT(*) FROM approval_service_bootstrap") == 2
    assert await pool.fetchval("SELECT COUNT(*) FROM tokens WHERE user_id=$1", user_id) == 1


@pytest.mark.parametrize("kind,admin", [("human", True), ("human", False), ("service", True)])
async def test_existing_name_cannot_be_adopted(pool, kind, admin):
    key, _, _ = auth_service.generate_pat("service")
    uid = uuid.uuid4()
    await pool.execute("""INSERT INTO users(id,username,email,password_hash,account_kind,is_admin,auth_provider)
        VALUES($1,$2,$3,'!fixture!',$4,$5,'local')""", uid, bootstrap.USERNAME, bootstrap.EMAIL, kind, admin)
    with pytest.raises(bootstrap.BootstrapRefused, match="approval_bootstrap_account_exists"):
        await bootstrap.provision(uuid.uuid4(), key)
    row = await pool.fetchrow("SELECT account_kind,is_admin FROM users WHERE id=$1", uid)
    assert (row["account_kind"], row["is_admin"]) == (kind, admin)
    assert await pool.fetchval("SELECT COUNT(*) FROM approval_service_bootstrap") == 0


async def test_changed_account_cannot_rotate_or_replay(pool):
    key, _, _ = auth_service.generate_pat("service")
    token_id = uuid.uuid4()
    receipt = await bootstrap.provision(token_id, key)
    uid = uuid.UUID(receipt["requester_user_id"])
    await pool.execute("UPDATE users SET is_admin=TRUE WHERE id=$1", uid)
    with pytest.raises(bootstrap.BootstrapRefused, match="approval_bootstrap_account_changed"):
        await bootstrap.provision(token_id, key)
    with pytest.raises(bootstrap.BootstrapRefused, match="approval_bootstrap_account_changed"):
        await bootstrap.provision(uuid.uuid4(), key)
    assert await pool.fetchval("SELECT COUNT(*) FROM approval_service_bootstrap") == 1


async def test_pipe_input_rejects_regular_files_and_malformed_values(tmp_path):
    key, _, _ = auth_service.generate_pat("service")
    file = tmp_path / "not-a-pipe"
    file.write_text(key)
    with file.open("rb") as stream:
        with pytest.raises(bootstrap.BootstrapRefused, match="approval_bootstrap_pipe_required"):
            bootstrap.read_pipe(stream.fileno())
    for value, valid in ((key.encode(), True), ((key + "\n").encode(), True), (b"broken", False),
                         ((key + "x").encode(), False)):
        read_fd, write_fd = os.pipe()
        try:
            os.write(write_fd, value)
            os.close(write_fd)
            write_fd = -1
            if valid:
                assert bootstrap.read_pipe(read_fd) == key
            else:
                with pytest.raises(bootstrap.BootstrapRefused):
                    bootstrap.read_pipe(read_fd)
        finally:
            os.close(read_fd)
            if write_fd >= 0:
                os.close(write_fd)


async def test_bootstrap_audit_failure_rolls_back_and_retry_succeeds(pool, monkeypatch):
    from unittest.mock import AsyncMock

    key, _, _ = auth_service.generate_pat("service")
    token_id = uuid.uuid4()
    original = bootstrap.emit_event
    monkeypatch.setattr(bootstrap, "emit_event", AsyncMock(side_effect=RuntimeError("audit unavailable")))
    with pytest.raises(RuntimeError, match="audit unavailable"):
        await bootstrap.provision(token_id, key)
    assert await pool.fetchval("SELECT COUNT(*) FROM users WHERE username=$1", bootstrap.USERNAME) == 0
    assert await pool.fetchval("SELECT COUNT(*) FROM tokens WHERE id=$1", token_id) == 0
    assert await pool.fetchval("SELECT COUNT(*) FROM approval_service_bootstrap") == 0
    monkeypatch.setattr(bootstrap, "emit_event", original)
    assert (await bootstrap.provision(token_id, key))["status"] == "issued"


async def test_concurrent_first_provision_replays_same_identity(pool):
    import asyncio

    key, _, _ = auth_service.generate_pat("service")
    token_id = uuid.uuid4()
    results = await asyncio.gather(bootstrap.provision(token_id, key), bootstrap.provision(token_id, key))
    assert sorted(result["status"] for result in results) == ["issued", "replayed"]
    assert results[0]["requester_user_id"] == results[1]["requester_user_id"]
    assert await pool.fetchval("SELECT COUNT(*) FROM approval_service_bootstrap") == 1


async def test_cli_pipe_writes_only_secret_free_receipt(monkeypatch, tmp_path, capsys):
    from unittest.mock import AsyncMock
    from app import cli
    from app.db import postgres

    key, _, _ = auth_service.generate_pat("service")
    token_id = uuid.uuid4()
    receipt = {"schema_version": 1, "status": "issued", "requester_user_id": str(uuid.uuid4()),
               "requester_token_id": str(token_id), "installation_id": str(uuid.uuid4())}
    monkeypatch.setattr(cli, "_initialize_operator_database", AsyncMock())
    monkeypatch.setattr(postgres, "close_pool", AsyncMock())
    provision = AsyncMock(return_value=receipt)
    monkeypatch.setattr(bootstrap, "provision", provision)
    read_fd, write_fd = os.pipe()
    os.write(write_fd, key.encode())
    os.close(write_fd)
    receipt_path = tmp_path / "receipt"
    with os.fdopen(read_fd) as source:
        monkeypatch.setattr(cli.sys, "stdin", source)
        assert await cli._provision_approval_service_credential(
            ["--token-id", str(token_id), "--receipt-file", str(receipt_path)]
        ) == 0
    provision.assert_awaited_once_with(token_id, key)
    captured = capsys.readouterr()
    assert captured.out == "" and captured.err == ""
    assert key not in receipt_path.read_text()
    assert receipt_path.stat().st_mode & 0o777 == 0o600


async def test_migration_keeps_existing_requests_and_is_idempotent(pool):
    from app.db.postgres import _load_migration

    request_id = uuid.uuid4()
    await pool.execute("ALTER TABLE sso_operation_requests DROP COLUMN requester_scope")
    await pool.execute("""INSERT INTO sso_operation_requests
        (request_id,kind,request,request_sha256,target_id,provider_alias,requested_by,requested_token_id,expires_at)
        VALUES($1,'ownership_claim','{}',$2,$3,'legacy',$4,$5,clock_timestamp()+INTERVAL '1 hour')""",
        request_id, "0" * 64, uuid.uuid4(), uuid.uuid4(), uuid.uuid4())
    migration = _load_migration("128_approval_service_bootstrap.py")
    assert migration is not None
    async with pool.acquire() as conn:
        await migration.migrate(conn=conn)
        await migration.migrate(conn=conn)
    row = await pool.fetchrow("SELECT request_id,requester_scope FROM sso_operation_requests WHERE request_id=$1", request_id)
    assert row["request_id"] == request_id and row["requester_scope"] is None
