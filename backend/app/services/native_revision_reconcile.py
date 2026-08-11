"""Operator-only final-ref reconciliation for the C9 native ledger.

This module is deliberately not composed into the request path.  Legacy Git
and the legacy document projection remain the sole writers; an operator may
construct this service to publish one final native delta for a later fixed
ref.  The fixed-ref bridge supplies the inventory and the migration repository
supplies the immutable cross-run selector bindings.
"""

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

from app.exceptions import ConflictError
from app.repositories.native_revision_migration_repo import (
    LegacyRevisionMapping,
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
from app.services.native_revision_backfill import (
    BackfillFailpointError,
    FAILPOINT_BOUNDARIES,
)


Failpoint = Callable[[str], Awaitable[None] | None]


class ReconcileIntegrityError(ConflictError):
    """The final fixed-ref state cannot be safely attached to native state."""

    def __init__(self, message: str, *, code: str = "native_revision_reconcile_integrity"):
        super().__init__(message)
        self.code = code


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
class ReconcileItemResult:
    resource_id: uuid.UUID
    action: str
    legacy_head_oid: str
    revision_id: str | None
    parent_revision_id: str | None


@dataclass(frozen=True, slots=True)
class ReconcileResult:
    run_id: uuid.UUID
    status: str
    completed_items: int
    failed_items: int
    skipped_items: int
    changed_items: int
    unchanged_items: int
    items: tuple[ReconcileItemResult, ...]
    idempotent_replay: bool = False


@dataclass(frozen=True, slots=True)
class _PendingChange:
    document: LegacyInventoryDocument
    action: str
    parent_revision_id: str
    path_from: str


_RECONCILE_BOUNDARIES: Final[tuple[str, ...]] = (
    *FAILPOINT_BOUNDARIES,
    "reconcile.after_item_commit",
)


class NativeRevisionReconcile:
    """Publish final-ref deltas while preserving the original migration run."""

    coverage_version = "c9-native-reconcile-v1"
    actor = "akb-native-revision-reconcile"
    fingerprint_schema = "c9-native-reconcile-v1"

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
        if boundary not in _RECONCILE_BOUNDARIES:
            raise ValueError(f"native reconcile failpoint boundary is not registered: {boundary}")
        try:
            result = self.failpoint(boundary)
            if inspect.isawaitable(result):
                await result
        except Exception as exc:  # noqa: BLE001 - preserve a stable test seam
            raise BackfillFailpointError(boundary) from exc

    @staticmethod
    def _uuid(run_id: uuid.UUID, resource_id: uuid.UUID, label: str) -> uuid.UUID:
        return uuid.uuid5(
            uuid.NAMESPACE_URL,
            f"akb:{NativeRevisionReconcile.fingerprint_schema}:{run_id}:{resource_id}:{label}",
        )

    @classmethod
    def _fingerprint(
        cls,
        *,
        run: MigrationRun,
        document: LegacyInventoryDocument,
        action: str,
        parent_revision_id: str,
        path_from: str,
    ) -> str:
        suffix_start = next(
            (
                index
                for index, entry in enumerate(document.lineage)
                if entry.legacy_git_oid == document.current_commit
            ),
            len(document.lineage),
        )
        canonical = {
            "schema": cls.fingerprint_schema,
            "run_id": str(run.run_id),
            "namespace_id": str(run.namespace_id),
            "resource_id": str(document.resource_id),
            "fixed_git_oid": run.fixed_git_oid,
            "base_native_revision_id": parent_revision_id,
            "action": action,
            "path_from": path_from,
            "path_to": document.current_path,
            "legacy_head_oid": document.current_commit,
            "body_digest": document.body_digest,
            "byte_size": document.byte_size,
            "lineage_suffix": [
                entry.legacy_git_oid for entry in document.lineage[suffix_start:]
            ],
        }
        return hashlib.sha256(
            json.dumps(canonical, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode(
                "utf-8"
            )
        ).hexdigest()

    async def reconcile(
        self,
        *,
        namespace_id: uuid.UUID,
        fixed_ref: str,
        coverage_version: str | None = None,
    ) -> ReconcileResult:
        """Capture a final fixed ref and publish only changed Resources."""

        coverage = coverage_version or self.coverage_version
        inventory = await self.bridge.capture_inventory(
            namespace_id=namespace_id,
            fixed_ref=fixed_ref,
            coverage_version=coverage,
        )
        await self._assert_inventory_covers_completed_resources(inventory)
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                run = await self.repository.get_or_create_run(
                    namespace_id=namespace_id,
                    fixed_git_oid=fixed_ref,
                    coverage_version=coverage,
                    inventory_digest=inventory.inventory_digest,
                    conn=conn,
                )

        if run.status == "complete":
            return ReconcileResult(
                run_id=run.run_id,
                status=run.status,
                completed_items=0,
                failed_items=0,
                skipped_items=len(inventory.documents),
                changed_items=0,
                unchanged_items=len(inventory.documents),
                items=tuple(
                    ReconcileItemResult(
                        resource_id=document.resource_id,
                        action="unchanged",
                        legacy_head_oid=document.current_commit,
                        revision_id=None,
                        parent_revision_id=None,
                    )
                    for document in inventory.documents
                ),
                idempotent_replay=True,
            )

        try:
            pending, unchanged = await self._classify_inventory(inventory, run=run)
            async with self.pool.acquire() as conn:
                async with conn.transaction():
                    await self.repository.ensure_pending_items(
                        run,
                        (change.document.item_facts() for change in pending),
                        conn=conn,
                    )
                    await self.repository.set_run_status(run.run_id, "running", conn=conn)

            completed = 0
            results = [
                ReconcileItemResult(
                    resource_id=document.resource_id,
                    action="unchanged",
                    legacy_head_oid=document.current_commit,
                    revision_id=None,
                    parent_revision_id=None,
                )
                for document in unchanged
            ]
            for index, change in enumerate(pending):
                item = await self.repository.get_item(run.run_id, change.document.resource_id)
                if item is None:
                    raise ReconcileIntegrityError("reconcile item disappeared before publication")
                if item.status == "complete":
                    results.append(
                        ReconcileItemResult(
                            resource_id=change.document.resource_id,
                            action=change.action,
                            legacy_head_oid=change.document.current_commit,
                            revision_id=item.native_head_revision_id,
                            parent_revision_id=change.parent_revision_id,
                        )
                    )
                    continue
                prepared = await self.body_store.prepare_text(
                    namespace_id=run.namespace_id,
                    payload=change.document.body,
                    expected_digest=change.document.body_digest,
                    expected_size=change.document.byte_size,
                )
                await self._hit("payload.after_prepare_before_tx")
                result = await self._publish_item(
                    run=run,
                    change=change,
                    inventory=inventory,
                    prepared=prepared,
                )
                results.append(result)
                completed += 1
                if index < len(pending) - 1:
                    await self._hit("reconcile.after_item_commit")

            async with self.pool.acquire() as conn:
                async with conn.transaction():
                    if await self.repository.all_items_complete(conn, run.run_id):
                        final_run = await self.repository.set_run_status(
                            run.run_id,
                            "complete",
                            conn=conn,
                        )
                    else:
                        final_run = await self.repository.set_run_status(
                            run.run_id,
                            "failed",
                            error="one or more reconcile items failed",
                            conn=conn,
                        )
            return ReconcileResult(
                run_id=run.run_id,
                status=final_run.status,
                completed_items=completed,
                failed_items=0 if final_run.status == "complete" else len(pending) - completed,
                skipped_items=len(unchanged),
                changed_items=len(pending),
                unchanged_items=len(unchanged),
                items=tuple(results),
            )
        except BackfillFailpointError:
            raise
        except Exception as exc:
            await self._mark_run_failed(run.run_id, exc)
            raise

    async def reconcile_run(self, run_id: uuid.UUID) -> ReconcileResult:
        """Resume a prepared run using its recorded fixed ref and coverage."""

        run = await self.repository.get_run(run_id)
        if run is None:
            raise ReconcileIntegrityError("reconcile run does not exist")
        return await self.reconcile(
            namespace_id=run.namespace_id,
            fixed_ref=run.fixed_git_oid,
            coverage_version=run.coverage_version,
        )

    async def _mark_run_failed(self, run_id: uuid.UUID, exc: Exception) -> None:
        error = getattr(exc, "code", None) or "native_revision_reconcile_failed"
        try:
            await self.repository.set_run_status(run_id, "failed", error=error)
        except Exception:  # noqa: BLE001 - preserve the original reconcile failure
            return

    async def _assert_inventory_covers_completed_resources(
        self,
        inventory: LegacyInventory,
    ) -> None:
        """Reject coverage loss before creating a new reconcile ledger row."""

        inventory_ids = {document.resource_id for document in inventory.documents}
        async with self.pool.acquire() as conn:
            async with conn.transaction(isolation="repeatable_read", readonly=True):
                migrated_ids = await self.repository.migrated_resource_ids(
                    namespace_id=inventory.namespace_id,
                    conn=conn,
                )
        missing = migrated_ids - inventory_ids
        if missing:
            raise ReconcileIntegrityError(
                "legacy inventory is missing a previously migrated Resource",
                code="native_revision_reconcile_missing_document",
            )

    async def _classify_inventory(
        self,
        inventory: LegacyInventory,
        *,
        run: MigrationRun,
    ) -> tuple[list[_PendingChange], list[LegacyInventoryDocument]]:
        pending: list[_PendingChange] = []
        unchanged: list[LegacyInventoryDocument] = []
        inventory_ids = {document.resource_id for document in inventory.documents}
        async with self.pool.acquire() as conn:
            async with conn.transaction(isolation="repeatable_read", readonly=True):
                migrated_ids = await self.repository.migrated_resource_ids(
                    namespace_id=inventory.namespace_id,
                    conn=conn,
                )
                missing = migrated_ids - inventory_ids
                if missing:
                    raise ReconcileIntegrityError(
                        "legacy inventory is missing a previously migrated Resource",
                        code="native_revision_reconcile_missing_document",
                    )

                for document in inventory.documents:
                    resource = await self.native_repository.get_resource_head(
                        resource_id=document.resource_id,
                        conn=conn,
                    )
                    if resource is None:
                        raise ReconcileIntegrityError(
                            f"native Resource is missing: {document.resource_id}",
                            code="native_revision_reconcile_missing_resource",
                        )
                    if (
                        resource["namespace_id"] != inventory.namespace_id
                        or resource["surface"] != "document"
                        or resource["lifecycle"] != "live"
                        or resource["revision_id"] is None
                    ):
                        raise ReconcileIntegrityError(
                            "native Resource is not a live document with a Head",
                            code="native_revision_reconcile_stale_head",
                        )
                    mappings = await self.repository.list_resource_mappings_for_reconcile(
                        namespace_id=inventory.namespace_id,
                        resource_id=document.resource_id,
                        run_id=run.run_id,
                        conn=conn,
                    )
                    head_mapping = await self.repository.mapping_for_native_revision_for_reconcile(
                        namespace_id=inventory.namespace_id,
                        resource_id=document.resource_id,
                        native_revision_id=resource["revision_id"],
                        run_id=run.run_id,
                        conn=conn,
                    )
                    if head_mapping is None or head_mapping.resolution != "native":
                        raise ReconcileIntegrityError(
                            "native Head has no completed native legacy binding",
                            code="native_revision_reconcile_stale_head",
                        )
                    if head_mapping.path_at_revision != resource["path"]:
                        raise ReconcileIntegrityError(
                            "native Head path disagrees with its legacy binding",
                            code="native_revision_reconcile_stale_path",
                        )

                    current_mapping = await self.repository.exact_mapping_for_reconcile(
                        namespace_id=inventory.namespace_id,
                        resource_id=document.resource_id,
                        legacy_git_oid=document.current_commit,
                        run_id=run.run_id,
                        conn=conn,
                    )
                    if current_mapping is not None:
                        if (
                            current_mapping.resolution != "native"
                            or current_mapping.native_revision_id != resource["revision_id"]
                        ):
                            raise ReconcileIntegrityError(
                                "final legacy OID is already bound to a different native state",
                                code="native_revision_reconcile_divergent_lineage",
                            )
                        self._verify_current(document, resource, head_mapping)
                        unchanged.append(document)
                        continue

                    if document.current_commit == head_mapping.legacy_git_oid:
                        self._verify_current(document, resource, head_mapping)
                        unchanged.append(document)
                        continue

                    change = self._validate_change(
                        inventory=inventory,
                        document=document,
                        resource=resource,
                        head_mapping=head_mapping,
                        mappings=mappings,
                    )
                    destination = await self.native_repository.find_live_path(
                        conn,
                        inventory.namespace_id,
                        "document",
                        document.current_path,
                    )
                    if destination is not None and destination["resource_id"] != document.resource_id:
                        raise ReconcileIntegrityError(
                            f"native destination path is already owned: {document.current_path}",
                            code="native_revision_reconcile_path_collision",
                        )
                    destination_alias = await self.native_repository.find_live_alias(
                        conn,
                        namespace_id=inventory.namespace_id,
                        surface="document",
                        old_path=document.current_path,
                    )
                    if destination_alias is not None and destination_alias["resource_id"] != document.resource_id:
                        raise ReconcileIntegrityError(
                            f"native destination alias is already owned: {document.current_path}",
                            code="native_revision_reconcile_path_collision",
                        )
                    pending.append(change)
        return pending, unchanged

    @staticmethod
    def _verify_current(
        document: LegacyInventoryDocument,
        resource: dict,
        head_mapping: LegacyRevisionMapping,
    ) -> None:
        if (
            resource["path"] != document.current_path
            or head_mapping.path_at_revision != document.current_path
            or resource.get("digest") != document.body_digest
            or resource.get("byte_size") != document.byte_size
            or head_mapping.legacy_git_oid != document.current_commit
        ):
            raise ReconcileIntegrityError(
                "legacy current head disagrees with native head facts",
                code="native_revision_reconcile_stale_head",
            )

    @classmethod
    def _validate_change(
        cls,
        *,
        inventory: LegacyInventory,
        document: LegacyInventoryDocument,
        resource: dict,
        head_mapping: LegacyRevisionMapping,
        mappings: list[LegacyRevisionMapping],
    ) -> _PendingChange:
        if document.activity.action not in {"update", "move"}:
            raise ReconcileIntegrityError(
                "final legacy activity is not an update or move",
                code="native_revision_reconcile_unsupported_activity",
            )
        if not document.lineage or document.lineage[-1].legacy_git_oid != document.current_commit:
            raise ReconcileIntegrityError(
                "final legacy lineage does not end at current_commit",
                code="native_revision_reconcile_divergent_lineage",
            )
        lineage_oids = [entry.legacy_git_oid for entry in document.lineage]
        if len(set(lineage_oids)) != len(lineage_oids):
            raise ReconcileIntegrityError(
                "final fixed-ref lineage contains an ambiguous legacy OID",
                code="native_revision_reconcile_divergent_lineage",
            )
        base_indexes = [
            index
            for index, entry in enumerate(document.lineage)
            if entry.legacy_git_oid == head_mapping.legacy_git_oid
        ]
        if len(base_indexes) != 1 or base_indexes[0] >= len(document.lineage) - 1:
            raise ReconcileIntegrityError(
                "final fixed-ref lineage has no new suffix",
                code="native_revision_reconcile_divergent_lineage",
            )
        base_index = base_indexes[0]
        suffix = document.lineage[base_index:]
        if (
            any(not entry.path_at_revision for entry in suffix)
            or suffix[0].path_at_revision != resource["path"]
            or suffix[-1].path_at_revision != document.current_path
        ):
            raise ReconcileIntegrityError(
                "final fixed-ref suffix has missing or divergent path lineage",
                code="native_revision_reconcile_divergent_lineage",
            )

        by_oid = {mapping.legacy_git_oid: mapping for mapping in mappings}
        for index, entry in enumerate(document.lineage[: base_index + 1]):
            mapping = by_oid.get(entry.legacy_git_oid)
            if (
                mapping is None
                or mapping.path_at_revision != entry.path_at_revision
                or mapping.lineage_ordinal != index
            ):
                raise ReconcileIntegrityError(
                    "final fixed-ref lineage diverges from completed mappings",
                    code="native_revision_reconcile_divergent_lineage",
                )
        if (
            head_mapping.lineage_ordinal != base_index
            or head_mapping.native_revision_id != resource["revision_id"]
        ):
            raise ReconcileIntegrityError(
                "native Head binding is stale for the final fixed ref",
                code="native_revision_reconcile_stale_head",
            )

        for entry in document.lineage[base_index + 1 :]:
            existing = by_oid.get(entry.legacy_git_oid)
            if existing is None:
                continue
            is_final = entry.legacy_git_oid == document.current_commit
            if (
                existing.path_at_revision != entry.path_at_revision
                or (is_final and existing.resolution != "native")
                or (not is_final and existing.resolution != "bridge")
                or (is_final and existing.native_revision_id is None)
                or (not is_final and existing.native_revision_id is not None)
            ):
                raise ReconcileIntegrityError(
                    "new fixed-ref suffix conflicts with an immutable mapping",
                    code="native_revision_reconcile_divergent_lineage",
                )

        # Terminal activity describes only the final OID.  Validate it against
        # that local edge, then classify the aggregate transition across the
        # complete suffix from the native base to the frozen final state.
        previous_path = suffix[-2].path_at_revision
        if document.activity.path_to != document.current_path:
            raise ReconcileIntegrityError(
                "final legacy activity disagrees with the lineage destination",
                code="native_revision_reconcile_divergent_lineage",
            )
        if document.activity.action == "update":
            terminal_edge_is_valid = (
                document.activity.path_from is None
                and previous_path == document.current_path
            )
        else:
            terminal_edge_is_valid = (
                document.activity.path_from == previous_path
                and previous_path != document.current_path
            )
        if not terminal_edge_is_valid:
            raise ReconcileIntegrityError(
                "final legacy activity disagrees with the terminal lineage edge",
                code="native_revision_reconcile_divergent_lineage",
            )

        old_path = resource["path"]
        path_changed = document.current_path != old_path
        body_changed = (
            resource.get("digest") != document.body_digest
            or resource.get("byte_size") != document.byte_size
        )
        if not path_changed and not body_changed:
            raise ReconcileIntegrityError(
                "final fixed-ref suffix has no aggregate body or path change",
                code="native_revision_reconcile_divergent_lineage",
            )
        return _PendingChange(
            document=document,
            action="move" if path_changed else "replace",
            parent_revision_id=resource["revision_id"],
            path_from=old_path,
        )

    async def _publish_item(
        self,
        *,
        run: MigrationRun,
        change: _PendingChange,
        inventory: LegacyInventory,
        prepared,
    ) -> ReconcileItemResult:
        del inventory
        document = change.document
        fingerprint = self._fingerprint(
            run=run,
            document=document,
            action=change.action,
            parent_revision_id=change.parent_revision_id,
            path_from=change.path_from,
        )
        mutation_id = self._uuid(run.run_id, document.resource_id, "mutation")
        manifest_id = self._uuid(run.run_id, document.resource_id, "manifest")
        activity_id = self._uuid(run.run_id, document.resource_id, "activity")
        intent_id = self._uuid(run.run_id, document.resource_id, "invalidation")
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                item = await self.repository.get_item(
                    run.run_id,
                    document.resource_id,
                    conn=conn,
                    for_update=True,
                )
                if item is None:
                    raise ReconcileIntegrityError("reconcile item disappeared during publication")
                if item.status == "complete":
                    return ReconcileItemResult(
                        resource_id=document.resource_id,
                        action=change.action,
                        legacy_head_oid=document.current_commit,
                        revision_id=item.native_head_revision_id,
                        parent_revision_id=change.parent_revision_id,
                    )

                await self.native_repository.lock_mutation(conn, run.namespace_id, mutation_id)
                prior = await self.native_repository.find_mutation(
                    conn,
                    run.namespace_id,
                    mutation_id,
                )
                if prior is not None:
                    if prior["request_fingerprint"] != fingerprint:
                        raise ReconcileIntegrityError(
                            "reconcile mutation ID was reused with different input",
                            code="native_revision_reconcile_fingerprint_conflict",
                        )
                    await self.repository.mark_item_complete(
                        conn,
                        run_id=run.run_id,
                        legacy_document_id=document.resource_id,
                        native_head_revision_id=prior["revision_id"],
                    )
                    return ReconcileItemResult(
                        resource_id=document.resource_id,
                        action=prior["action"],
                        legacy_head_oid=document.current_commit,
                        revision_id=prior["revision_id"],
                        parent_revision_id=prior["parent_revision_id"],
                    )

                resource = await self.native_repository.lock_resource(conn, document.resource_id)
                if resource is None or resource["namespace_id"] != run.namespace_id:
                    raise ReconcileIntegrityError(
                        "native Resource disappeared during publication",
                        code="native_revision_reconcile_missing_resource",
                    )
                if resource["lifecycle"] != "live":
                    raise ReconcileIntegrityError(
                        "native Resource is not live during publication",
                        code="native_revision_reconcile_stale_head",
                    )
                if (
                    resource["head_revision_id"] != change.parent_revision_id
                    or resource["current_path"] != change.path_from
                ):
                    raise ReconcileIntegrityError(
                        "native Head changed after final-ref classification",
                        code="native_revision_reconcile_stale_head",
                    )
                await self.native_repository.lock_paths(
                    conn,
                    run.namespace_id,
                    "document",
                    change.path_from,
                    document.current_path,
                )
                destination = await self.native_repository.find_live_path(
                    conn,
                    run.namespace_id,
                    "document",
                    document.current_path,
                )
                if destination is not None and destination["resource_id"] != document.resource_id:
                    raise ReconcileIntegrityError(
                        f"native destination path is already owned: {document.current_path}",
                        code="native_revision_reconcile_path_collision",
                    )
                destination_alias = await self.native_repository.find_live_alias(
                    conn,
                    namespace_id=run.namespace_id,
                    surface="document",
                    old_path=document.current_path,
                )
                if destination_alias is not None and destination_alias["resource_id"] != document.resource_id:
                    raise ReconcileIntegrityError(
                        f"native destination alias is already owned: {document.current_path}",
                        code="native_revision_reconcile_path_collision",
                    )

                retained_oids = {entry.legacy_git_oid for entry in document.lineage}
                revision_id = await self.repository.allocate_native_revision_id(
                    conn,
                    resource_id=document.resource_id,
                    retained_legacy_oids=retained_oids,
                    revision_id_factory=self.revision_id_factory,
                )
                occurred_at = document.lineage[-1].committed_at.astimezone(UTC)
                activity = document.activity
                await self.native_repository.insert_manifest(
                    conn,
                    payload_manifest_id=manifest_id,
                    namespace_id=run.namespace_id,
                    resource_id=document.resource_id,
                    payload=prepared,
                    occurred_at=occurred_at,
                )
                await self._hit("authority.after_manifest")
                await self.native_repository.insert_revision(
                    conn,
                    revision_id=revision_id,
                    namespace_id=run.namespace_id,
                    resource_id=document.resource_id,
                    parent_revision_id=change.parent_revision_id,
                    action=change.action,
                    path=document.current_path,
                    path_from=change.path_from if change.action == "move" else None,
                    path_to=document.current_path if change.action == "move" else None,
                    payload_manifest_id=manifest_id,
                    mutation_id=mutation_id,
                    request_fingerprint=fingerprint,
                    message="C9 fixed-ref native reconcile",
                    subject=activity.subject,
                    summary=activity.summary,
                    actor=activity.actor,
                    occurred_at=occurred_at,
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
                    occurred_at=occurred_at,
                )
                await self._hit("authority.after_head")
                if destination_alias is not None:
                    await self.native_repository.retire_live_alias(
                        conn,
                        namespace_id=run.namespace_id,
                        surface="document",
                        old_path=document.current_path,
                        retired_revision_id=revision_id,
                        occurred_at=occurred_at,
                    )
                if change.action == "move":
                    await self.native_repository.insert_path_alias(
                        conn,
                        namespace_id=run.namespace_id,
                        surface="document",
                        old_path=change.path_from,
                        resource_id=document.resource_id,
                        created_revision_id=revision_id,
                        occurred_at=occurred_at,
                    )
                await self._hit("authority.after_alias")
                await self.native_repository.insert_activity(
                    conn,
                    activity_event_id=activity_id,
                    namespace_id=run.namespace_id,
                    resource_id=document.resource_id,
                    revision_id=revision_id,
                    action=change.action,
                    actor=activity.actor,
                    subject=activity.subject,
                    summary=activity.summary,
                    path_from=change.path_from if change.action == "move" else None,
                    path_to=document.current_path if change.action == "move" else None,
                    occurred_at=occurred_at,
                )
                await self._hit("authority.after_activity")
                await self.native_repository.insert_invalidation_intent(
                    conn,
                    intent_id=intent_id,
                    namespace_id=run.namespace_id,
                    resource_id=document.resource_id,
                    revision_id=revision_id,
                    reason=change.action,
                    occurred_at=occurred_at,
                )
                await self._hit("authority.after_invalidation")

                mappings = await self.repository.list_resource_mappings_for_reconcile(
                    namespace_id=run.namespace_id,
                    resource_id=document.resource_id,
                    run_id=run.run_id,
                    conn=conn,
                )
                mapped_oids = {mapping.legacy_git_oid for mapping in mappings}
                base_mapping = next(
                    mapping
                    for mapping in mappings
                    if mapping.native_revision_id == change.parent_revision_id
                )
                base_index = next(
                    index
                    for index, entry in enumerate(document.lineage)
                    if entry.legacy_git_oid == base_mapping.legacy_git_oid
                )
                for ordinal, entry in enumerate(document.lineage[base_index + 1 :], start=base_index + 1):
                    if entry.legacy_git_oid in mapped_oids:
                        continue
                    await self.repository.ensure_cross_run_mapping(
                        conn,
                        namespace_id=run.namespace_id,
                        resource_id=document.resource_id,
                        legacy_git_oid=entry.legacy_git_oid,
                        path_at_revision=entry.path_at_revision,
                        resolution=("native" if entry.legacy_git_oid == document.current_commit else "bridge"),
                        native_revision_id=(revision_id if entry.legacy_git_oid == document.current_commit else None),
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
        return ReconcileItemResult(
            resource_id=document.resource_id,
            action=change.action,
            legacy_head_oid=document.current_commit,
            revision_id=revision_id,
            parent_revision_id=change.parent_revision_id,
        )


NativeRevisionReconciler = NativeRevisionReconcile
