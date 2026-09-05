"""Fixed-ref legacy inventory and Resource-scoped selector bridge."""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
import uuid
from collections.abc import AsyncIterator, Iterable, Mapping, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from types import MappingProxyType
from typing import Any, Literal, cast

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
        raise InventoryEligibilityError(f"{field} must be a full lowercase 40-hex commit OID")


def _iso(value: datetime) -> str:
    return value.astimezone(UTC).isoformat()


def _datetime(value: object, field: str) -> datetime:
    if not isinstance(value, str):
        raise InventoryEligibilityError(f"{field} must be an ISO-8601 timestamp")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise InventoryEligibilityError(f"{field} must be an ISO-8601 timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise InventoryEligibilityError(f"{field} must be timezone-aware")
    return parsed.astimezone(UTC)


def _mapping(value: object, field: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise InventoryEligibilityError(f"{field} must be an object")
    return value


def _sequence(value: object, field: str) -> list[object]:
    if not isinstance(value, list):
        raise InventoryEligibilityError(f"{field} must be an array")
    return value


def _string(value: object, field: str, *, allow_empty: bool = False) -> str:
    if not isinstance(value, str) or (not allow_empty and not value.strip()):
        raise InventoryEligibilityError(f"{field} must be a non-empty string")
    return value


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
        isinstance(entry, Mapping) and entry.get("action") == "create" for entry in entries
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
                raise LogicalLineageProjectionError("history contains a duplicate oldest anchor")
            continue
        if create_found:
            continue
        committed_at = entry.get("committed_at")
        if not isinstance(committed_at, datetime) or committed_at.tzinfo is None or committed_at.utcoffset() is None:
            raise LogicalLineageProjectionError("history contains an invalid commit timestamp")
        path = current_path if oid == current_commit else entry.get("path_at_revision")
        if not isinstance(path, str) or not path.strip():
            raise LogicalLineageProjectionError("history contains an invalid path")
        if oid in seen:
            if oid == oldest_anchor_oid:
                raise LogicalLineageProjectionError("history contains a duplicate oldest anchor")
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
        raise LogicalLineageProjectionError("completed oldest anchor is absent from fixed-ref history")
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
    def canonical_inventory_payload(
        cls,
        *,
        namespace_id: uuid.UUID,
        fixed_git_oid: str,
        coverage_version: str,
        documents: tuple[LegacyInventoryDocument, ...],
    ) -> dict[str, Any]:
        return {
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
                        "changed_paths": [dict(change) for change in document.activity.changed_paths],
                    },
                }
                for document in documents
            ],
        }

    @classmethod
    def canonical_inventory_digest(
        cls,
        *,
        namespace_id: uuid.UUID,
        fixed_git_oid: str,
        coverage_version: str,
        documents: tuple[LegacyInventoryDocument, ...],
    ) -> str:
        canonical = json.dumps(
            cls.canonical_inventory_payload(
                namespace_id=namespace_id,
                fixed_git_oid=fixed_git_oid,
                coverage_version=coverage_version,
                documents=documents,
            ),
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        return hashlib.sha256(canonical).hexdigest()

    @classmethod
    def inventory_from_payload(cls, payload: Mapping[str, object]) -> LegacyInventory:
        """Rehydrate one immutable run inventory without rescanning Git history."""

        if payload.get("schema") != cls.inventory_schema:
            raise InventoryEligibilityError("persisted inventory schema is unsupported")
        try:
            namespace_id = uuid.UUID(_string(payload.get("namespace_id"), "namespace_id"))
        except ValueError as exc:
            raise InventoryEligibilityError("namespace_id must be a UUID") from exc
        fixed_git_oid = _string(payload.get("fixed_git_oid"), "fixed_git_oid")
        _require_oid(fixed_git_oid, "fixed_git_oid")
        coverage_version = _string(payload.get("coverage_version"), "coverage_version")

        documents: list[LegacyInventoryDocument] = []
        for index, raw_document in enumerate(_sequence(payload.get("documents"), "documents")):
            prefix = f"documents[{index}]"
            document = _mapping(raw_document, prefix)
            try:
                resource_id = uuid.UUID(_string(document.get("resource_id"), f"{prefix}.resource_id"))
            except ValueError as exc:
                raise InventoryEligibilityError(f"{prefix}.resource_id must be a UUID") from exc
            current_path = _string(document.get("current_path"), f"{prefix}.current_path")
            current_commit = _string(document.get("current_commit"), f"{prefix}.current_commit")
            _require_oid(current_commit, f"{prefix}.current_commit")
            created_at = _datetime(document.get("created_at"), f"{prefix}.created_at")
            body_digest = _string(document.get("body_digest"), f"{prefix}.body_digest")
            if re.fullmatch(r"[0-9a-f]{64}", body_digest) is None:
                raise InventoryEligibilityError(f"{prefix}.body_digest must be a lowercase SHA-256 digest")
            byte_size = document.get("byte_size")
            if not isinstance(byte_size, int) or isinstance(byte_size, bool) or byte_size < 0:
                raise InventoryEligibilityError(f"{prefix}.byte_size must be a non-negative integer")

            aliases: list[LegacyInventoryAlias] = []
            for alias_index, raw_alias in enumerate(_sequence(document.get("aliases"), f"{prefix}.aliases")):
                alias = _mapping(raw_alias, f"{prefix}.aliases[{alias_index}]")
                aliases.append(
                    LegacyInventoryAlias(
                        old_ref=_string(
                            alias.get("old_ref"),
                            f"{prefix}.aliases[{alias_index}].old_ref",
                        ),
                        created_at=_datetime(
                            alias.get("created_at"),
                            f"{prefix}.aliases[{alias_index}].created_at",
                        ),
                    )
                )

            lineage: list[LegacyLineageEntry] = []
            for lineage_index, raw_entry in enumerate(_sequence(document.get("lineage"), f"{prefix}.lineage")):
                entry = _mapping(raw_entry, f"{prefix}.lineage[{lineage_index}]")
                legacy_git_oid = _string(
                    entry.get("legacy_git_oid"),
                    f"{prefix}.lineage[{lineage_index}].legacy_git_oid",
                )
                _require_oid(
                    legacy_git_oid,
                    f"{prefix}.lineage[{lineage_index}].legacy_git_oid",
                )
                lineage.append(
                    LegacyLineageEntry(
                        legacy_git_oid=legacy_git_oid,
                        path_at_revision=_string(
                            entry.get("path_at_revision"),
                            f"{prefix}.lineage[{lineage_index}].path_at_revision",
                        ),
                        committed_at=_datetime(
                            entry.get("committed_at"),
                            f"{prefix}.lineage[{lineage_index}].committed_at",
                        ),
                    )
                )
            if not lineage or lineage[-1].legacy_git_oid != current_commit:
                raise InventoryEligibilityError(f"{prefix}.lineage does not end at current_commit")

            raw_activity = _mapping(document.get("activity"), f"{prefix}.activity")
            action = raw_activity.get("action")
            if action not in {"create", "update", "move", "delete"}:
                raise InventoryEligibilityError(f"{prefix}.activity.action is unsupported")
            activity_oid = _string(
                raw_activity.get("legacy_git_oid"),
                f"{prefix}.activity.legacy_git_oid",
            )
            _require_oid(activity_oid, f"{prefix}.activity.legacy_git_oid")
            path_from = raw_activity.get("path_from")
            path_to = raw_activity.get("path_to")
            if path_from is not None and not isinstance(path_from, str):
                raise InventoryEligibilityError(f"{prefix}.activity.path_from must be a string or null")
            if path_to is not None and not isinstance(path_to, str):
                raise InventoryEligibilityError(f"{prefix}.activity.path_to must be a string or null")
            changed_paths: list[Mapping[str, str | None]] = []
            for change_index, raw_change in enumerate(
                _sequence(
                    raw_activity.get("changed_paths"),
                    f"{prefix}.activity.changed_paths",
                )
            ):
                change = _mapping(
                    raw_change,
                    f"{prefix}.activity.changed_paths[{change_index}]",
                )
                if any(
                    not isinstance(key, str) or (value is not None and not isinstance(value, str))
                    for key, value in change.items()
                ):
                    raise InventoryEligibilityError(f"{prefix}.activity.changed_paths[{change_index}] is invalid")
                changed_paths.append(
                    MappingProxyType(
                        {
                            cast(str, key): cast(str | None, value)
                            for key, value in change.items()
                        }
                    )
                )

            documents.append(
                LegacyInventoryDocument(
                    resource_id=resource_id,
                    current_path=current_path,
                    current_commit=current_commit,
                    created_at=created_at,
                    body_digest=body_digest,
                    byte_size=byte_size,
                    aliases=tuple(aliases),
                    lineage=tuple(lineage),
                    activity=LegacyActivitySemantics(
                        legacy_git_oid=activity_oid,
                        committed_at=_datetime(
                            raw_activity.get("committed_at"),
                            f"{prefix}.activity.committed_at",
                        ),
                        actor=_string(raw_activity.get("actor"), f"{prefix}.activity.actor"),
                        subject=_string(raw_activity.get("subject"), f"{prefix}.activity.subject"),
                        summary=_string(
                            raw_activity.get("summary"),
                            f"{prefix}.activity.summary",
                            allow_empty=True,
                        ),
                        action=cast(Literal["create", "update", "move", "delete"], action),
                        path_from=path_from,
                        path_to=path_to,
                        changed_paths=tuple(changed_paths),
                    ),
                )
            )

        frozen_documents = tuple(documents)
        inventory_digest = cls.canonical_inventory_digest(
            namespace_id=namespace_id,
            fixed_git_oid=fixed_git_oid,
            coverage_version=coverage_version,
            documents=frozen_documents,
        )
        canonical_payload = cls.canonical_inventory_payload(
            namespace_id=namespace_id,
            fixed_git_oid=fixed_git_oid,
            coverage_version=coverage_version,
            documents=frozen_documents,
        )
        if dict(payload) != canonical_payload:
            raise InventoryEligibilityError("persisted inventory is not canonical")
        return LegacyInventory(
            namespace_id=namespace_id,
            fixed_git_oid=fixed_git_oid,
            coverage_version=coverage_version,
            documents=frozen_documents,
            inventory_digest=inventory_digest,
        )

    @staticmethod
    def _document_from_fixed_snapshot(
        *,
        row: Mapping[str, object],
        raw_aliases: Sequence[Mapping[str, object]],
        raw_snapshot: object,
        oldest_anchor_oid: str | None,
    ) -> LegacyInventoryDocument:
        resource_id = row["id"]
        current_commit = row.get("current_commit")
        current_path = row.get("path")
        created_at = row.get("created_at")
        if not isinstance(resource_id, uuid.UUID):
            raise InventoryEligibilityError("document inventory contains an invalid resource id")
        if not isinstance(current_commit, str) or _OID_RE.fullmatch(current_commit) is None:
            raise InventoryEligibilityError(f"document {resource_id} has no full current_commit")
        if not isinstance(current_path, str) or not current_path:
            raise InventoryEligibilityError(f"document {resource_id} has no usable path")
        if not isinstance(created_at, datetime):
            raise InventoryEligibilityError(f"document {resource_id} has no usable created_at")
        if not isinstance(raw_snapshot, Mapping):
            raise InventoryEligibilityError(f"document {resource_id} returned invalid fixed-ref metadata")
        history = raw_snapshot.get("history")
        raw_activity = raw_snapshot.get("activity")
        body = raw_snapshot.get("body")
        body_digest = raw_snapshot.get("body_digest")
        byte_size = raw_snapshot.get("byte_size")
        if not isinstance(history, list) or not isinstance(raw_activity, Mapping):
            raise InventoryEligibilityError(f"document {resource_id} returned invalid fixed-ref metadata")
        if isinstance(body, bytes):
            try:
                body.decode("utf-8", errors="strict")
            except UnicodeDecodeError as exc:
                raise InventoryEligibilityError(f"document {resource_id} body is not valid UTF-8") from exc
            body_digest = hashlib.sha256(body).hexdigest()
            byte_size = len(body)
        elif (
            not isinstance(body_digest, str)
            or re.fullmatch(r"[0-9a-f]{64}", body_digest) is None
            or not isinstance(byte_size, int)
            or isinstance(byte_size, bool)
            or byte_size < 0
        ):
            raise InventoryEligibilityError(f"document {resource_id} is not readable at the fixed ref")
        if not history or history[0].get("legacy_git_oid") != current_commit:
            raise InventoryEligibilityError(
                f"document {resource_id} current_commit is not the fixed-ref file head"
            )

        aliases: list[LegacyInventoryAlias] = []
        for alias in raw_aliases:
            old_ref = alias.get("old_ref")
            alias_created_at = alias.get("created_at")
            if not isinstance(old_ref, str) or not old_ref.strip():
                raise InventoryEligibilityError(f"document {resource_id} has an invalid resource alias")
            if not isinstance(alias_created_at, datetime):
                raise InventoryEligibilityError(
                    f"document {resource_id} has an alias without a usable timestamp"
                )
            aliases.append(LegacyInventoryAlias(old_ref=old_ref, created_at=alias_created_at))

        activity_action = raw_activity.get("action")
        if activity_action not in {"create", "update", "move", "delete"}:
            raise InventoryEligibilityError(
                f"document {resource_id} has an unsupported current activity action"
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
            or activity_time != history[0].get("committed_at")
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
                f"document {resource_id} has incomplete current activity semantics"
            )
        if activity_action == "move" and not isinstance(path_from, str):
            raise InventoryEligibilityError(f"document {resource_id} move activity has no source path")
        if activity_action != "move" and path_from is not None:
            raise InventoryEligibilityError(f"document {resource_id} non-move activity has a source path")
        activity = LegacyActivitySemantics(
            legacy_git_oid=activity_oid,
            committed_at=activity_time,
            actor=activity_actor,
            subject=activity_subject,
            summary=activity_summary,
            action=activity_action,
            path_from=path_from,
            path_to=path_to,
            changed_paths=tuple(MappingProxyType(dict(change)) for change in changed_paths),
        )
        try:
            lineage = project_logical_lineage(
                history,
                current_commit=current_commit,
                current_path=current_path,
                created_at=created_at,
                oldest_anchor_oid=oldest_anchor_oid,
            )
        except LogicalLineageProjectionError as exc:
            raise InventoryEligibilityError(f"document {resource_id} history is invalid") from exc
        if current_commit not in {entry.legacy_git_oid for entry in lineage}:
            raise InventoryEligibilityError(
                f"document {resource_id} current_commit is absent from fixed-ref history"
            )
        if not lineage or lineage[-1].legacy_git_oid != current_commit:
            raise InventoryEligibilityError(f"document {resource_id} lineage does not end at current_commit")
        return LegacyInventoryDocument(
            resource_id=resource_id,
            current_path=current_path,
            current_commit=current_commit,
            created_at=created_at,
            body_digest=body_digest,
            byte_size=byte_size,
            aliases=tuple(aliases),
            lineage=lineage,
            activity=activity,
        )

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

                completed_anchor_rows = await self.repository.list_completed_lineage_anchors(
                    namespace_id=namespace_id,
                    conn=conn,
                )
                completed_anchors: dict[uuid.UUID, str] = {}
                for anchor in completed_anchor_rows:
                    if anchor.resource_id in completed_anchors:
                        raise InventoryEligibilityError("completed lineage has duplicate ordinal-zero anchors")
                    completed_anchors[anchor.resource_id] = anchor.legacy_git_oid

                aliases_by_resource: dict[uuid.UUID, list[dict]] = {}
                for alias in await self.repository.list_namespace_document_aliases(
                    conn,
                    namespace_id,
                ):
                    aliases_by_resource.setdefault(alias["resource_id"], []).append(alias)

        requests = []
        for row in rows:
            current_commit = row.get("current_commit")
            created_at = row.get("created_at")
            if not isinstance(current_commit, str) or _OID_RE.fullmatch(current_commit) is None:
                raise InventoryEligibilityError(f"document {row['id']} has no full current_commit")
            if not isinstance(created_at, datetime):
                raise InventoryEligibilityError(f"document {row['id']} has no usable created_at")
            requests.append(
                {
                    "file_path": row["path"],
                    "current_commit": current_commit,
                    "since_epoch": int(created_at.timestamp()),
                }
            )
        try:
            snapshots = await asyncio.to_thread(
                self.git.manual_fixed_ref_history_batch,
                vault["name"],
                fixed_ref,
                requests,
                include_bodies=False,
            )
        except FixedRefHistoryError as exc:
            raise InventoryEligibilityError("fixed-ref inventory history could not be read") from exc
        if len(snapshots) != len(rows):
            raise InventoryEligibilityError("fixed-ref inventory history is incomplete")
        documents = [
            self._document_from_fixed_snapshot(
                row=row,
                raw_aliases=aliases_by_resource.get(row["id"], []),
                raw_snapshot=snapshot,
                oldest_anchor_oid=completed_anchors.get(row["id"]),
            )
            for row, snapshot in zip(rows, snapshots, strict=True)
        ]

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
                raise InventoryEligibilityError("frozen inventory contains duplicate document resources")
            documents_by_id[document.resource_id] = document
        return LegacyInventoryScope(
            inventory=inventory,
            vault_name=vault_name,
            fixed_git_oid=inventory.fixed_git_oid,
            documents_by_id=MappingProxyType(documents_by_id),
        )

    async def _validated_current_catalog(
        self,
        inventory: LegacyInventory,
        *,
        conn: asyncpg.Connection | None = None,
    ) -> str:
        """Check mutable catalog facts without traversing immutable Git history."""

        async def validate(acquired: asyncpg.Connection) -> str:
            vault = await self.repository.get_manual_vault(
                inventory.namespace_id,
                conn=acquired,
            )
            if vault is None:
                raise NotFoundError("Vault", str(inventory.namespace_id))
            if vault["has_external_git"]:
                raise ManualVaultRequiredError()

            rows = [
                row
                for row in await self.repository.list_documents(
                    acquired,
                    inventory.namespace_id,
                )
                if row.get("source") == "manual"
            ]
            if len(rows) != len(inventory.documents):
                raise MigrationInventoryDriftError("manual document membership drifted from the fixed inventory")
            documents_by_id = {document.resource_id: document for document in inventory.documents}
            aliases_by_resource: dict[uuid.UUID, list[dict]] = {}
            for alias in await self.repository.list_namespace_document_aliases(
                acquired,
                inventory.namespace_id,
            ):
                aliases_by_resource.setdefault(alias["resource_id"], []).append(alias)

            for row in rows:
                document = documents_by_id.get(row["id"])
                created_at = row.get("created_at")
                if (
                    document is None
                    or row.get("path") != document.current_path
                    or row.get("current_commit") != document.current_commit
                    or not isinstance(created_at, datetime)
                    or created_at.astimezone(UTC) != document.created_at.astimezone(UTC)
                ):
                    raise MigrationInventoryDriftError("manual document catalog drifted from the fixed inventory")
                aliases = aliases_by_resource.get(document.resource_id, [])
                observed_aliases_list: list[tuple[object, datetime | None]] = []
                for alias in aliases:
                    alias_created_at = alias.get("created_at")
                    observed_aliases_list.append(
                        (
                            alias.get("old_ref"),
                            alias_created_at.astimezone(UTC)
                            if isinstance(alias_created_at, datetime)
                            else None,
                        )
                    )
                observed_aliases = tuple(observed_aliases_list)
                expected_aliases = tuple(
                    (alias.old_ref, alias.created_at.astimezone(UTC)) for alias in document.aliases
                )
                if observed_aliases != expected_aliases:
                    raise MigrationInventoryDriftError("manual document aliases drifted from the fixed inventory")

            completed_anchors = {
                anchor.resource_id: anchor.legacy_git_oid
                for anchor in await self.repository.list_completed_lineage_anchors(
                    namespace_id=inventory.namespace_id,
                    conn=acquired,
                )
            }
            for resource_id, anchor_oid in completed_anchors.items():
                document = documents_by_id.get(resource_id)
                if document is None or not document.lineage or document.lineage[0].legacy_git_oid != anchor_oid:
                    raise MigrationInventoryDriftError("completed lineage anchor drifted from the fixed inventory")
            return vault["name"]

        if conn is not None:
            return await validate(conn)
        async with self.pool.acquire() as acquired:
            async with acquired.transaction(isolation="repeatable_read", readonly=True):
                return await validate(acquired)

    async def validated_inventory_scope(
        self,
        inventory: LegacyInventory,
        *,
        conn: asyncpg.Connection | None = None,
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
        vault_name = await self._validated_current_catalog(inventory, conn=conn)
        return self._scope_from_validated_inventory(inventory, vault_name=vault_name)

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
            raise InventoryEligibilityError("body materialization requires a document from the frozen inventory")
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
            raise InventoryEligibilityError(f"document {document.resource_id} is not readable at the fixed ref")
        body = text.encode("utf-8")
        del text
        if len(body) != document.byte_size or hashlib.sha256(body).hexdigest() != document.body_digest:
            raise InventoryEligibilityError(f"document {document.resource_id} body differs from the frozen inventory")
        try:
            body.decode("utf-8", errors="strict")
        except UnicodeDecodeError as exc:
            raise InventoryEligibilityError(f"document {document.resource_id} body is not valid UTF-8") from exc
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
                await self._validated_current_catalog(inventory, conn=conn)
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
                await self.repository.store_inventory_snapshot(
                    run,
                    self.canonical_inventory_payload(
                        namespace_id=inventory.namespace_id,
                        fixed_git_oid=inventory.fixed_git_oid,
                        coverage_version=inventory.coverage_version,
                        documents=inventory.documents,
                    ),
                    conn=conn,
                )
        return run, inventory

    async def inventory_scope_for_run(
        self,
        run: MigrationRun,
        *,
        conn: asyncpg.Connection | None = None,
    ) -> LegacyInventoryScope:
        current_run = await self.repository.get_run(run.run_id, conn=conn)
        if current_run is None:
            raise MigrationInventoryDriftError("migration run disappeared")
        if (
            current_run.namespace_id != run.namespace_id
            or current_run.fixed_git_oid != run.fixed_git_oid
            or current_run.coverage_version != run.coverage_version
            or current_run.inventory_digest != run.inventory_digest
        ):
            raise MigrationInventoryDriftError("migration run binding drifted")
        run = current_run
        snapshot = await self.repository.get_inventory_snapshot(run.run_id, conn=conn)
        if snapshot is None:
            raise MigrationInventoryDriftError("fixed-ref migration inventory snapshot is missing")
        if (
            snapshot["namespace_id"] != run.namespace_id
            or snapshot["fixed_git_oid"] != run.fixed_git_oid
            or snapshot["coverage_version"] != run.coverage_version
            or snapshot["inventory_digest"] != run.inventory_digest
        ):
            raise MigrationInventoryDriftError("fixed-ref migration inventory snapshot binding drifted")
        inventory = self.inventory_from_payload(snapshot["payload"])
        if (
            inventory.namespace_id != run.namespace_id
            or inventory.fixed_git_oid != run.fixed_git_oid
            or inventory.coverage_version != run.coverage_version
            or inventory.inventory_digest != run.inventory_digest
        ):
            raise MigrationInventoryDriftError()
        scope = await self.validated_inventory_scope(inventory, conn=conn)
        if run.status == "complete":
            observed = await self.repository.list_namespace_completed_mappings(
                namespace_id=inventory.namespace_id,
                conn=conn,
            )
            observed_closure = sorted(
                (str(row.resource_id), row.legacy_git_oid, row.path_at_revision)
                for row in observed
            )
            expected_closure = sorted(
                (str(document.resource_id), entry.legacy_git_oid, entry.path_at_revision)
                for document in inventory.documents
                for entry in document.lineage
            )
            if observed_closure != expected_closure:
                raise MigrationInventoryDriftError(
                    "completed Legacy selector closure drifted from the fixed inventory"
                )
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
