"""The seven acceptance gates for source-keyed grant contributions.

Written before the implementation and expected to be red at `6db98bf`, which is
the point: gate 1 is impossible to satisfy against a single-row `vault_access`,
and a gate that cannot fail proves nothing.

Live PostgreSQL, because every claim here is about SQL semantics — a row that
survives, a role that is derived rather than stored, a stale write that is a
no-op. A fake connection would be asserting the test's own model.
"""

from __future__ import annotations

import importlib.util
import os
import re
import uuid
from contextlib import asynccontextmanager
from pathlib import Path

import asyncpg
import pytest

from app.exceptions import ForbiddenError, ValidationError
from app.services import access_service
from app.services import access_contributions as contrib

pytestmark = pytest.mark.asyncio

_BACKEND = Path(__file__).resolve().parents[1]
_INIT_SQL = (_BACKEND / "app" / "db" / "init.sql").read_text()
_MIGRATION_085 = (
    _BACKEND / "app" / "db" / "migrations" / "085_vault_access_contributions.py"
)
_MIGRATION_HEAD = (
    _BACKEND / "app" / "db" / "migrations" / "093_external_git_retirement.py"
)
_MIGRATIONS_DIR = _BACKEND / "app" / "db" / "migrations"


def _registered_migrations() -> list[Path]:
    """Every migration, in the order `postgres.py` registers them.

    Reading the registry rather than listing the directory keeps this database
    the same shape as a real one: `check_vault_access` reads the vault write
    policy and `emit_event` writes the outbox, and neither table is in
    `init.sql`. Picking migrations by hand is how a test database quietly stops
    resembling the thing under test.
    """
    registry = (_BACKEND / "app" / "db" / "postgres.py").read_text()
    names = re.findall(r'"(\d{3}_[a-z0-9_]+\.py)"', registry)
    seen: set[str] = set()
    ordered: list[Path] = []
    for name in names:
        if name in seen:
            continue
        seen.add(name)
        ordered.append(_MIGRATIONS_DIR / name)
    assert ordered, "no migrations found in the registry"
    assert ordered[-1].name == _MIGRATION_HEAD.name, (
        "the expected migration must be last for this test to be measuring "
        f"the current head; registry ends at {ordered[-1].name}"
    )
    return ordered
_DSN = os.environ.get(
    "AKB_TEST_DSN",
    "postgresql://akb:akb@localhost:15432/akb",  # pragma: allowlist secret
)


def _database_dsn(name: str) -> str:
    base, _ = _DSN.rsplit("/", 1)
    return f"{base}/{name}"


def _load_migration(path: Path):
    spec = importlib.util.spec_from_file_location(path.stem, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


async def _can_connect() -> bool:
    try:
        conn = await asyncpg.connect(_DSN, timeout=2)
    except (OSError, asyncpg.PostgresError):
        return False
    await conn.close()
    return True


@asynccontextmanager
async def _fresh_database():
    if not await _can_connect():
        if os.environ.get("REQUIRE_REAL_PG") == "1":
            pytest.fail(f"Required PostgreSQL is not reachable at {_DSN}")
        pytest.skip(f"Postgres not reachable at {_DSN}")
    admin = await asyncpg.connect(_DSN)
    name = f"akb_contrib_{uuid.uuid4().hex[:12]}"
    await admin.execute(f'CREATE DATABASE "{name}"')
    pool = await asyncpg.create_pool(_database_dsn(name), min_size=1, max_size=8)
    try:
        async with pool.acquire() as conn:
            await conn.execute(_INIT_SQL)
            for path in _registered_migrations():
                await _load_migration(path).migrate(conn=conn)
        yield pool
    finally:
        await pool.close()
        await admin.execute(f'DROP DATABASE "{name}" WITH (FORCE)')
        await admin.close()


class _RecordingRoleSync:
    """What the PG-native layer was told to do, so gate 5 can read it back."""

    def __init__(self) -> None:
        self.calls: list[tuple] = []

    async def on_grant(self, vault_id, user_id, scope):
        self.calls.append(("grant", str(vault_id), str(user_id), scope))

    async def on_revoke(self, vault_id, user_id):
        self.calls.append(("revoke", str(vault_id), str(user_id)))


@pytest.fixture
async def env(monkeypatch):
    async with _fresh_database() as pool:
        role_sync = _RecordingRoleSync()
        monkeypatch.setattr(access_service, "get_pool", lambda: pool)
        monkeypatch.setattr(access_service, "get_role_sync", lambda: role_sync)
        yield _Env(pool, role_sync)


class _Env:
    def __init__(self, pool, role_sync):
        self.pool = pool
        self.role_sync = role_sync

    async def user(self, label: str, *, is_admin: bool = False) -> uuid.UUID:
        async with self.pool.acquire() as conn:
            return await conn.fetchval(
                """
                INSERT INTO users (username, email, password_hash, is_admin)
                VALUES ($1, $2, 'fixture', $3) RETURNING id
                """,
                label, f"{label}@example.invalid", is_admin,
            )

    async def vault(self, name: str, owner_id: uuid.UUID) -> uuid.UUID:
        async with self.pool.acquire() as conn:
            return await conn.fetchval(
                "INSERT INTO vaults (name, git_path, owner_id) VALUES ($1, $2, $3) RETURNING id",
                name, f"/tmp/{name}.git", owner_id,
            )

    async def stored_role(self, vault_id, user_id) -> str | None:
        async with self.pool.acquire() as conn:
            return await conn.fetchval(
                "SELECT role FROM vault_access WHERE vault_id = $1 AND user_id = $2",
                vault_id, user_id,
            )

    async def bases(self, vault_id, user_id) -> dict[str, str]:
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT source_key, role FROM vault_access_contributions
                WHERE vault_id = $1 AND user_id = $2
                """,
                vault_id, user_id,
            )
        return {r["source_key"]: r["role"] for r in rows}


async def _fixture(env, label: str):
    """An owner, a vault they own, an admin who may grant, and a member."""
    suffix = uuid.uuid4().hex[:8]
    owner = await env.user(f"{label}-owner-{suffix}")
    member = await env.user(f"{label}-member-{suffix}")
    granter = await env.user(f"{label}-granter-{suffix}", is_admin=True)
    name = f"{label}-{suffix}"
    vault_id = await env.vault(name, owner)
    return name, vault_id, owner, member, granter


# ── Gate 1 ────────────────────────────────────────────────────────────


async def test_gate_1_independent_bases_survive_each_other(env):
    """direct reader + source writer → writer; the source leaves → reader.

    NOT no access. This is the whole proposal: at `6db98bf` step two overwrote
    the direct reader and step three deleted the row, so the person lost access
    nobody revoked.
    """
    name, vault_id, owner, member, granter = await _fixture(env, "gate1")
    member_name = await env.pool.fetchval(
        "SELECT username FROM users WHERE id = $1", member,
    )

    await access_service.grant_access(str(granter), name, member_name, "reader")
    assert await env.stored_role(vault_id, member) == "reader"

    await access_service.grant_access(
        str(granter), name, member_name, "writer", source_key="team:alpha",
    )
    assert await env.stored_role(vault_id, member) == "writer"

    result = await access_service.revoke_access(
        str(granter), name, member_name, source_key="team:alpha",
    )
    assert result["effective_role"] == "reader"
    assert await env.stored_role(vault_id, member) == "reader"
    assert await env.bases(vault_id, member) == {"direct": "reader"}


# ── Gate 2 ────────────────────────────────────────────────────────────


async def test_gate_2_the_last_removal_removes_the_row(env):
    name, vault_id, owner, member, granter = await _fixture(env, "gate2")
    member_name = await env.pool.fetchval(
        "SELECT username FROM users WHERE id = $1", member,
    )

    await access_service.grant_access(
        str(granter), name, member_name, "writer", source_key="team:alpha",
    )
    result = await access_service.revoke_access(
        str(granter), name, member_name, source_key="team:alpha",
    )
    assert result["effective_role"] is None
    assert await env.stored_role(vault_id, member) is None
    assert await env.bases(vault_id, member) == {}


# ── Gate 3 ────────────────────────────────────────────────────────────


async def test_gate_3_two_automated_sources_do_not_erase_each_other(env):
    name, vault_id, owner, member, granter = await _fixture(env, "gate3")
    member_name = await env.pool.fetchval(
        "SELECT username FROM users WHERE id = $1", member,
    )

    await access_service.grant_access(
        str(granter), name, member_name, "writer", source_key="team:alpha",
    )
    await access_service.grant_access(
        str(granter), name, member_name, "reader", source_key="policy:derived",
    )
    assert await env.stored_role(vault_id, member) == "writer"

    await access_service.revoke_access(
        str(granter), name, member_name, source_key="team:alpha",
    )
    assert await env.stored_role(vault_id, member) == "reader"
    assert await env.bases(vault_id, member) == {"policy:derived": "reader"}


# ── Gate 4 ────────────────────────────────────────────────────────────


async def test_gate_4_a_stale_revision_changes_nothing(env):
    """A grantor retrying after a lost response must not undo a newer write."""
    name, vault_id, owner, member, granter = await _fixture(env, "gate4")

    async with env.pool.acquire() as conn:
        await contrib.apply_contribution(
            conn, vault_id, member, "reader",
            source_key="team:alpha", granted_by=granter, revision=5,
        )
        await contrib.apply_contribution(
            conn, vault_id, member, "writer",
            source_key="team:alpha", granted_by=granter, revision=9,
        )
        replay = await contrib.apply_contribution(
            conn, vault_id, member, "reader",
            source_key="team:alpha", granted_by=granter, revision=5,
        )

    assert replay.applied is False
    assert replay.effective_role == "writer"
    assert await env.stored_role(vault_id, member) == "writer"

    async with env.pool.acquire() as conn:
        stale_removal = await contrib.remove_contribution(
            conn, vault_id, member, source_key="team:alpha", revision=5,
        )
    assert stale_removal.applied is False
    assert await env.stored_role(vault_id, member) == "writer"


# ── Gate 5 ────────────────────────────────────────────────────────────


async def test_gate_5_every_read_surface_agrees_after_a_downgrade(env):
    """The materialized row is the single answer, and the PG-native layer is
    told the EFFECTIVE role rather than the one the caller asked for.

    The predicates below are the ones the readers actually run: `search_service`
    and `m1_native_grep_service` test membership through `vault_access`, and
    `role_sync` reconciles PostgreSQL group membership from exactly
    `SELECT vault_id, user_id, role FROM vault_access`.
    """
    name, vault_id, owner, member, granter = await _fixture(env, "gate5")
    member_name = await env.pool.fetchval(
        "SELECT username FROM users WHERE id = $1", member,
    )

    await access_service.grant_access(str(granter), name, member_name, "reader")
    await access_service.grant_access(
        str(granter), name, member_name, "admin", source_key="team:alpha",
    )
    env.role_sync.calls.clear()

    await access_service.revoke_access(
        str(granter), name, member_name, source_key="team:alpha",
    )

    async with env.pool.acquire() as conn:
        # what a search sees
        visible = await conn.fetchval(
            "SELECT count(*) FROM vaults v WHERE v.id IN "
            "(SELECT vault_id FROM vault_access WHERE user_id = $1)",
            member,
        )
        # what grep sees
        greppable = await conn.fetchval(
            "SELECT count(*) FROM vaults v WHERE EXISTS ("
            "SELECT 1 FROM vault_access va WHERE va.vault_id = v.id AND va.user_id = $1)",
            member,
        )
        # what role_sync reconciles from
        reconciled = await conn.fetch(
            "SELECT vault_id, user_id, role FROM vault_access WHERE user_id = $1",
            member,
        )
        derived = await contrib.list_contributions(conn, vault_id, member)

    assert visible == 1
    assert greppable == 1
    assert [r["role"] for r in reconciled] == ["reader"]
    assert contrib.effective_role(c["role"] for c in derived) == "reader"

    # A surviving basis is a downgrade, not a removal: revoking every PG-native
    # membership here would take away in the database what the catalog grants.
    assert env.role_sync.calls == [("grant", str(vault_id), str(member), "reader")]


async def test_gate_5_the_pg_layer_is_never_told_to_demote_below_effective(env):
    """Granting `reader` to somebody a rule already made `admin` must not
    demote them in PostgreSQL."""
    name, vault_id, owner, member, granter = await _fixture(env, "gate5b")
    member_name = await env.pool.fetchval(
        "SELECT username FROM users WHERE id = $1", member,
    )

    await access_service.grant_access(
        str(granter), name, member_name, "admin", source_key="team:alpha",
    )
    env.role_sync.calls.clear()
    await access_service.grant_access(str(granter), name, member_name, "reader")

    assert await env.stored_role(vault_id, member) == "admin"
    assert env.role_sync.calls == [("grant", str(vault_id), str(member), "admin")]


# ── Gate 6 ────────────────────────────────────────────────────────────


async def test_gate_6_a_source_key_grants_no_authority_of_its_own(env):
    """A source key is a label on a grant, not a new authority axis."""
    name, vault_id, owner, member, granter = await _fixture(env, "gate6")
    member_name = await env.pool.fetchval(
        "SELECT username FROM users WHERE id = $1", member,
    )
    outsider = await env.user(f"gate6-outsider-{uuid.uuid4().hex[:8]}")

    with pytest.raises(ForbiddenError):
        await access_service.grant_access(
            str(outsider), name, member_name, "writer", source_key="team:alpha",
        )
    assert await env.bases(vault_id, member) == {}

    # A reader cannot promote themselves through a source key either.
    await access_service.grant_access(str(granter), name, member_name, "reader")
    with pytest.raises(ForbiddenError):
        await access_service.grant_access(
            str(member), name, member_name, "admin", source_key="team:alpha",
        )
    assert await env.stored_role(vault_id, member) == "reader"


async def test_gate_6_ownership_is_not_a_contribution(env):
    name, vault_id, owner, member, granter = await _fixture(env, "gate6b")
    member_name = await env.pool.fetchval(
        "SELECT username FROM users WHERE id = $1", member,
    )
    with pytest.raises(ForbiddenError):
        await access_service.grant_access(
            str(granter), name, member_name, "owner", source_key="team:alpha",
        )
    async with env.pool.acquire() as conn:
        with pytest.raises(ValueError):
            await contrib.apply_contribution(
                conn, vault_id, member, "owner", source_key="team:alpha",
            )


# ── The administrator's revoke ────────────────────────────────────────


async def test_an_administrators_revoke_removes_every_basis(env):
    """Without a source key, revoke keeps meaning what it has always meant.

    Removing only the `direct` basis would let the button report success while
    the person kept the access a rule gave them.
    """
    name, vault_id, owner, member, granter = await _fixture(env, "adminrevoke")
    member_name = await env.pool.fetchval(
        "SELECT username FROM users WHERE id = $1", member,
    )

    await access_service.grant_access(str(granter), name, member_name, "reader")
    await access_service.grant_access(
        str(granter), name, member_name, "writer", source_key="team:alpha",
    )
    result = await access_service.revoke_access(str(granter), name, member_name)

    assert result["effective_role"] is None
    assert await env.stored_role(vault_id, member) is None
    assert await env.bases(vault_id, member) == {}
    assert ("revoke", str(vault_id), str(member)) in env.role_sync.calls


# ── Source key shape ──────────────────────────────────────────────────


@pytest.mark.parametrize("bad", ["", "team alpha", "a" * 256, "team\talpha"])
async def test_a_source_key_is_validated_for_shape_only(env, bad):
    name, vault_id, owner, member, granter = await _fixture(env, "shape")
    member_name = await env.pool.fetchval(
        "SELECT username FROM users WHERE id = $1", member,
    )
    with pytest.raises(ValidationError):
        await access_service.grant_access(
            str(granter), name, member_name, "writer", source_key=bad,
        )
    # And a key AKB has no opinion about is accepted, because it must not have one.
    await access_service.grant_access(
        str(granter), name, member_name, "writer",
        source_key="anything:the-grantor-owns/42",
    )
    assert await env.stored_role(vault_id, member) == "writer"


# ── The backfill (C3) ─────────────────────────────────────────────────


async def test_the_backfill_moves_no_effective_role(env):
    """An existing database gains a basis per row and loses nothing.

    Simulated faithfully: drop the table a fresh `init.sql` creates, seed rows
    the way a pre-contribution AKB would have, then run the migration.
    """
    name, vault_id, owner, member, granter = await _fixture(env, "backfill")
    second = await env.user(f"backfill-second-{uuid.uuid4().hex[:8]}")

    async with env.pool.acquire() as conn:
        await conn.execute("DROP TABLE vault_access_contributions")
        for user_id, role in ((member, "reader"), (second, "admin")):
            await conn.execute(
                """
                INSERT INTO vault_access (vault_id, user_id, role, granted_by)
                VALUES ($1, $2, $3, $4)
                """,
                vault_id, user_id, role, granter,
            )
        before = {
            r["user_id"]: r["role"]
            for r in await conn.fetch(
                "SELECT user_id, role FROM vault_access WHERE vault_id = $1", vault_id,
            )
        }

        await _load_migration(_MIGRATION_085).migrate(conn=conn)

        after = {
            r["user_id"]: r["role"]
            for r in await conn.fetch(
                "SELECT user_id, role FROM vault_access WHERE vault_id = $1", vault_id,
            )
        }
        bases = await conn.fetch(
            """
            SELECT user_id, source_key, role, granted_by
            FROM vault_access_contributions WHERE vault_id = $1
            """,
            vault_id,
        )

    assert after == before
    assert {(b["source_key"], b["role"]) for b in bases} == {
        ("direct", "reader"), ("direct", "admin"),
    }
    assert all(b["granted_by"] == granter for b in bases)


async def test_the_backfill_refuses_to_commit_a_migration_that_moves_access(env):
    """The migration's own check, proved by making the thing it checks true.

    A rerun over a pair whose stored role drifted away from its basis is the
    realistic case: `ON CONFLICT DO NOTHING` leaves the older basis in place, so
    the derived value and the stored one disagree. Committing there would change
    somebody's access inside a migration that promises to change none.
    """
    name, vault_id, owner, member, granter = await _fixture(env, "backfillguard")

    async with env.pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO vault_access (vault_id, user_id, role) VALUES ($1, $2, 'reader')",
            vault_id, member,
        )
        await conn.execute(
            """
            INSERT INTO vault_access_contributions
                (vault_id, user_id, role, source_key) VALUES ($1, $2, 'reader', 'direct')
            """,
            vault_id, member,
        )
        # The stored row drifts away from the basis behind it.
        await conn.execute(
            "UPDATE vault_access SET role = 'writer' WHERE vault_id = $1 AND user_id = $2",
            vault_id, member,
        )
        with pytest.raises(RuntimeError, match="refusing to commit"):
            await _load_migration(_MIGRATION_085).migrate(conn=conn)


# ── The event payload (C4) ────────────────────────────────────────────


async def _events(env, vault_id, kind: str) -> list[dict]:
    import json
    async with env.pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT payload FROM events WHERE vault_id = $1 AND kind = $2 "
            "ORDER BY id",
            vault_id, kind,
        )
    return [
        json.loads(r["payload"]) if isinstance(r["payload"], str) else r["payload"]
        for r in rows
    ]


async def test_the_grant_event_names_the_basis_and_both_effective_roles(env):
    name, vault_id, owner, member, granter = await _fixture(env, "eventgrant")
    member_name = await env.pool.fetchval(
        "SELECT username FROM users WHERE id = $1", member,
    )

    await access_service.grant_access(str(granter), name, member_name, "reader")
    await access_service.grant_access(
        str(granter), name, member_name, "writer", source_key="team:alpha",
    )

    first, second = await _events(env, vault_id, "access.grant")
    assert first["source_key"] == "direct"
    assert (first["previous_effective_role"], first["effective_role"]) == (None, "reader")
    assert second["source_key"] == "team:alpha"
    assert (second["previous_effective_role"], second["effective_role"]) == (
        "reader", "writer",
    )


async def test_the_revoke_event_separates_a_downgrade_from_a_removal(env):
    """A subscriber reading only "revoked" cannot tell these two apart."""
    name, vault_id, owner, member, granter = await _fixture(env, "eventrevoke")
    member_name = await env.pool.fetchval(
        "SELECT username FROM users WHERE id = $1", member,
    )

    await access_service.grant_access(str(granter), name, member_name, "reader")
    await access_service.grant_access(
        str(granter), name, member_name, "writer", source_key="team:alpha",
    )
    await access_service.revoke_access(
        str(granter), name, member_name, source_key="team:alpha",
    )
    await access_service.revoke_access(str(granter), name, member_name)

    downgrade, removal = await _events(env, vault_id, "access.revoke")
    assert downgrade["source_key"] == "team:alpha"
    assert (downgrade["previous_effective_role"], downgrade["effective_role"]) == (
        "writer", "reader",
    )
    # No source key: every basis went, and the payload says so with a null.
    assert removal["source_key"] is None
    assert (removal["previous_effective_role"], removal["effective_role"]) == (
        "reader", None,
    )


# ── The explanation surface (C5) ──────────────────────────────────────


async def test_the_explanation_lists_every_basis_behind_the_role(env):
    name, vault_id, owner, member, granter = await _fixture(env, "explain")
    member_name = await env.pool.fetchval(
        "SELECT username FROM users WHERE id = $1", member,
    )

    await access_service.grant_access(str(granter), name, member_name, "reader")
    await access_service.grant_access(
        str(granter), name, member_name, "writer", source_key="team:alpha",
    )

    explained = await access_service.explain_vault_access(
        str(granter), name, member_name,
    )
    assert explained["effective_role"] == "writer"
    assert explained["derived_role"] == "writer"
    assert {(c["source_key"], c["role"]) for c in explained["contributions"]} == {
        ("direct", "reader"), ("team:alpha", "writer"),
    }
    assert explained["non_member_paths"] == {
        "owner": False,
        "system_admin": False,
        "public_access": "none",
        "write_policy_managed_by": None,
    }


async def test_the_explanation_shows_the_paths_the_member_list_never_had(env):
    """A user reachable only through `public_access` is in no member list."""
    name, vault_id, owner, member, granter = await _fixture(env, "explainpaths")
    member_name = await env.pool.fetchval(
        "SELECT username FROM users WHERE id = $1", member,
    )
    async with env.pool.acquire() as conn:
        await conn.execute(
            "UPDATE vaults SET public_access = 'reader' WHERE id = $1", vault_id,
        )

    explained = await access_service.explain_vault_access(
        str(granter), name, member_name,
    )
    assert explained["contributions"] == []
    assert explained["effective_role"] is None
    assert explained["non_member_paths"]["public_access"] == "reader"

    owner_name = await env.pool.fetchval(
        "SELECT username FROM users WHERE id = $1", owner,
    )
    owner_view = await access_service.explain_vault_access(
        str(granter), name, owner_name,
    )
    assert owner_view["non_member_paths"]["owner"] is True
    assert owner_view["contributions"] == []


async def test_explaining_somebody_else_requires_admin_but_yourself_does_not(env):
    name, vault_id, owner, member, granter = await _fixture(env, "explainauthz")
    member_name = await env.pool.fetchval(
        "SELECT username FROM users WHERE id = $1", member,
    )
    other = await env.user(f"explainauthz-other-{uuid.uuid4().hex[:8]}")
    other_name = await env.pool.fetchval(
        "SELECT username FROM users WHERE id = $1", other,
    )
    await access_service.grant_access(str(granter), name, member_name, "reader")
    await access_service.grant_access(str(granter), name, other_name, "reader")

    mine = await access_service.explain_vault_access(str(member), name, member_name)
    assert mine["effective_role"] == "reader"

    with pytest.raises(ForbiddenError):
        await access_service.explain_vault_access(str(member), name, other_name)


async def test_the_explanation_reports_a_recompute_that_fell_behind(env):
    """The two roles are separate fields because they can disagree.

    A materialized effective row can drift; a derived one cannot. That is the
    honest advantage of deriving at read time, and reporting one number here
    would hide exactly the failure the materialization introduces — so the
    explanation shows both, and a stale row is visible instead of authoritative.
    """
    name, vault_id, owner, member, granter = await _fixture(env, "explaindrift")
    member_name = await env.pool.fetchval(
        "SELECT username FROM users WHERE id = $1", member,
    )
    await access_service.grant_access(str(granter), name, member_name, "reader")

    async with env.pool.acquire() as conn:
        # A basis written without the recompute that belongs with it — what a
        # broken write path would leave behind.
        await conn.execute(
            """
            INSERT INTO vault_access_contributions
                (vault_id, user_id, role, source_key) VALUES ($1, $2, 'admin', 'team:alpha')
            """,
            vault_id, member,
        )

    explained = await access_service.explain_vault_access(
        str(granter), name, member_name,
    )
    assert explained["effective_role"] == "reader"
    assert explained["derived_role"] == "admin"


# ── The PostgreSQL half, with the real RoleSync ───────────────────────
#
# Every gate above records what `role_sync` was TOLD. That is not the same
# question as what PostgreSQL then ALLOWS, and the difference is the one class
# of authorization bug that cannot be seen from the application: `akb_sql` is
# gated by membership in `akb_vault_<vid>_<scope>`, not by `vault_access`.
# `test_role_sync.py` says it plainly — mocks do not catch GRANT semantics — so
# these two use the real thing against the real server.


class _LiveEnv(_Env):
    """`_Env`, except a vault also gets the three PostgreSQL group roles that
    real vault creation gives it.

    The SQL-only fixture above is enough while `role_sync` is a recorder. With
    the real one, a vault whose group roles were never created makes every
    `on_grant` fail — and it fails *quietly*, because the hook is best-effort
    by design ("reconciler covers drift"). That silence is what makes asserting
    PostgreSQL state, rather than the call, the point of these two tests.
    """

    async def vault(self, name: str, owner_id: uuid.UUID) -> uuid.UUID:
        vault_id = await super().vault(name, owner_id)
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                await self.role_sync.on_vault_create_in_conn(
                    conn, vault_id, owner_user_id=owner_id,
                )
        return vault_id


@pytest.fixture
async def live_role_sync(monkeypatch):
    from app.services.role_sync import RoleSync

    async with _fresh_database() as pool:
        role_sync = RoleSync(pool)
        monkeypatch.setattr(access_service, "get_pool", lambda: pool)
        monkeypatch.setattr(access_service, "get_role_sync", lambda: role_sync)
        yield _LiveEnv(pool, role_sync)


async def _pg_membership(env, vault_id, user_id) -> set[str]:
    """The scopes PostgreSQL actually grants this user on this vault."""
    from app.services.role_sync import user_role_name, vault_group_role_name

    member = user_role_name(user_id)
    held = set()
    async with env.pool.acquire() as conn:
        for scope in ("reader", "writer", "admin"):
            group = vault_group_role_name(vault_id, scope)
            direct = await conn.fetchval(
                """
                SELECT EXISTS (
                    SELECT 1 FROM pg_auth_members m
                    JOIN pg_roles g ON g.oid = m.roleid
                    JOIN pg_roles u ON u.oid = m.member
                    WHERE g.rolname = $1 AND u.rolname = $2
                )
                """,
                group, member,
            )
            if direct:
                held.add(scope)
    return held


async def test_a_downgrade_reaches_postgres_as_a_downgrade(live_role_sync):
    """Removing one basis must leave the weaker membership standing.

    Revoking every group membership here — which is what the old `on_revoke`
    path would do — takes away in the database what the catalog still grants,
    and no application-level test can see it.
    """
    env = live_role_sync
    name, vault_id, owner, member, granter = await _fixture(env, "pgdown")
    member_name = await env.pool.fetchval(
        "SELECT username FROM users WHERE id = $1", member,
    )

    await access_service.grant_access(str(granter), name, member_name, "reader")
    assert await _pg_membership(env, vault_id, member) == {"reader"}

    await access_service.grant_access(
        str(granter), name, member_name, "writer", source_key="team:alpha",
    )
    assert await _pg_membership(env, vault_id, member) == {"writer"}

    await access_service.revoke_access(
        str(granter), name, member_name, source_key="team:alpha",
    )
    # The catalog says reader. PostgreSQL must agree — not "gone", not "writer".
    assert await env.stored_role(vault_id, member) == "reader"
    assert await _pg_membership(env, vault_id, member) == {"reader"}

    # And the administrator's revoke takes it all, in both planes.
    await access_service.revoke_access(str(granter), name, member_name)
    assert await env.stored_role(vault_id, member) is None
    assert await _pg_membership(env, vault_id, member) == set()


async def test_a_weaker_grant_does_not_demote_in_postgres(live_role_sync):
    """Granting `reader` to somebody a rule already made `admin` must not
    reach PostgreSQL as a demotion."""
    env = live_role_sync
    name, vault_id, owner, member, granter = await _fixture(env, "pgweak")
    member_name = await env.pool.fetchval(
        "SELECT username FROM users WHERE id = $1", member,
    )

    await access_service.grant_access(
        str(granter), name, member_name, "admin", source_key="team:alpha",
    )
    assert await _pg_membership(env, vault_id, member) == {"admin"}

    await access_service.grant_access(str(granter), name, member_name, "reader")
    assert await env.stored_role(vault_id, member) == "admin"
    assert await _pg_membership(env, vault_id, member) == {"admin"}
