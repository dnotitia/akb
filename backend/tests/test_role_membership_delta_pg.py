"""Actual PG role-switch oracles for catalog membership convergence."""
import asyncio
import os
import uuid
from contextlib import asynccontextmanager

import asyncpg
import pytest

from app.services.role_sync import ReconcileReport, RoleStateDiff, RoleSync, _is_catalog_role, user_role_name, vault_group_role_name
from tests.test_boot_schema_serialization_pg import _empty_database

pytestmark = pytest.mark.asyncio


@asynccontextmanager
async def _fixture():
    async with _empty_database() as dsn:
        conn = await asyncpg.connect(dsn)
        pool = await asyncpg.create_pool(dsn, min_size=1, max_size=1)
        owner, member, vault = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
        users = [user_role_name(x) for x in (owner, member)]
        groups = [vault_group_role_name(vault, x) for x in ('reader', 'writer', 'admin')]
        external = 'operator_fixture_' + uuid.uuid4().hex
        try:
            await conn.execute("""CREATE TABLE users(id uuid PRIMARY KEY, account_status text, is_admin boolean DEFAULT false); CREATE TABLE tokens(id uuid, user_id uuid, vault_scope jsonb, expires_at timestamptz); CREATE TABLE vaults(id uuid PRIMARY KEY, owner_id uuid, status text, name text, public_access text DEFAULT 'none', description text, updated_at timestamptz); CREATE TABLE vault_access(vault_id uuid,user_id uuid,role text); CREATE TABLE fixture_private(id int); INSERT INTO fixture_private VALUES(42)""")
            await conn.execute("INSERT INTO users(id,account_status) VALUES($1,'active'),($2,'active')", owner, member)
            await conn.execute("INSERT INTO vaults(id,owner_id,status,name) VALUES($1,$2,'archived',$3)", vault, owner, str(vault))
            for role in users + groups + [external]:
                await conn.execute(f'CREATE ROLE "{role}" NOLOGIN')
            await conn.execute(f'GRANT "{groups[0]}" TO "{groups[1]}"; GRANT "{groups[1]}" TO "{groups[2]}"; GRANT SELECT ON fixture_private TO "{groups[0]}"; GRANT "{groups[2]}" TO "{users[0]}"')
            yield conn, RoleSync(pool), owner, member, vault, users, groups, external
        finally:
            await pool.close()
            for role in users + [external] + list(reversed(groups)):
                await conn.execute(f'DROP OWNED BY "{role}"; DROP ROLE IF EXISTS "{role}"')
            await conn.close()


async def _can_read(conn, role):
    try:
        async with conn.transaction():
            await conn.execute(f'SET LOCAL ROLE "{role}"')
            return await conn.fetchval('SELECT id FROM fixture_private') == 42
    except asyncpg.InsufficientPrivilegeError:
        return False


async def test_downgrade_removes_stale_admin_and_diff_reports_it():
    async with _fixture() as (conn, sync, owner, member, vault, users, groups, external):
        await conn.execute("INSERT INTO vault_access VALUES($1,$2,'reader')", vault, member)
        await conn.execute(f'GRANT "{groups[2]}" TO "{users[1]}"')
        diff = RoleStateDiff()
        await sync._diff_memberships(conn, diff)
        assert diff.stale_memberships == [{'vault_id': str(vault), 'user_id': str(member), 'scope': 'admin'}]
        report = ReconcileReport()
        await sync._reconcile_memberships(conn, report)
        assert not report.errors
        assert report.grants_removed == 1
        assert await conn.fetchval('SELECT pg_has_role($1,$2,$3)', users[1], groups[2], 'MEMBER') is False
        assert await _can_read(conn, users[1])


async def test_absent_catalog_membership_revokes_actual_sql_access():
    async with _fixture() as (conn, sync, owner, member, vault, users, groups, external):
        await conn.execute(f'GRANT "{groups[2]}" TO "{users[1]}"')
        assert await _can_read(conn, users[1])
        report = ReconcileReport()
        await sync._reconcile_memberships(conn, report)
        assert not report.errors
        assert not await _can_read(conn, users[1])
        assert await _can_read(conn, users[0])


async def test_owner_archived_hierarchy_and_operator_membership_survive():
    async with _fixture() as (conn, sync, owner, member, vault, users, groups, external):
        # A leftover weaker catalog row must not replace owner's admin authority.
        await conn.execute("INSERT INTO vault_access VALUES($1,$2,'reader')", vault, owner)
        await conn.execute(f'GRANT "{groups[1]}" TO "{external}"; GRANT "{external}" TO "{users[1]}"')
        report = ReconcileReport()
        await sync._reconcile_memberships(conn, report)
        assert not report.errors
        assert await conn.fetchval('SELECT pg_has_role($1,$2,$3)', users[0], groups[2], 'MEMBER')
        assert await conn.fetchval('SELECT pg_has_role($1,$2,$3)', groups[2], groups[0], 'MEMBER')
        assert await conn.fetchval('SELECT pg_has_role($1,$2,$3)', users[1], external, 'MEMBER')
        assert await conn.fetchval('SELECT pg_has_role($1,$2,$3)', external, groups[1], 'MEMBER')
        assert await _can_read(conn, users[0])
        diff = RoleStateDiff()
        await sync._diff_memberships(conn, diff)
        assert not diff.missing_memberships and not diff.stale_memberships


async def test_repeated_convergence_applies_no_membership_ddl_or_warning():
    async with _fixture() as (conn, sync, owner, member, vault, users, groups, external):
        await conn.execute("INSERT INTO vault_access VALUES($1,$2,'writer')", vault, member)
        report = ReconcileReport()
        await sync._reconcile_memberships(conn, report)
        messages = []
        conn.add_log_listener(lambda connection, message: messages.append(message))
        second = ReconcileReport()
        await sync._reconcile_memberships(conn, second)
        for role in groups:
            await sync._revoke_membership_if_present(conn, role, external)
        await sync._grant_membership(conn, groups[1], users[1])
        await asyncio.sleep(0)
        assert not second.errors
        assert second.grants_added == second.grants_removed == 0
        assert not messages


async def test_waiting_reconcile_observes_committed_revoke_not_pre_lock_snapshot(monkeypatch):
    from app.services.role_authority import role_authority_transaction
    async with _fixture() as (conn, sync, owner, member, vault, users, groups, external):
        await conn.execute("INSERT INTO vault_access VALUES($1,$2,'reader')", vault, member)
        await conn.execute(f'GRANT "{groups[0]}" TO "{users[1]}"')
        async def no_op(conn, report):
            pass
        for name in ('_reconcile_user_roles', '_reconcile_vault_roles', '_reconcile_table_grants', '_reconcile_public_access', '_reconcile_token_roles'):
            monkeypatch.setattr(sync, name, no_op)
        task = None
        try:
            async with role_authority_transaction(conn):
                await conn.execute('DELETE FROM vault_access WHERE vault_id=$1 AND user_id=$2', vault, member)
                await conn.execute(f'REVOKE "{groups[0]}" FROM "{users[1]}"')
                task = asyncio.create_task(sync.reconcile_from_catalog())
                async with asyncio.timeout(3):
                    while not await conn.fetchval("SELECT EXISTS(SELECT 1 FROM pg_locks WHERE locktype='advisory' AND NOT granted)"):
                        await asyncio.sleep(0.005)
            report = await asyncio.wait_for(task, 3)
            assert not report.errors
            assert not await _can_read(conn, users[1])
            assert not await conn.fetchval('SELECT pg_has_role($1,$2,$3)', users[1], groups[0], 'MEMBER')
        finally:
            if task is not None and not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)


async def test_delayed_grant_callback_cannot_reinstate_revoked_access_with_pool_one():
    async with _fixture() as (conn, sync, owner, member, vault, users, groups, external):
        # Callback was produced by an old writer grant, catalog has since revoked.
        await asyncio.wait_for(sync.on_grant(vault, member, 'admin'), 3)
        assert not sync.metrics.total_failures()
        assert not await _can_read(conn, users[1])
        await conn.execute("INSERT INTO vault_access VALUES($1,$2,'reader')", vault, member)
        # A delayed revoke similarly cannot remove a newly committed reader.
        await asyncio.wait_for(sync.on_revoke(vault, member), 3)
        assert not sync.metrics.total_failures()
        assert await _can_read(conn, users[1])
        assert not await conn.fetchval('SELECT pg_has_role($1,$2,$3)', users[1], groups[2], 'MEMBER')


async def test_other_grantor_residual_rolls_back_catalog_revoke():
    from app.services.role_authority import role_authority_transaction
    async with _fixture() as (conn, sync, owner, member, vault, users, groups, external):
        await conn.execute("INSERT INTO vault_access VALUES($1,$2,'admin')", vault, member)
        await conn.execute(f'GRANT "{groups[2]}" TO "{external}" WITH ADMIN TRUE')
        async with conn.transaction():
            await conn.execute(f'SET LOCAL ROLE "{external}"')
            await conn.execute(f'GRANT "{groups[2]}" TO "{users[1]}"')
        with pytest.raises(RuntimeError, match='another grantor'):
            async with role_authority_transaction(conn):
                await conn.execute('DELETE FROM vault_access WHERE vault_id=$1 AND user_id=$2', vault, member)
                await sync.sync_vault_user_in_conn(conn, vault, member)
        assert await conn.fetchval('SELECT role FROM vault_access WHERE vault_id=$1 AND user_id=$2', vault, member) == 'admin'
        assert await _can_read(conn, users[1])
        report = ReconcileReport()
        await conn.execute('DELETE FROM vault_access WHERE vault_id=$1 AND user_id=$2', vault, member)
        await sync._reconcile_memberships(conn, report)
        assert report.errors and report.grants_removed == 0
        diff = RoleStateDiff()
        await sync._diff_memberships(conn, diff)
        assert diff.stale_memberships


@pytest.mark.parametrize('option', ['ADMIN TRUE', 'INHERIT FALSE', 'SET FALSE'])
async def test_noncanonical_grant_options_are_visible_and_refused(option):
    async with _fixture() as (conn, sync, owner, member, vault, users, groups, external):
        await conn.execute("INSERT INTO vault_access VALUES($1,$2,'reader')", vault, member)
        await conn.execute(f'GRANT "{groups[0]}" TO "{users[1]}" WITH {option}')
        diff = RoleStateDiff()
        await sync._diff_memberships(conn, diff)
        assert diff.invalid_membership_options == [{'vault_id': str(vault), 'user_id': str(member), 'scope': 'reader'}]
        with pytest.raises(RuntimeError, match='options differ'):
            await sync.sync_vault_user_in_conn(conn, vault, member)
        report = ReconcileReport()
        await sync._reconcile_memberships(conn, report)
        assert report.errors and not report.grants_added


async def test_foreign_public_membership_prevents_false_success_and_rolls_back():
    from app.services.role_authority import role_authority_transaction
    from app.services.role_sync import AUTHENTICATED_ROLE
    async with _fixture() as (conn, sync, owner, member, vault, users, groups, external):
        await sync._create_role_if_missing(conn, AUTHENTICATED_ROLE)
        await conn.execute("UPDATE vaults SET public_access='reader' WHERE id=$1", vault)
        await conn.execute(f'GRANT "{groups[0]}" TO "{external}" WITH ADMIN TRUE')
        async with conn.transaction():
            await conn.execute(f'SET LOCAL ROLE "{external}"')
            await conn.execute(f'GRANT "{groups[0]}" TO "{AUTHENTICATED_ROLE}"')
        with pytest.raises(RuntimeError, match='another grantor'):
            async with role_authority_transaction(conn):
                await conn.execute("UPDATE vaults SET public_access='none' WHERE id=$1", vault)
                await sync.on_public_access_change_in_conn(conn, vault, 'none')
        assert await conn.fetchval('SELECT public_access FROM vaults WHERE id=$1', vault) == 'reader'


async def test_foreign_token_membership_prevents_false_success_and_rolls_back():
    from app.models.vault_scope import VaultScope
    from app.services.role_authority import role_authority_transaction
    from app.services.role_sync import token_role_name
    import json
    async with _fixture() as (conn, sync, owner, member, vault, users, groups, external):
        tid = uuid.uuid4()
        token = token_role_name(tid)
        scope = VaultScope(prefixes=(), extra_vaults=frozenset({str(vault)}))
        await conn.execute("INSERT INTO vault_access VALUES($1,$2,'reader')", vault, member)
        await conn.execute('INSERT INTO tokens VALUES($1,$2,$3::jsonb,NULL)', tid, member, json.dumps(scope.to_db_json()))
        await conn.execute(f'CREATE ROLE "{token}" NOLOGIN; GRANT "{groups[0]}" TO "{external}" WITH ADMIN TRUE')
        try:
            async with conn.transaction():
                await conn.execute(f'SET LOCAL ROLE "{external}"')
                await conn.execute(f'GRANT "{groups[0]}" TO "{token}"')
            assert await _can_read(conn, token)
            with pytest.raises(RuntimeError, match='another grantor'):
                async with role_authority_transaction(conn):
                    await conn.execute('DELETE FROM vault_access WHERE vault_id=$1 AND user_id=$2', vault, member)
                    await sync.sync_vault_user_in_conn(conn, vault, member)
            assert await conn.fetchval('SELECT role FROM vault_access WHERE vault_id=$1 AND user_id=$2', vault, member) == 'reader'
        finally:
            await conn.execute(f'DROP OWNED BY "{token}"; DROP ROLE "{token}"')


async def test_full_reconcile_preserves_similarly_prefixed_operator_roles():
    # PostgreSQL roles are cluster-global. This full prune oracle must run
    # separately before other fixtures allocate UUID roles in another DB.
    if os.environ.get("AKB_ROLE_CLUSTER_EXCLUSIVE") != "1":
        pytest.skip("full role-prune oracle requires an exclusively owned PG cluster")
    async with _fixture() as (conn, sync, owner, member, vault, users, groups, external):
        managed = {
            row["rolname"] for row in await conn.fetch("SELECT rolname FROM pg_roles")
            if any(_is_catalog_role(row["rolname"], kind) for kind in ("user", "vault", "token"))
        }
        assert managed == set(users + groups), "foreign managed roles in exclusive cluster"
        suffix = uuid.uuid4().hex
        operator_roles = [f'akb_{kind}_operator_{suffix}' for kind in ('user', 'vault', 'token')]
        await conn.execute('CREATE TABLE vault_tables(vault_id uuid, name text)')
        physical = 'vt_' + str(vault).replace('-', '_') + '__oracle'
        await conn.execute(f'CREATE TABLE "{physical}"(id int); INSERT INTO "{physical}" VALUES(42)')
        await conn.execute("INSERT INTO vault_tables VALUES($1,'oracle')", vault)
        # Full reconciliation must really create a missing user and repair
        # missing table privileges; this keeps those full-pass oracles in
        # the exclusive cluster instead of shared-server domain tests.
        await conn.execute(f'DROP ROLE "{users[1]}"')
        assert not await conn.fetchval('SELECT EXISTS(SELECT 1 FROM pg_roles WHERE rolname=$1)', users[1])
        assert not await conn.fetchval("SELECT has_table_privilege($1,$2,'SELECT')", groups[0], physical)
        for role in operator_roles:
            await conn.execute(f'CREATE ROLE "{role}" NOLOGIN')
        try:
            report = await asyncio.wait_for(sync.reconcile_from_catalog(), 5)
            assert not report.errors
            assert report.user_roles_created == 1
            assert report.table_grants_applied == 1
            assert await conn.fetchval("SELECT pg_has_role($1,$2,'MEMBER')", users[1], 'akb_authenticated')
            assert await conn.fetchval("SELECT has_table_privilege($1,$2,'SELECT')", groups[0], physical)
            async with conn.transaction():
                await conn.execute(f'SET LOCAL ROLE "{users[0]}"')
                assert await conn.fetchval(f'SELECT id FROM "{physical}"') == 42
            assert await conn.fetchval('SELECT count(*) FROM pg_roles WHERE rolname=ANY($1::text[])', operator_roles) == 3
        finally:
            for role in operator_roles:
                await conn.execute(f'DROP OWNED BY "{role}"; DROP ROLE "{role}"')


async def test_stale_table_callback_cannot_grant_an_unregistered_table():
    async with _fixture() as (conn, sync, owner, member, vault, users, groups, external):
        await conn.execute('CREATE TABLE vault_tables(vault_id uuid, name text); CREATE TABLE vt_fixture__unregistered(id int)')
        await asyncio.wait_for(sync.on_table_create(vault, 'vt_fixture__unregistered'), 3)
        assert not sync.metrics.total_failures()
        assert not await conn.fetchval("SELECT has_table_privilege($1,'vt_fixture__unregistered','SELECT')", groups[0])


@pytest.mark.parametrize("setter", ["public", "metadata"])
async def test_public_mutation_rechecks_owner_after_waiting_for_authority(monkeypatch, setter):
    from app.exceptions import ForbiddenError
    from app.services import access_service
    from app.services.role_authority import role_authority_transaction

    async with _fixture() as (conn, sync, owner, member, vault, users, groups, external):
        async def get_pool():
            return sync.pool
        async def no_policy(vault_id, *, conn):
            return None
        monkeypatch.setattr(access_service, "get_pool", get_pool)
        monkeypatch.setattr(access_service.write_policy_repo, "get_policy", no_policy)
        monkeypatch.setattr(access_service, "get_role_sync", lambda: sync)
        await conn.execute("UPDATE vaults SET status='active' WHERE id=$1", vault)
        task = None
        try:
            async with role_authority_transaction(conn):
                call = (
                    access_service.set_public_access(str(owner), str(vault), "writer")
                    if setter == "public" else
                    access_service.update_vault_metadata(str(owner), str(vault), "stale owner", "writer")
                )
                task = asyncio.create_task(call)
                async with asyncio.timeout(3):
                    while not await conn.fetchval("SELECT EXISTS(SELECT 1 FROM pg_locks WHERE locktype='advisory' AND NOT granted)"):
                        await asyncio.sleep(0.005)
                await conn.execute("UPDATE vaults SET owner_id=$1 WHERE id=$2", member, vault)
            with pytest.raises(ForbiddenError, match="Requires 'owner'"):
                await asyncio.wait_for(task, 3)
            row = await conn.fetchrow("SELECT public_access, description FROM vaults WHERE id=$1", vault)
            assert row["public_access"] == "none"
            assert row["description"] is None
        finally:
            if task is not None and not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
