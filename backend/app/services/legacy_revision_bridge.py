"""Fixed-ref legacy inventory and Resource-scoped selector bridge."""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
import uuid
from collections.abc import AsyncIterator, Iterable, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from types import MappingProxyType
from typing import Literal

import asyncpg

from app.exceptions import AKBError, ConflictError, NotFoundError
from app.repositories.native_revision_migration_repo import (
    MigrationInventoryDriftError,
    MigrationRun,
    NativeRevisionMigrationRepository,
)
from app.services.git_service import FixedRefHistoryError, GitService


_OID_RE = re.compile(r"^[0-9a-f]{40}$")
_SELECTOR_RE = re.compile(r"^[0-9a-f]{7,40}$")


class ManualVaultRequiredError(ConflictError):
    """The requested namespace is not an eligible manual vault."""

    def __init__(self):
        super().__init__("native revision migration requires one manual vault")
        self.code = "native_revision_migration_manual_vault_required"


class InventoryEligibilityError(ConflictError):
    """A legacy document cannot be safely frozen into the C9 inventory."""

    def __init__(self, message: str):
        super().__init__(message)
        self.code = "native_revision_migration_inventory_ineligible"


class SelectorInvalidError(AKBError):
    def __init__(self, selector: str):
        super().__init__(
            f"invalid legacy revision selector: {selector}",
            status_code=400,
            code="legacy_revision_selector_invalid",
        )


class SelectorUnknownError(NotFoundError):
    def __init__(self, selector: str):
        super().__init__("Legacy revision selector", selector)
        self.code = "legacy_revision_selector_unknown"


class SelectorAmbiguousError(ConflictError):
    def __init__(self, selector: str):
        super().__init__(f"legacy revision selector is ambiguous: {selector}")
        self.code = "legacy_revision_selector_ambiguous"


class LogicalLineageProjectionError(ValueError):
    """Raw fixed-ref history cannot form one logical Document lineage."""


@dataclass(frozen=True, slots=True)
class LegacyLineageEntry:
    legacy_git_oid: str
    path_at_revision: str
    committed_at: datetime


@dataclass(frozen=True, slots=True)
class LegacyActivitySemantics:
    legacy_git_oid: str
    committed_at: datetime
    actor: str
    subject: str
    summary: str
    action: Literal["create", "update", "move", "delete"]
    path_from: str | None
    path_to: str | None
    changed_paths: tuple[Mapping[str, str | None], ...]


@dataclass(frozen=True, slots=True)
class LegacyInventoryAlias:
    old_ref: str
    created_at: datetime


@dataclass(frozen=True, slots=True)
class LegacyInventoryDocument:
    resource_id: uuid.UUID
    current_path: str
    current_commit: str
    created_at: datetime
    body_digest: str
    byte_size: int
    aliases: tuple[LegacyInventoryAlias, ...]
    lineage: tuple[LegacyLineageEntry, ...]
    activity: LegacyActivitySemantics

    def item_facts(self) -> dict:
        return {
            "legacy_document_id": self.resource_id,
            "captured_path": self.current_path,
            "legacy_head_oid": self.current_commit,
            "body_digest": self.body_digest,
            "byte_size": self.byte_size,
        }


@dataclass(frozen=True, slots=True)
class LegacyInventory:
    namespace_id: uuid.UUID
    fixed_git_oid: str
    coverage_version: str
    documents: tuple[LegacyInventoryDocument, ...]
    inventory_digest: str


@dataclass(frozen=True, slots=True)
class LegacyInventoryScope:
    """Once-validated immutable facts for one inventory materialization pass."""

    inventory: LegacyInventory
    vault_name: str
    fixed_git_oid: str
    documents_by_id: Mapping[uuid.UUID, LegacyInventoryDocument]


@dataclass(frozen=True, slots=True)
class SelectorResolution:
    resource_id: uuid.UUID
    selector: str
    kind: Literal["native", "bridge"]
    native_revision_id: str | None
    fixed_git_oid: str | None
    legacy_git_oid: str | None
    path_at_revision: str | None
    run_id: uuid.UUID | None


def _require_oid(value: str, field: str) -> None:
    if not isinstance(value, str) or _OID_RE.fullmatch(value) is None:
        raise InventoryEligibilityError(
            f"{field} must be a full lowercase 40-hex commit OID"
        )


def _iso(value: datetime) -> str:
    return value.astimezone(UTC).isoformat()


def project_logical_lineage(
    history: Iterable[Mapping[str, object]],
    *,
    current_commit: str,
    current_path: str,
    created_at: datetime,
    oldest_anchor_oid: str | None = None,
) -> tuple[LegacyLineageEntry, ...]:
    """Project newest-first Git rows onto one oldest-first Document lineage."""

    if created_at.tzinfo is None or created_at.utcoffset() is None:
        raise LogicalLineageProjectionError("created_at must be timezone-aware")
    if oldest_anchor_oid is not None and _OID_RE.fullmatch(oldest_anchor_oid) is None:
        raise LogicalLineageProjectionError("oldest anchor must be a full Git OID")
    created_at_utc = created_at.astimezone(UTC)
    entries = tuple(history)
    use_create_boundary = oldest_anchor_oid is None and any(
        isinstance(entry, Mapping) and entry.get("action") == "create"
        for entry in entries
    )
    newest: list[LegacyLineageEntry] = []
    seen: set[str] = set()
    anchor_found = False
    create_found = False
    for entry in entries:
        if not isinstance(entry, Mapping):
            raise LogicalLineageProjectionError("history entry must be a mapping")
        oid = entry.get("legacy_git_oid")
        if not isinstance(oid, str) or _OID_RE.fullmatch(oid) is None:
            raise LogicalLineageProjectionError("history contains an invalid OID")
        if anchor_found:
            if oid == oldest_anchor_oid:
                raise LogicalLineageProjectionError(
                    "history contains a duplicate oldest anchor"
                )
            continue
        if create_found:
            continue
        committed_at = entry.get("committed_at")
        if (
            not isinstance(committed_at, datetime)
            or committed_at.tzinfo is None
            or committed_at.utcoffset() is None
        ):
            raise LogicalLineageProjectionError(
                "history contains an invalid commit timestamp"
            )
        path = current_path if oid == current_commit else entry.get("path_at_revision")
        if not isinstance(path, str) or not path.strip():
            raise LogicalLineageProjectionError("history contains an invalid path")
        if oid in seen:
            if oid == oldest_anchor_oid:
                raise LogicalLineageProjectionError(
                    "history contains a duplicate oldest anchor"
                )
            continue
        seen.add(oid)
        if (
            oldest_anchor_oid is None
            and not use_create_boundary
            and oid != current_commit
            and committed_at.astimezone(UTC) < created_at_utc
        ):
            continue
        newest.append(
            LegacyLineageEntry(
                legacy_git_oid=oid,
                path_at_revision=path,
                committed_at=committed_at,
            )
        )
        if oid == oldest_anchor_oid:
            anchor_found = True
        if use_create_boundary and entry.get("action") == "create":
            create_found = True
    if oldest_anchor_oid is not None and not anchor_found:
        raise LogicalLineageProjectionError(
            "completed oldest anchor is absent from fixed-ref history"
        )
    return tuple(reversed(newest))


class LegacyRevisionBridge:
    """Capture manual legacy history at one immutable Git tip."""

    inventory_schema = "c9-fixed-ref-inventory-v1"

    def __init__(
        self,
        pool: asyncpg.Pool,
        *,
        git: GitService,
        repository: NativeRevisionMigrationRepository | None = None,
    ):
        self.pool = pool
        self.git = git
        self.repository = repository or NativeRevisionMigrationRepository(pool)

    @classmethod
    def canonical_inventory_digest(
        cls,
        *,
        namespace_id: uuid.UUID,
        fixed_git_oid: str,
        coverage_version: str,
        documents: tuple[LegacyInventoryDocument, ...],
    ) -> str:
        payload = {
            "schema": cls.inventory_schema,
            "namespace_id": str(namespace_id),
            "fixed_git_oid": fixed_git_oid,
            "coverage_version": coverage_version,
            "documents": [
                {
                    "resource_id": str(document.resource_id),
                    "current_path": document.current_path,
                    "current_commit": document.current_commit,
                    "created_at": _iso(document.created_at),
                    "body_digest": document.body_digest,
                    "byte_size": document.byte_size,
                    "aliases": [
                        {
                            "old_ref": alias.old_ref,
                            "created_at": _iso(alias.created_at),
                        }
                        for alias in document.aliases
                    ],
                    "lineage": [
                        {
                            "legacy_git_oid": entry.legacy_git_oid,
                            "path_at_revision": entry.path_at_revision,
                            "committed_at": _iso(entry.committed_at),
                        }
                        for entry in document.lineage
                    ],
                    "activity": {
                        "legacy_git_oid": document.activity.legacy_git_oid,
                        "committed_at": _iso(document.activity.committed_at),
                        "actor": document.activity.actor,
                        "subject": document.activity.subject,
                        "summary": document.activity.summary,
                        "action": document.activity.action,
                        "path_from": document.activity.path_from,
                        "path_to": document.activity.path_to,
                        "changed_paths": [
                            dict(change) for change in document.activity.changed_paths
                        ],
                    },
                }
                for document in documents
            ],
        }
        canonical = json.dumps(
            payload,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        return hashlib.sha256(canonical).hexdigest()

    async def _capture_source_metadata(
        self,
        *,
        vault_name: str,
        fixed_ref: str,
        resource_id: uuid.UUID,
        current_path: str,
        current_commit: str,
        created_at: datetime,
    ) -> tuple[list[dict], dict, str, int]:
        """Read one source body and return only frozen metadata about it."""

        try:
            snapshot = await asyncio.to_thread(
                self.git.manual_fixed_ref_history,
                vault_name,
                fixed_ref,
                current_path,
                current_commit=current_commit,
                since_epoch=int(created_at.timestamp()),
            )
        except FixedRefHistoryError as exc:
            raise InventoryEligibilityError(
                f"document {resource_id} is not readable at the fixed ref"
            ) from exc
        if not isinstance(snapshot, dict):
            raise InventoryEligibilityError(
                f"document {resource_id} returned invalid fixed-ref metadata"
            )
        history = snapshot.get("history")
        activity = snapshot.get("activity")
        body = snapshot.get("body")
        if not isinstance(history, list) or not isinstance(activity, dict):
            raise InventoryEligibilityError(
                f"document {resource_id} returned invalid fixed-ref metadata"
            )
        if not isinstance(body, bytes):
            raise InventoryEligibilityError(
                f"document {resource_id} is not readable at the fixed ref"
            )
        if b"\x00" in body:
            raise InventoryEligibilityError(
                f"document {resource_id} body contains NUL bytes"
            )
        try:
            body.decode("utf-8", errors="strict")
        except UnicodeDecodeError as exc:
            raise InventoryEligibilityError(
                f"document {resource_id} body is not valid UTF-8"
            ) from exc
        return history, activity, hashlib.sha256(body).hexdigest(), len(body)

    async def capture_inventory_scope(
        self,
        *,
        namespace_id: uuid.UUID,
        fixed_ref: str,
        coverage_version: str,
    ) -> LegacyInventoryScope:
        _require_oid(fixed_ref, "fixed_ref")
        if not isinstance(coverage_version, str) or not coverage_version.strip():
            raise InventoryEligibilityError("coverage_version must be non-empty")

        async with self.pool.acquire() as conn:
            async with conn.transaction(isolation="repeatable_read", readonly=True):
                vault = await self.repository.get_manual_vault(namespace_id, conn=conn)
                if vault is None:
                    raise NotFoundError("Vault", str(namespace_id))
                if vault["has_external_git"]:
                    raise ManualVaultRequiredError()

                rows = await self.repository.list_documents(conn, namespace_id)
                # A manual vault can retain source rows that are owned by the
                # external-Git/document projection.  Those rows are explicit
                # excluded no-ops for C9: do not let them poison the eligible
                # manual inventory or create any authority/item facts.  The
                # vault-level external-Git marker above remains a hard reject.
                rows = [row for row in rows if row.get("source") == "manual"]

                completed_anchor_rows = (
                    await self.repository.list_completed_lineage_anchors(
                        namespace_id=namespace_id,
                        conn=conn,
                    )
                )
                completed_anchors: dict[uuid.UUID, str] = {}
                for anchor in completed_anchor_rows:
                    if anchor.resource_id in completed_anchors:
                        raise InventoryEligibilityError(
                            "completed lineage has duplicate ordinal-zero anchors"
                        )
                    completed_anchors[anchor.resource_id] = anchor.legacy_git_oid

                documents: list[LegacyInventoryDocument] = []
                for row in rows:
                    aliases: list[LegacyInventoryAlias] = []
                    for alias in await self.repository.list_document_aliases(
                        conn, namespace_id, row["id"]
                    ):
                        old_ref = alias.get("old_ref")
                        created_at = alias.get("created_at")
                        if not isinstance(old_ref, str) or not old_ref.strip():
                            raise InventoryEligibilityError(
                                f"document {row['id']} has an invalid resource alias"
                            )
                        if not isinstance(created_at, datetime):
                            raise InventoryEligibilityError(
                                f"document {row['id']} has an alias without a usable timestamp"
                            )
                        aliases.append(
                            LegacyInventoryAlias(
                                old_ref=old_ref,
                                created_at=created_at,
                            )
                        )
                    current_commit = row.get("current_commit")
                    if not isinstance(current_commit, str) or _OID_RE.fullmatch(current_commit) is None:
                        raise InventoryEligibilityError(
                            f"document {row['id']} has no full current_commit"
                        )
                    created_at = row.get("created_at")
                    if not isinstance(created_at, datetime):
                        raise InventoryEligibilityError(
                            f"document {row['id']} has no usable created_at"
                        )
                    history, raw_activity, body_digest, byte_size = (
                        await self._capture_source_metadata(
                            vault_name=vault["name"],
                            fixed_ref=fixed_ref,
                            resource_id=row["id"],
                            current_path=row["path"],
                            current_commit=current_commit,
                            created_at=created_at,
                        )
                    )
                    if not history or history[0]["legacy_git_oid"] != current_commit:
                        raise InventoryEligibilityError(
                            f"document {row['id']} current_commit is not the fixed-ref file head"
                        )
                    activity_action = raw_activity.get("action")
                    if activity_action not in {"create", "update", "move", "delete"}:
                        raise InventoryEligibilityError(
                            f"document {row['id']} has an unsupported current activity action"
                        )
                    activity_oid = raw_activity.get("legacy_git_oid")
                    activity_time = raw_activity.get("committed_at")
                    activity_actor = raw_activity.get("actor")
                    activity_subject = raw_activity.get("subject")
                    activity_summary = raw_activity.get("summary")
                    path_from = raw_activity.get("path_from")
                    path_to = raw_activity.get("path_to")
                    changed_paths = raw_activity.get("changed_paths")
                    if (
                        activity_oid != current_commit
                        or not isinstance(activity_time, datetime)
                        or activity_time != history[0]["committed_at"]
                        or not isinstance(activity_actor, str)
                        or not activity_actor
                        or not isinstance(activity_subject, str)
                        or not activity_subject
                        or not isinstance(activity_summary, str)
                        or not isinstance(path_to, str)
                        or not isinstance(changed_paths, list)
                        or not all(isinstance(change, dict) for change in changed_paths)
                    ):
                        raise InventoryEligibilityError(
                            f"document {row['id']} has incomplete current activity semantics"
                        )
                    if activity_action == "move" and not isinstance(path_from, str):
                        raise InventoryEligibilityError(
                            f"document {row['id']} move activity has no source path"
                        )
                    if activity_action != "move" and path_from is not None:
                        raise InventoryEligibilityError(
                            f"document {row['id']} non-move activity has a source path"
                        )
                    activity = LegacyActivitySemantics(
                        legacy_git_oid=activity_oid,
                        committed_at=activity_time,
                        actor=activity_actor,
                        subject=activity_subject,
                        summary=activity_summary,
                        action=activity_action,
                        path_from=path_from,
                        path_to=path_to,
                        changed_paths=tuple(
                            MappingProxyType(dict(change)) for change in changed_paths
                        ),
                    )
                    try:
                        lineage = project_logical_lineage(
                            history,
                            current_commit=current_commit,
                            current_path=row["path"],
                            created_at=created_at,
                            oldest_anchor_oid=completed_anchors.get(row["id"]),
                        )
                    except LogicalLineageProjectionError as exc:
                        raise InventoryEligibilityError(
                            f"document {row['id']} history is invalid"
                        ) from exc
                    seen = {entry.legacy_git_oid for entry in lineage}
                    if current_commit not in seen:
                        raise InventoryEligibilityError(
                            f"document {row['id']} current_commit is absent from fixed-ref history"
                        )
                    if not lineage or lineage[-1].legacy_git_oid != current_commit:
                        raise InventoryEligibilityError(
                            f"document {row['id']} lineage does not end at current_commit"
                        )
                    documents.append(
                        LegacyInventoryDocument(
                            resource_id=row["id"],
                            current_path=row["path"],
                            current_commit=current_commit,
                            created_at=created_at,
                            body_digest=body_digest,
                            byte_size=byte_size,
                            aliases=tuple(aliases),
                            lineage=lineage,
                            activity=activity,
                        )
                    )

        frozen_documents = tuple(documents)
        inventory = LegacyInventory(
            namespace_id=namespace_id,
            fixed_git_oid=fixed_ref,
            coverage_version=coverage_version,
            documents=frozen_documents,
            inventory_digest=self.canonical_inventory_digest(
                namespace_id=namespace_id,
                fixed_git_oid=fixed_ref,
                coverage_version=coverage_version,
                documents=frozen_documents,
            ),
        )
        return self._scope_from_validated_inventory(inventory, vault_name=vault["name"])

    async def capture_inventory(
        self,
        *,
        namespace_id: uuid.UUID,
        fixed_ref: str,
        coverage_version: str,
    ) -> LegacyInventory:
        scope = await self.capture_inventory_scope(
            namespace_id=namespace_id,
            fixed_ref=fixed_ref,
            coverage_version=coverage_version,
        )
        return scope.inventory

    @staticmethod
    def _scope_from_validated_inventory(
        inventory: LegacyInventory,
        *,
        vault_name: str,
    ) -> LegacyInventoryScope:
        documents_by_id: dict[uuid.UUID, LegacyInventoryDocument] = {}
        for document in inventory.documents:
            if document.resource_id in documents_by_id:
                raise InventoryEligibilityError(
                    "frozen inventory contains duplicate document resources"
                )
            documents_by_id[document.resource_id] = document
        return LegacyInventoryScope(
            inventory=inventory,
            vault_name=vault_name,
            fixed_git_oid=inventory.fixed_git_oid,
            documents_by_id=MappingProxyType(documents_by_id),
        )

    async def validated_inventory_scope(
        self,
        inventory: LegacyInventory,
    ) -> LegacyInventoryScope:
        """Validate externally held inventory facts once before body reads."""

        if (
            self.canonical_inventory_digest(
                namespace_id=inventory.namespace_id,
                fixed_git_oid=inventory.fixed_git_oid,
                coverage_version=inventory.coverage_version,
                documents=inventory.documents,
            )
            != inventory.inventory_digest
        ):
            raise InventoryEligibilityError("body materialization inventory digest is invalid")
        vault = await self.repository.get_manual_vault(inventory.namespace_id)
        if vault is None:
            raise NotFoundError("Vault", str(inventory.namespace_id))
        if vault["has_external_git"]:
            raise ManualVaultRequiredError()
        return self._scope_from_validated_inventory(inventory, vault_name=vault["name"])

    @asynccontextmanager
    async def materialize_body(
        self,
        scope: LegacyInventoryScope,
        document: LegacyInventoryDocument,
    ) -> AsyncIterator[bytes]:
        """Perform one body-only read inside a once-validated inventory scope."""

        if (
            scope.fixed_git_oid != scope.inventory.fixed_git_oid
            or scope.documents_by_id.get(document.resource_id) is not document
        ):
            raise InventoryEligibilityError(
                "body materialization requires a document from the frozen inventory"
            )
        try:
            text = await asyncio.to_thread(
                self.git.read_file,
                scope.vault_name,
                document.current_path,
                document.current_commit,
            )
        except (OSError, UnicodeDecodeError, ValueError) as exc:
            raise InventoryEligibilityError(
                f"document {document.resource_id} is not readable at the fixed ref"
            ) from exc
        if not isinstance(text, str):
            raise InventoryEligibilityError(
                f"document {document.resource_id} is not readable at the fixed ref"
            )
        body = text.encode("utf-8")
        del text
        if (
            len(body) != document.byte_size
            or hashlib.sha256(body).hexdigest() != document.body_digest
        ):
            raise InventoryEligibilityError(
                f"document {document.resource_id} body differs from the frozen inventory"
            )
        if b"\x00" in body:
            raise InventoryEligibilityError(
                f"document {document.resource_id} body contains NUL bytes"
            )
        try:
            body.decode("utf-8", errors="strict")
        except UnicodeDecodeError as exc:
            raise InventoryEligibilityError(
                f"document {document.resource_id} body is not valid UTF-8"
            ) from exc
        try:
            yield body
        finally:
            del body

    async def prepare_run(
        self,
        *,
        namespace_id: uuid.UUID,
        fixed_ref: str,
        coverage_version: str,
    ) -> tuple[MigrationRun, LegacyInventory]:
        scope = await self.capture_inventory_scope(
            namespace_id=namespace_id,
            fixed_ref=fixed_ref,
            coverage_version=coverage_version,
        )
        inventory = scope.inventory
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                run = await self.repository.get_or_create_run(
                    namespace_id=namespace_id,
                    fixed_git_oid=fixed_ref,
                    coverage_version=coverage_version,
                    inventory_digest=inventory.inventory_digest,
                    conn=conn,
                )
                await self.repository.ensure_pending_items(
                    run,
                    (document.item_facts() for document in inventory.documents),
                    conn=conn,
                )
        return run, inventory

    async def inventory_scope_for_run(self, run: MigrationRun) -> LegacyInventoryScope:
        scope = await self.capture_inventory_scope(
            namespace_id=run.namespace_id,
            fixed_ref=run.fixed_git_oid,
            coverage_version=run.coverage_version,
        )
        if scope.inventory.inventory_digest != run.inventory_digest:
            raise MigrationInventoryDriftError()
        return scope

    async def inventory_for_run(self, run: MigrationRun) -> LegacyInventory:
        return (await self.inventory_scope_for_run(run)).inventory

    async def resolve_selector(
        self,
        *,
        resource_id: uuid.UUID,
        selector: str,
    ) -> SelectorResolution:
        if not isinstance(selector, str) or _SELECTOR_RE.fullmatch(selector) is None:
            raise SelectorInvalidError(selector)

        if len(selector) == 40:
            native = await self.repository.exact_native_revision(
                namespace_id=await self._resource_namespace(resource_id),
                resource_id=resource_id,
                revision_id=selector,
            )
            mapping = await self.repository.exact_mapping(
                resource_id=resource_id,
                legacy_git_oid=selector,
            )
            if native is not None and mapping is not None:
                raise SelectorAmbiguousError(selector)
            if native is not None:
                return SelectorResolution(
                    resource_id=resource_id,
                    selector=selector,
                    kind="native",
                    native_revision_id=native["revision_id"],
                    fixed_git_oid=None,
                    legacy_git_oid=None,
                    path_at_revision=native["path_at_revision"],
                    run_id=None,
                )
            if mapping is not None:
                return self._mapping_resolution(resource_id, selector, mapping)
            raise SelectorUnknownError(selector)

        mappings = await self.repository.prefix_mappings(
            resource_id=resource_id,
            legacy_git_prefix=selector,
        )
        if not mappings:
            raise SelectorUnknownError(selector)
        if len(mappings) != 1:
            raise SelectorAmbiguousError(selector)
        return self._mapping_resolution(resource_id, selector, mappings[0])

    async def _resource_namespace(self, resource_id: uuid.UUID) -> uuid.UUID:
        async with self.pool.acquire() as conn:
            namespace_id = await conn.fetchval(
                "SELECT namespace_id FROM native_resources WHERE resource_id = $1",
                resource_id,
            )
        if namespace_id is None:
            raise SelectorUnknownError(str(resource_id))
        return namespace_id

    @staticmethod
    def _mapping_resolution(resource_id, selector, mapping) -> SelectorResolution:
        if mapping.resolution == "native":
            return SelectorResolution(
                resource_id=resource_id,
                selector=selector,
                kind="native",
                native_revision_id=mapping.native_revision_id,
                fixed_git_oid=mapping.fixed_git_oid,
                legacy_git_oid=mapping.legacy_git_oid,
                path_at_revision=mapping.path_at_revision,
                run_id=mapping.run_id,
            )
        return SelectorResolution(
            resource_id=resource_id,
            selector=selector,
            kind="bridge",
            native_revision_id=None,
            fixed_git_oid=mapping.fixed_git_oid,
            legacy_git_oid=mapping.legacy_git_oid,
            path_at_revision=mapping.path_at_revision,
            run_id=mapping.run_id,
        )
