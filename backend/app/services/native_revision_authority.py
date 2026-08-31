"""Crash-safe authority bootstrap and startup validation for PostgreSQL Native."""

from __future__ import annotations

import hashlib
import inspect
import json
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import asyncpg

from app.config import Settings, settings


AUTHORITY_BACKEND = "postgres_native"
AUTHORITY_RECORD_KIND = "new_database"
EXISTING_AUTHORITY_RECORD_KIND = "existing_database_cutover"
AUTHORITY_MARKER_KEY = True
# A single, product-owned PostgreSQL advisory-lock namespace.  Bootstrap uses
# the session form across init_db(); startup uses the transaction form.
AUTHORITY_LOCK_KEY = 0x414B42524E415449

_AKB_SENTINEL_TABLES = (
    "schema_migrations",
    "users",
    "vaults",
    "documents",
    "native_resources",
    "native_revisions",
)
_AUTHORITY_CONTENT_TABLES = (
    "vaults",
    "documents",
    "vault_files",
    "vault_tables",
    "native_resources",
    "native_revisions",
    "native_revision_activity",
    "native_invalidation_intents",
    "native_revision_migration_runs",
    "native_revision_migration_items",
    "legacy_revision_mappings",
)

Failpoint = Callable[[str], Any]
ExistingCutoverRevalidator = Callable[[asyncpg.Connection], Awaitable[None]]


class NativeAuthorityError(RuntimeError):
    """A Native authority preflight invariant failed."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class NativeAuthorityIdentity:
    tenant_id: str
    namespace: str
    database_id: uuid.UUID
    current_database: str
    runtime_image_digest: str

    @classmethod
    def from_settings(cls, configured: Settings = settings) -> "NativeAuthorityIdentity":
        if configured.document_revision_database_id is None:
            raise NativeAuthorityError(
                "native_authority_config_invalid",
                "postgres_native requires document_revision_database_id",
            )
        return cls(
            tenant_id=configured.document_revision_tenant_id,
            namespace=configured.document_revision_namespace,
            database_id=configured.document_revision_database_id,
            current_database=configured.db_name,
            runtime_image_digest=configured.document_revision_runtime_image_digest,
        )

    def params(self) -> tuple[Any, ...]:
        return (
            self.tenant_id,
            self.namespace,
            self.database_id,
            self.current_database,
            self.runtime_image_digest,
        )


def _binding_matches(row: asyncpg.Record | dict[str, Any], identity: NativeAuthorityIdentity) -> bool:
    """Match durable deployment identity, excluding the initializing image.

    The first image digest is immutable audit evidence.  A later AKB image may
    restart the same database without pretending to initialize it again.
    """
    return all(
        row[key] == value
        for key, value in (
            ("tenant_id", identity.tenant_id),
            ("namespace", identity.namespace),
            ("database_id", identity.database_id),
            ("current_database", identity.current_database),
        )
    )


def _initialization_matches(row: asyncpg.Record | dict[str, Any], identity: NativeAuthorityIdentity) -> bool:
    return _binding_matches(row, identity) and row["runtime_image_digest"] == identity.runtime_image_digest


def _canonical_digest(value: Any) -> str:
    payload = json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        default=str,
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


async def _trip(failpoint: Failpoint | None, name: str) -> None:
    if failpoint is None:
        return
    result = failpoint(name)
    if inspect.isawaitable(result):
        await result


async def _relation_exists(conn: asyncpg.Connection, relation: str) -> bool:
    return await conn.fetchval("SELECT to_regclass($1) IS NOT NULL", f"public.{relation}")


async def _existing_sentinels(conn: asyncpg.Connection) -> list[str]:
    rows = await conn.fetch(
        """
        SELECT tablename
          FROM pg_tables
         WHERE schemaname = 'public'
           AND tablename = ANY($1::text[])
         ORDER BY tablename
        """,
        list(_AKB_SENTINEL_TABLES),
    )
    return [row["tablename"] for row in rows]


async def _create_claim_table(conn: asyncpg.Connection) -> None:
    # This is intentionally the only AKB schema object created before init_db.
    # Migration 061 repeats the identical DDL with IF NOT EXISTS, then adds the
    # pending/marker tables and mutation guards.
    await conn.execute(
        """
        CREATE TABLE document_revision_bootstrap_claims (
            claim_key BOOLEAN PRIMARY KEY DEFAULT TRUE,
            claim_id UUID NOT NULL UNIQUE,
            record_kind TEXT NOT NULL DEFAULT 'new_database',
            tenant_id TEXT NOT NULL,
            namespace TEXT NOT NULL,
            database_id UUID NOT NULL,
            current_database TEXT NOT NULL,
            runtime_image_digest TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'claimed',
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            minted_at TIMESTAMPTZ,
            CONSTRAINT document_revision_bootstrap_claims_singleton_check CHECK (claim_key),
            CONSTRAINT document_revision_bootstrap_claims_kind_check CHECK (record_kind = 'new_database'),
            CONSTRAINT document_revision_bootstrap_claims_tenant_check CHECK (btrim(tenant_id) <> ''),
            CONSTRAINT document_revision_bootstrap_claims_namespace_check CHECK (btrim(namespace) <> ''),
            CONSTRAINT document_revision_bootstrap_claims_database_check CHECK (btrim(current_database) <> ''),
            CONSTRAINT document_revision_bootstrap_claims_digest_check
                CHECK (runtime_image_digest ~ '^sha256:[0-9a-f]{64}$'),
            CONSTRAINT document_revision_bootstrap_claims_status_check CHECK (status IN ('claimed', 'minted')),
            CONSTRAINT document_revision_bootstrap_claims_minted_shape CHECK (
                (status = 'claimed' AND minted_at IS NULL)
                OR (status = 'minted' AND minted_at IS NOT NULL)
            )
        )
        """
    )


async def mint_new_database_claim(
    conn: asyncpg.Connection,
    *,
    identity: NativeAuthorityIdentity,
    claim_id: uuid.UUID | None = None,
) -> uuid.UUID:
    """Claim a never-initialized AKB database before migrations.

    A matching claim can resume after interruption.  If no claim exists, any
    AKB sentinel table proves that this is not a new database even when all
    tenant rows were later deleted.
    """
    async with conn.transaction():
        await conn.execute("SELECT pg_advisory_xact_lock($1)", AUTHORITY_LOCK_KEY)
        if await _relation_exists(conn, "document_revision_bootstrap_claims"):
            rows = await conn.fetch("SELECT * FROM document_revision_bootstrap_claims FOR UPDATE")
            if not rows:
                raise NativeAuthorityError(
                    "native_authority_database_not_new",
                    "Native bootstrap cannot claim an AKB schema created without a pre-schema claim",
                )
            if len(rows) != 1:
                raise NativeAuthorityError(
                    "native_authority_claim_corrupt",
                    "Native bootstrap claim table must contain exactly one singleton record",
                )
            existing = rows[0]
            if (
                not _initialization_matches(existing, identity)
                or existing["record_kind"] != AUTHORITY_RECORD_KIND
                or existing["claim_key"] is not True
            ):
                raise NativeAuthorityError(
                    "native_authority_claim_mismatch",
                    "Native bootstrap claim identity mismatch or copied-database replay",
                )
            return existing["claim_id"]

        sentinels = await _existing_sentinels(conn)
        if sentinels:
            raise NativeAuthorityError(
                "native_authority_database_not_new",
                "Native bootstrap requires a never-initialized AKB database; "
                f"found sentinel table(s): {', '.join(sentinels)}",
            )

        await _create_claim_table(conn)
        claim_id = claim_id or uuid.uuid4()
        await conn.execute(
            """
            INSERT INTO document_revision_bootstrap_claims (
                claim_key, claim_id, tenant_id, namespace, database_id,
                current_database, runtime_image_digest
            ) VALUES (TRUE, $1, $2, $3, $4, $5, $6)
            """,
            claim_id,
            *identity.params(),
        )
        return claim_id


async def _content_inventory(conn: asyncpg.Connection) -> dict[str, int]:
    inventory: dict[str, int] = {}
    for table in _AUTHORITY_CONTENT_TABLES:
        inventory[table] = int(await conn.fetchval(f'SELECT COUNT(*) FROM "{table}"'))
    return inventory


async def _verified_cutover_binding(
    conn: asyncpg.Connection,
    cutover_id: uuid.UUID,
) -> tuple[asyncpg.Record, str]:
    run = await conn.fetchrow(
        """
        SELECT cutover_id, inventory_digest, verification_digest, status
          FROM native_revision_cutover_runs
         WHERE cutover_id = $1
        """,
        cutover_id,
    )
    if (
        run is None
        or run["status"] != "verified"
        or not isinstance(run["verification_digest"], str)
    ):
        raise NativeAuthorityError(
            "native_authority_cutover_not_verified",
            "Existing-database Native authority requires one verified cutover",
        )
    # The cutover receipt survives a source-vault lifecycle delete, while its
    # live migration run correctly cascades with that retired vault.
    vaults = await conn.fetch(
        """
        SELECT v.namespace_id, v.migration_run_id, v.fixed_git_oid,
               v.inventory_digest, v.verification_digest, v.status
          FROM native_revision_cutover_vaults v
         WHERE v.cutover_id = $1
         ORDER BY v.namespace_id
        """,
        cutover_id,
    )
    if not vaults or any(
        row["status"] != "verified"
        or not isinstance(row["verification_digest"], str)
        for row in vaults
    ):
        raise NativeAuthorityError(
            "native_authority_cutover_incomplete",
            "Existing-database Native authority requires every vault to be verified",
        )
    files_incomplete = await conn.fetchval(
        """
        SELECT EXISTS (
            SELECT 1
              FROM native_revision_cutover_files
             WHERE cutover_id = $1
               AND (
                    status <> 'verified'
                    OR verification_digest IS NULL
                    OR verification_digest !~ '^[0-9a-f]{64}$'
               )
        )
        """,
        cutover_id,
    )
    if files_incomplete:
        raise NativeAuthorityError(
            "native_authority_cutover_incomplete",
            "Existing-database Native authority requires every File to be verified",
        )
    binding = [
        {
            "namespace_id": str(row["namespace_id"]),
            "migration_run_id": str(row["migration_run_id"]),
            "fixed_git_oid": row["fixed_git_oid"],
            "inventory_digest": row["inventory_digest"],
            "verification_digest": row["verification_digest"],
        }
        for row in vaults
    ]
    return run, _canonical_digest(binding)


async def _activate_legacy_write_fence(
    conn: asyncpg.Connection,
    *,
    cutover_id: uuid.UUID,
) -> int:
    # SHARE ROW EXCLUSIVE conflicts with ordinary INSERT/UPDATE/DELETE's
    # ROW EXCLUSIVE lock while leaving the final read-only validation free to
    # inspect every source table. Once this transaction commits, the row-level
    # triggers reject any waiter that was admitted by a stopped Legacy image.
    await conn.execute(
        """
        LOCK TABLE vaults, collections, documents, resource_aliases,
                   vault_files, vault_external_git
        IN SHARE ROW EXCLUSIVE MODE
        """
    )
    fence = await conn.fetchrow(
        "SELECT * FROM native_revision_legacy_write_fence WHERE fence_key = TRUE FOR UPDATE"
    )
    if fence is None or fence["state"] != "open":
        raise NativeAuthorityError(
            "native_authority_legacy_fence_conflict",
            "Existing-database authority requires the open Legacy write fence",
        )
    row = await conn.fetchrow(
        """
        UPDATE native_revision_legacy_write_fence
           SET epoch = epoch + 1,
               state = 'fenced',
               cutover_id = $1,
               fenced_at = NOW()
         WHERE fence_key = TRUE AND state = 'open'
        RETURNING epoch
        """,
        cutover_id,
    )
    if row is None:
        raise NativeAuthorityError(
            "native_authority_legacy_fence_conflict",
            "Existing-database authority lost the Legacy write fence",
        )
    return int(row["epoch"])


async def _require_committed_legacy_fence(
    conn: asyncpg.Connection,
    authority: asyncpg.Record,
) -> None:
    fence = await conn.fetchrow(
        "SELECT state, epoch, cutover_id FROM native_revision_legacy_write_fence WHERE fence_key = TRUE"
    )
    if (
        fence is None
        or fence["state"] != "committed"
        or int(fence["epoch"]) != int(authority["legacy_write_epoch"])
        or fence["cutover_id"] != authority["cutover_id"]
    ):
        raise NativeAuthorityError(
            "native_authority_legacy_fence_mismatch",
            "Existing-database Native authority is not bound to its committed Legacy fence",
        )


async def mint_existing_database_authority(
    conn: asyncpg.Connection,
    *,
    identity: NativeAuthorityIdentity,
    cutover_id: uuid.UUID,
    authority_id: uuid.UUID | None = None,
    revalidate: ExistingCutoverRevalidator | None = None,
) -> uuid.UUID:
    """Fence Legacy writes, revalidate, and mint immutable Native authority."""
    async with conn.transaction():
        await conn.execute("SELECT pg_advisory_xact_lock($1)", AUTHORITY_LOCK_KEY)
        bootstrap_counts = await conn.fetchrow(
            """
            SELECT
                (SELECT COUNT(*) FROM document_revision_bootstrap_claims) AS claims,
                (SELECT COUNT(*) FROM document_revision_authority_pending) AS pending,
                (SELECT COUNT(*) FROM document_revision_authority_marker) AS markers
            """
        )
        if any(int(bootstrap_counts[name]) for name in bootstrap_counts.keys()):
            raise NativeAuthorityError(
                "native_authority_mode_conflict",
                "Existing-database cutover cannot coexist with new-database authority",
            )

        existing = await conn.fetchrow(
            "SELECT * FROM native_revision_existing_authority WHERE marker_id = TRUE FOR UPDATE"
        )
        if existing is not None:
            run, vault_binding_digest = await _verified_cutover_binding(conn, cutover_id)
            expected = {
                "cutover_id": cutover_id,
                "record_kind": EXISTING_AUTHORITY_RECORD_KIND,
                "backend": AUTHORITY_BACKEND,
                "inventory_digest": run["inventory_digest"],
                "verification_digest": run["verification_digest"],
                "vault_binding_digest": vault_binding_digest,
                "status": "committed",
            }
            if not _initialization_matches(existing, identity) or any(
                existing[key] != value for key, value in expected.items()
            ):
                raise NativeAuthorityError(
                    "native_authority_existing_mismatch",
                    "Existing-database Native authority does not match the verified cutover",
                )
            await _require_committed_legacy_fence(conn, existing)
            return existing["authority_id"]

        legacy_write_epoch = await _activate_legacy_write_fence(
            conn,
            cutover_id=cutover_id,
        )
        run, vault_binding_digest = await _verified_cutover_binding(conn, cutover_id)
        if revalidate is not None:
            await revalidate(conn)

        authority_id = authority_id or uuid.uuid4()
        await conn.execute(
            """
            INSERT INTO native_revision_existing_authority (
                marker_id, authority_id, cutover_id, tenant_id, namespace,
                database_id, current_database, runtime_image_digest,
                inventory_digest, verification_digest, vault_binding_digest,
                legacy_write_epoch, status, committed_at
            ) VALUES (
                TRUE, $1, $2, $3, $4, $5, $6, $7, $8, $9, $10,
                $11, 'committed', NOW()
            )
            """,
            authority_id,
            cutover_id,
            *identity.params(),
            run["inventory_digest"],
            run["verification_digest"],
            vault_binding_digest,
            legacy_write_epoch,
        )
        committed = await conn.execute(
            """
            UPDATE native_revision_legacy_write_fence
               SET state = 'committed', committed_at = NOW()
             WHERE fence_key = TRUE
               AND state = 'fenced'
               AND epoch = $1
               AND cutover_id = $2
            """,
            legacy_write_epoch,
            cutover_id,
        )
        if committed != "UPDATE 1":
            raise NativeAuthorityError(
                "native_authority_legacy_fence_conflict",
                "Existing-database authority could not commit the Legacy write fence",
            )
        return authority_id


async def consume_or_validate_existing_database_authority(
    conn: asyncpg.Connection,
    *,
    identity: NativeAuthorityIdentity,
    failpoint: Failpoint | None = None,
) -> str:
    """Validate the immutable existing-DB authority minted at cutover."""
    async with conn.transaction():
        await conn.execute("SELECT pg_advisory_xact_lock($1)", AUTHORITY_LOCK_KEY)
        await _trip(failpoint, "after_lock")
        row = await conn.fetchrow(
            "SELECT * FROM native_revision_existing_authority WHERE marker_id = TRUE FOR UPDATE"
        )
        if row is None:
            raise NativeAuthorityError(
                "native_authority_missing",
                "postgres_native requires an existing-database cutover authority record",
            )
        run, vault_binding_digest = await _verified_cutover_binding(conn, row["cutover_id"])
        if (
            not _binding_matches(row, identity)
            or row["record_kind"] != EXISTING_AUTHORITY_RECORD_KIND
            or row["backend"] != AUTHORITY_BACKEND
            or row["inventory_digest"] != run["inventory_digest"]
            or row["verification_digest"] != run["verification_digest"]
            or row["vault_binding_digest"] != vault_binding_digest
        ):
            raise NativeAuthorityError(
                "native_authority_existing_mismatch",
                "Existing-database Native authority binding drifted",
            )
        if row["status"] != "committed":
            raise NativeAuthorityError(
                "native_authority_existing_mismatch",
                "Existing-database authority was not committed at mint",
            )
        await _require_committed_legacy_fence(conn, row)
        await _trip(failpoint, "validated_marker")
        return "cutover_validated"


def git_authority_is_blank(root: str | Path) -> bool:
    """Return True when no bare repository below ``root`` contains a ref."""
    path = Path(root)
    if not path.exists():
        return True
    for bare in path.rglob("*.git"):
        refs = bare / "refs"
        if refs.is_dir() and any(candidate.is_file() for candidate in refs.rglob("*")):
            return False
        packed_refs = bare / "packed-refs"
        if packed_refs.is_file():
            for line in packed_refs.read_text(errors="replace").splitlines():
                if line and not line.startswith(("#", "^")):
                    return False
    return True


async def mint_pending_authority(
    conn: asyncpg.Connection,
    *,
    identity: NativeAuthorityIdentity,
    claim_id: uuid.UUID,
    git_storage_path: str | Path,
    authority_id: uuid.UUID | None = None,
) -> uuid.UUID:
    """Mint pending authority after migrations and zero-authority checks."""
    async with conn.transaction():
        await conn.execute("SELECT pg_advisory_xact_lock($1)", AUTHORITY_LOCK_KEY)
        claim = await conn.fetchrow(
            "SELECT * FROM document_revision_bootstrap_claims WHERE claim_key = TRUE FOR UPDATE"
        )
        if claim is None or claim["claim_id"] != claim_id or not _initialization_matches(claim, identity):
            raise NativeAuthorityError(
                "native_authority_claim_mismatch",
                "Native pending authority claim does not match configured identity",
            )

        existing = await conn.fetchrow(
            "SELECT * FROM document_revision_authority_pending WHERE claim_id = $1 FOR UPDATE",
            claim_id,
        )
        if claim["status"] == "minted":
            if existing is None or not _initialization_matches(existing, identity):
                raise NativeAuthorityError(
                    "native_authority_claim_corrupt",
                    "Minted Native claim has no matching pending authority",
                )
            return existing["authority_id"]
        if existing is not None:
            raise NativeAuthorityError(
                "native_authority_claim_corrupt",
                "Claimed Native bootstrap record already has pending authority",
            )

        if not await _relation_exists(conn, "schema_migrations"):
            raise NativeAuthorityError(
                "native_authority_schema_not_initialized",
                "Native pending authority requires completed AKB migrations",
            )
        inventory = await _content_inventory(conn)
        occupied = {name: count for name, count in inventory.items() if count}
        if occupied:
            raise NativeAuthorityError(
                "native_authority_inventory_not_empty",
                f"Native pending authority requires zero legacy and Native facts: {occupied}",
            )
        if not git_authority_is_blank(git_storage_path):
            raise NativeAuthorityError(
                "native_authority_git_not_blank",
                "Native pending authority requires Git storage with no repository refs",
            )

        authority_id = authority_id or uuid.uuid4()
        await conn.execute(
            """
            INSERT INTO document_revision_authority_pending (
                authority_id, claim_id, tenant_id, namespace, database_id,
                current_database, runtime_image_digest
            ) VALUES ($1, $2, $3, $4, $5, $6, $7)
            """,
            authority_id,
            claim_id,
            *identity.params(),
        )
        await conn.execute(
            """
            UPDATE document_revision_bootstrap_claims
               SET status = 'minted', minted_at = NOW()
             WHERE claim_key = TRUE AND status = 'claimed'
            """
        )
        return authority_id


async def consume_or_validate_native_authority(
    conn: asyncpg.Connection,
    *,
    identity: NativeAuthorityIdentity,
    failpoint: Failpoint | None = None,
) -> str:
    """Consume pending authority or validate the immutable durable marker."""
    async with conn.transaction():
        await conn.execute("SELECT pg_advisory_xact_lock($1)", AUTHORITY_LOCK_KEY)
        await _trip(failpoint, "after_lock")
        marker = await conn.fetchrow(
            "SELECT * FROM document_revision_authority_marker WHERE marker_id = TRUE FOR UPDATE"
        )
        if marker is not None:
            if not _binding_matches(marker, identity) or marker["backend"] != AUTHORITY_BACKEND:
                raise NativeAuthorityError(
                    "native_authority_marker_mismatch",
                    "Native authority marker does not match configured deployment identity",
                )
            pending = await conn.fetchrow(
                "SELECT status FROM document_revision_authority_pending WHERE authority_id = $1",
                marker["authority_id"],
            )
            if pending is None or pending["status"] != "consumed":
                raise NativeAuthorityError(
                    "native_authority_marker_incomplete",
                    "Native authority marker has no consumed pending record",
                )
            await _trip(failpoint, "validated_marker")
            return "validated"

        pending_rows = await conn.fetch(
            "SELECT * FROM document_revision_authority_pending WHERE status = 'pending' FOR UPDATE"
        )
        if len(pending_rows) != 1:
            raise NativeAuthorityError(
                "native_authority_missing",
                "postgres_native requires exactly one pending or initialized authority record",
            )
        pending = pending_rows[0]
        if not _initialization_matches(pending, identity) or pending["backend"] != AUTHORITY_BACKEND:
            raise NativeAuthorityError(
                "native_authority_pending_mismatch",
                "Native pending authority does not match configured tenant/database/image identity",
            )
        occupied = {name: count for name, count in (await _content_inventory(conn)).items() if count}
        if occupied:
            raise NativeAuthorityError(
                "native_authority_inventory_not_empty",
                f"First Native startup requires zero legacy and Native facts: {occupied}",
            )
        await _trip(failpoint, "before_marker")
        await conn.execute(
            """
            INSERT INTO document_revision_authority_marker (
                marker_id, authority_id, claim_id, tenant_id, namespace,
                database_id, current_database, runtime_image_digest
            ) VALUES (TRUE, $1, $2, $3, $4, $5, $6, $7)
            """,
            pending["authority_id"],
            pending["claim_id"],
            *identity.params(),
        )
        await _trip(failpoint, "after_marker")
        await conn.execute(
            """
            UPDATE document_revision_authority_pending
               SET status = 'consumed', consumed_at = NOW()
             WHERE authority_id = $1 AND status = 'pending'
            """,
            pending["authority_id"],
        )
        await _trip(failpoint, "after_consumed")
        return "initialized"


async def _reject_native_authority_for_non_product_mode(conn: asyncpg.Connection) -> None:
    async with conn.transaction():
        await conn.execute("SELECT pg_advisory_xact_lock($1)", AUTHORITY_LOCK_KEY)
        counts = await conn.fetchrow(
            """
            SELECT
                (SELECT COUNT(*) FROM document_revision_bootstrap_claims) AS claims,
                (SELECT COUNT(*) FROM document_revision_authority_pending) AS pending,
                (SELECT COUNT(*) FROM document_revision_authority_marker) AS markers,
                (SELECT COUNT(*) FROM native_revision_existing_authority) AS existing_cutovers
            """
        )
        if any(int(counts[name]) for name in counts.keys()):
            raise NativeAuthorityError(
                "native_authority_mode_conflict",
                "Bare Git and measurement modes reject a database claimed by postgres_native",
            )


async def startup_revision_authority_preflight(
    configured: Settings = settings,
    *,
    failpoint: Failpoint | None = None,
) -> str:
    """Validate authority after migrations and before workers or requests."""
    from app.db.postgres import get_pool

    pool = await get_pool()
    async with pool.acquire() as conn:
        if configured.document_revision_backend == "postgres_native":
            if await conn.fetchval(
                "SELECT COUNT(*) FROM native_revision_existing_authority"
            ):
                return await consume_or_validate_existing_database_authority(
                    conn,
                    identity=NativeAuthorityIdentity.from_settings(configured),
                    failpoint=failpoint,
                )
            return await consume_or_validate_native_authority(
                conn,
                identity=NativeAuthorityIdentity.from_settings(configured),
                failpoint=failpoint,
            )
        await _reject_native_authority_for_non_product_mode(conn)
        return "not_applicable"


async def pre_migration_revision_authority_guard(configured: Settings = settings) -> None:
    """Reject an invalid mode before ordinary startup is allowed to run DDL."""
    from app.db.postgres import get_pool

    pool = await get_pool()
    async with pool.acquire() as conn, conn.transaction():
        await conn.execute("SELECT pg_advisory_xact_lock($1)", AUTHORITY_LOCK_KEY)
        claim_table = await _relation_exists(conn, "document_revision_bootstrap_claims")
        claim_count = (
            int(await conn.fetchval("SELECT COUNT(*) FROM document_revision_bootstrap_claims"))
            if claim_table
            else 0
        )
        existing_table = await _relation_exists(conn, "native_revision_existing_authority")
        existing_count = (
            int(await conn.fetchval("SELECT COUNT(*) FROM native_revision_existing_authority"))
            if existing_table
            else 0
        )
        if configured.document_revision_backend == "postgres_native":
            if (claim_count, existing_count) not in {(1, 0), (0, 1)}:
                raise NativeAuthorityError(
                    "native_authority_missing",
                    "postgres_native startup requires exactly one new-database claim "
                    "or verified existing-database cutover authority",
                )
        elif claim_count or existing_count:
            raise NativeAuthorityError(
                "native_authority_mode_conflict",
                "Bare Git and measurement modes reject a database claimed by postgres_native",
            )


async def bootstrap_postgres_native(configured: Settings = settings) -> dict[str, str]:
    """Explicitly initialize a fresh database for stable postgres_native."""
    if configured.document_revision_backend != "postgres_native":
        raise NativeAuthorityError(
            "native_authority_mode_invalid",
            "initialize-postgres-native requires document_revision_backend=postgres_native",
        )

    from app.db.postgres import init_db

    identity = NativeAuthorityIdentity.from_settings(configured)
    conn = await asyncpg.connect(dsn=configured.asyncpg_dsn, command_timeout=30.0)
    try:
        await conn.execute("SELECT pg_advisory_lock($1)", AUTHORITY_LOCK_KEY)
        claim_id = await mint_new_database_claim(conn, identity=identity)
        await init_db()
        authority_id = await mint_pending_authority(
            conn,
            identity=identity,
            claim_id=claim_id,
            git_storage_path=configured.git_storage_path,
        )
        marker = await conn.fetchrow(
            "SELECT * FROM document_revision_authority_marker WHERE marker_id = TRUE"
        )
        status = "pending"
        if marker is not None:
            if not _binding_matches(marker, identity) or marker["backend"] != AUTHORITY_BACKEND:
                raise NativeAuthorityError(
                    "native_authority_marker_mismatch",
                    "Native authority marker does not match configured deployment identity",
                )
            pending = await conn.fetchrow(
                "SELECT status FROM document_revision_authority_pending WHERE authority_id = $1",
                marker["authority_id"],
            )
            if pending is None or pending["status"] != "consumed":
                raise NativeAuthorityError(
                    "native_authority_marker_incomplete",
                    "Native authority marker has no consumed pending record",
                )
            status = "initialized"
        return {
            "status": status,
            "backend": AUTHORITY_BACKEND,
            "claim_id": str(claim_id),
            "authority_id": str(authority_id),
            "database_id": str(identity.database_id),
        }
    finally:
        try:
            await conn.execute("SELECT pg_advisory_unlock($1)", AUTHORITY_LOCK_KEY)
        finally:
            await conn.close()
