"""Crash-safe authority bootstrap and startup validation for PostgreSQL Native."""

from __future__ import annotations

import hashlib
import inspect
import json
import uuid
from collections.abc import Callable
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

# These are the catalog, candidate, and receipt relations whose writes must be
# stopped while the caller performs the post-fence Git/S3 revalidation.  The
# three Legacy relations with an existing permanent trigger are deliberately
# absent from the transient-trigger set below; their old trigger behavior is
# part of the committed cutover contract.
_CUTOVER_LOCK_TABLES = (
    "vaults",
    "collections",
    "documents",
    "resource_aliases",
    "vault_files",
    "vault_external_git",
    "native_revision_cutover_runs",
    "native_revision_cutover_vaults",
    "native_revision_cutover_files",
    "native_revision_cutover_exclusions",
    "native_revision_migration_runs",
    "native_revision_migration_items",
    "legacy_revision_mappings",
    "native_revision_migration_inventories",
    "vault_tables",
    "m1_reference_payloads",
    "native_resources",
    "native_payload_manifests",
    "native_revisions",
    "native_revision_activity",
    "native_invalidation_intents",
    "native_resource_path_aliases",
    "native_derived_heads",
    "native_derived_chunks",
    "m1_file_transfer_intents",
    "native_file_projection_outbox",
)
_REQUIRED_CUTOVER_LOCK_TABLES = (
    "vaults",
    "collections",
    "documents",
    "resource_aliases",
    "vault_files",
    "vault_external_git",
    "native_revision_cutover_runs",
    "native_revision_cutover_vaults",
    "native_revision_cutover_files",
    "native_revision_cutover_exclusions",
    "native_revision_migration_runs",
    "native_revision_migration_items",
    "legacy_revision_mappings",
)
_PERMANENT_CUTOVER_GUARD_TABLES = {
    "documents",
    "resource_aliases",
    "vault_external_git",
}
_TRANSIENT_CUTOVER_GUARD_TABLES = tuple(
    table for table in _CUTOVER_LOCK_TABLES if table not in _PERMANENT_CUTOVER_GUARD_TABLES
)
_TRANSIENT_CUTOVER_GUARD_TRIGGER = "guard_native_revision_cutover_fenced_write"
_TRANSIENT_CUTOVER_TRUNCATE_TRIGGER = "guard_native_revision_cutover_fenced_truncate"
_AUTHORITY_FENCE_TABLE = "native_revision_existing_authority_fence"
Failpoint = Callable[[str], Any]
ExistingCutoverPreflight = Callable[[asyncpg.Connection], Any]
ExistingCutoverRevalidator = Callable[[asyncpg.Connection], Any]


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


@dataclass(frozen=True, slots=True)
class ExistingDatabaseAuthorityFence:
    """Durable phase-A receipt passed to the phase-B authority finalizer."""

    cutover_id: uuid.UUID
    fence_token: uuid.UUID
    legacy_write_epoch: int
    inventory_digest: str
    verification_digest: str
    vault_binding_digest: str
    authority_id: uuid.UUID | None = None
    state: str = "fenced"

    @property
    def epoch(self) -> int:
        """Short alias for callers that refer to the fence epoch directly."""
        return self.legacy_write_epoch

    @property
    def token(self) -> uuid.UUID:
        """Short alias for serialization/admission code."""
        return self.fence_token


# Keep the shorter name available to callers that use the fence as the API
# object rather than as a database-specific authority receipt.
ExistingAuthorityFence = ExistingDatabaseAuthorityFence


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


async def _available_cutover_tables(
    conn: asyncpg.Connection,
) -> tuple[str, ...]:
    available: list[str] = []
    for table in _CUTOVER_LOCK_TABLES:
        if await _relation_exists(conn, table):
            available.append(table)
    missing = [table for table in _REQUIRED_CUTOVER_LOCK_TABLES if table not in available]
    if missing:
        raise NativeAuthorityError(
            "native_authority_cutover_schema_incomplete",
            "Existing-database authority requires cutover tables: " + ", ".join(missing),
        )
    return tuple(available)


async def _lock_cutover_tables(conn: asyncpg.Connection) -> tuple[str, ...]:
    tables = await _available_cutover_tables(conn)
    qualified = ", ".join(f'public."{table}"' for table in tables)
    await conn.execute(
        f"LOCK TABLE {qualified} IN SHARE ROW EXCLUSIVE MODE"
    )
    return tables


async def _install_transient_cutover_guards(
    conn: asyncpg.Connection,
    tables: tuple[str, ...],
) -> None:
    for table in tables:
        if table not in _TRANSIENT_CUTOVER_GUARD_TABLES:
            continue
        await conn.execute(
            f"""
            DROP TRIGGER IF EXISTS {_TRANSIENT_CUTOVER_GUARD_TRIGGER}
                ON public."{table}";
            CREATE TRIGGER {_TRANSIENT_CUTOVER_GUARD_TRIGGER}
                BEFORE INSERT OR UPDATE OR DELETE ON public."{table}"
                FOR EACH ROW EXECUTE FUNCTION
                    reject_fenced_native_revision_cutover_write();
            DROP TRIGGER IF EXISTS {_TRANSIENT_CUTOVER_TRUNCATE_TRIGGER}
                ON public."{table}";
            CREATE TRIGGER {_TRANSIENT_CUTOVER_TRUNCATE_TRIGGER}
                BEFORE TRUNCATE ON public."{table}"
                FOR EACH STATEMENT EXECUTE FUNCTION
                    reject_fenced_native_revision_cutover_write();
            """
        )


async def _drop_transient_cutover_guards(
    conn: asyncpg.Connection,
    tables: tuple[str, ...],
) -> None:
    for table in tables:
        if table not in _TRANSIENT_CUTOVER_GUARD_TABLES:
            continue
        await conn.execute(
            f"""
            DROP TRIGGER IF EXISTS {_TRANSIENT_CUTOVER_GUARD_TRIGGER}
                ON public."{table}";
            DROP TRIGGER IF EXISTS {_TRANSIENT_CUTOVER_TRUNCATE_TRIGGER}
                ON public."{table}";
            """
        )


def _fence_from_row(
    row: asyncpg.Record | dict[str, Any],
    *,
    authority_id: uuid.UUID | None = None,
    state: str | None = None,
) -> ExistingDatabaseAuthorityFence:
    return ExistingDatabaseAuthorityFence(
        cutover_id=row["cutover_id"],
        fence_token=row["fence_token"],
        legacy_write_epoch=int(row["legacy_write_epoch"]),
        inventory_digest=row["inventory_digest"],
        verification_digest=row["verification_digest"],
        vault_binding_digest=row["vault_binding_digest"],
        authority_id=authority_id,
        state=state or "fenced",
    )


def _authority_fence_identity_matches(
    row: asyncpg.Record | dict[str, Any],
    identity: NativeAuthorityIdentity,
) -> bool:
    return all(
        row[key] == value
        for key, value in (
            ("tenant_id", identity.tenant_id),
            ("namespace", identity.namespace),
            ("database_id", identity.database_id),
            ("current_database", identity.current_database),
            ("runtime_image_digest", identity.runtime_image_digest),
        )
    )


def _cutover_snapshot_matches(
    row: asyncpg.Record | dict[str, Any],
    *,
    cutover_id: uuid.UUID,
    run: asyncpg.Record,
    vault_binding_digest: str,
) -> bool:
    return (
        row["cutover_id"] == cutover_id
        and row["inventory_digest"] == run["inventory_digest"]
        and row["verification_digest"] == run["verification_digest"]
        and row["vault_binding_digest"] == vault_binding_digest
    )


def _require_idle_authority_connection(conn: asyncpg.Connection) -> None:
    if conn.is_in_transaction():
        raise NativeAuthorityError(
            "native_authority_transaction_active",
            "Existing-database authority phases require an idle PostgreSQL connection",
        )


async def _invoke_existing_cutover_callback(
    callback: ExistingCutoverPreflight | None,
    conn: asyncpg.Connection,
) -> None:
    if callback is None:
        return
    result = callback(conn)
    if inspect.isawaitable(result):
        await result


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


async def _require_durable_authority_fence_schema(conn: asyncpg.Connection) -> None:
    """Require migration 096 rather than creating authority DDL at runtime."""
    if not await _relation_exists(conn, _AUTHORITY_FENCE_TABLE):
        raise NativeAuthorityError(
            "native_authority_fence_incomplete",
            "migration 096 must create the durable existing-database authority fence",
        )


async def _reject_new_database_authority_conflict(conn: asyncpg.Connection) -> None:
    counts = await conn.fetchrow(
        """
        SELECT
            (SELECT COUNT(*) FROM document_revision_bootstrap_claims) AS claims,
            (SELECT COUNT(*) FROM document_revision_authority_pending) AS pending,
            (SELECT COUNT(*) FROM document_revision_authority_marker) AS markers
        """
    )
    if any(int(counts[name]) for name in counts.keys()):
        raise NativeAuthorityError(
            "native_authority_mode_conflict",
            "Existing-database cutover cannot coexist with new-database authority",
        )


async def _fetch_legacy_write_fence(conn: asyncpg.Connection) -> asyncpg.Record:
    fence = await conn.fetchrow(
        "SELECT * FROM native_revision_legacy_write_fence "
        "WHERE fence_key = TRUE FOR UPDATE"
    )
    if fence is None:
        raise NativeAuthorityError(
            "native_authority_fence_incomplete",
            "Existing-database authority requires the Legacy write fence row",
        )
    return fence


async def _fetch_durable_authority_fence(
    conn: asyncpg.Connection,
) -> asyncpg.Record | None:
    return await conn.fetchrow(
        f'SELECT * FROM "{_AUTHORITY_FENCE_TABLE}" '
        "WHERE fence_key = TRUE FOR UPDATE"
    )


def _fence_identity_error() -> NativeAuthorityError:
    return NativeAuthorityError(
        "native_authority_fence_identity_mismatch",
        "Existing-database authority fence does not match the configured identity",
    )


def _fence_conflict_error() -> NativeAuthorityError:
    return NativeAuthorityError(
        "native_authority_fence_conflict",
        "Existing-database authority fence belongs to a different cutover",
    )


def _fence_incomplete_error(message: str) -> NativeAuthorityError:
    return NativeAuthorityError("native_authority_fence_incomplete", message)


async def _validate_existing_authority_fence(
    conn: asyncpg.Connection,
    *,
    existing: asyncpg.Record,
    durable: asyncpg.Record | None,
    legacy: asyncpg.Record,
    identity: NativeAuthorityIdentity,
    cutover_id: uuid.UUID,
) -> ExistingDatabaseAuthorityFence:
    """Validate an already committed authority and return its durable receipt."""
    if existing["cutover_id"] != cutover_id:
        raise _fence_conflict_error()
    if not _initialization_matches(existing, identity):
        raise _fence_identity_error()
    if durable is None:
        raise _fence_incomplete_error(
            "Committed existing-database authority has no phase-A fence receipt"
        )
    if durable["cutover_id"] != cutover_id:
        raise _fence_conflict_error()
    if not _authority_fence_identity_matches(durable, identity):
        raise _fence_identity_error()
    if legacy["state"] != "committed":
        raise _fence_incomplete_error(
            "Existing-database authority is not bound to a committed Legacy fence"
        )
    if int(durable["legacy_write_epoch"]) != int(legacy["epoch"]):
        raise _fence_incomplete_error(
            "Durable phase-A fence epoch does not match the Legacy fence"
        )
    run, vault_binding_digest = await _verified_cutover_binding(conn, cutover_id)
    expected = {
        "record_kind": EXISTING_AUTHORITY_RECORD_KIND,
        "backend": AUTHORITY_BACKEND,
        "inventory_digest": run["inventory_digest"],
        "verification_digest": run["verification_digest"],
        "vault_binding_digest": vault_binding_digest,
        "status": "committed",
    }
    if any(existing[key] != value for key, value in expected.items()):
        raise NativeAuthorityError(
            "native_authority_existing_mismatch",
            "Existing-database Native authority does not match the verified cutover",
        )
    if not _cutover_snapshot_matches(
        durable,
        cutover_id=cutover_id,
        run=run,
        vault_binding_digest=vault_binding_digest,
    ):
        raise _fence_incomplete_error(
            "Durable phase-A fence does not match the verified cutover"
        )
    await _require_committed_legacy_fence(conn, existing)
    return _fence_from_row(
        durable,
        authority_id=existing["authority_id"],
        state="committed",
    )


async def _validate_fenced_authority_receipt(
    conn: asyncpg.Connection,
    *,
    durable: asyncpg.Record | None,
    legacy: asyncpg.Record,
    identity: NativeAuthorityIdentity,
    cutover_id: uuid.UUID,
) -> tuple[asyncpg.Record, str]:
    """Validate the durable receipt that permits phase-A resume or phase B."""
    if legacy["state"] != "fenced":
        raise _fence_incomplete_error(
            "Existing-database authority is not in the fenced state"
        )
    if legacy["cutover_id"] != cutover_id:
        raise _fence_conflict_error()
    if durable is None:
        raise _fence_incomplete_error(
            "Legacy fence is fenced but has no durable phase-A receipt"
        )
    if durable["cutover_id"] != cutover_id:
        raise _fence_conflict_error()
    if not _authority_fence_identity_matches(durable, identity):
        raise _fence_identity_error()
    if int(durable["legacy_write_epoch"]) != int(legacy["epoch"]):
        raise _fence_incomplete_error(
            "Durable phase-A fence epoch does not match the Legacy fence"
        )
    run, vault_binding_digest = await _verified_cutover_binding(conn, cutover_id)
    if not _cutover_snapshot_matches(
        durable,
        cutover_id=cutover_id,
        run=run,
        vault_binding_digest=vault_binding_digest,
    ):
        raise _fence_incomplete_error(
            "Durable phase-A fence does not match the verified cutover"
        )
    return run, vault_binding_digest


async def begin_existing_database_authority(
    conn: asyncpg.Connection,
    *,
    identity: NativeAuthorityIdentity,
    cutover_id: uuid.UUID,
    preflight: ExistingCutoverPreflight | None = None,
) -> ExistingDatabaseAuthorityFence:
    """Durably fence one exact cutover in a short transaction.

    ``preflight`` runs only for the open -> fenced transition, after the
    original SHARE ROW EXCLUSIVE locks and the verified DB/catalog receipt are
    acquired.  It must be a bounded database-only check.  The returned receipt
    is safe to persist by the caller and can be recovered by calling this
    function again with the same cutover and identity after a crash.
    """
    _require_idle_authority_connection(conn)
    async with conn.transaction():
        await conn.execute("SELECT pg_advisory_xact_lock($1)", AUTHORITY_LOCK_KEY)
        await _require_durable_authority_fence_schema(conn)
        await _reject_new_database_authority_conflict(conn)
        tables = await _lock_cutover_tables(conn)
        legacy = await _fetch_legacy_write_fence(conn)
        durable = await _fetch_durable_authority_fence(conn)
        existing = await conn.fetchrow(
            "SELECT * FROM native_revision_existing_authority "
            "WHERE marker_id = TRUE FOR UPDATE"
        )

        if existing is not None:
            committed = await _validate_existing_authority_fence(
                conn,
                existing=existing,
                durable=durable,
                legacy=legacy,
                identity=identity,
                cutover_id=cutover_id,
            )
            await _drop_transient_cutover_guards(conn, tables)
            return committed

        if legacy["state"] == "open":
            if durable is not None:
                raise _fence_incomplete_error(
                    "Open Legacy fence already has a durable phase-A receipt"
                )
            if int(legacy["epoch"]) != 0:
                raise _fence_incomplete_error(
                    "Open Legacy fence has an invalid non-zero epoch"
                )
            run, vault_binding_digest = await _verified_cutover_binding(conn, cutover_id)
            await _invoke_existing_cutover_callback(preflight, conn)
            await _install_transient_cutover_guards(conn, tables)
            row = await conn.fetchrow(
                """
                UPDATE native_revision_legacy_write_fence
                   SET epoch = epoch + 1,
                       state = 'fenced',
                       cutover_id = $1,
                       fenced_at = NOW()
                 WHERE fence_key = TRUE AND state = 'open' AND epoch = 0
                RETURNING epoch
                """,
                cutover_id,
            )
            if row is None:
                raise NativeAuthorityError(
                    "native_authority_fence_conflict",
                    "Existing-database authority lost the open Legacy fence",
                )
            durable = await conn.fetchrow(
                f"""
                INSERT INTO "{_AUTHORITY_FENCE_TABLE}" (
                    fence_key, fence_token, cutover_id, legacy_write_epoch,
                    tenant_id, namespace, database_id, current_database,
                    runtime_image_digest, inventory_digest, verification_digest,
                    vault_binding_digest, fenced_at
                ) VALUES (
                    TRUE, $1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, NOW()
                )
                RETURNING *
                """,
                uuid.uuid4(),
                cutover_id,
                int(row["epoch"]),
                *identity.params(),
                run["inventory_digest"],
                run["verification_digest"],
                vault_binding_digest,
            )
            if durable is None:
                raise _fence_incomplete_error(
                    "Existing-database authority phase-A receipt was not stored"
                )
            return _fence_from_row(durable, state="fenced")

        if legacy["state"] == "fenced":
            await _validate_fenced_authority_receipt(
                conn,
                durable=durable,
                legacy=legacy,
                identity=identity,
                cutover_id=cutover_id,
            )
            await _install_transient_cutover_guards(conn, tables)
            assert durable is not None
            return _fence_from_row(durable, state="fenced")

        raise _fence_incomplete_error(
            "Committed Legacy fence has no committed existing-database authority"
        )


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


def _require_requested_fence_values(
    *,
    fence: ExistingDatabaseAuthorityFence | None,
    cutover_id: uuid.UUID | None,
    fence_token: uuid.UUID | None,
    legacy_write_epoch: int | None,
) -> tuple[uuid.UUID, uuid.UUID, int]:
    if fence is not None:
        values = (fence.cutover_id, fence.fence_token, fence.legacy_write_epoch)
        supplied = (cutover_id, fence_token, legacy_write_epoch)
        if any(value is not None and value != expected for value, expected in zip(supplied, values)):
            raise NativeAuthorityError(
                "native_authority_fence_token_mismatch",
                "Phase-B fence arguments do not match the phase-A receipt",
            )
        if fence.state not in {"fenced", "committed"}:
            raise _fence_incomplete_error("Phase-A receipt has an invalid state")
        return values
    if cutover_id is None or fence_token is None or legacy_write_epoch is None:
        raise NativeAuthorityError(
            "native_authority_fence_token_missing",
            "Phase B requires the exact phase-A cutover, token, and epoch",
        )
    return cutover_id, fence_token, int(legacy_write_epoch)


def _fence_arguments_match_receipt(
    durable: asyncpg.Record,
    *,
    cutover_id: uuid.UUID,
    fence_token: uuid.UUID,
    legacy_write_epoch: int,
    fence: ExistingDatabaseAuthorityFence | None,
) -> bool:
    if (
        durable["cutover_id"] != cutover_id
        or durable["fence_token"] != fence_token
        or int(durable["legacy_write_epoch"]) != legacy_write_epoch
    ):
        return False
    if fence is None:
        return True
    return all(
        getattr(fence, key) == durable[key]
        for key in ("inventory_digest", "verification_digest", "vault_binding_digest")
    )


async def finalize_existing_database_authority(
    conn: asyncpg.Connection,
    *,
    identity: NativeAuthorityIdentity,
    fence: ExistingDatabaseAuthorityFence | None = None,
    cutover_id: uuid.UUID | None = None,
    fence_token: uuid.UUID | None = None,
    legacy_write_epoch: int | None = None,
    authority_id: uuid.UUID | None = None,
) -> uuid.UUID:
    """Atomically mint immutable authority and commit an exact fenced cutover."""
    requested_cutover_id, requested_token, requested_epoch = _require_requested_fence_values(
        fence=fence,
        cutover_id=cutover_id,
        fence_token=fence_token,
        legacy_write_epoch=legacy_write_epoch,
    )
    _require_idle_authority_connection(conn)
    async with conn.transaction():
        await conn.execute("SELECT pg_advisory_xact_lock($1)", AUTHORITY_LOCK_KEY)
        await _require_durable_authority_fence_schema(conn)
        await _reject_new_database_authority_conflict(conn)
        tables = await _lock_cutover_tables(conn)
        legacy = await _fetch_legacy_write_fence(conn)
        durable = await _fetch_durable_authority_fence(conn)
        if durable is None:
            raise _fence_incomplete_error(
                "Phase B requires the durable phase-A fence receipt"
            )
        if not _fence_arguments_match_receipt(
            durable,
            cutover_id=requested_cutover_id,
            fence_token=requested_token,
            legacy_write_epoch=requested_epoch,
            fence=fence,
        ):
            raise NativeAuthorityError(
                "native_authority_fence_token_mismatch",
                "Phase-B token, epoch, or cutover does not match the durable phase-A receipt",
            )
        if legacy["cutover_id"] != requested_cutover_id or int(legacy["epoch"]) != requested_epoch:
            raise NativeAuthorityError(
                "native_authority_fence_token_mismatch",
                "Phase-B token and epoch do not match the Legacy fence",
            )
        if not _authority_fence_identity_matches(durable, identity):
            raise _fence_identity_error()

        existing = await conn.fetchrow(
            "SELECT * FROM native_revision_existing_authority "
            "WHERE marker_id = TRUE FOR UPDATE"
        )
        if existing is not None:
            committed = await _validate_existing_authority_fence(
                conn,
                existing=existing,
                durable=durable,
                legacy=legacy,
                identity=identity,
                cutover_id=requested_cutover_id,
            )
            await _drop_transient_cutover_guards(conn, tables)
            return committed.authority_id  # type: ignore[return-value]

        if legacy["state"] != "fenced":
            raise _fence_incomplete_error(
                "Phase B requires the exact Legacy fence to remain fenced"
            )
        run, vault_binding_digest = await _verified_cutover_binding(
            conn,
            requested_cutover_id,
        )
        if not _cutover_snapshot_matches(
            durable,
            cutover_id=requested_cutover_id,
            run=run,
            vault_binding_digest=vault_binding_digest,
        ):
            raise _fence_incomplete_error(
                "Phase-B verified cutover no longer matches the phase-A receipt"
            )

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
            requested_cutover_id,
            *identity.params(),
            run["inventory_digest"],
            run["verification_digest"],
            vault_binding_digest,
            requested_epoch,
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
            requested_epoch,
            requested_cutover_id,
        )
        if committed != "UPDATE 1":
            raise NativeAuthorityError(
                "native_authority_fence_conflict",
                "Existing-database authority could not commit the exact Legacy fence",
            )
        await _drop_transient_cutover_guards(conn, tables)
        return authority_id


async def mint_existing_database_authority(
    conn: asyncpg.Connection,
    *,
    identity: NativeAuthorityIdentity,
    cutover_id: uuid.UUID,
    authority_id: uuid.UUID | None = None,
    revalidate: ExistingCutoverRevalidator | None = None,
    preflight: ExistingCutoverPreflight | None = None,
) -> uuid.UUID:
    """Compatibility wrapper with long revalidation outside PostgreSQL transactions."""
    fence = await begin_existing_database_authority(
        conn,
        identity=identity,
        cutover_id=cutover_id,
        preflight=preflight,
    )
    if fence.authority_id is not None:
        return fence.authority_id
    # This callback is intentionally outside both short authority transactions.
    # It may perform the caller's Git/S3/full comparator work while the durable
    # fence triggers reject mutable cutover writes.
    await _invoke_existing_cutover_callback(revalidate, conn)
    return await finalize_existing_database_authority(
        conn,
        identity=identity,
        fence=fence,
        authority_id=authority_id,
    )


# Explicit names make the crash-resumable boundary available to callers while
# the wrapper above keeps the existing service integration source-compatible.
mint_existing_database_authority_phase_a = begin_existing_database_authority
mint_existing_database_authority_phase_b = finalize_existing_database_authority


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


async def _reject_incomplete_existing_authority_fence(
    conn: asyncpg.Connection,
) -> None:
    """Keep startup fail-closed if a durable fence has no committed authority."""
    if not await _relation_exists(conn, "native_revision_legacy_write_fence"):
        return
    fence = await conn.fetchrow(
        "SELECT state FROM native_revision_legacy_write_fence WHERE fence_key = TRUE"
    )
    if fence is None or fence["state"] == "open":
        return
    authority_count = 0
    if await _relation_exists(conn, "native_revision_existing_authority"):
        authority_count = int(
            await conn.fetchval("SELECT COUNT(*) FROM native_revision_existing_authority")
        )
    if fence["state"] == "fenced" or authority_count == 0:
        raise NativeAuthorityError(
            "native_authority_fence_incomplete",
            "Startup refuses a Legacy fence without committed existing-database authority",
        )


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
        if await _relation_exists(conn, "native_revision_legacy_write_fence"):
            state = await conn.fetchval(
                "SELECT state FROM native_revision_legacy_write_fence WHERE fence_key = TRUE"
            )
            if state in {"fenced", "committed"}:
                raise NativeAuthorityError(
                    "native_authority_mode_conflict",
                    "Bare Git and measurement modes reject an existing-database cutover fence",
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
            await _reject_incomplete_existing_authority_fence(conn)
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
        fence_state = None
        if await _relation_exists(conn, "native_revision_legacy_write_fence"):
            fence_state = await conn.fetchval(
                "SELECT state FROM native_revision_legacy_write_fence WHERE fence_key = TRUE"
            )
        if configured.document_revision_backend == "postgres_native":
            if fence_state == "fenced" or (fence_state == "committed" and existing_count == 0):
                raise NativeAuthorityError(
                    "native_authority_fence_incomplete",
                    "postgres_native startup refuses an uncommitted existing-database fence",
                )
            if (claim_count, existing_count) not in {(1, 0), (0, 1)}:
                raise NativeAuthorityError(
                    "native_authority_missing",
                    "postgres_native startup requires exactly one new-database claim "
                    "or verified existing-database cutover authority",
                )
        elif claim_count or existing_count or fence_state in {"fenced", "committed"}:
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
