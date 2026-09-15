"""External disable/delete suspends AKB access without deleting tenant content."""
from types import SimpleNamespace
from unittest.mock import AsyncMock
import uuid

import pytest

from app.services import sso_account_sync as sync
from app.sso.keycloak_admin import ProviderControlError
from tests.test_recovery_admin_provisioning_postgres import _fresh_database

pytestmark = pytest.mark.asyncio


@pytest.fixture
async def fixture(monkeypatch):
    async with _fresh_database() as pool:
        monkeypatch.setattr(sync, "get_pool", AsyncMock(return_value=pool))
        monkeypatch.setattr(sync, "cleanup_token_roles", AsyncMock())
        monkeypatch.setattr(sync, "_cursor", None)
        monkeypatch.setattr(sync, "_cursor_issuer", None)
        monkeypatch.setattr(sync, "_scan_page_size", sync._PAGE_SIZE)
        monkeypatch.setattr(sync, "_sweep_error", None)
        monkeypatch.setattr(sync, "_last_sweep_error", None)
        monkeypatch.setattr(sync.settings, "auth_mode", "sso")
        monkeypatch.setattr(sync.settings, "keycloak_server_url", "https://sso.example.test")
        monkeypatch.setattr(sync.settings, "keycloak_realm", "akb")
        monkeypatch.setattr(sync.settings, "sso_account_sync_enabled", True)
        control = SimpleNamespace(config=SimpleNamespace(broker_issuer=sync.settings.keycloak_issuer),
                                  read_managed_account_states=AsyncMock())
        monkeypatch.setattr(sync, "get_keycloak_provider_control", lambda: control)
        uid, eid, tid, vid, epoch = (uuid.uuid4() for _ in range(5))
        eid = uuid.UUID(int=1)
        monkeypatch.setattr(sync.settings, "auth_runtime_generation", 1)
        monkeypatch.setattr(sync.settings, "sso_session_epoch", epoch)
        await pool.execute("""INSERT INTO users(id,username,email,password_hash,auth_provider)
            VALUES($1,'managed-fixture','managed@example.test','!keycloak-sso!','keycloak')""", uid)
        await pool.execute("INSERT INTO external_identities(id,user_id,issuer,subject) VALUES($1,$2,$3,'subject')",
                           eid, uid, sync.settings.keycloak_issuer)
        await pool.execute("INSERT INTO tokens(id,user_id,name,token_hash,token_prefix) VALUES($1,$2,'fixture','hash','prefix')", tid, uid)
        await pool.execute("INSERT INTO vaults(id,name,owner_id,git_path) VALUES($1,'managed-vault',$2,'/unused')", vid, uid)
        await pool.execute("""INSERT INTO auth_runtime_state(singleton,runtime_generation,auth_mode,sso_session_epoch)
            VALUES(true,1,'sso',$1) ON CONFLICT(singleton) DO UPDATE SET auth_mode='sso',sso_session_epoch=$1""", epoch)
        await pool.execute("""INSERT INTO sso_browser_sessions(id,session_epoch,token_hash,csrf_token_hash,user_id,
            external_identity_id,identity_issuer,identity_subject,keycloak_sid,token_envelope,
            access_expires_at,refresh_expires_at,idle_expires_at,absolute_expires_at)
            VALUES($1,$2,repeat('a',64),repeat('b',64),$3,$4,$5,'subject','sid',repeat('x',32),
                now()+interval '1 hour',now()+interval '1 hour',now()+interval '1 hour',now()+interval '1 hour')""",
            uuid.uuid4(), epoch, uid, eid, sync.settings.keycloak_issuer)
        await pool.execute("""INSERT INTO admin_browser_sessions(session_epoch,token_hash,csrf_token_hash,user_id,
            external_identity_id,identity_issuer,identity_subject,keycloak_sid,expires_at)
            VALUES($1,repeat('c',64),repeat('d',64),$2,$3,$4,'subject','admin-sid',now()+interval '1 hour')""",
            epoch, uid, eid, sync.settings.keycloak_issuer)
        yield pool, control, uid, eid, tid, vid


@pytest.mark.parametrize("state", ["disabled", "missing"])
async def test_external_state_revokes_credentials_preserving_vault_and_binding(fixture, state):
    pool, control, uid, eid, tid, vid = fixture
    control.read_managed_account_states.return_value = {"subject": state}
    await sync.process_once()
    assert await pool.fetchval("SELECT account_status FROM users WHERE id=$1", uid) == "suspended"
    assert await pool.fetchval("SELECT owner_id FROM vaults WHERE id=$1", vid) == uid
    assert await pool.fetchval("SELECT user_id FROM external_identities WHERE id=$1", eid) == uid
    assert await pool.fetchval("SELECT count(*) FROM tokens WHERE user_id=$1", uid) == 0
    assert await pool.fetchval("SELECT count(*) FROM sso_browser_sessions WHERE user_id=$1", uid) == 0
    assert await pool.fetchval("SELECT count(*) FROM admin_browser_sessions WHERE user_id=$1", uid) == 0
    assert await pool.fetchval("SELECT user_id FROM account_token_cleanup WHERE token_id=$1", tid) == uid
    assert (await sync.pending_stats())["ready"]
    control.read_managed_account_states.return_value = {"subject": "active"}
    await sync.process_once()
    assert await pool.fetchval("SELECT account_status FROM users WHERE id=$1", uid) == "suspended"


@pytest.mark.parametrize("mode", ["outage", "binding_changed", "authority_changed", "runtime_changed", "recovery", "disabled"])
async def test_uncertain_changed_or_protected_accounts_are_never_suspended(fixture, monkeypatch, mode):
    pool, control, uid, eid, tid, vid = fixture
    async def read(subjects):
        if mode == "outage":
            raise ProviderControlError("account_sync_user_unavailable")
        if mode == "binding_changed":
            await pool.execute("UPDATE external_identities SET subject='replacement' WHERE id=$1", eid)
        if mode == "authority_changed":
            monkeypatch.setattr(sync.settings, "keycloak_realm", "other")
        if mode == "runtime_changed":
            await pool.execute("UPDATE auth_runtime_state SET runtime_generation=2 WHERE singleton")
        return {"subject": "disabled"}
    control.read_managed_account_states.side_effect = read
    if mode == "recovery":
        await pool.execute("UPDATE users SET is_admin=true,is_recovery_admin=true WHERE id=$1", uid)
    if mode == "disabled":
        monkeypatch.setattr(sync.settings, "sso_account_sync_enabled", False)
    await sync.process_once()
    assert await pool.fetchval("SELECT account_status FROM users WHERE id=$1", uid) == "active"
    assert await pool.fetchval("SELECT count(*) FROM tokens WHERE id=$1", tid) == 1
    if mode == "outage":
        assert (await sync.pending_stats())["last_error_code"] == "account_sync_user_unavailable"
    if mode in {"disabled", "recovery"}:
        control.read_managed_account_states.assert_not_called()


async def test_pages_are_bounded_and_sweep_progresses(fixture, monkeypatch):
    pool, control, uid, eid, tid, vid = fixture
    monkeypatch.setattr(sync, "_PAGE_SIZE", 2)
    for number in range(2):
        await pool.execute("INSERT INTO external_identities(user_id,issuer,subject) VALUES($1,$2,$3)",
                           uid, sync.settings.keycloak_issuer, f"subject-{number}")
    control.read_managed_account_states.side_effect = lambda subjects: {subject: "active" for subject in subjects}
    assert await sync.process_once() == 0
    assert await sync.process_once() == 0
    calls = [call.args[0] for call in control.read_managed_account_states.call_args_list]
    assert list(map(len, calls)) == [2, 1]
    assert set(calls[0] + calls[1]) == {"subject", "subject-0", "subject-1"}
    assert sync._cursor is None


async def test_audit_failure_rolls_back_account_and_all_credentials(fixture, monkeypatch):
    pool, control, uid, eid, tid, vid = fixture
    control.read_managed_account_states.return_value = {"subject": "disabled"}
    monkeypatch.setattr(sync, "emit_event", AsyncMock(side_effect=RuntimeError("fixture audit failure")))
    await sync.process_once()
    assert await pool.fetchval("SELECT account_status FROM users WHERE id=$1", uid) == "active"
    for table in ("tokens", "sso_browser_sessions", "admin_browser_sessions"):
        assert await pool.fetchval(f"SELECT count(*) FROM {table} WHERE user_id=$1", uid) == 1
    assert (await sync.pending_stats())["last_error_code"] == "RuntimeError"


@pytest.mark.parametrize("failure", [ProviderControlError("account_sync_user_unavailable"), TimeoutError()])
async def test_failed_page_does_not_starve_later_or_neighbor_accounts(fixture, failure):
    pool, control, uid, eid, tid, vid = fixture
    targets = []
    for number in range(2, 27):
        target = uuid.uuid4()
        targets.append(target)
        await pool.execute("""INSERT INTO users(id,username,email,password_hash,auth_provider)
            VALUES($1,$2,$3,'!keycloak-sso!','keycloak')""", target, f"user-{number}", f"user-{number}@example.test")
        await pool.execute("INSERT INTO external_identities(id,user_id,issuer,subject) VALUES($1,$2,$3,$4)",
                           uuid.UUID(int=number), target, sync.settings.keycloak_issuer, f"subject-{number}")

    async def read(subjects):
        if "subject" in subjects:
            raise failure
        return {subject: "disabled" for subject in subjects}
    control.read_managed_account_states.side_effect = read
    await sync.process_once()  # Failed first 25 identities: no mutation.
    assert sync._scan_page_size == 1
    assert await pool.fetchval("SELECT account_status FROM users WHERE id=$1", targets[0]) == "active"
    await sync.process_once()  # The 26th identity must be reached despite the error.
    assert await pool.fetchval("SELECT account_status FROM users WHERE id=$1", targets[-1]) == "suspended"
    assert not (await sync.pending_stats())["ready"]
    await sync.process_once()  # End of sweep.
    await sync.process_once()  # Retry the poison identity, by itself.
    await sync.process_once()  # Its neighbor in the original failed page now succeeds.
    assert await pool.fetchval("SELECT account_status FROM users WHERE id=$1", targets[0]) == "suspended"
    assert await pool.fetchval("SELECT account_status FROM users WHERE id=$1", uid) == "active"
    assert await pool.fetchval("SELECT count(*) FROM tokens WHERE id=$1", tid) == 1
    assert not (await sync.pending_stats())["ready"]
    control.read_managed_account_states.side_effect = lambda subjects: {subject: "active" for subject in subjects}
    for _ in range(60):
        await sync.process_once()
        if (await sync.pending_stats())["ready"]:
            break
    assert (await sync.pending_stats())["ready"]
    assert sync._scan_page_size == 25
    assert any(call.args[0] == ("subject",) for call in control.read_managed_account_states.call_args_list)


async def test_restart_keeps_durable_error_until_full_clean_sweep(fixture, monkeypatch):
    pool, control, uid, eid, tid, vid = fixture
    monkeypatch.setattr(sync, "_PAGE_SIZE", 1)
    await sync._record(error="account_sync_user_unavailable", checked=0, suspended=0)
    control.read_managed_account_states.return_value = {"subject": "active"}
    await sync.process_once()  # Fresh process, one healthy page is not a full sweep.
    assert not (await sync.pending_stats())["ready"]
    assert (await sync.pending_stats())["last_error_code"] == "account_sync_user_unavailable"
    await sync.process_once()  # End of a fully clean sweep clears the durable error.
    assert (await sync.pending_stats())["ready"]


async def test_failed_partial_final_page_restarts_in_individual_mode(fixture, monkeypatch):
    pool, control, uid, eid, tid, vid = fixture
    monkeypatch.setattr(sync, "_PAGE_SIZE", 2)
    for number in (2, 3):
        await pool.execute("INSERT INTO external_identities(id,user_id,issuer,subject) VALUES($1,$2,$3,$4)",
                           uuid.UUID(int=number), uid, sync.settings.keycloak_issuer, f"subject-{number}")

    async def read(subjects):
        if "subject-3" in subjects:
            raise ProviderControlError("account_sync_user_unavailable")
        return {subject: "active" for subject in subjects}
    control.read_managed_account_states.side_effect = read
    await sync.process_once()
    await sync.process_once()  # A partial final page fails and closes this sweep.
    assert sync._cursor is None
    assert sync._scan_page_size == 1
    await sync.process_once()  # Next sweep starts at the first identity, individually.
    assert control.read_managed_account_states.call_args.args[0] == ("subject",)
    assert not (await sync.pending_stats())["ready"]
