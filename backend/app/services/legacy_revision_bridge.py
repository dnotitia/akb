"""Fixed-ref legacy inventory and Resource-scoped selector bridge."""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
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
    changed_paths: tuple[dict[str, str | None], ...]


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
                        "changed_paths": list(document.activity.changed_paths),
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

    async def capture_inventory(
        self,
        *,
        namespace_id: uuid.UUID,
        fixed_ref: str,
        coverage_version: str,
    ) -> LegacyInventory:
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
                    try:
                        snapshot = await asyncio.to_thread(
                            self.git.manual_fixed_ref_history,
                            vault["name"],
                            fixed_ref,
                            row["path"],
                            current_commit=current_commit,
                            since_epoch=int(created_at.timestamp()),
                        )
                    except FixedRefHistoryError as exc:
                        raise InventoryEligibilityError(
                            f"document {row['id']} is not readable at the fixed ref"
                        ) from exc
                    history = snapshot["history"]
                    if not history or history[0]["legacy_git_oid"] != current_commit:
                        raise InventoryEligibilityError(
                            f"document {row['id']} current_commit is not the fixed-ref file head"
                        )
                    raw_activity = snapshot.get("activity")
                    if not isinstance(raw_activity, dict):
                        raise InventoryEligibilityError(
                            f"document {row['id']} has no fixed-ref activity semantics"
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
                        changed_paths=tuple(changed_paths),
                    )
                    body = snapshot["body"]
                    if b"\x00" in body:
                        raise InventoryEligibilityError(
                            f"document {row['id']} body contains NUL bytes"
                        )
                    try:
                        body.decode("utf-8", errors="strict")
                    except UnicodeDecodeError as exc:
                        raise InventoryEligibilityError(
                            f"document {row['id']} body is not valid UTF-8"
                        ) from exc

                    newest: list[LegacyLineageEntry] = []
                    seen: set[str] = set()
                    for entry in history:
                        oid = entry["legacy_git_oid"]
                        if not isinstance(oid, str) or _OID_RE.fullmatch(oid) is None:
                            raise InventoryEligibilityError(
                                f"document {row['id']} history contains an invalid OID"
                            )
                        if oid in seen:
                            continue
                        committed_at = entry["committed_at"]
                        if oid != current_commit and committed_at < created_at:
                            continue
                        seen.add(oid)
                        newest.append(
                            LegacyLineageEntry(
                                legacy_git_oid=oid,
                                path_at_revision=(
                                    row["path"] if oid == current_commit
                                    else entry["path_at_revision"]
                                ),
                                committed_at=committed_at,
                            )
                        )
                    if current_commit not in seen:
                        raise InventoryEligibilityError(
                            f"document {row['id']} current_commit is absent from fixed-ref history"
                        )
                    lineage = tuple(reversed(newest))
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
                            body_digest=hashlib.sha256(body).hexdigest(),
                            byte_size=len(body),
                            aliases=tuple(aliases),
                            lineage=lineage,
                            activity=activity,
                        )
                    )

        frozen_documents = tuple(documents)
        return LegacyInventory(
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

    @asynccontextmanager
    async def materialize_body(
        self,
        inventory: LegacyInventory,
        document: LegacyInventoryDocument,
    ) -> AsyncIterator[bytes]:
        """Read and verify one frozen body without retaining it in inventory."""

        if document not in inventory.documents:
            raise InventoryEligibilityError(
                "body materialization requires a document from the frozen inventory"
            )
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
        try:
            snapshot = await asyncio.to_thread(
                self.git.manual_fixed_ref_history,
                vault["name"],
                inventory.fixed_git_oid,
                document.current_path,
                current_commit=document.current_commit,
                since_epoch=int(document.created_at.timestamp()),
            )
        except FixedRefHistoryError as exc:
            raise InventoryEligibilityError(
                f"document {document.resource_id} is not readable at the fixed ref"
            ) from exc

        body = snapshot.get("body") if isinstance(snapshot, dict) else None
        if (
            not isinstance(body, bytes)
            or snapshot.get("fixed_ref") != inventory.fixed_git_oid
            or snapshot.get("current_commit") != document.current_commit
            or len(body) != document.byte_size
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
            del snapshot

    async def prepare_run(
        self,
        *,
        namespace_id: uuid.UUID,
        fixed_ref: str,
        coverage_version: str,
    ) -> tuple[MigrationRun, LegacyInventory]:
        inventory = await self.capture_inventory(
            namespace_id=namespace_id,
            fixed_ref=fixed_ref,
            coverage_version=coverage_version,
        )
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

    async def inventory_for_run(self, run: MigrationRun) -> LegacyInventory:
        inventory = await self.capture_inventory(
            namespace_id=run.namespace_id,
            fixed_ref=run.fixed_git_oid,
            coverage_version=run.coverage_version,
        )
        if inventory.inventory_digest != run.inventory_digest:
            raise MigrationInventoryDriftError()
        return inventory

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
