"""Unit-ish tests for RoleSync.

Talk to a real Postgres (mocks don't catch GRANT semantics) but stay
focused on RoleSync invariants — no service layer, no HTTP, no
lifecycle wiring. Each test acquires its own pool, applies init.sql
to a throwaway DB or the dev DB, and cleans up after itself.

DSN comes from ``AKB_TEST_DSN`` (default
``postgresql://akb:akb@localhost:15432/akb`` to match the dev override).
Skip the module if the DB isn't reachable — running ``pytest`` without
a local Postgres is still a no-op rather than a failure.
"""

from __future__ import annotations

import os
import uuid
from pathlib import Path

import asyncpg
import pytest
import pytest_asyncio

from app.services.role_sync import (
    AUTHENTICATED_ROLE,
    HookMetrics,
    RoleSync,
    _is_safe_pg_table_name,
    _public_access_scope,
    user_role_name,
    vault_group_role_name,
)


_DSN = os.environ.get(
    "AKB_TEST_DSN",
    "postgresql://akb:akb@localhost:15432/akb",
)


async def _can_connect(dsn: str) -> bool:
    try:
        conn = await asyncpg.connect(dsn, timeout=2.0)
    except (OSError, asyncpg.PostgresError):
        return False
    await conn.close()
    return True


@pytest_asyncio.fixture
async def pool():
    if not await _can_connect(_DSN):
        pytest.skip(f"Postgres not reachable at {_DSN}")
    pool = await asyncpg.create_pool(dsn=_DSN, min_size=1, max_size=4)
    init_sql = (
        Path(__file__).resolve().parents[1] / "app" / "db" / "init.sql"
    ).read_text()
    async with pool.acquire() as conn:
        await conn.execute(init_sql)
    try:
        yield pool
    finally:
        await pool.close()


@pytest_asyncio.fixture
async def role_sync(pool):
    rs = RoleSync(pool)
    yield rs


@pytest_asyncio.fixture
async def cleanup_roles(pool):
    """Capture every akb_user_*/akb_vault_* role name created during a
    test (via the suffix bound to the fixture's uuid prefix) and drop
    them on teardown. Tests that interact with RoleSync only touch
    roles tagged with their own prefix."""
    prefix = f"_t_{uuid.uuid4().hex[:8]}_"
    created: list[str] = []
    yield created, prefix
    if not created:
        return
    async with pool.acquire() as conn:
        for role in created:
            try:
                await conn.execute(f'DROP OWNED BY "{role}"')
            except asyncpg.exceptions.UndefinedObjectError:
                pass
            except Exception:
                pass
            try:
                await conn.execute(f'DROP ROLE IF EXISTS "{role}"')
            except Exception:
                pass


# ── Pure-function helpers ─────────────────────────────────────


def test_user_role_name_underscored():
    uid = uuid.UUID("00000000-0000-0000-0000-000000000001")
    assert user_role_name(uid) == "akb_user_00000000_0000_0000_0000_000000000001"
    assert user_role_name(str(uid)) == "akb_user_00000000_0000_0000_0000_000000000001"


def test_vault_group_role_name_per_scope():
    vid = uuid.UUID("11111111-2222-3333-4444-555555555555")
    assert vault_group_role_name(vid, "reader").endswith("_reader")
    assert vault_group_role_name(vid, "writer").endswith("_writer")
    assert vault_group_role_name(vid, "admin").endswith("_admin")


def test_vault_group_role_name_rejects_bad_scope():
    vid = uuid.uuid4()
    with pytest.raises(ValueError):
        vault_group_role_name(vid, "owner")
    with pytest.raises(ValueError):
        vault_group_role_name(vid, "")


def test_is_safe_pg_table_name():
    # Valid shapes (matches `pg_table_name()` output).
    assert _is_safe_pg_table_name("vt_a__b")
    assert _is_safe_pg_table_name("vt_my_vault__my_table")
    assert _is_safe_pg_table_name("vt_a1_b2__c3_d4")
    # Wrong prefix.
    assert not _is_safe_pg_table_name("foo__bar")
    # Missing separator.
    assert not _is_safe_pg_table_name("vt_foobar")
    # Bad chars (capitals, dashes, dots, quotes, semicolons).
    assert not _is_safe_pg_table_name("vt_A__b")
    assert not _is_safe_pg_table_name("vt_a-b__c")
    assert not _is_safe_pg_table_name("vt_a__b; DROP TABLE x")
    assert not _is_safe_pg_table_name('vt_a__b"--')
    # Overlong.
    assert not _is_safe_pg_table_name("vt_" + "a" * 30 + "__" + "b" * 30)


def test_public_access_scope_mapping():
    assert _public_access_scope("reader") == "reader"
    assert _public_access_scope("writer") == "writer"
    assert _public_access_scope("none") is None
    assert _public_access_scope("") is None
    assert _public_access_scope("admin") is None  # public_access can't be admin


def test_hook_metrics_counts():
    m = HookMetrics()
    assert m.total_failures() == 0
    m.record_failure("on_user_create")
    m.record_failure("on_user_create")
    m.record_failure("on_grant")
    assert m.total_failures() == 3
    snap = m.snapshot()
    assert snap["hook_failures_total"] == 3
    assert snap["hook_failures_by_name"] == {"on_user_create": 2, "on_grant": 1}


# ── PG-touching tests ─────────────────────────────────────────


@pytest.mark.asyncio
async def test_on_user_create_idempotent(pool, role_sync, cleanup_roles):
    created, prefix = cleanup_roles
    uid = uuid.UUID(f"00000000-0000-0000-0000-{uuid.uuid4().hex[12:24]}")
    role = user_role_name(uid)
    created.append(role)
    # Two consecutive calls — second is a no-op.
    await role_sync.on_user_create(uid)
    await role_sync.on_user_create(uid)
    async with pool.acquire() as conn:
        exists = await conn.fetchval(
            "SELECT 1 FROM pg_roles WHERE rolname = $1", role,
        )
        assert exists == 1
        # Member of akb_authenticated.
        is_member = await conn.fetchval(
            """
            SELECT 1 FROM pg_auth_members am
              JOIN pg_roles r ON r.oid = am.roleid
              JOIN pg_roles m ON m.oid = am.member
             WHERE r.rolname = $1 AND m.rolname = $2
            """,
            AUTHENTICATED_ROLE, role,
        )
        assert is_member == 1
    assert role_sync.metrics.total_failures() == 0


@pytest.mark.asyncio
async def test_on_vault_create_owner_gets_admin(pool, role_sync, cleanup_roles):
    """`on_vault_create(vault_id, owner_user_id=...)` grants admin
    to the owner role immediately — before any explicit on_grant."""
    created, _ = cleanup_roles
    vid = uuid.uuid4()
    uid = uuid.uuid4()
    for scope in ("reader", "writer", "admin"):
        created.append(vault_group_role_name(vid, scope))
    created.append(user_role_name(uid))

    await role_sync.on_vault_create(vid, owner_user_id=uid)

    async with pool.acquire() as conn:
        mem = await conn.fetchval(
            """
            SELECT 1 FROM pg_auth_members am
              JOIN pg_roles r ON r.oid = am.roleid
              JOIN pg_roles m ON m.oid = am.member
             WHERE r.rolname = $1 AND m.rolname = $2
            """,
            vault_group_role_name(vid, "admin"), user_role_name(uid),
        )
        assert mem == 1, "owner should be member of vault admin group"


@pytest.mark.asyncio
async def test_on_grant_clears_other_scopes_on_downgrade(
    pool, role_sync, cleanup_roles,
):
    created, _ = cleanup_roles
    vid = uuid.uuid4()
    uid = uuid.uuid4()
    for scope in ("reader", "writer", "admin"):
        created.append(vault_group_role_name(vid, scope))
    created.append(user_role_name(uid))

    await role_sync.on_vault_create(vid, owner_user_id=None)
    await role_sync.on_user_create(uid)
    await role_sync.on_grant(vid, uid, "writer")
    # Downgrade to reader: writer membership should be revoked.
    await role_sync.on_grant(vid, uid, "reader")

    async with pool.acquire() as conn:
        writer_mem = await conn.fetchval(
            """
            SELECT 1 FROM pg_auth_members am
              JOIN pg_roles r ON r.oid = am.roleid
              JOIN pg_roles m ON m.oid = am.member
             WHERE r.rolname = $1 AND m.rolname = $2
            """,
            vault_group_role_name(vid, "writer"), user_role_name(uid),
        )
        reader_mem = await conn.fetchval(
            """
            SELECT 1 FROM pg_auth_members am
              JOIN pg_roles r ON r.oid = am.roleid
              JOIN pg_roles m ON m.oid = am.member
             WHERE r.rolname = $1 AND m.rolname = $2
            """,
            vault_group_role_name(vid, "reader"), user_role_name(uid),
        )
        assert writer_mem is None, "writer should be revoked after downgrade"
        assert reader_mem == 1, "reader grant missing after downgrade"


@pytest.mark.asyncio
async def test_on_public_access_change_transitions(
    pool, role_sync, cleanup_roles,
):
    created, _ = cleanup_roles
    vid = uuid.uuid4()
    for scope in ("reader", "writer", "admin"):
        created.append(vault_group_role_name(vid, scope))
    await role_sync.on_vault_create(vid, owner_user_id=None)

    async def auth_has(scope: str) -> bool:
        async with pool.acquire() as conn:
            return await conn.fetchval(
                """
                SELECT 1 FROM pg_auth_members am
                  JOIN pg_roles r ON r.oid = am.roleid
                  JOIN pg_roles m ON m.oid = am.member
                 WHERE r.rolname = $1 AND m.rolname = $2
                """,
                vault_group_role_name(vid, scope), AUTHENTICATED_ROLE,
            ) == 1

    # none → reader → writer → reader → none. Verify the wildcard
    # only holds the target scope at any moment.
    await role_sync.on_public_access_change(vid, "reader")
    assert await auth_has("reader")
    assert not await auth_has("writer")

    await role_sync.on_public_access_change(vid, "writer")
    assert await auth_has("writer")
    assert not await auth_has("reader")

    await role_sync.on_public_access_change(vid, "reader")
    assert await auth_has("reader")
    assert not await auth_has("writer")

    await role_sync.on_public_access_change(vid, "none")
    assert not await auth_has("reader")
    assert not await auth_has("writer")


@pytest.mark.asyncio
async def test_on_vault_delete_drops_group_roles(pool, role_sync, cleanup_roles):
    created, _ = cleanup_roles
    vid = uuid.uuid4()
    roles = [vault_group_role_name(vid, s) for s in ("reader", "writer", "admin")]
    created.extend(roles)
    await role_sync.on_vault_create(vid, owner_user_id=None)

    await role_sync.on_vault_delete(vid)
    async with pool.acquire() as conn:
        for r in roles:
            exists = await conn.fetchval(
                "SELECT 1 FROM pg_roles WHERE rolname = $1", r,
            )
            assert exists is None, f"role {r} should be dropped"


@pytest.mark.asyncio
async def test_diff_against_catalog_detects_drift(pool, role_sync, cleanup_roles):
    """Insert a fake users row, then check diff reports the missing PG role."""
    created, _ = cleanup_roles
    uid = uuid.uuid4()
    role = user_role_name(uid)
    created.append(role)
    async with pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO users (id, username, email, password_hash)
            VALUES ($1, $2, $3, 'x')
            """,
            uid, f"_t_drift_{uid.hex[:8]}", f"_t_drift_{uid.hex[:8]}@test",
        )
    try:
        diff = await role_sync.diff_against_catalog()
        assert role in diff.missing_user_roles
        assert not diff.is_clean()

        # After reconcile, the same diff is clean again for this user.
        await role_sync.reconcile_from_catalog()
        diff2 = await role_sync.diff_against_catalog()
        assert role not in diff2.missing_user_roles
        assert uid not in [
            uuid.UUID(u) if isinstance(u, str) else u
            for u in diff2.users_not_in_authenticated
        ]
    finally:
        async with pool.acquire() as conn:
            await conn.execute("DELETE FROM users WHERE id = $1", uid)


@pytest.mark.asyncio
async def test_metrics_records_failure_on_invalid_scope(pool, role_sync):
    starting = role_sync.metrics.total_failures()
    await role_sync.on_grant(uuid.uuid4(), uuid.uuid4(), "owner")
    assert role_sync.metrics.total_failures() == starting + 1
    assert role_sync.metrics.failures.get("on_grant", 0) >= 1


@pytest.mark.asyncio
async def test_diff_detects_missing_table_grant(pool, role_sync, cleanup_roles):
    """Manually REVOKE SELECT from a vault reader role on a vt_* table;
    diff_against_catalog must surface the exact (table, scope) missing
    so an operator can act before reconcile."""
    created, _ = cleanup_roles

    # Wire up a synthetic vault + table the same way create_vault /
    # create_table would. Raw SQL keeps the test independent of the
    # service layer.
    owner_id = uuid.uuid4()
    vid = uuid.uuid4()
    vname = f"_t_diff_tg_{vid.hex[:8]}"
    tname = f"items_{vid.hex[:6]}"
    pg_name = f"vt_{vname}__{tname}"
    for scope in ("reader", "writer", "admin"):
        created.append(vault_group_role_name(vid, scope))
    created.append(user_role_name(owner_id))

    async with pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO users (id, username, email, password_hash)
            VALUES ($1, $2, $3, 'x')
            """,
            owner_id, f"_t_diff_owner_{owner_id.hex[:8]}",
            f"_t_diff_owner_{owner_id.hex[:8]}@test",
        )
        await conn.execute(
            """
            INSERT INTO vaults (id, name, description, git_path, owner_id)
            VALUES ($1, $2, '', $3, $4)
            """,
            vid, vname, f"/tmp/{vname}.git", owner_id,
        )
        await conn.execute(
            f"""
            CREATE TABLE "{pg_name}" (
                id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
                label TEXT
            )
            """
        )
        await conn.execute(
            """
            INSERT INTO vault_tables (id, vault_id, name, description, columns)
            VALUES ($1, $2, $3, '', '[]'::jsonb)
            """,
            uuid.uuid4(), vid, tname,
        )

    try:
        await role_sync.on_user_create(owner_id)
        await role_sync.on_vault_create(vid, owner_user_id=owner_id)
        await role_sync.on_table_create(vid, pg_name)

        # Fresh state: no drift for this table.
        diff = await role_sync.diff_against_catalog()
        for tg in diff.missing_table_grants:
            assert tg["table"] != pg_name, (
                f"unexpected drift on freshly-created table: {tg}"
            )

        # Now sabotage: manually revoke SELECT from the reader role.
        async with pool.acquire() as conn:
            await conn.execute(
                f'REVOKE SELECT ON "{pg_name}" FROM '
                f'"{vault_group_role_name(vid, "reader")}"'
            )

        diff2 = await role_sync.diff_against_catalog()
        hits = [
            tg for tg in diff2.missing_table_grants
            if tg["table"] == pg_name and tg["scope"] == "reader"
        ]
        assert hits, (
            f"diff failed to detect missing SELECT on reader; "
            f"missing_table_grants={diff2.missing_table_grants}"
        )
        assert "SELECT" in hits[0]["missing_privileges"]
        assert not diff2.is_clean()

        # Reconcile should re-apply the GRANT.
        await role_sync.reconcile_from_catalog()
        diff3 = await role_sync.diff_against_catalog()
        residual = [
            tg for tg in diff3.missing_table_grants if tg["table"] == pg_name
        ]
        assert not residual, (
            f"reconcile did not heal the GRANT: residual={residual}"
        )
    finally:
        async with pool.acquire() as conn:
            await conn.execute(f'DROP TABLE IF EXISTS "{pg_name}"')
            await conn.execute("DELETE FROM vault_tables WHERE vault_id = $1", vid)
            await conn.execute("DELETE FROM vaults WHERE id = $1", vid)
            await conn.execute("DELETE FROM users WHERE id = $1", owner_id)
