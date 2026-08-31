"""Persistence for a database-local, multi-vault Native cutover fixture."""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass
from typing import Any, Iterable

import asyncpg


_OID_RE = re.compile(r"^[0-9a-f]{40}$")
_DIGEST_RE = re.compile(r"^[0-9a-f]{64}$")
_STATUSES = ("planned", "applied", "verified")


@dataclass(frozen=True, slots=True)
class CutoverRun:
    cutover_id: uuid.UUID
    coverage_version: str
    inventory_digest: str
    status: str
    verification_digest: str | None
    created_at: Any
    applied_at: Any
    verified_at: Any
    aborted_from_status: str | None
    aborted_at: Any


@dataclass(frozen=True, slots=True)
class CutoverVault:
    cutover_id: uuid.UUID
    namespace_id: uuid.UUID
    migration_run_id: uuid.UUID
    fixed_git_oid: str
    inventory_digest: str
    status: str
    verification_digest: str | None
    applied_at: Any
    verified_at: Any


@dataclass(frozen=True, slots=True)
class CutoverFile:
    cutover_id: uuid.UUID
    namespace_id: uuid.UUID
    file_id: uuid.UUID
    logical_path: str
    mime_type: str
    content_hash: str
    byte_size: int
    s3_key: str
    etag: str | None
    storage_version: str | None
    created_by: str | None
    disposition: str
    status: str
    native_revision_id: str | None
    verification_digest: str | None
    applied_at: Any
    verified_at: Any


@dataclass(frozen=True, slots=True)
class CutoverExclusion:
    cutover_id: uuid.UUID
    namespace_id: uuid.UUID
    fixed_git_oid: str
    reason: str
    created_at: Any


def _run(row: asyncpg.Record) -> CutoverRun:
    return CutoverRun(**dict(row))


def _vault(row: asyncpg.Record) -> CutoverVault:
    return CutoverVault(**dict(row))


def _file(row: asyncpg.Record) -> CutoverFile:
    return CutoverFile(**dict(row))


def _exclusion(row: asyncpg.Record) -> CutoverExclusion:
    return CutoverExclusion(**dict(row))


class CutoverIntegrityError(RuntimeError):
    """Persisted cutover facts differ from the fixture's frozen plan."""


class NativeRevisionCutoverRepository:
    def __init__(self, pool: asyncpg.Pool):
        self.pool = pool

    async def get_run(
        self,
        cutover_id: uuid.UUID,
        *,
        conn: asyncpg.Connection | None = None,
    ) -> CutoverRun | None:
        sql = """
            SELECT cutover_id, coverage_version, inventory_digest, status,
                   verification_digest, created_at, applied_at, verified_at,
                   aborted_from_status, aborted_at
              FROM native_revision_cutover_runs
             WHERE cutover_id = $1
        """
        if conn is not None:
            row = await conn.fetchrow(sql, cutover_id)
        else:
            async with self.pool.acquire() as acquired:
                row = await acquired.fetchrow(sql, cutover_id)
        return _run(row) if row is not None else None

    async def list_vaults(
        self,
        cutover_id: uuid.UUID,
        *,
        conn: asyncpg.Connection | None = None,
    ) -> list[CutoverVault]:
        sql = """
            SELECT cutover_id, namespace_id, migration_run_id, fixed_git_oid,
                   inventory_digest, status, verification_digest,
                   applied_at, verified_at
              FROM native_revision_cutover_vaults
             WHERE cutover_id = $1
             ORDER BY namespace_id
        """
        if conn is not None:
            rows = await conn.fetch(sql, cutover_id)
        else:
            async with self.pool.acquire() as acquired:
                rows = await acquired.fetch(sql, cutover_id)
        return [_vault(row) for row in rows]

    async def list_files(
        self,
        cutover_id: uuid.UUID,
        *,
        conn: asyncpg.Connection | None = None,
    ) -> list[CutoverFile]:
        sql = """
            SELECT cutover_id, namespace_id, file_id, logical_path, mime_type,
                   content_hash, byte_size, s3_key, etag, storage_version,
                   created_by, disposition, status, native_revision_id,
                   verification_digest, applied_at, verified_at
              FROM native_revision_cutover_files
             WHERE cutover_id = $1
             ORDER BY namespace_id, file_id
        """
        if conn is not None:
            rows = await conn.fetch(sql, cutover_id)
        else:
            async with self.pool.acquire() as acquired:
                rows = await acquired.fetch(sql, cutover_id)
        return [_file(row) for row in rows]

    async def list_exclusions(
        self,
        cutover_id: uuid.UUID,
        *,
        conn: asyncpg.Connection | None = None,
    ) -> list[CutoverExclusion]:
        sql = """
            SELECT cutover_id, namespace_id, fixed_git_oid, reason, created_at
              FROM native_revision_cutover_exclusions
             WHERE cutover_id = $1
             ORDER BY namespace_id
        """
        if conn is not None:
            rows = await conn.fetch(sql, cutover_id)
        else:
            async with self.pool.acquire() as acquired:
                rows = await acquired.fetch(sql, cutover_id)
        return [_exclusion(row) for row in rows]

    async def get_or_create_run(
        self,
        *,
        coverage_version: str,
        inventory_digest: str,
        vaults: Iterable[CutoverVault],
        files: Iterable[CutoverFile] = (),
        exclusions: Iterable[CutoverExclusion] = (),
    ) -> CutoverRun:
        if not coverage_version.strip():
            raise ValueError("coverage_version must be non-empty")
        if _DIGEST_RE.fullmatch(inventory_digest) is None:
            raise ValueError("inventory_digest must be a lowercase 64-hex digest")
        expected = sorted(
            (
                item.namespace_id,
                item.migration_run_id,
                item.fixed_git_oid,
                item.inventory_digest,
            )
            for item in vaults
        )
        if not expected:
            raise ValueError("cutover requires at least one vault")
        if len({item[0] for item in expected}) != len(expected):
            raise ValueError("cutover vaults must be unique")
        for _, _, fixed_git_oid, vault_digest in expected:
            if _OID_RE.fullmatch(fixed_git_oid) is None:
                raise ValueError("fixed_git_oid must be a lowercase 40-hex OID")
            if _DIGEST_RE.fullmatch(vault_digest) is None:
                raise ValueError("vault inventory_digest must be a lowercase 64-hex digest")
        expected_files = sorted(
            (
                item.namespace_id,
                item.file_id,
                item.logical_path,
                item.mime_type,
                item.content_hash,
                item.byte_size,
                item.s3_key,
                item.etag,
                item.storage_version,
                item.created_by,
                item.disposition,
            )
            for item in files
        )
        for file in expected_files:
            if not file[2].strip() or not file[3].strip() or not file[6].strip():
                raise ValueError("cutover File path, MIME type and S3 key must be non-empty")
            if _DIGEST_RE.fullmatch(file[4]) is None or file[5] < 0:
                raise ValueError("cutover File digest or byte size is invalid")
            if file[10] not in {"native_text", "preserved_binary"}:
                raise ValueError("cutover File disposition is invalid")
        expected_exclusions = sorted((item.namespace_id, item.fixed_git_oid, item.reason) for item in exclusions)
        if len({item[0] for item in expected_exclusions}) != len(expected_exclusions):
            raise ValueError("cutover exclusions must be unique")
        if {item[0] for item in expected} & {item[0] for item in expected_exclusions}:
            raise ValueError("a cutover vault cannot be both eligible and excluded")
        for _, fixed_git_oid, reason in expected_exclusions:
            if _OID_RE.fullmatch(fixed_git_oid) is None:
                raise ValueError("excluded fixed_git_oid must be a lowercase 40-hex OID")
            if reason != "external_git_requires_collector":
                raise ValueError("unsupported cutover exclusion reason")

        async with self.pool.acquire() as conn:
            async with conn.transaction():
                row = await conn.fetchrow(
                    """
                    SELECT cutover_id, coverage_version, inventory_digest, status,
                           verification_digest, created_at, applied_at, verified_at,
                           aborted_from_status, aborted_at
                      FROM native_revision_cutover_runs
                     WHERE coverage_version = $1 AND inventory_digest = $2
                     FOR UPDATE
                    """,
                    coverage_version,
                    inventory_digest,
                )
                if row is None:
                    row = await conn.fetchrow(
                        """
                        INSERT INTO native_revision_cutover_runs
                            (coverage_version, inventory_digest)
                        VALUES ($1, $2)
                        RETURNING cutover_id, coverage_version, inventory_digest, status,
                                  verification_digest, created_at, applied_at, verified_at,
                                  aborted_from_status, aborted_at
                        """,
                        coverage_version,
                        inventory_digest,
                    )
                    assert row is not None
                    for namespace_id, migration_run_id, fixed_git_oid, vault_digest in expected:
                        await conn.execute(
                            """
                            INSERT INTO native_revision_cutover_vaults
                                (cutover_id, namespace_id, migration_run_id,
                                 fixed_git_oid, inventory_digest)
                            VALUES ($1, $2, $3, $4, $5)
                            """,
                            row["cutover_id"],
                            namespace_id,
                            migration_run_id,
                            fixed_git_oid,
                            vault_digest,
                        )
                    for namespace_id, fixed_git_oid, reason in expected_exclusions:
                        await conn.execute(
                            """
                            INSERT INTO native_revision_cutover_exclusions
                                (cutover_id, namespace_id, fixed_git_oid, reason)
                            VALUES ($1, $2, $3, $4)
                            """,
                            row["cutover_id"],
                            namespace_id,
                            fixed_git_oid,
                            reason,
                        )
                    for file in expected_files:
                        await conn.execute(
                            """
                            INSERT INTO native_revision_cutover_files
                                (cutover_id, namespace_id, file_id, logical_path,
                                 mime_type, content_hash, byte_size, s3_key,
                                 etag, storage_version, created_by, disposition)
                            VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12)
                            """,
                            row["cutover_id"],
                            *file,
                        )
                persisted = await self.list_vaults(row["cutover_id"], conn=conn)
                observed = [
                    (
                        item.namespace_id,
                        item.migration_run_id,
                        item.fixed_git_oid,
                        item.inventory_digest,
                    )
                    for item in persisted
                ]
                if observed != expected:
                    raise CutoverIntegrityError("cutover vault membership drifted")
                persisted_files = await self.list_files(row["cutover_id"], conn=conn)
                observed_files = [
                    (
                        item.namespace_id,
                        item.file_id,
                        item.logical_path,
                        item.mime_type,
                        item.content_hash,
                        item.byte_size,
                        item.s3_key,
                        item.etag,
                        item.storage_version,
                        item.created_by,
                        item.disposition,
                    )
                    for item in persisted_files
                ]
                if observed_files != expected_files:
                    raise CutoverIntegrityError("cutover File inventory drifted")
                persisted_exclusions = await self.list_exclusions(
                    row["cutover_id"],
                    conn=conn,
                )
                observed_exclusions = [
                    (item.namespace_id, item.fixed_git_oid, item.reason) for item in persisted_exclusions
                ]
                if observed_exclusions != expected_exclusions:
                    raise CutoverIntegrityError("cutover exclusion inventory drifted")
                return _run(row)

    async def set_file_status(
        self,
        *,
        cutover_id: uuid.UUID,
        file_id: uuid.UUID,
        status: str,
        native_revision_id: str | None,
        verification_digest: str | None = None,
    ) -> CutoverFile:
        if status not in _STATUSES:
            raise ValueError("invalid cutover File status")
        if native_revision_id is not None and _OID_RE.fullmatch(native_revision_id) is None:
            raise ValueError("native_revision_id must be a lowercase 40-hex id")
        if status == "verified" and (verification_digest is None or _DIGEST_RE.fullmatch(verification_digest) is None):
            raise ValueError("verified Files require a lowercase 64-hex digest")
        if status != "verified" and verification_digest is not None:
            raise ValueError("only verified Files carry a verification digest")
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                row = await conn.fetchrow(
                    """
                    SELECT cutover_id, namespace_id, file_id, logical_path, mime_type,
                           content_hash, byte_size, s3_key, etag, storage_version,
                           created_by, disposition, status, native_revision_id,
                           verification_digest, applied_at, verified_at
                      FROM native_revision_cutover_files
                     WHERE cutover_id = $1 AND file_id = $2
                     FOR UPDATE
                    """,
                    cutover_id,
                    file_id,
                )
                if row is None:
                    raise CutoverIntegrityError("cutover File disappeared")
                if _STATUSES.index(row["status"]) > _STATUSES.index(status):
                    return _file(row)
                row = await conn.fetchrow(
                    """
                    UPDATE native_revision_cutover_files
                       SET status = $3,
                           native_revision_id = $4,
                           applied_at = CASE
                               WHEN $3 IN ('applied', 'verified')
                               THEN COALESCE(applied_at, NOW()) ELSE NULL END,
                           verified_at = CASE WHEN $3 = 'verified' THEN NOW() ELSE NULL END,
                           verification_digest = $5
                     WHERE cutover_id = $1 AND file_id = $2
                    RETURNING cutover_id, namespace_id, file_id, logical_path, mime_type,
                              content_hash, byte_size, s3_key, etag, storage_version,
                              created_by, disposition, status, native_revision_id,
                              verification_digest, applied_at, verified_at
                    """,
                    cutover_id,
                    file_id,
                    status,
                    native_revision_id,
                    verification_digest,
                )
                assert row is not None
                return _file(row)

    async def set_vault_status(
        self,
        *,
        cutover_id: uuid.UUID,
        namespace_id: uuid.UUID,
        status: str,
        verification_digest: str | None = None,
    ) -> CutoverVault:
        if status not in _STATUSES:
            raise ValueError("invalid cutover vault status")
        if status == "verified" and (verification_digest is None or _DIGEST_RE.fullmatch(verification_digest) is None):
            raise ValueError("verified vaults require a lowercase 64-hex digest")
        if status != "verified" and verification_digest is not None:
            raise ValueError("only verified vaults carry a verification digest")

        async with self.pool.acquire() as conn:
            async with conn.transaction():
                row = await conn.fetchrow(
                    """
                    SELECT cutover_id, namespace_id, migration_run_id, fixed_git_oid,
                           inventory_digest, status, verification_digest,
                           applied_at, verified_at
                      FROM native_revision_cutover_vaults
                     WHERE cutover_id = $1 AND namespace_id = $2
                     FOR UPDATE
                    """,
                    cutover_id,
                    namespace_id,
                )
                if row is None:
                    raise CutoverIntegrityError("cutover vault disappeared")
                if _STATUSES.index(row["status"]) > _STATUSES.index(status):
                    return _vault(row)
                row = await conn.fetchrow(
                    """
                    UPDATE native_revision_cutover_vaults
                       SET status = $3,
                           applied_at = CASE
                               WHEN $3 IN ('applied', 'verified')
                               THEN COALESCE(applied_at, NOW()) ELSE NULL END,
                           verified_at = CASE WHEN $3 = 'verified' THEN NOW() ELSE NULL END,
                           verification_digest = $4
                     WHERE cutover_id = $1 AND namespace_id = $2
                     RETURNING cutover_id, namespace_id, migration_run_id, fixed_git_oid,
                               inventory_digest, status, verification_digest,
                               applied_at, verified_at
                    """,
                    cutover_id,
                    namespace_id,
                    status,
                    verification_digest,
                )
                assert row is not None
                return _vault(row)

    async def set_run_status(
        self,
        *,
        cutover_id: uuid.UUID,
        status: str,
        verification_digest: str | None = None,
    ) -> CutoverRun:
        if status not in _STATUSES:
            raise ValueError("invalid cutover status")
        if status == "verified" and (verification_digest is None or _DIGEST_RE.fullmatch(verification_digest) is None):
            raise ValueError("verified cutovers require a lowercase 64-hex digest")
        if status != "verified" and verification_digest is not None:
            raise ValueError("only verified cutovers carry a verification digest")

        async with self.pool.acquire() as conn:
            async with conn.transaction():
                row = await conn.fetchrow(
                    """
                    SELECT cutover_id, coverage_version, inventory_digest, status,
                           verification_digest, created_at, applied_at, verified_at,
                           aborted_from_status, aborted_at
                      FROM native_revision_cutover_runs
                     WHERE cutover_id = $1
                     FOR UPDATE
                    """,
                    cutover_id,
                )
                if row is None:
                    raise CutoverIntegrityError("cutover run disappeared")
                if row["status"] == "aborted":
                    raise CutoverIntegrityError("cutover run is aborted")
                if _STATUSES.index(row["status"]) > _STATUSES.index(status):
                    return _run(row)
                row = await conn.fetchrow(
                    """
                    UPDATE native_revision_cutover_runs
                       SET status = $2,
                           applied_at = CASE
                               WHEN $2 IN ('applied', 'verified')
                               THEN COALESCE(applied_at, NOW()) ELSE NULL END,
                           verified_at = CASE WHEN $2 = 'verified' THEN NOW() ELSE NULL END,
                           verification_digest = $3
                     WHERE cutover_id = $1
                    RETURNING cutover_id, coverage_version, inventory_digest, status,
                               verification_digest, created_at, applied_at, verified_at,
                               aborted_from_status, aborted_at
                    """,
                    cutover_id,
                    status,
                    verification_digest,
                )
                assert row is not None
                return _run(row)

    async def abort_run(self, cutover_id: uuid.UUID) -> CutoverRun:
        """Permanently close a pre-authority cutover without deleting evidence."""
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                row = await conn.fetchrow(
                    """
                    SELECT cutover_id, coverage_version, inventory_digest, status,
                           verification_digest, created_at, applied_at, verified_at,
                           aborted_from_status, aborted_at
                      FROM native_revision_cutover_runs
                     WHERE cutover_id = $1
                     FOR UPDATE
                    """,
                    cutover_id,
                )
                if row is None:
                    raise CutoverIntegrityError("cutover run disappeared")
                if row["status"] == "aborted":
                    return _run(row)
                if await conn.fetchval(
                    """
                    SELECT EXISTS (
                        SELECT 1 FROM native_revision_existing_authority
                         WHERE cutover_id = $1
                    )
                    """,
                    cutover_id,
                ):
                    raise CutoverIntegrityError("committed cutover authority cannot be aborted")
                fence_state = await conn.fetchval(
                    """
                    SELECT state FROM native_revision_legacy_write_fence
                     WHERE fence_key = TRUE
                    """
                )
                if fence_state != "open":
                    raise CutoverIntegrityError("cutover cannot abort after the Legacy write fence closes")
                row = await conn.fetchrow(
                    """
                    UPDATE native_revision_cutover_runs
                       SET aborted_from_status = status,
                           status = 'aborted',
                           aborted_at = NOW()
                     WHERE cutover_id = $1
                    RETURNING cutover_id, coverage_version, inventory_digest, status,
                              verification_digest, created_at, applied_at, verified_at,
                              aborted_from_status, aborted_at
                    """,
                    cutover_id,
                )
                assert row is not None
                return _run(row)
