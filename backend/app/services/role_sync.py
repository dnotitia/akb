"""PG-native RBAC sync for vault isolation.

Maps AKB's catalog (`users`, `vaults`, `vault_access`, `vault_tables`)
onto PostgreSQL role + GRANT state. `akb_sql` then runs each user's
query under their PG role, and Postgres itself returns `42501` for
any reference to a table the role doesn't have GRANT on. The
application doesn't inspect user SQL for security — PG does.

Roles
-----

For each AKB user (`users.id = <uid>`):
    akb_user_<uid_underscored>          NOLOGIN

For each vault (`vaults.id = <vid>`):
    akb_vault_<vid>_reader              NOLOGIN, SELECT
    akb_vault_<vid>_writer              NOLOGIN, +INSERT/UPDATE/DELETE
    akb_vault_<vid>_admin               NOLOGIN, +TRUNCATE

Plus one process-wide wildcard:
    akb_authenticated                   NOLOGIN — every akb_user_<uid> is a
                                        member. Vaults with
                                        `public_access != 'none'` grant
                                        the corresponding scope to this
                                        role, so any authenticated user
                                        can read/write the vault via
                                        akb_sql without an explicit
                                        vault_access row.

Hierarchy: writer inherits reader; admin inherits writer. So a
`GRANT akb_vault_X_writer TO akb_user_Y` confers both write and
read in one statement.

Each `vault_access(user, vault, role)` row maps 1:1 to
`GRANT akb_vault_<vid>_<role> TO akb_user_<uid>`. The vault owner
gets `akb_vault_<vid>_admin`. The `vaults.public_access` value maps
1:1 to a grant of `akb_vault_<vid>_<level>` to `akb_authenticated`.

Each `vault_tables` row gets table-level GRANTs on
`public.vt_<vault>__<table>` to all three group roles. Tables stay
physically in `public`; the GRANT overlay is what enforces the
boundary. No schemas are reorganized, no data is moved.

Semantics
---------

Lifecycle hooks (`on_user_create`, `on_grant`, …) are **best-effort**.
A failure is logged but does NOT roll back the system DB write. The
reconciler (`reconcile_from_catalog`) reads the catalog at backend
startup and on demand, emits any missing CREATE/GRANT and drops any
orphan AKB-owned role. The catalog is the authoritative source of
truth; PG role state is derived.

System administrators (`users.is_admin = TRUE`) bypass this layer:
their `akb_sql` runs under the connection's default role (the
backend service role) without `SET LOCAL ROLE`. This matches the
existing system-admin trust model.

Identifier safety
-----------------

UUID strings contain only `[0-9a-f-]`; dashes are translated to
underscores when building role names so no quoting is required. PG
table names produced by `table_data_repo.pg_table_name()` are
strictly `vt_<alnum_or_underscore>__<alnum_or_underscore>`; we
re-verify the shape before interpolating into raw SQL as
defense-in-depth.
"""

from __future__ import annotations

import asyncio
import logging
import re
import uuid
from dataclasses import dataclass, field
from typing import Optional

import asyncpg

logger = logging.getLogger("akb.role_sync")


# ── Identifier helpers ────────────────────────────────────────

# Wildcard role used to grant public-vault access without iterating
# every existing user. Every `akb_user_<uid>` is a member, so granting
# this role any vault-scope group is equivalent to granting all
# authenticated users that scope. Public vaults attach here; private
# vaults never do.
AUTHENTICATED_ROLE = "akb_authenticated"


def user_role_name(user_id: uuid.UUID | str) -> str:
    """`akb_user_<uid_with_dashes_to_underscores>`."""
    return f"akb_user_{str(user_id).replace('-', '_')}"


def vault_group_role_name(
    vault_id: uuid.UUID | str, scope: str,
) -> str:
    """`akb_vault_<vid_underscored>_<scope>` where scope ∈ {reader,writer,admin}."""
    if scope not in ("reader", "writer", "admin"):
        raise ValueError(f"invalid scope: {scope!r}")
    return f"akb_vault_{str(vault_id).replace('-', '_')}_{scope}"


def _public_access_scope(level: str) -> str | None:
    """Map `vaults.public_access` enum to the vault group scope the
    `akb_authenticated` role should be granted, or None when no grant
    applies. Anything not in {'reader', 'writer'} resolves to None
    (i.e. 'none' / unknown → revoke all)."""
    if level == "reader":
        return "reader"
    if level == "writer":
        return "writer"
    return None


_VT_TABLE_RE = re.compile(r"^vt_[a-z0-9_]+__[a-z0-9_]+$")


def _is_safe_pg_table_name(name: str) -> bool:
    """Guard before interpolating a `vt_*` name into raw SQL. Upstream
    `pg_table_name` already sanitizes; this is defense-in-depth."""
    return bool(_VT_TABLE_RE.match(name)) and len(name) <= 63


# ── Reconcile report ──────────────────────────────────────────


@dataclass
class ReconcileReport:
    user_roles_created: int = 0
    user_roles_dropped: int = 0
    vault_roles_created: int = 0
    vault_roles_dropped: int = 0
    grants_added: int = 0
    grants_removed: int = 0
    table_grants_applied: int = 0
    public_grants_applied: int = 0
    errors: list[str] = field(default_factory=list)

    def __str__(self) -> str:
        return (
            f"users(+{self.user_roles_created}/-{self.user_roles_dropped}) "
            f"vaults(+{self.vault_roles_created}/-{self.vault_roles_dropped}) "
            f"grants(+{self.grants_added}/-{self.grants_removed}) "
            f"table_grants({self.table_grants_applied}) "
            f"public_grants({self.public_grants_applied}) "
            f"errors({len(self.errors)})"
        )


# ── Drift report (read-only inspect) ──────────────────────────


@dataclass
class RoleStateDiff:
    """What `reconcile_from_catalog` WOULD change. Read-only diff so
    operators can inspect before mutating."""

    missing_user_roles: list[str] = field(default_factory=list)
    orphan_user_roles: list[str] = field(default_factory=list)
    missing_vault_roles: list[str] = field(default_factory=list)
    orphan_vault_roles: list[str] = field(default_factory=list)
    missing_memberships: list[dict] = field(default_factory=list)
    missing_public_grants: list[dict] = field(default_factory=list)
    stale_public_grants: list[dict] = field(default_factory=list)
    missing_table_grants: list[dict] = field(default_factory=list)
    authenticated_role_missing: bool = False
    users_not_in_authenticated: list[str] = field(default_factory=list)

    def drift_count(self) -> int:
        return (
            len(self.missing_user_roles)
            + len(self.orphan_user_roles)
            + len(self.missing_vault_roles)
            + len(self.orphan_vault_roles)
            + len(self.missing_memberships)
            + len(self.missing_public_grants)
            + len(self.stale_public_grants)
            + len(self.missing_table_grants)
            + (1 if self.authenticated_role_missing else 0)
            + len(self.users_not_in_authenticated)
        )

    def is_clean(self) -> bool:
        return self.drift_count() == 0


# Privileges that the reconciler grants on every vt_* table, keyed by
# scope. Used by `_diff_table_grants` to spot drift (a hook silently
# failed; an operator REVOKE'd manually). Matches `_grant_table`.
_EXPECTED_TABLE_PRIVS: dict[str, frozenset[str]] = {
    "reader": frozenset({"SELECT"}),
    "writer": frozenset({"SELECT", "INSERT", "UPDATE", "DELETE"}),
    "admin": frozenset({"SELECT", "INSERT", "UPDATE", "DELETE", "TRUNCATE"}),
}


# ── Hook telemetry ────────────────────────────────────────────


@dataclass
class HookMetrics:
    """Per-hook counters for drift surveillance.

    Lifecycle hooks are best-effort by design: a failure logs and
    moves on, the reconciler converges. But silent best-effort
    failures are the exact scenario operators need to detect, so we
    bump a counter on every hook failure and surface it via /health.
    """

    failures: dict[str, int] = field(default_factory=dict)
    last_reconcile_errors: int = 0
    last_reconcile_at: str | None = None

    def record_failure(self, hook: str) -> None:
        self.failures[hook] = self.failures.get(hook, 0) + 1

    def total_failures(self) -> int:
        return sum(self.failures.values())

    def snapshot(self) -> dict:
        return {
            "hook_failures_total": self.total_failures(),
            "hook_failures_by_name": dict(self.failures),
            "last_reconcile_errors": self.last_reconcile_errors,
            "last_reconcile_at": self.last_reconcile_at,
        }


# ── RoleSync ─────────────────────────────────────────────────


class RoleSync:
    """Idempotent PG role + GRANT manager for AKB vault isolation."""

    def __init__(self, pool: asyncpg.Pool) -> None:
        self.pool = pool
        self.metrics = HookMetrics()
        self._reconcile_task: Optional[asyncio.Task] = None

    def metrics_snapshot(self) -> dict:
        """Read-only snapshot of hook-failure counters + last reconcile
        outcome. Surfaced via /health so operators see silent drift
        without grepping logs."""
        return self.metrics.snapshot()

    # ── Periodic reconcile timer ──

    def start_reconcile_timer(self, interval_secs: int) -> None:
        """Spawn an asyncio task that re-runs `reconcile_from_catalog`
        every `interval_secs`. No-op if `interval_secs <= 0` or the
        timer is already running. Called from `start_workers` so it
        joins the rest of the background loops."""
        if interval_secs <= 0:
            logger.info("role_sync timer disabled (interval = %s)", interval_secs)
            return
        if self._reconcile_task is not None and not self._reconcile_task.done():
            return
        self._reconcile_task = asyncio.create_task(
            self._reconcile_loop(interval_secs),
            name="role_sync_reconcile_loop",
        )
        logger.info(
            "role_sync_reconcile_loop started (interval=%ss)", interval_secs,
        )

    async def stop_reconcile_timer(self) -> None:
        """Cancel the timer and await its exit. Idempotent."""
        if self._reconcile_task is None:
            return
        self._reconcile_task.cancel()
        try:
            await self._reconcile_task
        except (asyncio.CancelledError, Exception):  # noqa: BLE001
            pass
        self._reconcile_task = None

    async def _reconcile_loop(self, interval_secs: int) -> None:
        """Sleep → reconcile → repeat. Failures don't terminate the
        loop — they're counted via metrics + logged so operators see
        them, but the next interval still fires."""
        while True:
            try:
                await asyncio.sleep(interval_secs)
            except asyncio.CancelledError:
                return
            try:
                await self.reconcile_from_catalog()
            except asyncio.CancelledError:
                return
            except Exception as e:  # noqa: BLE001
                logger.warning("periodic reconcile failed: %s", e)
                self.metrics.record_failure("reconcile_loop")

    def _record_failure(self, hook: str, exc: BaseException, *fmt_args) -> None:
        """Single place that combines warn-logging + metric counter for
        every hook exception. Keeps the except blocks one line."""
        if fmt_args:
            logger.warning(f"{hook}({fmt_args}) failed: %s", exc)
        else:
            logger.warning(f"{hook} failed: %s", exc)
        self.metrics.record_failure(hook)

    # ── Lifecycle hooks ──

    async def on_user_create(self, user_id: uuid.UUID | str) -> None:
        """Create `akb_user_<uid>` (NOLOGIN) and add it to the
        `akb_authenticated` wildcard so public-vault access works.
        Idempotent."""
        role = user_role_name(user_id)
        try:
            async with self.pool.acquire() as conn:
                await self._create_role_if_missing(conn, role)
                await self._create_role_if_missing(conn, AUTHENTICATED_ROLE)
                await self._grant_membership(conn, AUTHENTICATED_ROLE, role)
        except Exception as e:  # noqa: BLE001
            self._record_failure("on_user_create", e, user_id)

    async def on_user_delete(self, user_id: uuid.UUID | str) -> None:
        """Drop `akb_user_<uid>`. Memberships in vault group roles are
        cleared automatically when the role is dropped."""
        role = user_role_name(user_id)
        try:
            async with self.pool.acquire() as conn:
                await self._drop_role_if_present(conn, role)
        except Exception as e:  # noqa: BLE001
            self._record_failure("on_user_delete", e, user_id)

    async def on_vault_create(
        self,
        vault_id: uuid.UUID | str,
        owner_user_id: Optional[uuid.UUID | str] = None,
    ) -> None:
        """Create the three group roles for the vault + role hierarchy.
        If `owner_user_id` is provided, grant admin to that user role."""
        reader = vault_group_role_name(vault_id, "reader")
        writer = vault_group_role_name(vault_id, "writer")
        admin = vault_group_role_name(vault_id, "admin")
        try:
            async with self.pool.acquire() as conn:
                for role in (reader, writer, admin):
                    await self._create_role_if_missing(conn, role)
                # Hierarchy: writer ⊇ reader, admin ⊇ writer.
                await self._grant_membership(conn, reader, writer)
                await self._grant_membership(conn, writer, admin)
                if owner_user_id:
                    owner_role = user_role_name(owner_user_id)
                    await self._create_role_if_missing(conn, owner_role)
                    await self._grant_membership(conn, admin, owner_role)
        except Exception as e:  # noqa: BLE001
            self._record_failure("on_vault_create", e, vault_id)

    async def on_vault_delete(self, vault_id: uuid.UUID | str) -> None:
        """Drop the three group roles. Dependent GRANTs are cleared by
        `DROP OWNED BY`. Memberships of dropped roles auto-clean."""
        try:
            async with self.pool.acquire() as conn:
                for scope in ("admin", "writer", "reader"):
                    role = vault_group_role_name(vault_id, scope)
                    await self._drop_role_if_present(conn, role)
        except Exception as e:  # noqa: BLE001
            self._record_failure("on_vault_delete", e, vault_id)

    async def on_grant(
        self,
        vault_id: uuid.UUID | str,
        user_id: uuid.UUID | str,
        scope: str,
    ) -> None:
        """`GRANT akb_vault_<vid>_<scope> TO akb_user_<uid>`. If a stronger
        scope was previously granted, the older grant remains until
        `on_revoke` is called explicitly (matches `vault_access` semantics
        which is unique on `(vault_id, user_id)`)."""
        if scope not in ("reader", "writer", "admin"):
            logger.warning("on_grant: invalid scope %r", scope)
            self.metrics.record_failure("on_grant")
            return
        group = vault_group_role_name(vault_id, scope)
        user = user_role_name(user_id)
        try:
            async with self.pool.acquire() as conn:
                # Drop any prior membership in this vault's groups first
                # so a downgrade (admin → reader) doesn't leave the user
                # still admin via membership chain.
                for s in ("reader", "writer", "admin"):
                    other = vault_group_role_name(vault_id, s)
                    if other == group:
                        continue
                    await self._revoke_membership_if_present(conn, other, user)
                await self._create_role_if_missing(conn, user)
                await self._grant_membership(conn, group, user)
        except Exception as e:  # noqa: BLE001
            self._record_failure("on_grant", e, vault_id, user_id, scope)

    async def on_revoke(
        self,
        vault_id: uuid.UUID | str,
        user_id: uuid.UUID | str,
    ) -> None:
        """Revoke all memberships in this vault's three group roles from
        the user role. Mirrors `vault_access` DELETE semantics."""
        user = user_role_name(user_id)
        try:
            async with self.pool.acquire() as conn:
                for scope in ("reader", "writer", "admin"):
                    group = vault_group_role_name(vault_id, scope)
                    await self._revoke_membership_if_present(conn, group, user)
        except Exception as e:  # noqa: BLE001
            self._record_failure("on_revoke", e, vault_id, user_id)

    async def on_ownership_transfer(
        self,
        vault_id: uuid.UUID | str,
        old_owner_id: Optional[uuid.UUID | str],
        new_owner_id: uuid.UUID | str,
    ) -> None:
        """Grant admin to new owner. The old owner is preserved as admin
        via the `vault_access` row that `transfer_ownership` writes; that
        row triggers `on_grant` separately."""
        admin = vault_group_role_name(vault_id, "admin")
        new_role = user_role_name(new_owner_id)
        try:
            async with self.pool.acquire() as conn:
                await self._create_role_if_missing(conn, new_role)
                await self._grant_membership(conn, admin, new_role)
        except Exception as e:  # noqa: BLE001
            self._record_failure(
                "on_ownership_transfer", e, vault_id, old_owner_id, new_owner_id,
            )

    async def on_public_access_change(
        self,
        vault_id: uuid.UUID | str,
        level: str,
    ) -> None:
        """Sync the vault's public-access grants to `akb_authenticated`.

        `level` must be one of {'none', 'reader', 'writer'} (the
        `validate_public_access` enum). For 'none' we revoke any
        prior reader/writer membership; otherwise we revoke any
        opposite-scope membership first (handles reader → writer
        and writer → reader transitions cleanly) and grant the
        target scope. Idempotent.
        """
        target = _public_access_scope(level)
        try:
            async with self.pool.acquire() as conn:
                await self._create_role_if_missing(conn, AUTHENTICATED_ROLE)
                # Revoke any scope the wildcard previously held on this
                # vault that isn't the target. Always revoke admin —
                # public_access never grants admin.
                for scope in ("reader", "writer", "admin"):
                    if scope == target:
                        continue
                    group = vault_group_role_name(vault_id, scope)
                    await self._revoke_membership_if_present(
                        conn, group, AUTHENTICATED_ROLE,
                    )
                if target is not None:
                    group = vault_group_role_name(vault_id, target)
                    await self._grant_membership(conn, group, AUTHENTICATED_ROLE)
        except Exception as e:  # noqa: BLE001
            self._record_failure("on_public_access_change", e, vault_id, level)

    async def on_table_create(
        self,
        vault_id: uuid.UUID | str,
        pg_table_name: str,
    ) -> None:
        """Grant SELECT/INSERT/UPDATE/DELETE/ALL on the new table to the
        vault's reader/writer/admin group roles, matching their scope.

        Reconciler path (acquires own connection). Prefer
        :meth:`grant_table_in_conn` from inside the caller's TX so the
        grant commits atomically with the CREATE TABLE — pre-fix, this
        path ran post-commit and left a window where the table existed
        but akb_sql callers got 42501."""
        if not _is_safe_pg_table_name(pg_table_name):
            logger.error("on_table_create: unsafe pg_table_name %r — refusing", pg_table_name)
            self.metrics.record_failure("on_table_create")
            return
        try:
            async with self.pool.acquire() as conn:
                await self._grant_table(conn, vault_id, pg_table_name)
        except Exception as e:  # noqa: BLE001
            self._record_failure("on_table_create", e, vault_id, pg_table_name)

    async def grant_table_in_conn(
        self,
        conn,
        vault_id: uuid.UUID | str,
        pg_table_name: str,
    ) -> None:
        """In-TX variant: grant runs on the caller's connection so it
        commits atomically with the CREATE TABLE that preceded it.

        Errors propagate — the create_table TX rolls back rather than
        leaving an ungranted table (intentional; the reconciler path
        above swallows for self-healing, this path does not)."""
        if not _is_safe_pg_table_name(pg_table_name):
            raise ValueError(
                f"unsafe pg_table_name {pg_table_name!r} — refusing grant"
            )
        await self._grant_table(conn, vault_id, pg_table_name)

    async def on_table_drop(
        self,
        vault_id: uuid.UUID | str,
        pg_table_name: str,
    ) -> None:
        """No-op — `DROP TABLE` cascades GRANTs automatically. Hook
        retained for symmetry and future audit logging."""
        logger.debug(
            "on_table_drop(%s, %s) — DROP TABLE cascades GRANTs",
            vault_id, pg_table_name,
        )

    # ── Reconciler ──

    async def reconcile_from_catalog(self) -> ReconcileReport:
        """Read the catalog from system DB and converge PG role state.

        Steps:
          1. Ensure the `akb_authenticated` wildcard exists.
          2. For each user → ensure `akb_user_<uid>` exists and is a
             member of `akb_authenticated`. Drop orphan user roles.
          3. For each vault → ensure three group roles + hierarchy.
             Drop orphan vault roles.
          4. For each vault_access row + each owner → grant membership.
             (Membership drift isn't explicitly cleared row-by-row;
             dropping the parent role clears its memberships.)
          5. For each vault_tables row → grant table-level perms.
          6. For each vault → sync `public_access` to akb_authenticated
             memberships.

        Idempotent. Safe to run repeatedly.
        """
        from datetime import datetime, timezone

        report = ReconcileReport()
        async with self.pool.acquire() as conn:
            await self._create_role_if_missing(conn, AUTHENTICATED_ROLE)
            await self._reconcile_user_roles(conn, report)
            await self._reconcile_vault_roles(conn, report)
            await self._reconcile_memberships(conn, report)
            await self._reconcile_table_grants(conn, report)
            await self._reconcile_public_access(conn, report)

        self.metrics.last_reconcile_errors = len(report.errors)
        self.metrics.last_reconcile_at = datetime.now(timezone.utc).isoformat()

        if report.errors:
            logger.warning("Reconcile complete with errors: %s", report)
        else:
            logger.info("Reconcile complete: %s", report)
        return report

    async def _reconcile_user_roles(
        self, conn: asyncpg.Connection, report: ReconcileReport,
    ) -> None:
        rows = await conn.fetch("SELECT id FROM users")
        wanted = {user_role_name(r["id"]) for r in rows}
        existing = {
            r["rolname"]
            for r in await conn.fetch(
                "SELECT rolname FROM pg_roles WHERE rolname LIKE 'akb_user\\_%' ESCAPE '\\'"
            )
        }
        for role in wanted - existing:
            try:
                await conn.execute(f'CREATE ROLE "{role}" NOLOGIN')
                report.user_roles_created += 1
            except Exception as e:  # noqa: BLE001
                report.errors.append(f"CREATE ROLE {role}: {e}")
        # Every user role must be a member of the authenticated wildcard.
        # Idempotent GRANT re-applies are no-ops.
        for role in wanted:
            try:
                await self._grant_membership(conn, AUTHENTICATED_ROLE, role)
            except Exception as e:  # noqa: BLE001
                report.errors.append(f"GRANT {AUTHENTICATED_ROLE}→{role}: {e}")
        for orphan in existing - wanted:
            try:
                await self._drop_role_if_present(conn, orphan)
                report.user_roles_dropped += 1
            except Exception as e:  # noqa: BLE001
                report.errors.append(f"DROP ROLE {orphan}: {e}")

    async def _reconcile_vault_roles(
        self, conn: asyncpg.Connection, report: ReconcileReport,
    ) -> None:
        # Archived vaults are READ-ONLY, not deleted: their group roles +
        # table GRANTs MUST survive reconcile so akb_sql SELECT (and the
        # 0.3.4 publication table_query, which runs under the creator's
        # akb_user_* role) keeps working. The write block for archived
        # vaults lives in the app layer (execute_sql / check_vault_access),
        # NOT by dropping PG grants — so unarchive needs no re-grant. Only
        # a real vault DELETE removes these roles (on_vault_delete). MUST
        # use the SAME vault set as _diff_vaults (all vaults) or is_clean()
        # never converges.
        rows = await conn.fetch("SELECT id, owner_id FROM vaults")
        wanted: set[str] = set()
        for r in rows:
            for scope in ("reader", "writer", "admin"):
                wanted.add(vault_group_role_name(r["id"], scope))
        existing = {
            r["rolname"]
            for r in await conn.fetch(
                "SELECT rolname FROM pg_roles WHERE rolname LIKE 'akb_vault\\_%' ESCAPE '\\'"
            )
        }
        for role in wanted - existing:
            try:
                await conn.execute(f'CREATE ROLE "{role}" NOLOGIN')
                report.vault_roles_created += 1
            except Exception as e:  # noqa: BLE001
                report.errors.append(f"CREATE ROLE {role}: {e}")

        # Re-establish hierarchy + owner admin membership idempotently.
        for r in rows:
            reader = vault_group_role_name(r["id"], "reader")
            writer = vault_group_role_name(r["id"], "writer")
            admin = vault_group_role_name(r["id"], "admin")
            try:
                await self._grant_membership(conn, reader, writer)
                await self._grant_membership(conn, writer, admin)
            except Exception as e:  # noqa: BLE001
                report.errors.append(f"hierarchy {r['id']}: {e}")
            if r["owner_id"]:
                owner_role = user_role_name(r["owner_id"])
                try:
                    await self._create_role_if_missing(conn, owner_role)
                    await self._grant_membership(conn, admin, owner_role)
                except Exception as e:  # noqa: BLE001
                    report.errors.append(f"owner grant {r['id']}: {e}")

        for orphan in existing - wanted:
            try:
                await self._drop_role_if_present(conn, orphan)
                report.vault_roles_dropped += 1
            except Exception as e:  # noqa: BLE001
                report.errors.append(f"DROP ROLE {orphan}: {e}")

    async def _reconcile_memberships(
        self, conn: asyncpg.Connection, report: ReconcileReport,
    ) -> None:
        rows = await conn.fetch(
            "SELECT vault_id, user_id, role FROM vault_access"
        )
        for ar in rows:
            user = user_role_name(ar["user_id"])
            group = vault_group_role_name(ar["vault_id"], ar["role"])
            try:
                await self._grant_membership(conn, group, user)
                report.grants_added += 1
            except Exception as e:  # noqa: BLE001
                report.errors.append(f"GRANT {group}→{user}: {e}")

    async def _reconcile_public_access(
        self, conn: asyncpg.Connection, report: ReconcileReport,
    ) -> None:
        """Sync each vault's public_access state to memberships on
        `akb_authenticated`. Walks every vault — for those with
        public_access='none' the on_public_access_change logic
        revokes any stale grant, so transitions to private also
        self-heal here."""
        rows = await conn.fetch(
            "SELECT id, COALESCE(public_access, 'none') AS public_access FROM vaults"
        )
        for r in rows:
            target = _public_access_scope(r["public_access"])
            try:
                for scope in ("reader", "writer", "admin"):
                    if scope == target:
                        continue
                    group = vault_group_role_name(r["id"], scope)
                    await self._revoke_membership_if_present(
                        conn, group, AUTHENTICATED_ROLE,
                    )
                if target is not None:
                    group = vault_group_role_name(r["id"], target)
                    await self._grant_membership(conn, group, AUTHENTICATED_ROLE)
                    report.public_grants_applied += 1
            except Exception as e:  # noqa: BLE001
                report.errors.append(
                    f"public access {r['id']} ({r['public_access']}): {e}"
                )

    async def _reconcile_table_grants(
        self, conn: asyncpg.Connection, report: ReconcileReport,
    ) -> None:
        # Lazy import — avoid circular at module import time.
        from app.repositories import table_data_repo

        rows = await conn.fetch(
            """
            SELECT vt.vault_id, vt.name, v.name AS vault_name
              FROM vault_tables vt
              JOIN vaults v ON vt.vault_id = v.id
            """
        )
        for tr in rows:
            pg_name = table_data_repo.pg_table_name(tr["vault_name"], tr["name"])
            if not _is_safe_pg_table_name(pg_name):
                report.errors.append(f"unsafe pg_name: {pg_name}")
                continue
            try:
                await self._grant_table(conn, tr["vault_id"], pg_name)
                report.table_grants_applied += 1
            except Exception as e:  # noqa: BLE001
                report.errors.append(f"GRANT on {pg_name}: {e}")

    # ── Drift inspection (read-only) ──

    async def diff_against_catalog(self) -> RoleStateDiff:
        """Compute what `reconcile_from_catalog` would change without
        mutating anything. Cheap to call repeatedly. Returns a
        structured diff that operators can inspect via
        ``GET /admin/role-state`` before triggering a reconcile.

        All passes use bulk catalog queries (one `pg_roles`, one
        `pg_auth_members`, one `information_schema.role_table_grants`)
        so the total cost is O(catalog rows) regardless of vault count.
        Per-table `has_table_privilege` introspection is explicitly
        avoided.
        """
        diff = RoleStateDiff()
        async with self.pool.acquire() as conn:
            await self._diff_users(conn, diff)
            await self._diff_vaults(conn, diff)
            await self._diff_memberships(conn, diff)
            await self._diff_public_grants(conn, diff)
            await self._diff_authenticated(conn, diff)
            await self._diff_table_grants(conn, diff)
        return diff

    async def _diff_users(
        self, conn: asyncpg.Connection, diff: RoleStateDiff,
    ) -> None:
        rows = await conn.fetch("SELECT id FROM users")
        wanted = {user_role_name(r["id"]) for r in rows}
        existing = {
            r["rolname"]
            for r in await conn.fetch(
                "SELECT rolname FROM pg_roles WHERE rolname LIKE 'akb_user\\_%' ESCAPE '\\'"
            )
        }
        diff.missing_user_roles = sorted(wanted - existing)
        diff.orphan_user_roles = sorted(existing - wanted)

    async def _diff_vaults(
        self, conn: asyncpg.Connection, diff: RoleStateDiff,
    ) -> None:
        # MUST use the same vault set as _reconcile_vault_roles — ALL
        # vaults incl. archived (archived = read-only, roles preserved).
        # If you ever filter here, mirror it there or is_clean() never
        # converges (perpetual reconcile/diff flip-flop).
        rows = await conn.fetch("SELECT id FROM vaults")
        wanted: set[str] = set()
        for r in rows:
            for scope in ("reader", "writer", "admin"):
                wanted.add(vault_group_role_name(r["id"], scope))
        existing = {
            r["rolname"]
            for r in await conn.fetch(
                "SELECT rolname FROM pg_roles WHERE rolname LIKE 'akb_vault\\_%' ESCAPE '\\'"
            )
        }
        diff.missing_vault_roles = sorted(wanted - existing)
        diff.orphan_vault_roles = sorted(existing - wanted)

    async def _diff_memberships(
        self, conn: asyncpg.Connection, diff: RoleStateDiff,
    ) -> None:
        access_rows = await conn.fetch(
            "SELECT vault_id, user_id, role FROM vault_access"
        )
        # Pull current PG memberships in one go for cheaper comparison.
        pg_mems = await conn.fetch(
            """
            SELECT r.rolname AS group_role, m.rolname AS member_role
              FROM pg_auth_members am
              JOIN pg_roles r ON r.oid = am.roleid
              JOIN pg_roles m ON m.oid = am.member
             WHERE r.rolname LIKE 'akb_vault\\_%' ESCAPE '\\'
            """
        )
        actual = {(row["group_role"], row["member_role"]) for row in pg_mems}
        for ar in access_rows:
            group = vault_group_role_name(ar["vault_id"], ar["role"])
            member = user_role_name(ar["user_id"])
            if (group, member) not in actual:
                diff.missing_memberships.append(
                    {
                        "vault_id": str(ar["vault_id"]),
                        "user_id": str(ar["user_id"]),
                        "scope": ar["role"],
                    }
                )

    async def _diff_public_grants(
        self, conn: asyncpg.Connection, diff: RoleStateDiff,
    ) -> None:
        rows = await conn.fetch(
            "SELECT id, COALESCE(public_access, 'none') AS public_access FROM vaults"
        )
        # All akb_vault_* memberships that include akb_authenticated.
        auth_mems = await conn.fetch(
            """
            SELECT r.rolname AS group_role
              FROM pg_auth_members am
              JOIN pg_roles r ON r.oid = am.roleid
              JOIN pg_roles m ON m.oid = am.member
             WHERE m.rolname = $1
               AND r.rolname LIKE 'akb_vault\\_%' ESCAPE '\\'
            """,
            AUTHENTICATED_ROLE,
        )
        actual_grants = {row["group_role"] for row in auth_mems}
        wanted_grants: set[str] = set()
        for r in rows:
            scope = _public_access_scope(r["public_access"])
            if scope:
                role = vault_group_role_name(r["id"], scope)
                wanted_grants.add(role)
                if role not in actual_grants:
                    diff.missing_public_grants.append(
                        {
                            "vault_id": str(r["id"]),
                            "scope": scope,
                            "role": role,
                        }
                    )
        for stale in actual_grants - wanted_grants:
            diff.stale_public_grants.append({"role": stale})

    async def _diff_table_grants(
        self, conn: asyncpg.Connection, diff: RoleStateDiff,
    ) -> None:
        """Detect per-table GRANT drift on `vt_*` tables.

        Sources of drift this catches:
          - `on_table_create` hook fired but PG returned a transient
            error → GRANTs never applied. Next user akb_sql on the
            table returns 42501.
          - Operator manually REVOKE'd a privilege without realising
            the reconciler owns this state.
          - DB restore from a snapshot taken before some tables were
            granted.

        One catalog-wide query, set difference in memory. The expected
        privilege set per scope mirrors `_grant_table`."""
        # Lazy import — same pattern as the table-grants reconcile pass.
        from app.repositories import table_data_repo

        # 1. All existing GRANTs of an akb_vault_* role on a vt_* table.
        grant_rows = await conn.fetch(
            """
            SELECT grantee, table_name, privilege_type
              FROM information_schema.role_table_grants
             WHERE table_schema = 'public'
               AND table_name LIKE 'vt\\_%' ESCAPE '\\'
               AND grantee LIKE 'akb_vault\\_%' ESCAPE '\\'
            """
        )
        actual: dict[tuple[str, str], set[str]] = {}
        for r in grant_rows:
            actual.setdefault((r["grantee"], r["table_name"]), set()).add(
                r["privilege_type"],
            )

        # 2. Expected: every (vault_table, scope, expected_privs).
        table_rows = await conn.fetch(
            """
            SELECT vt.vault_id, vt.name, v.name AS vault_name
              FROM vault_tables vt
              JOIN vaults v ON vt.vault_id = v.id
            """
        )
        for tr in table_rows:
            pg_name = table_data_repo.pg_table_name(tr["vault_name"], tr["name"])
            if not _is_safe_pg_table_name(pg_name):
                continue
            for scope, expected in _EXPECTED_TABLE_PRIVS.items():
                role = vault_group_role_name(tr["vault_id"], scope)
                got = actual.get((role, pg_name), set())
                missing = expected - got
                if missing:
                    diff.missing_table_grants.append(
                        {
                            "vault_id": str(tr["vault_id"]),
                            "table": pg_name,
                            "scope": scope,
                            "role": role,
                            "missing_privileges": sorted(missing),
                        }
                    )

    async def _diff_authenticated(
        self, conn: asyncpg.Connection, diff: RoleStateDiff,
    ) -> None:
        exists = await conn.fetchval(
            "SELECT 1 FROM pg_roles WHERE rolname = $1", AUTHENTICATED_ROLE,
        )
        if not exists:
            diff.authenticated_role_missing = True
            return
        # All user roles that should be members.
        wanted_members = {
            user_role_name(r["id"])
            for r in await conn.fetch("SELECT id FROM users")
        }
        actual_members = {
            r["member_role"]
            for r in await conn.fetch(
                """
                SELECT m.rolname AS member_role
                  FROM pg_auth_members am
                  JOIN pg_roles r ON r.oid = am.roleid
                  JOIN pg_roles m ON m.oid = am.member
                 WHERE r.rolname = $1
                """,
                AUTHENTICATED_ROLE,
            )
        }
        diff.users_not_in_authenticated = sorted(wanted_members - actual_members)

    # ── Low-level helpers ──

    async def _create_role_if_missing(
        self, conn: asyncpg.Connection, role: str,
    ) -> None:
        try:
            await conn.execute(f'CREATE ROLE "{role}" NOLOGIN')
        except asyncpg.exceptions.DuplicateObjectError:
            pass

    async def _drop_role_if_present(
        self, conn: asyncpg.Connection, role: str,
    ) -> None:
        # `DROP OWNED BY` must run before `DROP ROLE` if the role owns
        # any privileges/objects. UndefinedObjectError on the OWNED
        # phase means the role doesn't exist — nothing further to do.
        # Any other failure on OWNED is reported but doesn't block the
        # DROP ROLE attempt; PG will still refuse if dependencies
        # remain and surface a clearer error there.
        try:
            await conn.execute(f'DROP OWNED BY "{role}"')
        except asyncpg.exceptions.UndefinedObjectError:
            return
        except Exception as e:  # noqa: BLE001
            logger.warning("DROP OWNED BY %s failed (continuing to DROP ROLE): %s", role, e)
        try:
            await conn.execute(f'DROP ROLE IF EXISTS "{role}"')
        except asyncpg.exceptions.UndefinedObjectError:
            pass

    async def _grant_membership(
        self,
        conn: asyncpg.Connection,
        group_role: str,
        member_role: str,
    ) -> None:
        """`GRANT group_role TO member_role`. Idempotent — re-grants are
        no-ops in PG."""
        await conn.execute(f'GRANT "{group_role}" TO "{member_role}"')

    async def _revoke_membership_if_present(
        self,
        conn: asyncpg.Connection,
        group_role: str,
        member_role: str,
    ) -> None:
        try:
            await conn.execute(f'REVOKE "{group_role}" FROM "{member_role}"')
        except asyncpg.exceptions.UndefinedObjectError:
            pass
        except asyncpg.exceptions.InvalidGrantOperationError:
            pass

    async def _grant_table(
        self,
        conn: asyncpg.Connection,
        vault_id: uuid.UUID | str,
        pg_table_name: str,
    ) -> None:
        """Apply the three vault group-role GRANTs on a vt_* table.
        Caller must validate `pg_table_name` shape."""
        reader = vault_group_role_name(vault_id, "reader")
        writer = vault_group_role_name(vault_id, "writer")
        admin = vault_group_role_name(vault_id, "admin")
        tbl = f'public."{pg_table_name}"'
        # Idempotent — GRANT is no-op on re-apply.
        await conn.execute(f'GRANT SELECT ON {tbl} TO "{reader}"')
        await conn.execute(
            f'GRANT SELECT, INSERT, UPDATE, DELETE ON {tbl} TO "{writer}"'
        )
        await conn.execute(f'GRANT ALL ON {tbl} TO "{admin}"')


# ── Singleton accessor ────────────────────────────────────────


_role_sync: Optional[RoleSync] = None


def get_role_sync() -> RoleSync:
    """Return the process-global RoleSync. Must be initialized once
    in `lifecycle.init_storage` after the pool is ready."""
    if _role_sync is None:
        raise RuntimeError("RoleSync not initialized — call set_role_sync() in lifespan")
    return _role_sync


def set_role_sync(rs: RoleSync) -> None:
    global _role_sync
    _role_sync = rs
