"""Atomic current-head publication for the fixed-ref C9 inventory."""

from __future__ import annotations

import hashlib
import inspect
import json
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC
from typing import Final, Protocol

import asyncpg

from app.exceptions import NotFoundError
from app.repositories.native_revision_migration_repo import (
    MigrationRun,
    NativeRevisionMigrationRepository,
)
from app.repositories.native_revision_repo import NativeRevisionRepository
from app.services.git_service import GitService
from app.services.legacy_revision_bridge import (
    LegacyInventory,
    LegacyInventoryDocument,
    LegacyRevisionBridge,
)
from app.services.m1_pg_body_store import M1PgBodyStore


Failpoint = Callable[[str], Awaitable[None] | None]

FAILPOINT_BOUNDARIES: Final[tuple[str, ...]] = (
    "payload.after_prepare_before_tx",
    "authority.after_resource",
    "authority.after_manifest",
    "authority.after_revision",
    "authority.after_head",
    "authority.after_alias",
    "authority.after_activity",
    "authority.after_invalidation",
    "authority.after_mapping",
    "authority.before_commit",
)


class BackfillFailpointError(RuntimeError):
    """A test failpoint interrupted the authority transaction."""

    def __init__(self, boundary: str):
        super().__init__(f"native backfill failpoint: {boundary}")
        self.boundary = boundary


class TextBodyStore(Protocol):
    async def prepare_text(
        self,
        *,
        namespace_id: uuid.UUID,
        payload: str | bytes,
        expected_digest: str | None = None,
        expected_size: int | None = None,
    ): ...


@dataclass(frozen=True, slots=True)
class BackfillResult:
    run_id: uuid.UUID
    status: str
    completed_items: int
    failed_items: int
    skipped_items: int


class NativeRevisionBackfill:
    """Publish one frozen inventory item at a time, atomically per item."""

    actor = "akb-native-revision-migration"

    def __init__(
        self,
        pool: asyncpg.Pool,
        *,
        git: GitService,
        bridge: LegacyRevisionBridge | None = None,
        repository: NativeRevisionMigrationRepository | None = None,
        native_repository: NativeRevisionRepository | None = None,
        body_store: TextBodyStore | None = None,
        failpoint: Failpoint | None = None,
        revision_id_factory: Callable[[], str] | None = None,
    ):
        self.pool = pool
        self.git = git
        self.repository = repository or NativeRevisionMigrationRepository(pool)
        self.native_repository = native_repository or NativeRevisionRepository(pool)
        self.bridge = bridge or LegacyRevisionBridge(
            pool,
            git=git,
            repository=self.repository,
        )
        self.body_store = body_store or M1PgBodyStore(pool)
        self.failpoint = failpoint
        self.revision_id_factory = revision_id_factory

    async def _hit(self, boundary: str) -> None:
        if self.failpoint is None:
            return
        if boundary not in FAILPOINT_BOUNDARIES:
            raise ValueError(f"native backfill failpoint boundary is not registered: {boundary}")
        try:
            result = self.failpoint(boundary)
            if inspect.isawaitable(result):
                await result
        except Exception as exc:  # noqa: BLE001 - preserve a stable test seam
            raise BackfillFailpointError(boundary) from exc

    async def prepare_run(
        self,
        *,
        namespace_id: uuid.UUID,
        fixed_ref: str,
        coverage_version: str,
    ) -> tuple[MigrationRun, LegacyInventory]:
        return await self.bridge.prepare_run(
            namespace_id=namespace_id,
            fixed_ref=fixed_ref,
            coverage_version=coverage_version,
        )

    async def backfill_run(self, run_id: uuid.UUID) -> BackfillResult:
        run = await self.repository.get_run(run_id)
        if run is None:
            raise NotFoundError("Native revision migration run", str(run_id))
        if run.status == "complete":
            return BackfillResult(
                run_id=run_id,
                status="complete",
                completed_items=0,
                failed_items=0,
                skipped_items=0,
            )
        scope = await self.bridge.inventory_scope_for_run(run)
        inventory = scope.inventory

        async with self.pool.acquire() as conn:
            async with conn.transaction():
                await self.repository.ensure_pending_items(
                    run,
                    (document.item_facts() for document in inventory.documents),
                    conn=conn,
                )
                await self.repository.set_run_status(run_id, "running", conn=conn)

        completed = 0
        failed = 0
        skipped = 0
        for document in inventory.documents:
            item = await self.repository.get_item(run_id, document.resource_id)
            if item is None:
                raise RuntimeError("migration inventory item disappeared before backfill")
            if item.status == "complete":
                skipped += 1
                continue
            try:
                async with self.bridge.materialize_body(scope, document) as body:
                    prepared = await self.body_store.prepare_text(
                        namespace_id=run.namespace_id,
                        payload=body,
                        expected_digest=document.body_digest,
                        expected_size=document.byte_size,
                    )
                await self._hit("payload.after_prepare_before_tx")
                await self._publish_item(
                    run=run,
                    document=document,
                    inventory=inventory,
                    prepared=prepared,
                )
                completed += 1
            except BackfillFailpointError:
                raise
            except Exception:
                await self._mark_failed(run_id, document.resource_id, "authority_publish_failed")
                failed += 1

        async with self.pool.acquire() as conn:
            async with conn.transaction():
                if await self.repository.all_items_complete(conn, run_id):
                    final_run = await self.repository.set_run_status(
                        run_id, "complete", conn=conn,
                    )
                    status = final_run.status
                else:
                    final_run = await self.repository.set_run_status(
                        run_id,
                        "failed" if failed else "running",
                        error="one or more migration items failed" if failed else None,
                        conn=conn,
                    )
                    status = final_run.status
        return BackfillResult(
            run_id=run_id,
            status=status,
            completed_items=completed,
            failed_items=failed,
            skipped_items=skipped,
        )

    async def _mark_failed(
        self, run_id: uuid.UUID, resource_id: uuid.UUID, error_code: str,
    ) -> None:
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                await self.repository.mark_item_failed(
                    conn,
                    run_id=run_id,
                    legacy_document_id=resource_id,
                    error_code=error_code,
                )

    @staticmethod
    def _uuid(run_id: uuid.UUID, resource_id: uuid.UUID, label: str) -> uuid.UUID:
        return uuid.uuid5(
            uuid.NAMESPACE_URL,
            f"akb:c9:native-genesis:{run_id}:{resource_id}:{label}",
        )

    @staticmethod
    def _fingerprint(
        run: MigrationRun, document: LegacyInventoryDocument,
    ) -> str:
        payload = {
            "schema": "c9-native-genesis-v1",
            "run_id": str(run.run_id),
            "namespace_id": str(run.namespace_id),
            "resource_id": str(document.resource_id),
            "fixed_git_oid": run.fixed_git_oid,
            "legacy_head_oid": document.current_commit,
            "path": document.current_path,
            "body_digest": document.body_digest,
            "byte_size": document.byte_size,
        }
        return hashlib.sha256(
            json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()

    async def _publish_item(
        self,
        *,
        run: MigrationRun,
        document: LegacyInventoryDocument,
        inventory: LegacyInventory,
        prepared,
    ) -> None:
        del inventory
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                item = await self.repository.get_item(
                    run.run_id,
                    document.resource_id,
                    conn=conn,
                    for_update=True,
                )
                if item is None:
                    raise RuntimeError("migration item disappeared during authority publication")
                if item.status == "complete":
                    return

                if (
                    not document.lineage
                    or document.lineage[-1].legacy_git_oid != document.current_commit
                ):
                    raise RuntimeError("frozen lineage does not end at the legacy current head")
                current_lineage = document.lineage[-1]

                existing = await conn.fetchrow(
                    """
                    SELECT resource_id, namespace_id, surface, lifecycle,
                           current_path, head_revision_id
                      FROM native_resources
                     WHERE resource_id = $1
                     FOR UPDATE
                    """,
                    document.resource_id,
                )
                if existing is not None:
                    raise RuntimeError("native Resource exists for an incomplete migration item")

                retained_oids = {
                    entry.legacy_git_oid for entry in document.lineage
                }
                revision_id = await self.repository.allocate_native_revision_id(
                    conn,
                    resource_id=document.resource_id,
                    retained_legacy_oids=retained_oids,
                    revision_id_factory=self.revision_id_factory,
                )
                resource_created_at = document.created_at.astimezone(UTC)
                head_occurred_at = current_lineage.committed_at.astimezone(UTC)
                manifest_id = self._uuid(run.run_id, document.resource_id, "manifest")
                mutation_id = self._uuid(run.run_id, document.resource_id, "mutation")
                activity_id = self._uuid(run.run_id, document.resource_id, "activity")
                intent_id = self._uuid(run.run_id, document.resource_id, "invalidation")
                fingerprint = self._fingerprint(run, document)

                await self.native_repository.insert_resource(
                    conn,
                    resource_id=document.resource_id,
                    namespace_id=run.namespace_id,
                    surface="document",
                    content_profile="text",
                    path=document.current_path,
                    occurred_at=resource_created_at,
                )
                await self._hit("authority.after_resource")
                await self.native_repository.insert_manifest(
                    conn,
                    payload_manifest_id=manifest_id,
                    namespace_id=run.namespace_id,
                    resource_id=document.resource_id,
                    payload=prepared,
                    occurred_at=head_occurred_at,
                )
                await self._hit("authority.after_manifest")
                await self.native_repository.insert_revision(
                    conn,
                    revision_id=revision_id,
                    namespace_id=run.namespace_id,
                    resource_id=document.resource_id,
                    parent_revision_id=None,
                    action="create",
                    path=document.current_path,
                    path_from=None,
                    path_to=document.current_path,
                    payload_manifest_id=manifest_id,
                    mutation_id=mutation_id,
                    request_fingerprint=fingerprint,
                    message="C9 fixed-ref native genesis",
                    subject=None,
                    summary=None,
                    actor=self.actor,
                    occurred_at=head_occurred_at,
                    activity_event_id=activity_id,
                    invalidation_intent_id=intent_id,
                )
                await self._hit("authority.after_revision")
                await self.native_repository.set_head(
                    conn,
                    resource_id=document.resource_id,
                    revision_id=revision_id,
                    path=document.current_path,
                    lifecycle="live",
                    occurred_at=head_occurred_at,
                )
                await self._hit("authority.after_head")

                for alias in document.aliases:
                    if alias.old_ref == document.current_path:
                        continue
                    await self.native_repository.insert_path_alias(
                        conn,
                        namespace_id=run.namespace_id,
                        surface="document",
                        old_path=alias.old_ref,
                        resource_id=document.resource_id,
                        created_revision_id=revision_id,
                        occurred_at=alias.created_at,
                    )
                await self._hit("authority.after_alias")
                await self.native_repository.insert_activity(
                    conn,
                    activity_event_id=activity_id,
                    namespace_id=run.namespace_id,
                    resource_id=document.resource_id,
                    revision_id=revision_id,
                    action="create",
                    actor=self.actor,
                    subject=None,
                    summary=None,
                    path_from=None,
                    path_to=document.current_path,
                    occurred_at=head_occurred_at,
                )
                await self._hit("authority.after_activity")
                await self.native_repository.insert_invalidation_intent(
                    conn,
                    intent_id=intent_id,
                    namespace_id=run.namespace_id,
                    resource_id=document.resource_id,
                    revision_id=revision_id,
                    reason="create",
                    occurred_at=head_occurred_at,
                )
                await self._hit("authority.after_invalidation")

                for ordinal, entry in enumerate(document.lineage):
                    is_current = entry.legacy_git_oid == document.current_commit
                    await self.repository.ensure_mapping(
                        conn,
                        namespace_id=run.namespace_id,
                        resource_id=document.resource_id,
                        legacy_git_oid=entry.legacy_git_oid,
                        path_at_revision=entry.path_at_revision,
                        resolution="native" if is_current else "bridge",
                        native_revision_id=revision_id if is_current else None,
                        run_id=run.run_id,
                        lineage_ordinal=ordinal,
                    )
                await self._hit("authority.after_mapping")
                await self.repository.mark_item_complete(
                    conn,
                    run_id=run.run_id,
                    legacy_document_id=document.resource_id,
                    native_head_revision_id=revision_id,
                )
                await self._hit("authority.before_commit")
