"""Real isolated PostgreSQL transactions; no existing database is reset."""
import asyncio
import json
import os
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit
import uuid

import asyncpg
import pytest

from app.config import settings
from app.services import auth_service, platform_service_bootstrap as bootstrap
from app.services.role_authority import role_authority_transaction
from app.services.role_sync import AUTHENTICATED_ROLE, RoleSync, token_role_name, user_role_name
from tests.test_platform_service_bootstrap_unit import wire

pytestmark = pytest.mark.asyncio


@pytest.fixture
async def lane(monkeypatch):
    dsn = os.environ.get("AKB_TEST_DSN")
    if not dsn:
        pytest.fail("AKB_TEST_DSN required; this lane never skips")
    admin = await asyncpg.connect(dsn, timeout=5)
    database = "akb_service_cli_" + uuid.uuid4().hex
    pool = None
    owned_roles = {}
    authenticated_before = await admin.fetchval("SELECT oid FROM pg_roles WHERE rolname=$1", AUTHENTICATED_ROLE)
    authenticated_created = None
    original_project = RoleSync.on_user_create_in_conn

    async def tracked_project(sync, conn, user_id):
        nonlocal authenticated_created
        role = user_role_name(user_id)
        assert await conn.fetchval("SELECT current_database()") == database
        assert await conn.fetchval("SELECT id FROM users WHERE id=$1 AND username='platform-bot'", uuid.UUID(str(user_id)))
        if role not in owned_roles:
            assert not await conn.fetchval("SELECT oid FROM pg_roles WHERE rolname=$1", role)
        await original_project(sync, conn, user_id)
        owned_roles[role] = await conn.fetchval("SELECT oid FROM pg_roles WHERE rolname=$1", role)
        if authenticated_before is None:
            authenticated_created = await conn.fetchval("SELECT oid FROM pg_roles WHERE rolname=$1", AUTHENTICATED_ROLE)

    monkeypatch.setattr(RoleSync, "on_user_create_in_conn", tracked_project)
    try:
        await admin.execute(f'CREATE DATABASE "{database}"')
        target = urlunsplit(urlsplit(dsn)._replace(path="/"+database))
        pool = await asyncpg.create_pool(target, min_size=1, max_size=5)
        await pool.execute((Path(__file__).parents[1]/"app/db/init.sql").read_text())
        from app.db.postgres import _load_migration
        async with pool.acquire() as conn:
            for filename in ("015_events_outbox.py", "126_platform_service_bootstrap.py"):
                await _load_migration(filename).migrate(conn=conn)
        epoch = uuid.uuid4()
        await pool.execute("INSERT INTO auth_runtime_state(singleton,runtime_generation,auth_mode,sso_session_epoch) VALUES(true,1,'sso',$1)", epoch)
        async def get_pool():
            return pool
        monkeypatch.setattr(bootstrap, "get_pool", get_pool)
        monkeypatch.setattr(auth_service, "get_pool", get_pool)
        for key, value in {"auth_mode":"sso", "keycloak_enabled":True, "keycloak_server_url":"https://identity.example",
            "keycloak_realm":"akb", "keycloak_service_admin_client_id":"akb-sso-manager", "auth_runtime_generation":1, "sso_session_epoch":epoch,
            "document_revision_backend":"postgres_native"}.items():
            monkeypatch.setattr(settings, key, value)
        yield pool, bootstrap.parse_intent(wire())
    finally:
        if pool:
            await pool.close()
        await admin.execute(f'DROP DATABASE IF EXISTS "{database}"')
        # Roles are cluster-wide: exact UUID/OID ownership, never a prefix sweep.
        for role, oid in owned_roles.items():
            current = await admin.fetchval("SELECT oid FROM pg_roles WHERE rolname=$1", role)
            if current is not None:
                assert current == oid and user_role_name(uuid.UUID(role.removeprefix("akb_user_").replace("_", "-"))) == role
                await admin.execute(f'DROP ROLE "{role}"')
        if authenticated_before is None and authenticated_created is not None:
            current = await admin.fetchval("SELECT oid FROM pg_roles WHERE rolname=$1", AUTHENTICATED_ROLE)
            if current == authenticated_created:
                assert not await admin.fetchval("SELECT EXISTS(SELECT 1 FROM pg_auth_members WHERE roleid=$1 OR member=$1)", current)
                await admin.execute(f'DROP ROLE "{AUTHENTICATED_ROLE}"')
        else:
            assert await admin.fetchval("SELECT oid FROM pg_roles WHERE rolname=$1", AUTHENTICATED_ROLE) == authenticated_before
        await admin.close()


async def test_first_run_concurrent_retry_actual_authentication_and_lost_ack(lane):
    pool, intent = lane
    results = await asyncio.gather(*(bootstrap.provision(intent) for _ in range(5)))
    assert [r["status"] for r in results].count("issued") == 1
    assert len({r["user_id"] for r in results}) == 1
    for table in ("users", "tokens", "platform_service_bootstrap"):
        assert await pool.fetchval(f"SELECT count(*) FROM {table}") == 1
    assert await pool.fetchval("SELECT count(*) FROM external_identities") == 0
    role = user_role_name(results[0]["user_id"])
    attrs = await pool.fetchrow("SELECT rolcanlogin,rolsuper,rolcreaterole,rolcreatedb FROM pg_roles WHERE rolname=$1", role)
    assert dict(attrs) == {"rolcanlogin": False, "rolsuper": False, "rolcreaterole": False, "rolcreatedb": False}
    async with pool.acquire() as conn:
        assert await RoleSync(pool).user_role_projected_in_conn(conn, results[0]["user_id"])
    assert not await pool.fetchval("SELECT oid FROM pg_roles WHERE rolname=$1", token_role_name(intent.token_id))
    assert await pool.fetchval("SELECT count(*) FROM events WHERE kind='auth.platform_service_bootstrapped'") == 1
    user = await auth_service.resolve_rest_user_authorization("Bearer " + intent.credential)
    assert user.user_id == results[0]["user_id"]
    assert user.username == "platform-bot" and user.email == "platform-bot@workspace-test.local"
    assert user.is_admin and user.auth_method == "pat" and user.account_kind == "service" and user.key_class == "service"
    again = await bootstrap.provision(intent)
    assert again["status"] == "replayed" and again["token_id"] == str(intent.token_id)
    assert intent.credential not in json.dumps(again)
    assert intent.credential not in str(await pool.fetchrow("SELECT * FROM platform_service_bootstrap"))


@pytest.mark.parametrize("change", ["delete-token", "delete-user", "demote", "suspend", "kind", "provider", "username", "email", "external", "service-identity", "password", "required-action", "scope", "class", "vault", "expiry", "future-expiry", "revoked-before", "token-owner", "token-hash", "token-prefix", "token-name"])
async def test_replay_never_repairs_revocation_or_identity_drift(lane, change):
    pool, intent = lane
    report = await bootstrap.provision(intent)
    user = uuid.UUID(report["user_id"])
    mutations = {
        "delete-token": ("DELETE FROM tokens WHERE id=$1", intent.token_id),
        "delete-user": ("DELETE FROM users WHERE id=$1", user),
        "demote": ("UPDATE users SET is_admin=false WHERE id=$1", user),
        "suspend": ("UPDATE users SET account_status='suspended' WHERE id=$1", user),
        "kind": ("UPDATE users SET account_kind='human' WHERE id=$1", user),
        "provider": ("UPDATE users SET auth_provider='keycloak' WHERE id=$1", user),
        "username": ("UPDATE users SET username='renamed' WHERE id=$1", user),
        "email": ("UPDATE users SET email='other@example.test' WHERE id=$1", user),
        "external": ("INSERT INTO external_identities(user_id,issuer,subject) VALUES($1,'https://foreign.example','foreign')", user),
        "service-identity": ("INSERT INTO service_identities(user_id,issuer,client_id,subject) VALUES($1,'https://foreign.example','foreign','subject')", user),
        "password": ("UPDATE users SET password_hash='changed' WHERE id=$1", user),  # pragma: allowlist secret -- synthetic invalid password hash
        "required-action": ("UPDATE users SET credential_change_required=true WHERE id=$1", user),
        "token-hash": ("UPDATE tokens SET token_hash=repeat('0',64) WHERE id=$1", intent.token_id),
        "token-prefix": ("UPDATE tokens SET token_prefix='changed' WHERE id=$1", intent.token_id),
        "token-name": ("UPDATE tokens SET name='changed' WHERE id=$1", intent.token_id),
        "scope": ("UPDATE tokens SET scopes=ARRAY['read'] WHERE id=$1", intent.token_id),
        "class": ("UPDATE tokens SET key_class='pat' WHERE id=$1", intent.token_id),
        "vault": ("UPDATE tokens SET vault_scope='{}'::jsonb WHERE id=$1", intent.token_id),
        "expiry": ("UPDATE tokens SET expires_at=clock_timestamp()-INTERVAL '1 second' WHERE id=$1", intent.token_id),
        "future-expiry": ("UPDATE tokens SET expires_at=clock_timestamp()+INTERVAL '1 hour' WHERE id=$1", intent.token_id),
        "revoked-before": ("UPDATE users SET tokens_revoked_before=clock_timestamp()+INTERVAL '1 hour' WHERE id=$1", user),
    }
    if change == "token-owner":
        other = await pool.fetchval("INSERT INTO users(username,email,password_hash) VALUES('other','other@example.test','!') RETURNING id")
        await pool.execute("UPDATE tokens SET user_id=$1 WHERE id=$2", other, intent.token_id)
    else:
        await pool.execute(*mutations[change])
    before = (await pool.fetch("SELECT * FROM users ORDER BY id"), await pool.fetch("SELECT * FROM tokens ORDER BY id"))
    with pytest.raises(bootstrap.BootstrapRefused):
        await bootstrap.provision(intent)
    assert (await pool.fetch("SELECT * FROM users ORDER BY id"), await pool.fetch("SELECT * FROM tokens ORDER BY id")) == before
    assert await pool.fetchval("SELECT count(*) FROM platform_service_bootstrap") == 1
    with pytest.raises(bootstrap.BootstrapRefused):
        await bootstrap.provision(bootstrap.parse_intent(wire()))


@pytest.mark.parametrize("changed", ["operation_id", "org_id", "owner_uid", "token_id", "credential", "tenant_slug"])
async def test_new_intent_and_changed_runtime_do_not_widen_lineage(lane, changed):
    pool, intent = lane
    await bootstrap.provision(intent)
    from dataclasses import replace
    replacement = getattr(bootstrap.parse_intent(wire()), changed)
    changed_intent = replace(intent, **{changed: replacement}, intent_sha256="0"*64)
    with pytest.raises(bootstrap.BootstrapRefused):
        await bootstrap.provision(changed_intent)
    assert await pool.fetchval("SELECT count(*) FROM users") == 1
    assert await pool.fetchval("SELECT count(*) FROM tokens") == 1


async def test_lineageless_canonical_account_not_adopted(lane):
    pool, intent = lane
    existing = await pool.fetchval("""INSERT INTO users(username,email,password_hash,is_admin,auth_provider,account_kind)
        VALUES('platform-bot','platform-bot@workspace-test.local','!foreign!',true,'service','service') RETURNING id""")
    with pytest.raises(bootstrap.BootstrapRefused, match="account_exists"):
        await bootstrap.provision(intent)
    assert await pool.fetchval("SELECT count(*) FROM platform_service_bootstrap") == 0
    assert await pool.fetchval("SELECT count(*) FROM tokens") == 0
    assert await pool.fetchval("SELECT id FROM users") == existing


async def test_atomic_failure_rolls_back_account_token_and_lineage(lane, monkeypatch):
    pool, intent = lane
    original = bootstrap.emit_event
    attempted_user = None
    async def failed(conn, *args, **kwargs):
        nonlocal attempted_user
        attempted_user = kwargs["payload"]["user_id"]
        await original(conn, *args, **kwargs)
        raise RuntimeError("forced event transaction failure")
    monkeypatch.setattr(bootstrap, "emit_event", failed)
    with pytest.raises(RuntimeError):
        await bootstrap.provision(intent)
    for table in ("users", "tokens", "platform_service_bootstrap", "events"):
        assert await pool.fetchval(f"SELECT count(*) FROM {table}") == 0
    assert attempted_user is not None
    assert not await pool.fetchval("SELECT oid FROM pg_roles WHERE rolname=$1", user_role_name(attempted_user))


async def test_replay_waits_for_committed_revocation_before_readback(lane):
    pool, intent = lane
    await bootstrap.provision(intent)
    async with pool.acquire() as conn:
        async with role_authority_transaction(conn):
            await conn.execute("DELETE FROM tokens WHERE id=$1", intent.token_id)
            task = asyncio.create_task(bootstrap.provision(intent))
            await asyncio.sleep(0.05)
            assert not task.done()
        with pytest.raises(bootstrap.BootstrapRefused, match="credential_retired"):
            await task
    assert await pool.fetchval("SELECT count(*) FROM tokens") == 0


async def test_runtime_mismatch_and_native_mode_refuse_before_issuance(lane, monkeypatch):
    pool, intent = lane
    await pool.execute("UPDATE auth_runtime_state SET runtime_generation=2")
    with pytest.raises(bootstrap.BootstrapRefused, match="runtime_conflict"):
        await bootstrap.provision(intent)
    await pool.execute("UPDATE auth_runtime_state SET runtime_generation=1")
    monkeypatch.setattr(settings, "keycloak_service_admin_client_id", "")
    with pytest.raises(bootstrap.BootstrapRefused, match="runtime_conflict"):
        await bootstrap.provision(intent)
    assert await pool.fetchval("SELECT count(*) FROM users") == 0


async def test_migration_fresh_upgrade_equivalence_and_no_cascade(lane):
    pool, _ = lane
    fresh = await pool.fetchval("SELECT pg_get_constraintdef(oid) FROM pg_constraint WHERE conrelid='platform_service_bootstrap'::regclass AND contype='p'")
    await pool.execute("DROP TABLE platform_service_bootstrap")
    from app.db.postgres import _load_migration
    async with pool.acquire() as conn:
        module = _load_migration("126_platform_service_bootstrap.py")
        await module.migrate(conn=conn)
        await module.migrate(conn=conn)
    assert await pool.fetchval("SELECT pg_get_constraintdef(oid) FROM pg_constraint WHERE conrelid='platform_service_bootstrap'::regclass AND contype='p'") == fresh
    assert await pool.fetchval("SELECT count(*) FROM pg_constraint WHERE conrelid='platform_service_bootstrap'::regclass AND contype='f'") == 0


async def test_explicit_bare_git_workspace_has_same_auth_bootstrap_contract(lane, monkeypatch):
    pool, intent = lane
    monkeypatch.setattr(settings, "document_revision_backend", "bare_git")
    report = await bootstrap.provision(intent)
    assert report["status"] == "issued"
    assert (await bootstrap.provision(intent))["status"] == "replayed"
    assert await pool.fetchval("SELECT count(*) FROM tokens") == 1


async def test_image_owned_semantic_probe_requires_real_prepared_lineage(lane, tmp_path):
    pool, intent = lane
    from app.services.platform_service_bootstrap_probe import probe
    # Reconstruct the same public wire; secret remains only in protected file.
    value = {"schema_version":1,"purpose":bootstrap.PURPOSE, "operation_id":str(intent.operation_id),
        "org_id":str(intent.org_id),"tenant_slug":intent.tenant_slug,"namespace":intent.namespace,
        "owner_uid":str(intent.owner_uid),"token_id":str(intent.token_id),"credential":intent.credential}
    source, receipt = tmp_path/"intent", tmp_path/"receipt"
    source.write_text(json.dumps(value))
    source.chmod(0o600)
    receipt.write_text('{}')
    with pytest.raises(bootstrap.BootstrapRefused, match="not_prepared"):
        await probe(str(source), str(receipt))
    assert await pool.fetchval("SELECT count(*) FROM tokens") == 0
    issued = await bootstrap.provision(intent)
    bootstrap.write_receipt(str(receipt), issued)
    result = await probe(str(source), str(receipt))
    assert result["status"] == "verified" and result["capability"] == bootstrap.CAPABILITY
    assert result["token_id"] == str(intent.token_id) and intent.credential not in json.dumps(result)
    changed = dict(issued, user_id=str(uuid.uuid4()))
    bootstrap.write_receipt(str(receipt), changed)
    with pytest.raises(bootstrap.BootstrapRefused, match="receipt_mismatch"):
        await probe(str(source), str(receipt))
    assert await pool.fetchval("SELECT count(*) FROM tokens") == 1


async def test_replay_pins_original_service_client_and_runtime(lane, monkeypatch):
    pool, intent = lane
    await bootstrap.provision(intent)
    with monkeypatch.context() as changed:
        changed.setattr(settings, "keycloak_service_admin_client_id", "replacement-manager")
        with pytest.raises(bootstrap.BootstrapRefused, match="intent_conflict"):
            await bootstrap.provision(intent)
    await pool.execute("UPDATE auth_runtime_state SET runtime_generation=2")
    monkeypatch.setattr(settings, "auth_runtime_generation", 2)
    with pytest.raises(bootstrap.BootstrapRefused, match="intent_conflict"):
        await bootstrap.provision(intent)
    assert await pool.fetchval("SELECT count(*) FROM users") == 1
    assert await pool.fetchval("SELECT count(*) FROM tokens") == 1


async def test_projection_failure_rolls_back_catalog_role_membership_and_audit(lane, monkeypatch):
    pool, intent = lane
    original = RoleSync.on_user_create_in_conn
    attempted_user = None
    async def failed(sync, conn, user_id):
        nonlocal attempted_user
        attempted_user = user_id
        for table in ("users", "tokens", "platform_service_bootstrap"):
            assert await conn.fetchval(f"SELECT count(*) FROM {table}") == 1
        await original(sync, conn, user_id)
        assert await sync.user_role_projected_in_conn(conn, user_id)
        raise RuntimeError("forced projection transaction failure")
    monkeypatch.setattr(RoleSync, "on_user_create_in_conn", failed)
    with pytest.raises(RuntimeError, match="projection transaction failure"):
        await bootstrap.provision(intent)
    for table in ("users", "tokens", "platform_service_bootstrap", "events"):
        assert await pool.fetchval(f"SELECT count(*) FROM {table}") == 0
    assert attempted_user is not None
    assert not await pool.fetchval("SELECT oid FROM pg_roles WHERE rolname=$1", user_role_name(attempted_user))
    assert not await pool.fetchval("SELECT oid FROM pg_roles WHERE rolname=$1", token_role_name(intent.token_id))


@pytest.mark.parametrize("change", ["missing-role", "revoked-membership", "admin-option", "no-inherit", "no-set"])
async def test_replay_never_restores_changed_role_projection(lane, change):
    pool, intent = lane
    report = await bootstrap.provision(intent)
    role = user_role_name(report["user_id"])
    if change == "missing-role":
        await pool.execute(f'DROP ROLE "{role}"')
    elif change == "revoked-membership":
        await pool.execute(f'REVOKE "{AUTHENTICATED_ROLE}" FROM "{role}"')
    else:
        option = {"admin-option": "ADMIN TRUE", "no-inherit": "INHERIT FALSE", "no-set": "SET FALSE"}[change]
        await pool.execute(f'GRANT "{AUTHENTICATED_ROLE}" TO "{role}" WITH {option}')
    before = await pool.fetch("SELECT roleid,member,grantor,admin_option,inherit_option,set_option FROM pg_auth_members WHERE member=(SELECT oid FROM pg_roles WHERE rolname=$1) ORDER BY roleid,grantor", role)
    for replay_only in (False, True):
        with pytest.raises(bootstrap.BootstrapRefused, match="role_projection_changed"):
            await bootstrap.provision(intent, replay_only=replay_only)
    after = await pool.fetch("SELECT roleid,member,grantor,admin_option,inherit_option,set_option FROM pg_auth_members WHERE member=(SELECT oid FROM pg_roles WHERE rolname=$1) ORDER BY roleid,grantor", role)
    assert after == before
    assert await pool.fetchval("SELECT count(*) FROM platform_service_bootstrap") == 1
    assert await pool.fetchval("SELECT count(*) FROM tokens") == 1
    if change == "missing-role":
        assert not await pool.fetchval("SELECT oid FROM pg_roles WHERE rolname=$1", role)
