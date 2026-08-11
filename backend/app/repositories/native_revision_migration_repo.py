"""PostgreSQL primitives for the bounded C9 migration bridge.

The repository deliberately owns only migration-run, inventory-item, and
legacy-selector mapping state.  It never writes a legacy document and every
authority publication is composed by the backfill service on a caller-owned
transaction.
"""

from __future__ import annotations

import re
import secrets
import uuid
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import Any

import asyncpg

from app.exceptions import ConflictError
from app.repositories.native_revision_repo import NativeRevisionIdCollisionError


_OID_RE = re.compile(r"^[0-9a-f]{40}$")
_DIGEST_RE = re.compile(r"^[0-9a-f]{64}$")
_ERROR_CODE_RE = re.compile(r"^[a-z][a-z0-9_]*$")


class MigrationInventoryDriftError(ConflictError):
    """The immutable run key was observed with different inventory facts."""

    def __init__(self, message: str = "fixed-ref migration inventory drifted"):
        super().__init__(message)
        self.code = "native_revision_migration_inventory_drift"


class MigrationIntegrityError(ConflictError):
    """Persisted bridge facts do not match the worker's frozen input."""

    def __init__(self, message: str):
        super().__init__(message)
        self.code = "native_revision_migration_integrity"


@dataclass(frozen=True, slots=True)
class MigrationRun:
    run_id: uuid.UUID
    namespace_id: uuid.UUID
    fixed_git_oid: str
    coverage_version: str
    inventory_digest: str
    status: str
    created_at: Any
    started_at: Any
    completed_at: Any
    error: str | None


@dataclass(frozen=True, slots=True)
class MigrationItem:
    run_id: uuid.UUID
    namespace_id: uuid.UUID
    legacy_document_id: uuid.UUID
    native_resource_id: uuid.UUID
    captured_path: str
    legacy_head_oid: str
    native_head_revision_id: str | None
    body_digest: str
    byte_size: int
    status: str
    error_code: str | None


@dataclass(frozen=True, slots=True)
class LegacyRevisionMapping:
    namespace_id: uuid.UUID
    resource_id: uuid.UUID
    legacy_git_oid: str
    path_at_revision: str
    resolution: str
    native_revision_id: str | None
    run_id: uuid.UUID
    lineage_ordinal: int
    fixed_git_oid: str


def _require_oid(value: str, field: str) -> str:
    if not isinstance(value, str) or _OID_RE.fullmatch(value) is None:
        raise ValueError(f"{field} must be a full lowercase 40-hex OID")
    return value


def _require_digest(value: str, field: str) -> str:
    if not isinstance(value, str) or _DIGEST_RE.fullmatch(value) is None:
        raise ValueError(f"{field} must be a lowercase 64-hex digest")
    return value


def _run(row: asyncpg.Record) -> MigrationRun:
    return MigrationRun(
        run_id=row["run_id"],
        namespace_id=row["namespace_id"],
        fixed_git_oid=row["fixed_git_oid"],
        coverage_version=row["coverage_version"],
        inventory_digest=row["inventory_digest"],
        status=row["status"],
        created_at=row["created_at"],
        started_at=row["started_at"],
        completed_at=row["completed_at"],
        error=row["error"],
    )


def _item(row: asyncpg.Record) -> MigrationItem:
    return MigrationItem(
        run_id=row["run_id"],
        namespace_id=row["namespace_id"],
        legacy_document_id=row["legacy_document_id"],
        native_resource_id=row["native_resource_id"],
        captured_path=row["captured_path"],
        legacy_head_oid=row["legacy_head_oid"],
        native_head_revision_id=row["native_head_revision_id"],
        body_digest=row["body_digest"],
        byte_size=row["byte_size"],
        status=row["status"],
        error_code=row["error_code"],
    )


def _mapping(row: asyncpg.Record) -> LegacyRevisionMapping:
    return LegacyRevisionMapping(
        namespace_id=row["namespace_id"],
        resource_id=row["resource_id"],
        legacy_git_oid=row["legacy_git_oid"],
        path_at_revision=row["path_at_revision"],
        resolution=row["resolution"],
        native_revision_id=row["native_revision_id"],
        run_id=row["run_id"],
        lineage_ordinal=row["lineage_ordinal"],
        fixed_git_oid=row["fixed_git_oid"],
    )


class NativeRevisionMigrationRepository:
    """Caller-transaction-friendly repository for migration bridge facts."""

    def __init__(self, pool: asyncpg.Pool):
        self.pool = pool

    async def get_manual_vault(
        self, namespace_id: uuid.UUID, *, conn: asyncpg.Connection | None = None,
    ) -> dict | None:
        sql = """
            SELECT v.id, v.name, v.git_path,
                   (eg.vault_id IS NOT NULL) AS has_external_git
              FROM vaults v
             LEFT JOIN vault_external_git eg ON eg.vault_id = v.id
             WHERE v.id = $1
               AND v.status = 'active'
        """
        if conn is not None:
            row = await conn.fetchrow(sql, namespace_id)
        else:
            async with self.pool.acquire() as acquired:
                row = await acquired.fetchrow(sql, namespace_id)
        return dict(row) if row is not None else None

    @staticmethod
    async def list_documents(
        conn: asyncpg.Connection, namespace_id: uuid.UUID,
    ) -> list[dict]:
        rows = await conn.fetch(
            """
            SELECT id, path, current_commit, created_at, source
              FROM documents
             WHERE vault_id = $1
             ORDER BY id
            """,
            namespace_id,
        )
        return [dict(row) for row in rows]

    @staticmethod
    async def list_document_aliases(
        conn: asyncpg.Connection, namespace_id: uuid.UUID, resource_id: uuid.UUID,
    ) -> list[dict]:
        rows = await conn.fetch(
            """
            SELECT old_ref, created_at
              FROM resource_aliases
             WHERE vault_id = $1
               AND resource_type = 'document'
               AND resource_id = $2
             ORDER BY created_at, old_ref
            """,
            namespace_id,
            resource_id,
        )
        return [dict(row) for row in rows]

    async def get_run(
        self, run_id: uuid.UUID, *, conn: asyncpg.Connection | None = None,
    ) -> MigrationRun | None:
        sql = """
            SELECT run_id, namespace_id, fixed_git_oid, coverage_version,
                   inventory_digest, status, created_at, started_at,
                   completed_at, error
              FROM native_revision_migration_runs
             WHERE run_id = $1
        """
        if conn is not None:
            row = await conn.fetchrow(sql, run_id)
        else:
            async with self.pool.acquire() as acquired:
                row = await acquired.fetchrow(sql, run_id)
        return _run(row) if row is not None else None

    async def get_or_create_run(
        self,
        *,
        namespace_id: uuid.UUID,
        fixed_git_oid: str,
        coverage_version: str,
        inventory_digest: str,
        conn: asyncpg.Connection | None = None,
    ) -> MigrationRun:
        _require_oid(fixed_git_oid, "fixed_git_oid")
        _require_digest(inventory_digest, "inventory_digest")
        if not isinstance(coverage_version, str) or not coverage_version.strip():
            raise ValueError("coverage_version must be non-empty")

        async def _get_or_create(acquired: asyncpg.Connection) -> MigrationRun:
            row = await acquired.fetchrow(
                """
                SELECT run_id, namespace_id, fixed_git_oid, coverage_version,
                       inventory_digest, status, created_at, started_at,
                       completed_at, error
                  FROM native_revision_migration_runs
                 WHERE namespace_id = $1
                   AND fixed_git_oid = $2
                   AND coverage_version = $3
                 FOR UPDATE
                """,
                namespace_id,
                fixed_git_oid,
                coverage_version,
            )
            if row is None:
                await acquired.execute(
                    """
                    INSERT INTO native_revision_migration_runs
                        (namespace_id, fixed_git_oid, coverage_version, inventory_digest)
                    VALUES ($1, $2, $3, $4)
                    ON CONFLICT (namespace_id, fixed_git_oid, coverage_version)
                    DO NOTHING
                    """,
                    namespace_id,
                    fixed_git_oid,
                    coverage_version,
                    inventory_digest,
                )
                row = await acquired.fetchrow(
                    """
                    SELECT run_id, namespace_id, fixed_git_oid, coverage_version,
                           inventory_digest, status, created_at, started_at,
                           completed_at, error
                      FROM native_revision_migration_runs
                     WHERE namespace_id = $1
                       AND fixed_git_oid = $2
                       AND coverage_version = $3
                     FOR UPDATE
                    """,
                    namespace_id,
                    fixed_git_oid,
                    coverage_version,
                )
            if row is None:
                raise MigrationIntegrityError("migration run disappeared during creation")
            if row["inventory_digest"] != inventory_digest:
                raise MigrationInventoryDriftError()
            return _run(row)

        if conn is not None:
            return await _get_or_create(conn)
        async with self.pool.acquire() as acquired:
            async with acquired.transaction():
                return await _get_or_create(acquired)

    async def ensure_pending_items(
        self,
        run: MigrationRun,
        items: Iterable[dict],
        *,
        conn: asyncpg.Connection | None = None,
    ) -> list[MigrationItem]:
        """Insert the pre-authority inventory, preserving completed work."""

        async def _ensure(acquired: asyncpg.Connection) -> list[MigrationItem]:
            result: list[MigrationItem] = []
            for observed in items:
                document_id = observed["legacy_document_id"]
                expected = (
                    run.run_id,
                    run.namespace_id,
                    document_id,
                    document_id,
                    observed["captured_path"],
                    observed["legacy_head_oid"],
                    observed["body_digest"],
                    observed["byte_size"],
                )
                row = await acquired.fetchrow(
                    """
                    SELECT run_id, namespace_id, legacy_document_id,
                           native_resource_id, captured_path, legacy_head_oid,
                           native_head_revision_id, body_digest, byte_size,
                           status, error_code
                      FROM native_revision_migration_items
                     WHERE run_id = $1 AND legacy_document_id = $2
                     FOR UPDATE
                    """,
                    run.run_id,
                    document_id,
                )
                if row is None:
                    await acquired.execute(
                        """
                        INSERT INTO native_revision_migration_items
                            (run_id, namespace_id, legacy_document_id,
                             native_resource_id, captured_path, legacy_head_oid,
                             body_digest, byte_size, status)
                        VALUES ($1, $2, $3, $4, $5, $6, $7, $8, 'pending')
                        """,
                        *expected,
                    )
                else:
                    observed_identity = (
                        row["run_id"],
                        row["namespace_id"],
                        row["legacy_document_id"],
                        row["native_resource_id"],
                        row["captured_path"],
                        row["legacy_head_oid"],
                        row["body_digest"],
                        row["byte_size"],
                    )
                    if observed_identity != expected:
                        raise MigrationInventoryDriftError(
                            "migration item facts differ from the frozen inventory"
                        )
                    if row["status"] == "failed":
                        await acquired.execute(
                            """
                            UPDATE native_revision_migration_items
                               SET status = 'pending', error_code = NULL,
                                   updated_at = NOW()
                             WHERE run_id = $1 AND legacy_document_id = $2
                            """,
                            run.run_id,
                            document_id,
                        )
                refreshed = await acquired.fetchrow(
                    """
                    SELECT run_id, namespace_id, legacy_document_id,
                           native_resource_id, captured_path, legacy_head_oid,
                           native_head_revision_id, body_digest, byte_size,
                           status, error_code
                      FROM native_revision_migration_items
                     WHERE run_id = $1 AND legacy_document_id = $2
                    """,
                    run.run_id,
                    document_id,
                )
                if refreshed is None:
                    raise MigrationIntegrityError("migration item disappeared during insertion")
                result.append(_item(refreshed))
            return result

        if conn is not None:
            return await _ensure(conn)
        async with self.pool.acquire() as acquired:
            async with acquired.transaction():
                return await _ensure(acquired)

    async def list_items(
        self, run_id: uuid.UUID, *, conn: asyncpg.Connection | None = None,
    ) -> list[MigrationItem]:
        sql = """
            SELECT run_id, namespace_id, legacy_document_id, native_resource_id,
                   captured_path, legacy_head_oid, native_head_revision_id,
                   body_digest, byte_size, status, error_code
              FROM native_revision_migration_items
             WHERE run_id = $1
             ORDER BY legacy_document_id
        """
        if conn is not None:
            rows = await conn.fetch(sql, run_id)
        else:
            async with self.pool.acquire() as acquired:
                rows = await acquired.fetch(sql, run_id)
        return [_item(row) for row in rows]

    async def get_item(
        self,
        run_id: uuid.UUID,
        legacy_document_id: uuid.UUID,
        *,
        conn: asyncpg.Connection | None = None,
        for_update: bool = False,
    ) -> MigrationItem | None:
        suffix = " FOR UPDATE" if for_update else ""
        sql = """
            SELECT run_id, namespace_id, legacy_document_id, native_resource_id,
                   captured_path, legacy_head_oid, native_head_revision_id,
                   body_digest, byte_size, status, error_code
              FROM native_revision_migration_items
             WHERE run_id = $1 AND legacy_document_id = $2
        """ + suffix
        if conn is not None:
            row = await conn.fetchrow(sql, run_id, legacy_document_id)
        else:
            async with self.pool.acquire() as acquired:
                row = await acquired.fetchrow(sql, run_id, legacy_document_id)
        return _item(row) if row is not None else None

    @staticmethod
    async def mark_item_complete(
        conn: asyncpg.Connection,
        *,
        run_id: uuid.UUID,
        legacy_document_id: uuid.UUID,
        native_head_revision_id: str,
    ) -> None:
        _require_oid(native_head_revision_id, "native_head_revision_id")
        await conn.execute(
            """
            UPDATE native_revision_migration_items
               SET native_head_revision_id = $3,
                   status = 'complete',
                   error_code = NULL,
                   updated_at = NOW()
             WHERE run_id = $1 AND legacy_document_id = $2
            """,
            run_id,
            legacy_document_id,
            native_head_revision_id,
        )

    @staticmethod
    async def mark_item_failed(
        conn: asyncpg.Connection,
        *,
        run_id: uuid.UUID,
        legacy_document_id: uuid.UUID,
        error_code: str,
    ) -> None:
        if _ERROR_CODE_RE.fullmatch(error_code) is None:
            raise ValueError("error_code must be a stable lowercase token")
        await conn.execute(
            """
            UPDATE native_revision_migration_items
               SET native_head_revision_id = NULL,
                   status = 'failed',
                   error_code = $3,
                   updated_at = NOW()
             WHERE run_id = $1
               AND legacy_document_id = $2
               AND status <> 'complete'
            """,
            run_id,
            legacy_document_id,
            error_code,
        )

    async def set_run_status(
        self,
        run_id: uuid.UUID,
        status: str,
        *,
        error: str | None = None,
        conn: asyncpg.Connection | None = None,
    ) -> MigrationRun:
        if status not in {"planned", "running", "complete", "failed"}:
            raise ValueError("invalid migration run status")
        if status == "failed" and not error:
            raise ValueError("failed migration runs require an error")
        if status != "failed" and error is not None:
            raise ValueError("non-failed migration runs cannot carry an error")

        async def _set(acquired: asyncpg.Connection) -> MigrationRun:
            row = await acquired.fetchrow(
                """
                SELECT run_id, namespace_id, fixed_git_oid, coverage_version,
                       inventory_digest, status, created_at, started_at,
                       completed_at, error
                  FROM native_revision_migration_runs
                 WHERE run_id = $1
                 FOR UPDATE
                """,
                run_id,
            )
            if row is None:
                raise MigrationIntegrityError("migration run disappeared during status update")
            if row["status"] == "complete":
                return _run(row)
            await acquired.execute(
                """
                UPDATE native_revision_migration_runs
                   SET status = $2,
                       started_at = CASE
                           WHEN $2 = 'running' THEN COALESCE(started_at, NOW())
                           ELSE started_at
                       END,
                       completed_at = CASE
                           WHEN $2 IN ('complete', 'failed') THEN COALESCE(completed_at, NOW())
                           ELSE NULL
                       END,
                       error = $3
                 WHERE run_id = $1
                """,
                run_id,
                status,
                error,
            )
            row = await acquired.fetchrow(
                """
                SELECT run_id, namespace_id, fixed_git_oid, coverage_version,
                       inventory_digest, status, created_at, started_at,
                       completed_at, error
                  FROM native_revision_migration_runs
                 WHERE run_id = $1
                """,
                run_id,
            )
            if row is None:
                raise MigrationIntegrityError("migration run disappeared during status update")
            return _run(row)

        if conn is not None:
            return await _set(conn)
        async with self.pool.acquire() as acquired:
            async with acquired.transaction():
                return await _set(acquired)

    @staticmethod
    async def all_items_complete(
        conn: asyncpg.Connection, run_id: uuid.UUID,
    ) -> bool:
        return bool(
            await conn.fetchval(
                """
                SELECT NOT EXISTS (
                    SELECT 1
                      FROM native_revision_migration_items
                     WHERE run_id = $1 AND status <> 'complete'
                )
                """,
                run_id,
            )
        )

    @staticmethod
    async def ensure_mapping(
        conn: asyncpg.Connection,
        *,
        namespace_id: uuid.UUID,
        resource_id: uuid.UUID,
        legacy_git_oid: str,
        path_at_revision: str,
        resolution: str,
        native_revision_id: str | None,
        run_id: uuid.UUID,
        lineage_ordinal: int,
    ) -> LegacyRevisionMapping:
        _require_oid(legacy_git_oid, "legacy_git_oid")
        if resolution not in {"native", "bridge"}:
            raise ValueError("resolution must be native or bridge")
        if resolution == "native" and native_revision_id is None:
            raise ValueError("native mappings require native_revision_id")
        if resolution == "bridge" and native_revision_id is not None:
            raise ValueError("bridge mappings cannot carry native_revision_id")
        if native_revision_id is not None:
            _require_oid(native_revision_id, "native_revision_id")
        if not path_at_revision.strip() or lineage_ordinal < 0:
            raise ValueError("mapping path and lineage ordinal are invalid")

        row = await conn.fetchrow(
            """
            SELECT m.namespace_id, m.resource_id, m.legacy_git_oid,
                   m.path_at_revision, m.resolution, m.native_revision_id,
                   m.run_id, m.lineage_ordinal, r.fixed_git_oid
              FROM legacy_revision_mappings m
              JOIN native_revision_migration_runs r
                ON r.run_id = m.run_id AND r.namespace_id = m.namespace_id
             WHERE m.resource_id = $1 AND m.legacy_git_oid = $2
             FOR UPDATE
            """,
            resource_id,
            legacy_git_oid,
        )
        expected = (
            namespace_id,
            resource_id,
            legacy_git_oid,
            path_at_revision,
            resolution,
            native_revision_id,
            run_id,
            lineage_ordinal,
        )
        if row is None:
            await conn.execute(
                """
                INSERT INTO legacy_revision_mappings
                    (namespace_id, resource_id, legacy_git_oid, path_at_revision,
                     resolution, native_revision_id, run_id, lineage_ordinal)
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
                """,
                *expected,
            )
        else:
            observed = (
                row["namespace_id"],
                row["resource_id"],
                row["legacy_git_oid"],
                row["path_at_revision"],
                row["resolution"],
                row["native_revision_id"],
                row["run_id"],
                row["lineage_ordinal"],
            )
            if observed != expected:
                raise MigrationIntegrityError(
                    "legacy revision mapping conflicts with frozen lineage"
                )
        row = await conn.fetchrow(
            """
            SELECT m.namespace_id, m.resource_id, m.legacy_git_oid,
                   m.path_at_revision, m.resolution, m.native_revision_id,
                   m.run_id, m.lineage_ordinal, r.fixed_git_oid
              FROM legacy_revision_mappings m
              JOIN native_revision_migration_runs r
                ON r.run_id = m.run_id AND r.namespace_id = m.namespace_id
             WHERE m.resource_id = $1 AND m.legacy_git_oid = $2
            """,
            resource_id,
            legacy_git_oid,
        )
        if row is None:
            raise MigrationIntegrityError("mapping disappeared during insertion")
        return _mapping(row)

    async def list_resource_mappings(
        self,
        *,
        namespace_id: uuid.UUID,
        resource_id: uuid.UUID,
        conn: asyncpg.Connection | None = None,
    ) -> list[LegacyRevisionMapping]:
        """List completed immutable mappings for one Resource.

        Reconcile uses this read to prove that a later fixed-ref inventory is
        an extension of the already-published lineage.  It intentionally
        does not expose mappings from an incomplete run as selector authority.
        """

        sql = """
            SELECT m.namespace_id, m.resource_id, m.legacy_git_oid,
                   m.path_at_revision, m.resolution, m.native_revision_id,
                   m.run_id, m.lineage_ordinal, r.fixed_git_oid
              FROM legacy_revision_mappings m
              JOIN native_revision_migration_runs r
                ON r.run_id = m.run_id AND r.namespace_id = m.namespace_id
             WHERE m.namespace_id = $1
               AND m.resource_id = $2
               AND r.status = 'complete'
             ORDER BY m.lineage_ordinal, m.legacy_git_oid
        """
        if conn is not None:
            rows = await conn.fetch(sql, namespace_id, resource_id)
        else:
            async with self.pool.acquire() as acquired:
                rows = await acquired.fetch(sql, namespace_id, resource_id)
        return [_mapping(row) for row in rows]

    async def list_completed_lineage_anchors(
        self,
        *,
        namespace_id: uuid.UUID,
        conn: asyncpg.Connection | None = None,
    ) -> list[LegacyRevisionMapping]:
        """List immutable ordinal-zero anchors with one bulk namespace read."""

        sql = """
            SELECT m.namespace_id, m.resource_id, m.legacy_git_oid,
                   m.path_at_revision, m.resolution, m.native_revision_id,
                   m.run_id, m.lineage_ordinal, r.fixed_git_oid
              FROM legacy_revision_mappings m
              JOIN native_revision_migration_runs r
                ON r.run_id = m.run_id AND r.namespace_id = m.namespace_id
             WHERE m.namespace_id = $1
               AND m.lineage_ordinal = 0
               AND r.status = 'complete'
             ORDER BY m.resource_id, m.legacy_git_oid
        """
        if conn is not None:
            rows = await conn.fetch(sql, namespace_id)
        else:
            async with self.pool.acquire() as acquired:
                rows = await acquired.fetch(sql, namespace_id)
        return [_mapping(row) for row in rows]

    async def list_resource_mappings_for_reconcile(
        self,
        *,
        namespace_id: uuid.UUID,
        resource_id: uuid.UUID,
        run_id: uuid.UUID,
        conn: asyncpg.Connection | None = None,
    ) -> list[LegacyRevisionMapping]:
        """Read lineage with the explicit completed-item reconcile exception.

        Completed runs are always visible.  The only incomplete-run exception
        is the explicitly named reconcile run, and even then only after the
        corresponding migration item has committed as ``complete``.  This is
        intentionally separate from the public selector read above.
        """

        sql = """
            SELECT m.namespace_id, m.resource_id, m.legacy_git_oid,
                   m.path_at_revision, m.resolution, m.native_revision_id,
                   m.run_id, m.lineage_ordinal, r.fixed_git_oid
              FROM legacy_revision_mappings m
              JOIN native_revision_migration_runs r
                ON r.run_id = m.run_id AND r.namespace_id = m.namespace_id
              LEFT JOIN native_revision_migration_items current_item
                ON current_item.run_id = $3
               AND current_item.namespace_id = m.namespace_id
               AND current_item.native_resource_id = m.resource_id
             WHERE m.namespace_id = $1
               AND m.resource_id = $2
               AND (
                   r.status = 'complete'
                   OR (
                       m.run_id = $3
                       AND current_item.status = 'complete'
                   )
               )
             ORDER BY m.lineage_ordinal, m.legacy_git_oid
        """
        if conn is not None:
            rows = await conn.fetch(sql, namespace_id, resource_id, run_id)
        else:
            async with self.pool.acquire() as acquired:
                rows = await acquired.fetch(sql, namespace_id, resource_id, run_id)
        return [_mapping(row) for row in rows]

    async def mapping_for_native_revision(
        self,
        *,
        namespace_id: uuid.UUID,
        resource_id: uuid.UUID,
        native_revision_id: str,
        conn: asyncpg.Connection | None = None,
    ) -> LegacyRevisionMapping | None:
        """Return the completed legacy binding for a native Head revision."""

        _require_oid(native_revision_id, "native_revision_id")
        sql = """
            SELECT m.namespace_id, m.resource_id, m.legacy_git_oid,
                   m.path_at_revision, m.resolution, m.native_revision_id,
                   m.run_id, m.lineage_ordinal, r.fixed_git_oid
              FROM legacy_revision_mappings m
              JOIN native_revision_migration_runs r
                ON r.run_id = m.run_id AND r.namespace_id = m.namespace_id
             WHERE m.namespace_id = $1
               AND m.resource_id = $2
               AND m.native_revision_id = $3
               AND m.resolution = 'native'
               AND r.status = 'complete'
        """
        if conn is not None:
            row = await conn.fetchrow(sql, namespace_id, resource_id, native_revision_id)
        else:
            async with self.pool.acquire() as acquired:
                row = await acquired.fetchrow(sql, namespace_id, resource_id, native_revision_id)
        return _mapping(row) if row is not None else None

    async def mapping_for_native_revision_for_reconcile(
        self,
        *,
        namespace_id: uuid.UUID,
        resource_id: uuid.UUID,
        native_revision_id: str,
        run_id: uuid.UUID,
        conn: asyncpg.Connection | None = None,
    ) -> LegacyRevisionMapping | None:
        """Read a Head binding under reconcile's completed-item authority."""

        _require_oid(native_revision_id, "native_revision_id")
        sql = """
            SELECT m.namespace_id, m.resource_id, m.legacy_git_oid,
                   m.path_at_revision, m.resolution, m.native_revision_id,
                   m.run_id, m.lineage_ordinal, r.fixed_git_oid
              FROM legacy_revision_mappings m
              JOIN native_revision_migration_runs r
                ON r.run_id = m.run_id AND r.namespace_id = m.namespace_id
              LEFT JOIN native_revision_migration_items current_item
                ON current_item.run_id = $3
               AND current_item.namespace_id = m.namespace_id
               AND current_item.native_resource_id = m.resource_id
             WHERE m.namespace_id = $1
               AND m.resource_id = $2
               AND m.native_revision_id = $4
               AND m.resolution = 'native'
               AND (
                   r.status = 'complete'
                   OR (
                       m.run_id = $3
                       AND current_item.status = 'complete'
                   )
               )
        """
        if conn is not None:
            row = await conn.fetchrow(
                sql,
                namespace_id,
                resource_id,
                run_id,
                native_revision_id,
            )
        else:
            async with self.pool.acquire() as acquired:
                row = await acquired.fetchrow(
                    sql,
                    namespace_id,
                    resource_id,
                    run_id,
                    native_revision_id,
                )
        return _mapping(row) if row is not None else None

    async def migrated_resource_ids(
        self,
        *,
        namespace_id: uuid.UUID,
        conn: asyncpg.Connection | None = None,
    ) -> set[uuid.UUID]:
        """Return Resources with at least one completed bridge mapping."""

        sql = """
            SELECT DISTINCT m.resource_id
              FROM legacy_revision_mappings m
              JOIN native_revision_migration_runs r
                ON r.run_id = m.run_id AND r.namespace_id = m.namespace_id
             WHERE m.namespace_id = $1 AND r.status = 'complete'
        """
        if conn is not None:
            rows = await conn.fetch(sql, namespace_id)
        else:
            async with self.pool.acquire() as acquired:
                rows = await acquired.fetch(sql, namespace_id)
        return {row["resource_id"] for row in rows}

    @staticmethod
    async def ensure_cross_run_mapping(
        conn: asyncpg.Connection,
        *,
        namespace_id: uuid.UUID,
        resource_id: uuid.UUID,
        legacy_git_oid: str,
        path_at_revision: str,
        resolution: str,
        native_revision_id: str | None,
        run_id: uuid.UUID,
        lineage_ordinal: int,
    ) -> LegacyRevisionMapping:
        """Insert a new suffix mapping without re-homing an old mapping.

        A legacy OID is globally immutable for a Resource.  If another
        completed run already owns the exact facts, the existing row is
        returned as-is; its original ``run_id`` and ordinal are never changed.
        A non-complete row from another run is rejected because it is not
        selector authority.
        """

        _require_oid(legacy_git_oid, "legacy_git_oid")
        if resolution not in {"native", "bridge"}:
            raise ValueError("resolution must be native or bridge")
        if resolution == "native" and native_revision_id is None:
            raise ValueError("native mappings require native_revision_id")
        if resolution == "bridge" and native_revision_id is not None:
            raise ValueError("bridge mappings cannot carry native_revision_id")
        if native_revision_id is not None:
            _require_oid(native_revision_id, "native_revision_id")
        if not isinstance(path_at_revision, str) or not path_at_revision.strip():
            raise ValueError("mapping path must be non-empty")
        if lineage_ordinal < 0:
            raise ValueError("lineage ordinal must be non-negative")

        select_sql = """
            SELECT m.namespace_id, m.resource_id, m.legacy_git_oid,
                   m.path_at_revision, m.resolution, m.native_revision_id,
                   m.run_id, m.lineage_ordinal, r.fixed_git_oid, r.status
              FROM legacy_revision_mappings m
              JOIN native_revision_migration_runs r
                ON r.run_id = m.run_id AND r.namespace_id = m.namespace_id
             WHERE m.resource_id = $1 AND m.legacy_git_oid = $2
             FOR UPDATE
        """
        row = await conn.fetchrow(select_sql, resource_id, legacy_git_oid)
        if row is None:
            await conn.execute(
                """
                INSERT INTO legacy_revision_mappings
                    (namespace_id, resource_id, legacy_git_oid, path_at_revision,
                     resolution, native_revision_id, run_id, lineage_ordinal)
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
                ON CONFLICT (resource_id, legacy_git_oid) DO NOTHING
                """,
                namespace_id,
                resource_id,
                legacy_git_oid,
                path_at_revision,
                resolution,
                native_revision_id,
                run_id,
                lineage_ordinal,
            )
            row = await conn.fetchrow(select_sql, resource_id, legacy_git_oid)
        if row is None:
            raise MigrationIntegrityError("cross-run mapping disappeared during insertion")
        if row["status"] != "complete" and row["run_id"] != run_id:
            raise MigrationIntegrityError("cross-run mapping belongs to an incomplete run")

        observed = (
            row["namespace_id"],
            row["resource_id"],
            row["legacy_git_oid"],
            row["path_at_revision"],
            row["resolution"],
            row["native_revision_id"],
        )
        expected = (
            namespace_id,
            resource_id,
            legacy_git_oid,
            path_at_revision,
            resolution,
            native_revision_id,
        )
        if observed != expected:
            raise MigrationIntegrityError("cross-run mapping conflicts with immutable lineage")

        return _mapping(row)

    async def allocate_native_revision_id(
        self,
        conn: asyncpg.Connection,
        *,
        resource_id: uuid.UUID,
        retained_legacy_oids: set[str],
        revision_id_factory: Callable[[], str] | None = None,
        attempts: int = 8,
    ) -> str:
        """Allocate a random opaque Revision token with both collision fences."""
        for oid in retained_legacy_oids:
            _require_oid(oid, "retained legacy OID")
        factory = revision_id_factory or (lambda: secrets.token_hex(20))
        for _ in range(attempts):
            candidate = factory()
            _require_oid(candidate, "native revision id")
            if candidate in retained_legacy_oids:
                continue
            collision = await conn.fetchval(
                """
                SELECT EXISTS (
                           SELECT 1 FROM native_revisions
                            WHERE revision_id = $1
                       )
                    OR EXISTS (
                           SELECT 1 FROM legacy_revision_mappings
                            WHERE resource_id = $2
                              AND legacy_git_oid = $1
                       )
                """,
                candidate,
                resource_id,
            )
            if not collision:
                return candidate
        raise NativeRevisionIdCollisionError()

    async def exact_native_revision(
        self,
        *,
        namespace_id: uuid.UUID,
        resource_id: uuid.UUID,
        revision_id: str,
        conn: asyncpg.Connection | None = None,
    ) -> dict | None:
        sql = """
            SELECT n.revision_id, n.namespace_id, n.resource_id,
                   n.path_at_revision
              FROM native_revisions n
             WHERE n.namespace_id = $1
               AND n.resource_id = $2
               AND n.revision_id = $3
               AND EXISTS (
                   SELECT 1
                     FROM legacy_revision_mappings m
                     JOIN native_revision_migration_runs r
                       ON r.run_id = m.run_id
                      AND r.namespace_id = m.namespace_id
                    WHERE m.namespace_id = n.namespace_id
                      AND m.resource_id = n.resource_id
                      AND m.native_revision_id = n.revision_id
                      AND m.resolution = 'native'
                      AND r.status = 'complete'
               )
        """
        if conn is not None:
            row = await conn.fetchrow(sql, namespace_id, resource_id, revision_id)
        else:
            async with self.pool.acquire() as acquired:
                row = await acquired.fetchrow(sql, namespace_id, resource_id, revision_id)
        return dict(row) if row is not None else None

    async def exact_mapping(
        self,
        *,
        resource_id: uuid.UUID,
        legacy_git_oid: str,
        conn: asyncpg.Connection | None = None,
    ) -> LegacyRevisionMapping | None:
        sql = """
            SELECT m.namespace_id, m.resource_id, m.legacy_git_oid,
                   m.path_at_revision, m.resolution, m.native_revision_id,
                   m.run_id, m.lineage_ordinal, r.fixed_git_oid
              FROM legacy_revision_mappings m
             JOIN native_revision_migration_runs r
                ON r.run_id = m.run_id AND r.namespace_id = m.namespace_id
             WHERE m.resource_id = $1 AND m.legacy_git_oid = $2
               AND r.status = 'complete'
        """
        if conn is not None:
            row = await conn.fetchrow(sql, resource_id, legacy_git_oid)
        else:
            async with self.pool.acquire() as acquired:
                row = await acquired.fetchrow(sql, resource_id, legacy_git_oid)
        return _mapping(row) if row is not None else None

    async def exact_mapping_for_reconcile(
        self,
        *,
        namespace_id: uuid.UUID,
        resource_id: uuid.UUID,
        legacy_git_oid: str,
        run_id: uuid.UUID,
        conn: asyncpg.Connection | None = None,
    ) -> LegacyRevisionMapping | None:
        """Read an exact mapping with the explicit reconcile-run exception."""

        _require_oid(legacy_git_oid, "legacy_git_oid")
        sql = """
            SELECT m.namespace_id, m.resource_id, m.legacy_git_oid,
                   m.path_at_revision, m.resolution, m.native_revision_id,
                   m.run_id, m.lineage_ordinal, r.fixed_git_oid
              FROM legacy_revision_mappings m
              JOIN native_revision_migration_runs r
                ON r.run_id = m.run_id AND r.namespace_id = m.namespace_id
              LEFT JOIN native_revision_migration_items current_item
                ON current_item.run_id = $3
               AND current_item.namespace_id = m.namespace_id
               AND current_item.native_resource_id = m.resource_id
             WHERE m.namespace_id = $1
               AND m.resource_id = $2
               AND m.legacy_git_oid = $4
               AND (
                   r.status = 'complete'
                   OR (
                       m.run_id = $3
                       AND current_item.status = 'complete'
                   )
               )
        """
        if conn is not None:
            row = await conn.fetchrow(
                sql,
                namespace_id,
                resource_id,
                run_id,
                legacy_git_oid,
            )
        else:
            async with self.pool.acquire() as acquired:
                row = await acquired.fetchrow(
                    sql,
                    namespace_id,
                    resource_id,
                    run_id,
                    legacy_git_oid,
                )
        return _mapping(row) if row is not None else None

    async def prefix_mappings(
        self,
        *,
        resource_id: uuid.UUID,
        legacy_git_prefix: str,
        conn: asyncpg.Connection | None = None,
    ) -> list[LegacyRevisionMapping]:
        sql = """
            SELECT m.namespace_id, m.resource_id, m.legacy_git_oid,
                   m.path_at_revision, m.resolution, m.native_revision_id,
                   m.run_id, m.lineage_ordinal, r.fixed_git_oid
              FROM legacy_revision_mappings m
             JOIN native_revision_migration_runs r
                ON r.run_id = m.run_id AND r.namespace_id = m.namespace_id
             WHERE m.resource_id = $1
               AND m.legacy_git_oid LIKE $2 || '%'
               AND r.status = 'complete'
             ORDER BY m.legacy_git_oid
        """
        if conn is not None:
            rows = await conn.fetch(sql, resource_id, legacy_git_prefix)
        else:
            async with self.pool.acquire() as acquired:
                rows = await acquired.fetch(sql, resource_id, legacy_git_prefix)
        return [_mapping(row) for row in rows]
