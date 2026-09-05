"""Fixture-first orchestration for an existing database Native cutover."""

from __future__ import annotations

import asyncio
import hashlib
import json
import uuid
from collections.abc import AsyncIterator, Callable, Mapping, Sequence
from contextlib import ExitStack, asynccontextmanager
from dataclasses import dataclass
from typing import Any, Protocol

import asyncpg

from app.repositories.native_revision_cutover_repo import (
    CutoverIntegrityError,
    CutoverExclusion,
    CutoverFile,
    CutoverRun,
    CutoverVault,
    NativeRevisionCutoverRepository,
)
from app.repositories.native_revision_migration_repo import (
    MigrationInventoryDriftError,
    MigrationRun,
    NativeRevisionMigrationRepository,
)
from app.services.native_revision_backfill import NativeRevisionBackfill
from app.services.m1_pg_body_store import M1_PG_TEXT_MAX_BYTES, M1PgBodyStore
from app.services.native_revision_service import NativeRevisionService
from app.services.adapters import s3_adapter
from app.services.git_service import GitService
from app.services.legacy_revision_bridge import (
    InventoryEligibilityError,
    LegacyInventory,
    LegacyRevisionBridge,
)
from app.services.native_revision_authority import (
    NativeAuthorityIdentity,
    begin_existing_database_authority,
    finalize_existing_database_authority,
)
from app.services.native_revision_shadow import NativeRevisionShadowComparator
from app.services.native_revision_shadow_reader import (
    LegacyFixedRefShadowReader,
    NativeRevisionShadowReader,
)


class CutoverVerifier(Protocol):
    async def compare_run(self, run_id: uuid.UUID) -> Mapping[str, Any]: ...


class CutoverApplyError(RuntimeError):
    """A vault-scoped backfill did not reach complete."""


class CutoverVerificationError(RuntimeError):
    """A completed vault run did not produce an acceptable comparison."""


class NativeRevisionCutoverVerifier:
    """Compose the existing product readers for one vault-scoped run."""

    def __init__(self, pool: asyncpg.Pool, *, git: GitService):
        self.pool = pool
        self.git = git
        self.repository = NativeRevisionMigrationRepository(pool)
        self.bridge = LegacyRevisionBridge(
            pool,
            git=git,
            repository=self.repository,
        )

    async def compare_run(self, run_id: uuid.UUID) -> Mapping[str, Any]:
        run = await self.repository.get_run(run_id)
        if run is None:
            raise CutoverVerificationError(f"migration run {run_id} does not exist")
        async with self.pool.acquire() as conn:
            vault_name = await conn.fetchval(
                "SELECT name FROM vaults WHERE id = $1",
                run.namespace_id,
            )
        if not isinstance(vault_name, str) or not vault_name:
            raise CutoverVerificationError(f"vault {run.namespace_id} for migration run {run_id} does not exist")
        comparator = NativeRevisionShadowComparator(
            pool=self.pool,
            repository=self.repository,
            bridge=self.bridge,
            legacy_reader=LegacyFixedRefShadowReader(
                git=self.git,
                vault_name=vault_name,
            ),  # type: ignore[arg-type]  # activity_evidence is candidate-only
            candidate_reader=NativeRevisionShadowReader(
                self.pool,
                namespace_id=run.namespace_id,
                vault_name=vault_name,
                selector_bridge=self.bridge,
            ),
        )
        return await comparator.compare_run(run_id)


@dataclass(frozen=True, slots=True)
class CutoverVaultInput:
    namespace_id: uuid.UUID
    fixed_ref: str


@dataclass(frozen=True, slots=True)
class CutoverState:
    cutover_id: uuid.UUID
    coverage_version: str
    inventory_digest: str
    status: str
    verification_digest: str | None
    aborted_from_status: str | None
    aborted_at: Any
    vaults: tuple[CutoverVault, ...]
    files: tuple[CutoverFile, ...]
    exclusions: tuple[CutoverExclusion, ...]


@dataclass(frozen=True, slots=True)
class CutoverAuthorityState:
    authority_id: uuid.UUID
    cutover_id: uuid.UUID
    inventory_digest: str
    status: str


def _digest(value: Any) -> str:
    payload = json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        default=str,
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


class NativeRevisionCutover:
    """Group existing per-vault backfills and hand off verified authority."""

    def __init__(
        self,
        pool: asyncpg.Pool,
        *,
        backfill: NativeRevisionBackfill,
        verifier: CutoverVerifier,
        repository: NativeRevisionCutoverRepository | None = None,
        file_reader: Callable[[str], bytes] | None = None,
    ):
        self.pool = pool
        self.backfill = backfill
        self.verifier = verifier
        self.repository = repository or NativeRevisionCutoverRepository(pool)
        self.file_reader = file_reader or self._read_s3_file

    @staticmethod
    def _read_s3_file(s3_key: str) -> bytes:
        return b"".join(s3_adapter.iter_chunks(s3_key))

    async def _retained_vault_inventory(
        self,
        *,
        conn: asyncpg.Connection | None = None,
    ) -> list[asyncpg.Record]:
        sql = """
            SELECT v.id, v.name, v.status,
                   EXISTS (
                       SELECT 1 FROM vault_external_git veg
                        WHERE veg.vault_id = v.id
                   ) AS external_git
              FROM vaults v
             WHERE v.status <> 'deleted'
             ORDER BY v.id
        """
        if conn is not None:
            return list(await conn.fetch(sql))
        async with self.pool.acquire() as acquired:
            return list(await acquired.fetch(sql))

    async def _persisted_external_git_count(
        self,
        *,
        conn: asyncpg.Connection | None = None,
    ) -> int:
        """Count every external-Git sidecar, including retired vault rows."""
        sql = "SELECT COUNT(*) FROM vault_external_git"
        if conn is not None:
            return int(await conn.fetchval(sql))
        async with self.pool.acquire() as acquired:
            return int(await acquired.fetchval(sql))

    @asynccontextmanager
    async def _hold_git_write_fences(
        self,
        vault_names: Sequence[str],
    ) -> AsyncIterator[None]:
        def acquire() -> ExitStack:
            stack = ExitStack()
            try:
                for vault_name in sorted(vault_names):
                    stack.enter_context(self.backfill.git._vault_write_lock(vault_name))
            except BaseException:
                stack.close()
                raise
            return stack

        stack = await asyncio.to_thread(acquire)
        try:
            yield
        finally:
            await asyncio.to_thread(stack.close)

    async def _file_inventory(
        self,
        namespace_ids: list[uuid.UUID],
        *,
        conn: asyncpg.Connection | None = None,
    ) -> list[CutoverFile]:
        sql = """
            SELECT vf.vault_id AS namespace_id, vf.id AS file_id,
                   CASE WHEN c.path IS NULL THEN vf.name
                        ELSE c.path || '/' || vf.name END AS logical_path,
                   COALESCE(NULLIF(vf.mime_type, ''), 'application/octet-stream')
                       AS mime_type,
                   vf.content_hash, vf.size_bytes, vf.s3_key,
                   vf.etag, vf.storage_version, vf.created_by
              FROM vault_files vf
         LEFT JOIN collections c ON c.id = vf.collection_id
             WHERE vf.vault_id = ANY($1::uuid[])
               AND vf.kind = 'file'
               AND vf.upload_state = 'confirmed'
               AND vf.hash_verified_at IS NOT NULL
               AND vf.content_hash ~ '^[0-9a-f]{64}$'
               AND vf.size_bytes >= 0
             ORDER BY vf.vault_id, vf.id
        """
        if conn is not None:
            rows = await conn.fetch(sql, namespace_ids)
        else:
            async with self.pool.acquire() as acquired:
                rows = await acquired.fetch(sql, namespace_ids)

        files: list[CutoverFile] = []
        for row in rows:
            data = await asyncio.to_thread(self.file_reader, row["s3_key"])
            disposition = self._classify_file_bytes(
                file_id=row["file_id"],
                mime_type=row["mime_type"],
                content_hash=row["content_hash"],
                byte_size=int(row["size_bytes"]),
                data=data,
            )
            files.append(
                CutoverFile(
                    cutover_id=uuid.UUID(int=0),
                    namespace_id=row["namespace_id"],
                    file_id=row["file_id"],
                    logical_path=row["logical_path"],
                    mime_type=row["mime_type"],
                    content_hash=row["content_hash"],
                    byte_size=int(row["size_bytes"]),
                    s3_key=row["s3_key"],
                    etag=row["etag"],
                    storage_version=row["storage_version"],
                    created_by=row["created_by"],
                    disposition=disposition,
                    status="planned",
                    native_revision_id=None,
                    verification_digest=None,
                    applied_at=None,
                    verified_at=None,
                )
            )
        return files

    @staticmethod
    def _classify_file_bytes(
        *,
        file_id: uuid.UUID,
        mime_type: str,
        content_hash: str,
        byte_size: int,
        data: bytes,
    ) -> str:
        if len(data) != byte_size:
            raise CutoverApplyError(f"File {file_id} byte size drifted")
        if hashlib.sha256(data).hexdigest() != content_hash:
            raise CutoverApplyError(f"File {file_id} content digest drifted")
        if not mime_type.lower().startswith("text/"):
            return "preserved_binary"
        if len(data) > M1_PG_TEXT_MAX_BYTES or b"\x00" in data:
            return "preserved_binary"
        try:
            data.decode("utf-8", errors="strict")
        except UnicodeDecodeError:
            return "preserved_binary"
        return "native_text"

    async def _partition_vaults(
        self,
        vaults: Sequence[CutoverVaultInput],
    ) -> tuple[list[CutoverVaultInput], list[CutoverExclusion]]:
        rows = await self._retained_vault_inventory()
        by_input = {item.namespace_id: item for item in vaults}
        retained_ids = {row["id"] for row in rows}
        if set(by_input) != retained_ids:
            omitted = sorted(str(item) for item in retained_ids - set(by_input))
            added = sorted(str(item) for item in set(by_input) - retained_ids)
            raise ValueError(
                f"cutover vaults must equal the complete retained vault inventory (omitted={omitted}, added={added})"
            )
        eligible: list[CutoverVaultInput] = []
        exclusions: list[CutoverExclusion] = []
        for row in rows:
            item = by_input[row["id"]]
            current_ref = await asyncio.to_thread(
                self.backfill.git.current_commit,
                row["name"],
            )
            if current_ref != item.fixed_ref:
                raise ValueError(f"cutover fixed ref is not the current Git ref: {item.namespace_id}")
            if row["external_git"]:
                exclusions.append(
                    CutoverExclusion(
                        cutover_id=uuid.UUID(int=0),
                        namespace_id=item.namespace_id,
                        fixed_git_oid=item.fixed_ref,
                        reason="external_git_requires_collector",
                        created_at=None,
                    )
                )
                continue
            eligible.append(item)
        return eligible, exclusions

    @staticmethod
    def _vault_inventory_fact(item: CutoverVault) -> dict[str, str]:
        return {
            "namespace_id": str(item.namespace_id),
            "migration_run_id": str(item.migration_run_id),
            "fixed_git_oid": item.fixed_git_oid,
            "inventory_digest": item.inventory_digest,
        }

    @staticmethod
    def _file_inventory_fact(item: CutoverFile) -> dict[str, Any]:
        return {
            "namespace_id": str(item.namespace_id),
            "file_id": str(item.file_id),
            "logical_path": item.logical_path,
            "mime_type": item.mime_type,
            "content_hash": item.content_hash,
            "byte_size": item.byte_size,
            "s3_key": item.s3_key,
            "etag": item.etag,
            "storage_version": item.storage_version,
            "created_by": item.created_by,
            "disposition": item.disposition,
        }

    @staticmethod
    def _exclusion_inventory_fact(item: CutoverExclusion) -> dict[str, str]:
        return {
            "namespace_id": str(item.namespace_id),
            "fixed_git_oid": item.fixed_git_oid,
            "reason": item.reason,
        }

    @classmethod
    def _cutover_inventory_digest(
        cls,
        *,
        vaults: Sequence[CutoverVault],
        files: Sequence[CutoverFile],
        exclusions: Sequence[CutoverExclusion],
    ) -> str:
        return _digest(
            {
                "vaults": [cls._vault_inventory_fact(item) for item in vaults],
                "files": [cls._file_inventory_fact(item) for item in files],
                "exclusions": [cls._exclusion_inventory_fact(item) for item in exclusions],
            }
        )

    async def plan(
        self,
        *,
        vaults: Sequence[CutoverVaultInput],
        coverage_version: str,
    ) -> CutoverState:
        ordered = sorted(vaults, key=lambda item: str(item.namespace_id))
        if not ordered:
            raise ValueError("cutover requires at least one vault")
        if len({item.namespace_id for item in ordered}) != len(ordered):
            raise ValueError("cutover vaults must be unique")

        eligible, exclusions = await self._partition_vaults(ordered)
        if not eligible:
            raise ValueError("cutover requires at least one eligible manual vault")

        prepared: list[CutoverVault] = []
        try:
            for item in eligible:
                migration_run, _ = await self.backfill.prepare_run(
                    namespace_id=item.namespace_id,
                    fixed_ref=item.fixed_ref,
                    coverage_version=coverage_version,
                )
                prepared.append(
                    CutoverVault(
                        cutover_id=uuid.UUID(int=0),
                        namespace_id=migration_run.namespace_id,
                        migration_run_id=migration_run.run_id,
                        fixed_git_oid=migration_run.fixed_git_oid,
                        inventory_digest=migration_run.inventory_digest,
                        status="planned",
                        verification_digest=None,
                        applied_at=None,
                        verified_at=None,
                    )
                )
            files = await self._file_inventory([item.namespace_id for item in eligible])
            inventory_digest = self._cutover_inventory_digest(
                vaults=prepared,
                files=files,
                exclusions=exclusions,
            )
            run = await self.repository.get_or_create_run(
                coverage_version=coverage_version,
                inventory_digest=inventory_digest,
                vaults=prepared,
                files=files,
                exclusions=exclusions,
            )
        except Exception as exc:
            try:
                if prepared:
                    await self.backfill.repository.supersede_unlinked_pending_runs(
                        item.migration_run_id for item in prepared
                    )
            except Exception as cleanup_exc:
                exc.add_note(f"failed to compensate unlinked migration plans: {cleanup_exc}")
            raise
        return await self._state(run)

    async def supersede_orphan_plan(self, migration_run_id: uuid.UUID) -> MigrationRun:
        """Release one exact never-applied run that has no outer cutover."""

        superseded = await self.backfill.repository.supersede_unlinked_pending_runs([migration_run_id])
        if superseded != (migration_run_id,):
            raise CutoverIntegrityError("migration run is not an unlinked all-pending plan")
        run = await self.backfill.repository.get_run(migration_run_id)
        if run is None or run.status != "superseded":
            raise CutoverIntegrityError("orphan migration run supersession was not durable")
        return run

    @staticmethod
    def _validate_text_file(file: CutoverFile, data: bytes) -> str:
        disposition = NativeRevisionCutover._classify_file_bytes(
            file_id=file.file_id,
            mime_type=file.mime_type,
            content_hash=file.content_hash,
            byte_size=file.byte_size,
            data=data,
        )
        if disposition != "native_text":
            raise CutoverApplyError(f"File {file.file_id} is not eligible searchable text")
        return data.decode("utf-8", errors="strict")

    async def _apply_files(self, cutover_id: uuid.UUID) -> None:
        native = NativeRevisionService(
            self.pool,
            payload_store=M1PgBodyStore(self.pool),
        )
        for file in await self.repository.list_files(cutover_id):
            if file.status in {"applied", "verified"}:
                continue
            if file.disposition == "preserved_binary":
                await self.repository.set_file_status(
                    cutover_id=cutover_id,
                    file_id=file.file_id,
                    status="applied",
                    native_revision_id=None,
                )
                continue
            data = await asyncio.to_thread(self.file_reader, file.s3_key)
            self._validate_text_file(file, data)
            result = await native.create_text(
                namespace_id=file.namespace_id,
                surface="file",
                path=file.logical_path,
                payload=data,
                actor=file.created_by or "akb-native-revision-migration",
                mutation_id=uuid.uuid5(
                    uuid.NAMESPACE_URL,
                    f"akb:existing-file-cutover:{cutover_id}:{file.file_id}:{file.content_hash}",
                ),
                resource_id=file.file_id,
                message="Existing File searchable-text cutover",
                expected_digest=file.content_hash,
                expected_size=file.byte_size,
            )
            if result.resource_id != file.file_id:
                raise CutoverApplyError("Native File publication changed public identity")
            await self.repository.set_file_status(
                cutover_id=cutover_id,
                file_id=file.file_id,
                status="applied",
                native_revision_id=result.revision_id,
            )

    async def _verify_files(self, cutover_id: uuid.UUID) -> list[dict[str, str]]:
        receipts: list[dict[str, str]] = []
        for file in await self.repository.list_files(cutover_id):
            if file.status == "verified":
                assert file.verification_digest is not None
                receipts.append({"file_id": str(file.file_id), "verification_digest": file.verification_digest})
                continue
            if file.status != "applied":
                raise CutoverVerificationError(f"File {file.file_id} was not applied")
            async with self.pool.acquire() as conn:
                source = await conn.fetchrow(
                    """
                    SELECT vf.vault_id, vf.id,
                           CASE WHEN c.path IS NULL THEN vf.name
                                ELSE c.path || '/' || vf.name END AS logical_path,
                           COALESCE(NULLIF(vf.mime_type, ''), 'application/octet-stream')
                               AS mime_type,
                           vf.content_hash, vf.size_bytes, vf.s3_key,
                           vf.etag, vf.storage_version, vf.created_by,
                           vf.kind, vf.upload_state, vf.hash_verified_at
                      FROM vault_files vf
                 LEFT JOIN collections c ON c.id = vf.collection_id
                     WHERE vf.id = $1 AND vf.vault_id = $2
                    """,
                    file.file_id,
                    file.namespace_id,
                )
                native = await conn.fetchrow(
                    """
                    SELECT r.namespace_id, r.resource_id, r.surface, r.lifecycle,
                           r.current_path, r.head_revision_id,
                           pm.digest, pm.byte_size
                      FROM native_resources r
                 LEFT JOIN native_revisions nr
                        ON nr.resource_id = r.resource_id
                       AND nr.revision_id = r.head_revision_id
                 LEFT JOIN native_payload_manifests pm
                        ON pm.payload_manifest_id = nr.payload_manifest_id
                     WHERE r.resource_id = $1
                    """,
                    file.file_id,
                )
            expected_source = (
                file.namespace_id,
                file.file_id,
                file.logical_path,
                file.mime_type,
                file.content_hash,
                file.byte_size,
                file.s3_key,
                file.etag,
                file.storage_version,
                file.created_by,
                "file",
                "confirmed",
                True,
            )
            observed_source = (
                None
                if source is None
                else (
                    source["vault_id"],
                    source["id"],
                    source["logical_path"],
                    source["mime_type"],
                    source["content_hash"],
                    int(source["size_bytes"]),
                    source["s3_key"],
                    source["etag"],
                    source["storage_version"],
                    source["created_by"],
                    source["kind"],
                    source["upload_state"],
                    source["hash_verified_at"] is not None,
                )
            )
            if observed_source != expected_source:
                raise CutoverVerificationError(f"File {file.file_id} source facts drifted")
            data = await asyncio.to_thread(self.file_reader, file.s3_key)
            if len(data) != file.byte_size or hashlib.sha256(data).hexdigest() != file.content_hash:
                raise CutoverVerificationError(f"File {file.file_id} S3 bytes drifted")
            if file.disposition == "native_text":
                self._validate_text_file(file, data)
                expected_native = (
                    file.namespace_id,
                    file.file_id,
                    "file",
                    "live",
                    file.logical_path,
                    file.native_revision_id,
                    file.content_hash,
                    file.byte_size,
                )
                observed_native = (
                    None
                    if native is None
                    else (
                        native["namespace_id"],
                        native["resource_id"],
                        native["surface"],
                        native["lifecycle"],
                        native["current_path"],
                        native["head_revision_id"],
                        native["digest"],
                        int(native["byte_size"]),
                    )
                )
                if observed_native != expected_native:
                    raise CutoverVerificationError(f"File {file.file_id} Native searchable projection drifted")
            elif native is not None:
                raise CutoverVerificationError(f"binary File {file.file_id} unexpectedly gained text authority")
            receipt = {
                "file_id": str(file.file_id),
                "disposition": file.disposition,
                "content_hash": file.content_hash,
                "native_revision_id": file.native_revision_id,
            }
            verification_digest = _digest(receipt)
            await self.repository.set_file_status(
                cutover_id=cutover_id,
                file_id=file.file_id,
                status="verified",
                native_revision_id=file.native_revision_id,
                verification_digest=verification_digest,
            )
            receipts.append({"file_id": str(file.file_id), "verification_digest": verification_digest})
        return receipts

    async def _require_native_file_bindings(
        self,
        conn: asyncpg.Connection,
        files: Sequence[CutoverFile],
    ) -> None:
        rows = await conn.fetch(
            """
                SELECT r.namespace_id, r.resource_id, r.surface, r.lifecycle,
                       r.current_path, r.head_revision_id,
                       pm.digest, pm.byte_size
                  FROM native_resources r
             LEFT JOIN native_revisions nr
                    ON nr.resource_id = r.resource_id
                   AND nr.revision_id = r.head_revision_id
             LEFT JOIN native_payload_manifests pm
                    ON pm.payload_manifest_id = nr.payload_manifest_id
                 WHERE r.resource_id = ANY($1::uuid[])
            """,
            [file.file_id for file in files],
        )
        native_by_id = {row["resource_id"]: row for row in rows}
        for file in files:
            native = native_by_id.get(file.file_id)
            if file.disposition == "preserved_binary":
                if native is not None:
                    raise CutoverVerificationError(f"binary File {file.file_id} unexpectedly gained text authority")
                continue
            expected = (
                file.namespace_id,
                file.file_id,
                "file",
                "live",
                file.logical_path,
                file.native_revision_id,
                file.content_hash,
                file.byte_size,
            )
            observed = (
                None
                if native is None
                else (
                    native["namespace_id"],
                    native["resource_id"],
                    native["surface"],
                    native["lifecycle"],
                    native["current_path"],
                    native["head_revision_id"],
                    native["digest"],
                    int(native["byte_size"]),
                )
            )
            if observed != expected:
                raise CutoverVerificationError(f"File {file.file_id} Native searchable projection drifted")

    async def _require_source_file_bindings(
        self,
        conn: asyncpg.Connection,
        *,
        namespace_ids: Sequence[uuid.UUID],
        files: Sequence[CutoverFile],
    ) -> None:
        rows = await conn.fetch(
            """
            SELECT vf.vault_id AS namespace_id, vf.id AS file_id,
                   CASE WHEN c.path IS NULL THEN vf.name
                        ELSE c.path || '/' || vf.name END AS logical_path,
                   COALESCE(NULLIF(vf.mime_type, ''), 'application/octet-stream')
                       AS mime_type,
                   vf.content_hash, vf.size_bytes, vf.s3_key,
                   vf.etag, vf.storage_version, vf.created_by
              FROM vault_files vf
         LEFT JOIN collections c ON c.id = vf.collection_id
             WHERE vf.vault_id = ANY($1::uuid[])
               AND vf.kind = 'file'
               AND vf.upload_state = 'confirmed'
               AND vf.hash_verified_at IS NOT NULL
               AND vf.content_hash ~ '^[0-9a-f]{64}$'
               AND vf.size_bytes >= 0
             ORDER BY vf.vault_id, vf.id
            """,
            list(namespace_ids),
        )
        observed = [
            {
                "namespace_id": str(row["namespace_id"]),
                "file_id": str(row["file_id"]),
                "logical_path": row["logical_path"],
                "mime_type": row["mime_type"],
                "content_hash": row["content_hash"],
                "byte_size": int(row["size_bytes"]),
                "s3_key": row["s3_key"],
                "etag": row["etag"],
                "storage_version": row["storage_version"],
                "created_by": row["created_by"],
            }
            for row in rows
        ]
        expected = [
            {key: value for key, value in self._file_inventory_fact(file).items() if key != "disposition"}
            for file in files
        ]
        if observed != expected:
            raise CutoverVerificationError("cutover File catalog drifted after planning")

    async def _require_native_document_bindings(
        self,
        conn: asyncpg.Connection,
        inventory: LegacyInventory,
    ) -> None:
        """Recheck frozen Native heads and payload facts without reading bodies."""

        mappings = await self.backfill.repository.list_namespace_completed_mappings(
            namespace_id=inventory.namespace_id,
            conn=conn,
        )
        current_mappings = {
            (mapping.resource_id, mapping.legacy_git_oid): mapping
            for mapping in mappings
            if mapping.resolution == "native"
        }
        resource_ids = [document.resource_id for document in inventory.documents]
        rows = await conn.fetch(
            """
            SELECT resource.resource_id, resource.namespace_id, resource.surface,
                   resource.lifecycle, resource.current_path,
                   resource.head_revision_id, revision.parent_revision_id,
                   revision.action, revision.path_at_revision,
                   manifest.digest, manifest.byte_size
              FROM native_resources resource
              JOIN native_revisions revision
                ON revision.resource_id = resource.resource_id
               AND revision.revision_id = resource.head_revision_id
              JOIN native_payload_manifests manifest
                ON manifest.payload_manifest_id = revision.payload_manifest_id
             WHERE resource.resource_id = ANY($1::uuid[])
             ORDER BY resource.resource_id
            """,
            resource_ids,
        )
        by_resource = {row["resource_id"]: row for row in rows}
        if len(by_resource) != len(resource_ids):
            raise CutoverVerificationError(
                f"vault {inventory.namespace_id} Native document closure drifted"
            )
        for document in inventory.documents:
            mapping = current_mappings.get(
                (document.resource_id, document.current_commit)
            )
            native = by_resource.get(document.resource_id)
            if mapping is None or mapping.native_revision_id is None or native is None:
                raise CutoverVerificationError(
                    f"document {document.resource_id} Native head binding drifted"
                )
            observed = (
                native["namespace_id"],
                native["surface"],
                native["lifecycle"],
                native["current_path"],
                native["head_revision_id"],
                native["parent_revision_id"],
                native["action"],
                native["path_at_revision"],
                native["digest"],
                int(native["byte_size"]),
            )
            expected = (
                inventory.namespace_id,
                "document",
                "live",
                document.current_path,
                mapping.native_revision_id,
                None,
                "create",
                document.current_path,
                document.body_digest,
                document.byte_size,
            )
            if observed != expected:
                raise CutoverVerificationError(
                    f"document {document.resource_id} Native head binding drifted"
                )

    async def _revalidate_authority_catalog(
        self,
        conn: asyncpg.Connection,
        *,
        cutover_id: uuid.UUID,
    ) -> None:
        run = await self.repository.get_run(cutover_id, conn=conn)
        if run is None or run.status != "verified":
            raise CutoverVerificationError("cutover must remain verified at authority mint")
        vaults = await self.repository.list_vaults(cutover_id, conn=conn)
        files = await self.repository.list_files(cutover_id, conn=conn)
        exclusions = await self.repository.list_exclusions(cutover_id, conn=conn)
        persisted_external_git_count = await self._persisted_external_git_count(conn=conn)
        retained = await self._retained_vault_inventory(conn=conn)

        planned_ids = {item.namespace_id for item in vaults}
        planned_ids.update(item.namespace_id for item in exclusions)
        retained_ids = {row["id"] for row in retained}
        if planned_ids != retained_ids:
            raise CutoverVerificationError("retained vault inventory drifted after planning")

        vault_by_id = {item.namespace_id: item for item in vaults}
        exclusion_by_id = {item.namespace_id: item for item in exclusions}
        for row in retained:
            if row["external_git"]:
                exclusion = exclusion_by_id.get(row["id"])
                if exclusion is None or exclusion.reason != "external_git_requires_collector":
                    raise CutoverVerificationError("retained vault classification drifted after planning")
            elif row["id"] not in vault_by_id:
                raise CutoverVerificationError("retained vault classification drifted after planning")
        if persisted_external_git_count:
            raise CutoverVerificationError("cutover authority cannot commit while persisted external Git vaults remain")
        if exclusions:
            raise CutoverVerificationError("cutover exclusion inventory drifted after external Git retirement")

        for vault in vaults:
            if vault.status != "verified" or vault.verification_digest is None:
                raise CutoverVerificationError(f"vault {vault.namespace_id} verification receipt disappeared")
            migration_run = await self.backfill.repository.get_run(
                vault.migration_run_id,
                conn=conn,
            )
            if (
                migration_run is None
                or migration_run.status != "complete"
                or migration_run.namespace_id != vault.namespace_id
                or migration_run.fixed_git_oid != vault.fixed_git_oid
                or migration_run.inventory_digest != vault.inventory_digest
            ):
                raise CutoverVerificationError(f"vault {vault.namespace_id} migration run disappeared")
            try:
                scope = await self.backfill.bridge.inventory_scope_for_run(
                    migration_run,
                    conn=conn,
                )
            except (InventoryEligibilityError, MigrationInventoryDriftError) as exc:
                raise CutoverVerificationError(
                    f"vault {vault.namespace_id} inventory drifted after verification"
                ) from exc
            await self._require_native_document_bindings(conn, scope.inventory)

        await self._require_source_file_bindings(
            conn,
            namespace_ids=[item.namespace_id for item in vaults],
            files=files,
        )
        await self._require_native_file_bindings(conn, files)

        if run.inventory_digest != self._cutover_inventory_digest(
            vaults=vaults,
            files=files,
            exclusions=exclusions,
        ):
            raise CutoverVerificationError("cutover inventory digest drifted after planning")

    async def _revalidate_authority_external(
        self,
        *,
        cutover_id: uuid.UUID,
    ) -> None:
        """Recheck immutable Git/S3 source objects outside a database transaction."""

        run = await self._required_run(cutover_id)
        if run.status != "verified":
            raise CutoverVerificationError("cutover must remain verified at authority mint")
        vaults = await self.repository.list_vaults(cutover_id)
        files = await self.repository.list_files(cutover_id)
        retained = {row["id"]: row for row in await self._retained_vault_inventory()}
        for vault in vaults:
            row = retained.get(vault.namespace_id)
            if row is None:
                raise CutoverVerificationError(f"vault {vault.namespace_id} disappeared after fencing")
            current_ref = await asyncio.to_thread(
                self.backfill.git.current_commit,
                row["name"],
            )
            if current_ref != vault.fixed_git_oid:
                raise CutoverVerificationError(f"vault {vault.namespace_id} Git ref drifted after verification")

        for file in files:
            data = await asyncio.to_thread(self.file_reader, file.s3_key)
            try:
                disposition = self._classify_file_bytes(
                    file_id=file.file_id,
                    mime_type=file.mime_type,
                    content_hash=file.content_hash,
                    byte_size=file.byte_size,
                    data=data,
                )
            except CutoverApplyError as exc:
                raise CutoverVerificationError(str(exc)) from exc
            if disposition != file.disposition:
                raise CutoverVerificationError(f"File {file.file_id} classification drifted after verification")

    async def commit(
        self,
        cutover_id: uuid.UUID,
        *,
        identity: NativeAuthorityIdentity,
    ) -> CutoverAuthorityState:
        run = await self._required_run(cutover_id)
        if run.status == "aborted":
            raise CutoverVerificationError("aborted cutover cannot commit authority")
        if run.status != "verified" or run.verification_digest is None:
            raise CutoverVerificationError("cutover must be verified before authority can be committed")
        retained = await self._retained_vault_inventory()
        async with self._hold_git_write_fences([row["name"] for row in retained]):
            async with self.pool.acquire() as conn:
                fence = await begin_existing_database_authority(
                    conn,
                    identity=identity,
                    cutover_id=cutover_id,
                    preflight=lambda transaction_conn: self._revalidate_authority_catalog(
                        transaction_conn,
                        cutover_id=cutover_id,
                    ),
                )
            if fence.authority_id is None:
                await self._revalidate_authority_external(cutover_id=cutover_id)
                async with self.pool.acquire() as conn:
                    authority_id = await finalize_existing_database_authority(
                        conn,
                        identity=identity,
                        fence=fence,
                    )
            else:
                authority_id = fence.authority_id
            async with self.pool.acquire() as conn:
                row = await conn.fetchrow(
                    """
                    SELECT authority_id, cutover_id, inventory_digest, status
                      FROM native_revision_existing_authority
                     WHERE authority_id = $1
                    """,
                    authority_id,
                )
        if row is None:
            raise CutoverVerificationError("existing-database authority disappeared")
        return CutoverAuthorityState(**dict(row))

    async def apply(self, cutover_id: uuid.UUID) -> CutoverState:
        run = await self._required_run(cutover_id)
        if run.status == "aborted":
            raise CutoverApplyError("aborted cutover cannot be applied")
        if run.status in {"applied", "verified"}:
            return await self._state(run)
        vaults = await self.repository.list_vaults(cutover_id)
        for vault in vaults:
            if vault.status in {"applied", "verified"}:
                continue
            result = await self.backfill.backfill_run(vault.migration_run_id)
            if result.status != "complete" or result.failed_items:
                raise CutoverApplyError(f"vault {vault.namespace_id} backfill ended as {result.status}")
            await self.repository.set_vault_status(
                cutover_id=cutover_id,
                namespace_id=vault.namespace_id,
                status="applied",
            )
        await self._apply_files(cutover_id)
        run = await self.repository.set_run_status(
            cutover_id=cutover_id,
            status="applied",
        )
        return await self._state(run)

    async def verify(self, cutover_id: uuid.UUID) -> CutoverState:
        run = await self._required_run(cutover_id)
        if run.status == "aborted":
            raise CutoverVerificationError("aborted cutover cannot be verified")
        if run.status == "verified":
            return await self._state(run)
        if run.status != "applied":
            raise CutoverVerificationError("cutover must be applied before verification")

        digests: list[dict[str, str]] = []
        vaults = await self.repository.list_vaults(cutover_id)
        for vault in vaults:
            if vault.status == "verified":
                assert vault.verification_digest is not None
                receipt_digest = vault.verification_digest
            else:
                receipt = await self.verifier.compare_run(vault.migration_run_id)
                self._require_passed_receipt(vault, receipt)
                receipt_digest = _digest(receipt)
                await self.repository.set_vault_status(
                    cutover_id=cutover_id,
                    namespace_id=vault.namespace_id,
                    status="verified",
                    verification_digest=receipt_digest,
                )
            digests.append(
                {
                    "namespace_id": str(vault.namespace_id),
                    "migration_run_id": str(vault.migration_run_id),
                    "verification_digest": receipt_digest,
                }
            )
        file_digests = await self._verify_files(cutover_id)
        run = await self.repository.set_run_status(
            cutover_id=cutover_id,
            status="verified",
            verification_digest=_digest(
                {
                    "vaults": digests,
                    "files": file_digests,
                    "exclusions": [
                        {
                            "namespace_id": str(item.namespace_id),
                            "fixed_git_oid": item.fixed_git_oid,
                            "reason": item.reason,
                        }
                        for item in await self.repository.list_exclusions(cutover_id)
                    ],
                }
            ),
        )
        return await self._state(run)

    async def abort(self, cutover_id: uuid.UUID) -> CutoverState:
        """Close a pre-authority cutover while retaining all additive evidence."""
        try:
            run = await self.repository.abort_run(cutover_id)
        except CutoverIntegrityError as exc:
            raise CutoverVerificationError(str(exc)) from exc
        return await self._state(run)

    @staticmethod
    def _require_passed_receipt(
        vault: CutoverVault,
        receipt: Mapping[str, Any],
    ) -> None:
        summary = receipt.get("summary")
        if not isinstance(summary, Mapping):
            raise CutoverVerificationError(f"vault {vault.namespace_id} verification summary is missing")
        if (
            receipt.get("status") != "passed"
            or receipt.get("passed") is not True
            or summary.get("unexplained_mismatch_count") != 0
        ):
            raise CutoverVerificationError(f"vault {vault.namespace_id} verification did not pass")

    async def _required_run(self, cutover_id: uuid.UUID) -> CutoverRun:
        run = await self.repository.get_run(cutover_id)
        if run is None:
            raise CutoverApplyError(f"cutover {cutover_id} does not exist")
        return run

    async def _state(self, run: CutoverRun) -> CutoverState:
        return CutoverState(
            cutover_id=run.cutover_id,
            coverage_version=run.coverage_version,
            inventory_digest=run.inventory_digest,
            status=run.status,
            verification_digest=run.verification_digest,
            aborted_from_status=run.aborted_from_status,
            aborted_at=run.aborted_at,
            vaults=tuple(await self.repository.list_vaults(run.cutover_id)),
            files=tuple(await self.repository.list_files(run.cutover_id)),
            exclusions=tuple(await self.repository.list_exclusions(run.cutover_id)),
        )
