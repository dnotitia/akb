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

Hierarchy: writer inherits reader; admin inherits writer. So a
`GRANT akb_vault_X_writer TO akb_user_Y` confers both write and
read in one statement.

Each `vault_access(user, vault, role)` row maps 1:1 to
`GRANT akb_vault_<vid>_<role> TO akb_user_<uid>`. The vault owner
gets `akb_vault_<vid>_admin`.

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

import logging
import re
import uuid
from dataclasses import dataclass, field
from typing import Optional

import asyncpg

logger = logging.getLogger("akb.role_sync")


# ── Identifier helpers ────────────────────────────────────────


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
    errors: list[str] = field(default_factory=list)

    def __str__(self) -> str:
        return (
            f"users(+{self.user_roles_created}/-{self.user_roles_dropped}) "
            f"vaults(+{self.vault_roles_created}/-{self.vault_roles_dropped}) "
            f"grants(+{self.grants_added}/-{self.grants_removed}) "
            f"table_grants({self.table_grants_applied}) "
            f"errors({len(self.errors)})"
        )


# ── RoleSync ─────────────────────────────────────────────────


class RoleSync:
    """Idempotent PG role + GRANT manager for AKB vault isolation."""

    def __init__(self, pool: asyncpg.Pool) -> None:
        self.pool = pool

    # ── Lifecycle hooks ──

    async def on_user_create(self, user_id: uuid.UUID | str) -> None:
        """Create `akb_user_<uid>` (NOLOGIN). Idempotent."""
        role = user_role_name(user_id)
        try:
            async with self.pool.acquire() as conn:
                await self._create_role_if_missing(conn, role)
        except Exception as e:  # noqa: BLE001
            logger.warning("on_user_create(%s) failed: %s", user_id, e)

    async def on_user_delete(self, user_id: uuid.UUID | str) -> None:
        """Drop `akb_user_<uid>`. Memberships in vault group roles are
        cleared automatically when the role is dropped."""
        role = user_role_name(user_id)
        try:
            async with self.pool.acquire() as conn:
                await self._drop_role_if_present(conn, role)
        except Exception as e:  # noqa: BLE001
            logger.warning("on_user_delete(%s) failed: %s", user_id, e)

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
            logger.warning("on_vault_create(%s) failed: %s", vault_id, e)

    async def on_vault_delete(self, vault_id: uuid.UUID | str) -> None:
        """Drop the three group roles. Dependent GRANTs are cleared by
        `DROP OWNED BY`. Memberships of dropped roles auto-clean."""
        try:
            async with self.pool.acquire() as conn:
                for scope in ("admin", "writer", "reader"):
                    role = vault_group_role_name(vault_id, scope)
                    await self._drop_role_if_present(conn, role)
        except Exception as e:  # noqa: BLE001
            logger.warning("on_vault_delete(%s) failed: %s", vault_id, e)

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
            logger.warning("on_grant(%s, %s, %s) failed: %s", vault_id, user_id, scope, e)

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
            logger.warning("on_revoke(%s, %s) failed: %s", vault_id, user_id, e)

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
            logger.warning(
                "on_ownership_transfer(%s, %s→%s) failed: %s",
                vault_id, old_owner_id, new_owner_id, e,
            )

    async def on_table_create(
        self,
        vault_id: uuid.UUID | str,
        pg_table_name: str,
    ) -> None:
        """Grant SELECT/INSERT/UPDATE/DELETE/ALL on the new table to the
        vault's reader/writer/admin group roles, matching their scope."""
        if not _is_safe_pg_table_name(pg_table_name):
            logger.error("on_table_create: unsafe pg_table_name %r — refusing", pg_table_name)
            return
        try:
            async with self.pool.acquire() as conn:
                await self._grant_table(conn, vault_id, pg_table_name)
        except Exception as e:  # noqa: BLE001
            logger.warning("on_table_create(%s, %s) failed: %s", vault_id, pg_table_name, e)

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
          1. For each user → ensure `akb_user_<uid>` exists.
             Drop any `akb_user_*` role not in the catalog.
          2. For each vault → ensure three group roles + hierarchy.
             Drop any `akb_vault_*_{reader,writer,admin}` not in catalog.
          3. For each vault_access row + each owner → grant membership.
             (Memberships not in catalog are not explicitly dropped
             here — they're attached to roles which themselves are
             owned by AKB. Dropping the parent role clears them.)
          4. For each vault_tables row → grant table-level perms.

        Idempotent. Safe to run repeatedly.
        """
        report = ReconcileReport()
        async with self.pool.acquire() as conn:
            await self._reconcile_user_roles(conn, report)
            await self._reconcile_vault_roles(conn, report)
            await self._reconcile_memberships(conn, report)
            await self._reconcile_table_grants(conn, report)

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
        for orphan in existing - wanted:
            try:
                await self._drop_role_if_present(conn, orphan)
                report.user_roles_dropped += 1
            except Exception as e:  # noqa: BLE001
                report.errors.append(f"DROP ROLE {orphan}: {e}")

    async def _reconcile_vault_roles(
        self, conn: asyncpg.Connection, report: ReconcileReport,
    ) -> None:
        rows = await conn.fetch("SELECT id, owner_id FROM vaults WHERE status != 'archived' OR status IS NULL")
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
        # `DROP OWNED BY` must run before `DROP ROLE` if the role owns any
        # privileges/objects. Both no-op when the role is absent.
        try:
            await conn.execute(f'DROP OWNED BY "{role}"')
        except asyncpg.exceptions.UndefinedObjectError:
            return
        except Exception:  # noqa: BLE001 — log and continue to DROP ROLE
            pass
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
