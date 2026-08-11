"""Product-owned, read-only readers for the P2 shadow comparison.

The readers deliberately expose the small C10 envelopes rather than calling a
public route or registering a second request path.  The legacy reader is
bounded by the immutable fixed-ref inventory; the native reader is bounded by
the Resource identity and the native genesis Revision published by C9.
"""

from __future__ import annotations

import asyncio
import difflib
import hashlib
import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Protocol
from uuid import UUID

import asyncpg

from app.exceptions import AKBError, ConflictError, NotFoundError
from app.repositories.native_revision_repo import NativeRevisionRepository
from app.services.document_service import _parse_markdown
from app.services.git_service import FixedRefHistoryError, GitService
from app.services.legacy_revision_bridge import (
    LegacyInventoryDocument,
)
from app.services.native_revision_service import NativeRevisionService
from app.services.uri_service import doc_uri


_OID_RE = re.compile(r"^[0-9a-f]{40}$")
_HUNK_RE = re.compile(
    r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@(?: .*)?$"
)
_NATIVE_ACTIVITY_MIGRATION_ACTOR = "akb-native-revision-migration"
_NATIVE_ACTIVITY_AUDIT_PROFILE = "akb-native-revision-p2-activity-audit/v1"


class ShadowReaderScopeError(ConflictError):
    """A product shadow read was not bound to its frozen C9 scope."""

    def __init__(self, message: str = "native revision shadow reader scope is invalid"):
        super().__init__(message)
        self.code = "native_revision_shadow_reader_scope_invalid"


@dataclass(frozen=True, slots=True)
class NativeActivityEvidence:
    """Audited native activity envelope plus private receipt-binding facts."""

    envelope: dict[str, Any]
    binding_fact: dict[str, Any]


class NativeRevisionReadService(Protocol):
    async def get_resource_revision(
        self,
        *,
        namespace_id,
        surface: str,
        resource_id,
        revision_id: str,
    ): ...


class NativeRevisionReadRepository(Protocol):
    async def get_revision(
        self,
        *,
        resource_id,
        revision_id: str,
    ) -> dict[str, Any] | None: ...

    async def list_history(
        self,
        *,
        resource_id,
        limit: int = 100,
    ) -> list[dict[str, Any]]: ...

    async def get_activity_for_revision(
        self,
        *,
        namespace_id,
        surface: str,
        resource_id,
        revision_id: str,
    ) -> dict[str, Any] | None: ...


class SelectorBridge(Protocol):
    async def resolve_selector(
        self,
        *,
        resource_id,
        selector: str,
    ): ...


@dataclass(frozen=True, slots=True)
class _LegacySnapshotKey:
    resource_id: UUID
    current_commit: str
    fixed_ref: str
    path: str
    body_digest: str
    byte_size: int


@dataclass(frozen=True, slots=True)
class _LegacyMaterialization:
    fixed_ref: str
    current_commit: str
    body: bytes
    history: tuple[dict[str, Any], ...]
    activity: dict[str, Any]


@dataclass(frozen=True, slots=True)
class _NativeSnapshotKey:
    resource_id: UUID
    selector: str
    fixed_ref: str
    path: str
    body_digest: str
    byte_size: int


@dataclass(frozen=True, slots=True)
class _NativeMaterialization:
    resource_id: UUID
    revision_id: str
    surface: str
    path: str
    text: str
    digest: str
    byte_size: int
    action: str
    parent_revision_id: str | None
    occurred_at: Any


def _require_oid(value: str, field: str) -> None:
    if not isinstance(value, str) or _OID_RE.fullmatch(value) is None:
        raise ShadowReaderScopeError(f"shadow reader {field} is not a full revision token")


def _value(value: Any, field: str) -> Any:
    if isinstance(value, Mapping):
        return value.get(field)
    return getattr(value, field, None)


def _parsed_body(text: str) -> tuple[dict[str, Any], str]:
    try:
        metadata, body = _parse_markdown(text)
    except Exception as exc:  # noqa: BLE001 - a shadow read must fail closed
        raise ShadowReaderScopeError("shadow reader payload is not valid markdown") from exc
    return metadata, body


def _validate_lineage(document: LegacyInventoryDocument) -> None:
    if not document.lineage or document.lineage[-1].legacy_git_oid != document.current_commit:
        raise ShadowReaderScopeError("shadow reader inventory lineage has no current-head boundary")
    for entry in document.lineage:
        _require_oid(entry.legacy_git_oid, "lineage selector")
        if not isinstance(entry.path_at_revision, str) or not entry.path_at_revision.strip():
            raise ShadowReaderScopeError("shadow reader inventory lineage has an invalid path")


def _snapshot_diff(text: str) -> str:
    """Render the shared final-state form after a verified transition."""
    normalized = text.replace("\r\n", "\n")
    lines = normalized.splitlines()
    if not lines:
        return "@@ snapshot @@\n"
    return "@@ snapshot @@\n" + "\n".join(f" {line}" for line in lines)


def _canonical_transition(parent_text: str, current_text: str) -> str:
    """Derive the parity envelope through both immutable transition bodies."""
    delta = tuple(
        difflib.ndiff(
            parent_text.splitlines(keepends=True),
            current_text.splitlines(keepends=True),
        )
    )
    rebuilt = "".join(difflib.restore(delta, 2))
    if rebuilt != current_text:
        raise ShadowReaderScopeError("shadow reader transition could not reproduce current body")
    _, body = _parsed_body(rebuilt)
    return _snapshot_diff(body)


def _apply_unified_patch(parent_text: str, patch: str, diff_type: str) -> str:
    """Apply the exact Git patch to an independently read parent body."""
    patch_lines = patch.split("\n")
    if patch_lines and patch_lines[-1] == "":
        patch_lines.pop()
    hunk_indexes = [
        index for index, line in enumerate(patch_lines) if _HUNK_RE.fullmatch(line)
    ]
    if not hunk_indexes:
        prefix = "+" if diff_type == "added" else "-" if diff_type == "deleted" else None
        if prefix is None or any(not line.startswith(prefix) for line in patch_lines):
            raise ShadowReaderScopeError("legacy fixed-ref diff patch is not canonical unified data")
        materialized = "\n".join(line[1:] for line in patch_lines)
        if diff_type == "deleted":
            if materialized != parent_text:
                raise ShadowReaderScopeError("legacy fixed-ref diff patch differs from parent body")
            return ""
        if parent_text:
            raise ShadowReaderScopeError("legacy fixed-ref added patch has a non-empty parent")
        return materialized

    if hunk_indexes[0] != 0:
        allowed_headers = {"diff --git", "index ", "--- ", "+++ "}
        if any(
            not any(line.startswith(prefix) for prefix in allowed_headers)
            for line in patch_lines[: hunk_indexes[0]]
        ):
            raise ShadowReaderScopeError("legacy fixed-ref diff patch has invalid headers")

    source = parent_text.split("\n") if parent_text else []
    source_has_newline = parent_text.endswith("\n")
    if source_has_newline:
        source.pop()
    output: list[tuple[str, bool]] = []
    source_index = 0
    patch_index = hunk_indexes[0]
    old_terminal_closed = False
    new_terminal_closed = False
    while patch_index < len(patch_lines):
        header = _HUNK_RE.fullmatch(patch_lines[patch_index])
        if header is None:
            raise ShadowReaderScopeError("legacy fixed-ref diff patch has trailing data")
        old_start = int(header.group(1))
        old_count = int(header.group(2)) if header.group(2) is not None else 1
        new_count = int(header.group(4)) if header.group(4) is not None else 1
        hunk_source = 0 if old_start == 0 else old_start - 1
        if hunk_source < source_index or hunk_source > len(source):
            raise ShadowReaderScopeError("legacy fixed-ref diff patch has an invalid parent range")
        if old_terminal_closed and hunk_source > source_index:
            raise ShadowReaderScopeError("legacy fixed-ref diff patch has an invalid newline marker")
        if new_terminal_closed and hunk_source > source_index:
            raise ShadowReaderScopeError("legacy fixed-ref diff patch has an invalid newline marker")
        output.extend(
            (
                source[index],
                index < len(source) - 1 or source_has_newline,
            )
            for index in range(source_index, hunk_source)
        )
        source_index = hunk_source
        observed_old = 0
        observed_new = 0
        patch_index += 1
        while patch_index < len(patch_lines) and _HUNK_RE.fullmatch(
            patch_lines[patch_index]
        ) is None:
            line = patch_lines[patch_index]
            patch_index += 1
            if line == r"\ No newline at end of file":
                raise ShadowReaderScopeError("legacy fixed-ref diff patch has an invalid newline marker")
            if not line or line[0] not in {" ", "+", "-"}:
                raise ShadowReaderScopeError("legacy fixed-ref diff patch has invalid hunk data")
            marker, value = line[0], line[1:]
            no_newline = False
            if patch_index < len(patch_lines) and patch_lines[patch_index] == r"\ No newline at end of file":
                no_newline = True
                patch_index += 1
            old_line = marker in {" ", "-"}
            new_line = marker in {" ", "+"}
            if old_line and old_terminal_closed or new_line and new_terminal_closed:
                raise ShadowReaderScopeError("legacy fixed-ref diff patch has an invalid newline marker")
            if marker in {" ", "-"}:
                if source_index >= len(source) or source[source_index] != value:
                    raise ShadowReaderScopeError(
                        "legacy fixed-ref diff patch differs from parent body"
                    )
                source_line_has_newline = source_index < len(source) - 1 or source_has_newline
                if no_newline != (not source_line_has_newline):
                    raise ShadowReaderScopeError(
                        "legacy fixed-ref diff patch has an invalid newline marker"
                    )
                source_line = source[source_index]
                source_index += 1
                observed_old += 1
                if no_newline:
                    old_terminal_closed = True
            else:
                source_line = ""
            if marker in {" ", "+"}:
                if marker == " ":
                    output.append((source_line, not no_newline))
                else:
                    output.append((value, not no_newline))
                observed_new += 1
                if no_newline:
                    new_terminal_closed = True
        if (observed_old, observed_new) != (old_count, new_count):
            raise ShadowReaderScopeError("legacy fixed-ref diff patch has invalid hunk counts")
    if old_terminal_closed and source_index != len(source):
        raise ShadowReaderScopeError("legacy fixed-ref diff patch has an invalid newline marker")
    if new_terminal_closed and source_index != len(source):
        raise ShadowReaderScopeError("legacy fixed-ref diff patch has an invalid newline marker")
    output.extend(
        (
            source[index],
            index < len(source) - 1 or source_has_newline,
        )
        for index in range(source_index, len(source))
    )
    return "".join(value + ("\n" if has_newline else "") for value, has_newline in output)


def _history_entries(
    document: LegacyInventoryDocument,
    *,
    native_revision_id: str | None,
) -> list[dict[str, Any]]:
    entries = []
    for index, lineage in enumerate(reversed(document.lineage)):
        selector = native_revision_id if native_revision_id is not None and index == 0 else lineage.legacy_git_oid
        entries.append(
            {
                "selector": selector,
                "payload_sha256": hashlib.sha256(lineage.legacy_git_oid.encode()).hexdigest(),
                "projection_revision": selector if index == 0 else None,
                "summary": document.activity.summary if index == 0 else "retained history",
            }
        )
    return entries


class LegacyFixedRefShadowReader:
    """Read one frozen legacy Document through the C9 fixed-ref Git seam."""

    def __init__(self, *, git: GitService, vault_name: str):
        if not isinstance(vault_name, str) or not vault_name.strip():
            raise ValueError("vault_name must be non-empty")
        self.git = git
        self.vault_name = vault_name
        self._snapshot_lock = asyncio.Lock()
        self._snapshot_cache: tuple[_LegacySnapshotKey, _LegacyMaterialization] | None = None

    @staticmethod
    def _validate_materialization(
        snapshot: _LegacyMaterialization,
        document: LegacyInventoryDocument,
        *,
        fixed_ref: str,
    ) -> None:
        body = snapshot.body
        if (
            snapshot.fixed_ref != fixed_ref
            or snapshot.current_commit != document.current_commit
            or len(body) != document.byte_size
            or hashlib.sha256(body).hexdigest() != document.body_digest
        ):
            raise ShadowReaderScopeError("legacy fixed-ref materialization differs from C9 inventory")
        try:
            body.decode("utf-8", errors="strict")
        except UnicodeDecodeError as exc:
            raise ShadowReaderScopeError("legacy fixed-ref body is not valid UTF-8") from exc

        expected_history = [
            (entry.legacy_git_oid, entry.path_at_revision, entry.committed_at)
            for entry in reversed(document.lineage)
        ]
        observed_history = [
            (
                entry.get("legacy_git_oid"),
                entry.get("path_at_revision"),
                entry.get("committed_at"),
            )
            for entry in snapshot.history
        ]
        if observed_history != expected_history:
            raise ShadowReaderScopeError(
                "legacy fixed-ref history differs from C9 inventory"
            )

        activity = document.activity
        expected_activity = (
            activity.legacy_git_oid,
            activity.committed_at,
            activity.actor,
            activity.subject,
            activity.summary,
            activity.action,
            activity.path_from,
            activity.path_to,
            tuple(dict(change) for change in activity.changed_paths),
        )
        raw_activity = snapshot.activity
        changed_paths = raw_activity.get("changed_paths")
        if not isinstance(changed_paths, (list, tuple)):
            raise ShadowReaderScopeError("legacy fixed-ref activity is invalid")
        observed_activity = (
            raw_activity.get("legacy_git_oid"),
            raw_activity.get("committed_at"),
            raw_activity.get("actor"),
            raw_activity.get("subject"),
            raw_activity.get("summary"),
            raw_activity.get("action"),
            raw_activity.get("path_from"),
            raw_activity.get("path_to"),
            tuple(dict(change) for change in changed_paths if isinstance(change, Mapping)),
        )
        if observed_activity != expected_activity:
            raise ShadowReaderScopeError(
                "legacy fixed-ref activity differs from C9 inventory"
            )

    async def _fixed_snapshot(
        self,
        document: LegacyInventoryDocument,
        *,
        fixed_ref: str,
    ) -> _LegacyMaterialization:
        _validate_lineage(document)
        _require_oid(fixed_ref, "fixed_ref")
        _require_oid(document.current_commit, "legacy current revision")
        key = _LegacySnapshotKey(
            resource_id=document.resource_id,
            current_commit=document.current_commit,
            fixed_ref=fixed_ref,
            path=document.current_path,
            body_digest=document.body_digest,
            byte_size=document.byte_size,
        )
        async with self._snapshot_lock:
            if self._snapshot_cache is not None and self._snapshot_cache[0] == key:
                cached = self._snapshot_cache[1]
                try:
                    self._validate_materialization(cached, document, fixed_ref=fixed_ref)
                except ShadowReaderScopeError:
                    self._snapshot_cache = None
                    raise
                return cached

            # A miss evicts before I/O: a failed replacement cannot expose the
            # prior Resource's body through a later cache hit.
            self._snapshot_cache = None
            try:
                raw_snapshot = await asyncio.to_thread(
                    self.git.manual_fixed_ref_history,
                    self.vault_name,
                    fixed_ref,
                    document.current_path,
                    current_commit=document.current_commit,
                    since_epoch=int(document.created_at.timestamp()),
                )
            except (FixedRefHistoryError, OSError, ValueError) as exc:
                raise ShadowReaderScopeError("legacy fixed-ref materialization failed") from exc
            if not isinstance(raw_snapshot, Mapping):
                raise ShadowReaderScopeError("legacy fixed-ref materialization is invalid")
            body = raw_snapshot.get("body")
            history = raw_snapshot.get("history")
            activity = raw_snapshot.get("activity")
            materialized_fixed_ref = raw_snapshot.get("fixed_ref")
            materialized_current_commit = raw_snapshot.get("current_commit")
            if not isinstance(body, bytes):
                raise ShadowReaderScopeError("legacy fixed-ref materialization returned no body")
            if not isinstance(history, list) or not all(
                isinstance(entry, Mapping) for entry in history
            ):
                raise ShadowReaderScopeError("legacy fixed-ref history is invalid")
            if not isinstance(activity, Mapping):
                raise ShadowReaderScopeError("legacy fixed-ref activity is invalid")
            if not isinstance(materialized_fixed_ref, str) or not isinstance(
                materialized_current_commit,
                str,
            ):
                raise ShadowReaderScopeError("legacy fixed-ref materialization has invalid scope")
            snapshot = _LegacyMaterialization(
                fixed_ref=materialized_fixed_ref,
                current_commit=materialized_current_commit,
                body=bytes(body),
                history=tuple(dict(entry) for entry in history),
                activity=dict(activity),
            )
            self._validate_materialization(snapshot, document, fixed_ref=fixed_ref)
            self._snapshot_cache = (key, snapshot)
            return snapshot

    async def get(
        self,
        document: LegacyInventoryDocument,
        *,
        selector: str,
        fixed_ref: str,
    ) -> dict[str, Any]:
        if selector != document.current_commit:
            raise ShadowReaderScopeError("legacy get selector is outside the frozen current head")
        snapshot = await self._fixed_snapshot(document, fixed_ref=fixed_ref)
        metadata, body = _parsed_body(snapshot.body.decode("utf-8"))
        title = metadata.get("title")
        if not isinstance(title, str) or not title:
            title = document.current_path.rsplit("/", 1)[-1]
        return {
            "kind": "document",
            "uri": doc_uri(self.vault_name, document.current_path),
            "vault": self.vault_name,
            "path": document.current_path,
            "title": title,
            "current_commit": selector,
            "content": body,
            "projection": {"revision": selector, "authoritative": False},
            "actor": {
                "id": document.activity.actor,
                "display": document.activity.actor,
            },
        }

    async def history(
        self,
        document: LegacyInventoryDocument,
        *,
        selector: str,
        fixed_ref: str,
    ) -> dict[str, Any]:
        if selector != document.current_commit:
            raise ShadowReaderScopeError("legacy history selector is outside the frozen current head")
        await self._fixed_snapshot(document, fixed_ref=fixed_ref)
        return {
            "history_source": "legacy-git-log",
            "lineage_boundary": "legacy-document-start",
            "entries": _history_entries(document, native_revision_id=None),
        }

    async def diff(
        self,
        document: LegacyInventoryDocument,
        *,
        selector: str,
        fixed_ref: str,
    ) -> dict[str, Any]:
        if selector != document.current_commit:
            raise ShadowReaderScopeError("legacy diff selector is outside the frozen current head")
        snapshot = await self._fixed_snapshot(document, fixed_ref=fixed_ref)
        try:
            raw_diff = await asyncio.to_thread(
                self.git.file_diff,
                self.vault_name,
                document.current_path,
                selector,
            )
        except (OSError, ValueError) as exc:
            raise ShadowReaderScopeError("legacy fixed-ref diff failed") from exc
        if not isinstance(raw_diff, Mapping):
            raise ShadowReaderScopeError("legacy fixed-ref diff is invalid")
        diff_type = raw_diff.get("type")
        allowed_types = {
            "create": {"added"},
            "update": {"modified"},
            "move": {"added", "modified"},
            "delete": {"deleted"},
        }[document.activity.action]
        if (
            raw_diff.get("file") != document.current_path
            or raw_diff.get("commit") != selector
            or diff_type not in allowed_types
            or not isinstance(raw_diff.get("diff"), str)
            or not raw_diff.get("diff")
        ):
            raise ShadowReaderScopeError(
                "legacy fixed-ref diff differs from C9 inventory"
            )
        previous = document.lineage[-2] if len(document.lineage) > 1 else None
        try:
            current_text, parent_text = await asyncio.gather(
                asyncio.to_thread(
                    self.git.read_file,
                    self.vault_name,
                    document.current_path,
                    commit=selector,
                ),
                asyncio.to_thread(
                    self.git.read_file,
                    self.vault_name,
                    previous.path_at_revision,
                    commit=previous.legacy_git_oid,
                )
                if previous is not None
                else asyncio.sleep(0, result=""),
            )
        except (OSError, ValueError) as exc:
            raise ShadowReaderScopeError("legacy fixed-ref diff body reads failed") from exc
        if not isinstance(current_text, str) or not isinstance(parent_text, str):
            raise ShadowReaderScopeError("legacy fixed-ref diff body facts are missing")
        snapshot_text = snapshot.body.decode("utf-8")
        if current_text != snapshot_text:
            raise ShadowReaderScopeError("legacy fixed-ref diff current body differs from C9 facts")
        patch_parent = "" if diff_type == "added" else parent_text
        applied = _apply_unified_patch(patch_parent, raw_diff["diff"], diff_type)
        if applied != current_text:
            raise ShadowReaderScopeError("legacy fixed-ref diff patch differs from current body")
        return {
            "file": document.current_path,
            "commit": selector,
            "basis": "git-parent",
            "text": _canonical_transition(parent_text, applied),
            "format": "unified",
        }

    async def activity(
        self,
        document: LegacyInventoryDocument,
        *,
        selector: str,
        fixed_ref: str,
    ) -> dict[str, Any]:
        if selector != document.current_commit:
            raise ShadowReaderScopeError("legacy activity selector is outside the frozen current head")
        await self._fixed_snapshot(document, fixed_ref=fixed_ref)
        activity = document.activity
        return {
            "events": [
                {
                    "hash": selector,
                    "subject": activity.subject,
                    "author": {
                        "id": activity.actor,
                        "display": f"{activity.actor} (Git)",
                    },
                    "action": activity.action,
                    "summary": activity.summary,
                    "projection_revision": selector,
                }
            ]
        }


class NativeRevisionShadowReader:
    """Read one C9 native genesis through the PostgreSQL Revision service."""

    def __init__(
        self,
        pool: asyncpg.Pool | None,
        *,
        namespace_id,
        vault_name: str,
        native_service: NativeRevisionReadService | None = None,
        native_repository: NativeRevisionReadRepository | None = None,
        selector_bridge: SelectorBridge | None = None,
    ):
        if native_service is None and pool is None:
            raise ValueError("pool or native_service is required")
        if not isinstance(vault_name, str) or not vault_name.strip():
            raise ValueError("vault_name must be non-empty")
        if selector_bridge is None:
            raise ValueError("selector_bridge is required for product shadow evidence")
        self.pool = pool
        self.namespace_id = namespace_id
        self.vault_name = vault_name
        self.native_service = native_service or NativeRevisionService(pool)  # type: ignore[arg-type]
        if native_repository is None:
            if pool is not None:
                native_repository = NativeRevisionRepository(pool)
            else:
                candidate = getattr(self.native_service, "repository", None)
                if candidate is None:
                    raise ValueError("pool or native_repository is required")
                native_repository = candidate
        self.native_repository = native_repository
        self.selector_bridge = selector_bridge
        self._snapshot_lock = asyncio.Lock()
        self._snapshot_cache: tuple[_NativeSnapshotKey, _NativeMaterialization] | None = None

    @staticmethod
    def _validate_materialization(
        snapshot: _NativeMaterialization,
        document: LegacyInventoryDocument,
        *,
        selector: str,
    ) -> None:
        if (
            snapshot.resource_id != document.resource_id
            or snapshot.revision_id != selector
            or snapshot.surface != "document"
            or snapshot.path != document.current_path
            or snapshot.digest != document.body_digest
            or snapshot.byte_size != document.byte_size
        ):
            raise ShadowReaderScopeError("native revision facts differ from C9 inventory")
        if snapshot.parent_revision_id is not None:
            _require_oid(snapshot.parent_revision_id, "native parent revision")
        if snapshot.occurred_at is None:
            raise ShadowReaderScopeError("native revision chronology is missing")
        try:
            body = snapshot.text.encode("utf-8", errors="strict")
        except UnicodeEncodeError as exc:
            raise ShadowReaderScopeError("native revision materialization is not valid UTF-8") from exc
        if len(body) != document.byte_size or hashlib.sha256(body).hexdigest() != document.body_digest:
            raise ShadowReaderScopeError("native revision body differs from C9 inventory")

    async def _native_snapshot(
        self,
        document: LegacyInventoryDocument,
        *,
        selector: str,
        fixed_ref: str,
    ) -> _NativeMaterialization:
        _validate_lineage(document)
        _require_oid(fixed_ref, "fixed_ref")
        _require_oid(selector, "native revision")
        key = _NativeSnapshotKey(
            resource_id=document.resource_id,
            selector=selector,
            fixed_ref=fixed_ref,
            path=document.current_path,
            body_digest=document.body_digest,
            byte_size=document.byte_size,
        )
        async with self._snapshot_lock:
            if self._snapshot_cache is not None and self._snapshot_cache[0] == key:
                cached = self._snapshot_cache[1]
                try:
                    self._validate_materialization(cached, document, selector=selector)
                except ShadowReaderScopeError:
                    self._snapshot_cache = None
                    raise
                return cached

            self._snapshot_cache = None
            try:
                raw_snapshot = await self.native_service.get_resource_revision(
                    namespace_id=self.namespace_id,
                    surface="document",
                    resource_id=document.resource_id,
                    revision_id=selector,
                )
            except NotFoundError as exc:
                raise ShadowReaderScopeError("native revision is outside the C9 Resource scope") from exc
            snapshot = self._materialization(raw_snapshot)
            self._validate_materialization(snapshot, document, selector=selector)
            self._snapshot_cache = (key, snapshot)
            return snapshot

    @staticmethod
    def _row_path(row: Mapping[str, Any]) -> Any:
        return row.get("path_at_revision", row.get("path"))

    @classmethod
    def _validate_selected_revision(
        cls,
        row: Mapping[str, Any],
        snapshot: _NativeMaterialization,
        document: LegacyInventoryDocument,
        *,
        source: str,
    ) -> None:
        if (
            row.get("resource_id") != document.resource_id
            or row.get("revision_id") != snapshot.revision_id
            or cls._row_path(row) != document.current_path
            or row.get("action") != snapshot.action
            or row.get("parent_revision_id") != snapshot.parent_revision_id
            or row.get("occurred_at") != snapshot.occurred_at
        ):
            raise ShadowReaderScopeError(
                f"native revision {source} differs from the selected Revision"
            )

    @classmethod
    def _validate_revision_body_facts(
        cls,
        row: Mapping[str, Any],
        snapshot: _NativeMaterialization,
        *,
        namespace_id: Any,
        source: str,
    ) -> None:
        try:
            body = snapshot.text.encode("utf-8", errors="strict")
        except UnicodeEncodeError as exc:
            raise ShadowReaderScopeError(
                f"native revision {source} body facts are invalid"
            ) from exc
        if (
            not isinstance(snapshot.revision_id, str)
            or _OID_RE.fullmatch(snapshot.revision_id) is None
            or snapshot.surface != "document"
            or not isinstance(snapshot.path, str)
            or not snapshot.path
            or snapshot.action not in {"create", "replace", "move"}
            or snapshot.occurred_at is None
            or (
                snapshot.parent_revision_id is not None
                and (
                    not isinstance(snapshot.parent_revision_id, str)
                    or _OID_RE.fullmatch(snapshot.parent_revision_id) is None
                )
            )
            or row.get("namespace_id") != namespace_id
            or row.get("resource_id") != snapshot.resource_id
            or row.get("revision_id") != snapshot.revision_id
            or row.get("surface") != snapshot.surface
            or cls._row_path(row) != snapshot.path
            or row.get("action") != snapshot.action
            or row.get("parent_revision_id") != snapshot.parent_revision_id
            or row.get("occurred_at") != snapshot.occurred_at
            or row.get("digest") != snapshot.digest
            or row.get("byte_size") != snapshot.byte_size
            or len(body) != snapshot.byte_size
            or hashlib.sha256(body).hexdigest() != snapshot.digest
        ):
            raise ShadowReaderScopeError(
                f"native revision {source} body facts differ from persisted Revision"
            )

    @staticmethod
    def _materialization(raw_snapshot: Any) -> _NativeMaterialization:
        text = _value(raw_snapshot, "text")
        if not isinstance(text, str):
            raise ShadowReaderScopeError("native revision materialization returned no text")
        action = _value(raw_snapshot, "action")
        if not isinstance(action, str) or not action:
            raise ShadowReaderScopeError("native revision materialization returned no action")
        return _NativeMaterialization(
            resource_id=_value(raw_snapshot, "resource_id"),
            revision_id=_value(raw_snapshot, "revision_id"),
            surface=_value(raw_snapshot, "surface"),
            path=_value(raw_snapshot, "path"),
            text=text,
            digest=_value(raw_snapshot, "digest"),
            byte_size=_value(raw_snapshot, "byte_size"),
            action=action,
            parent_revision_id=_value(raw_snapshot, "parent_revision_id"),
            occurred_at=_value(raw_snapshot, "occurred_at"),
        )

    async def _native_history_rows(
        self,
        document: LegacyInventoryDocument,
        snapshot: _NativeMaterialization,
    ) -> list[dict[str, Any]]:
        rows = await self.native_repository.list_history(
            resource_id=document.resource_id,
            limit=len(document.lineage) + 1,
        )
        if not isinstance(rows, list) or not rows or not all(
            isinstance(row, Mapping) for row in rows
        ):
            raise ShadowReaderScopeError("native revision history is missing or invalid")
        copied = [dict(row) for row in rows]
        revision_ids = [row.get("revision_id") for row in copied]
        if len(set(revision_ids)) != len(revision_ids) or any(
            not isinstance(revision_id, str) or _OID_RE.fullmatch(revision_id) is None
            for revision_id in revision_ids
        ):
            raise ShadowReaderScopeError("native revision history has invalid selectors")
        if any(row.get("occurred_at") is None for row in copied):
            raise ShadowReaderScopeError("native revision history has missing chronology")
        rows_by_id = {row["revision_id"]: row for row in copied}
        selected = rows_by_id.get(snapshot.revision_id)
        if selected is None:
            raise ShadowReaderScopeError("native revision history has no selected head")
        self._validate_selected_revision(
            selected,
            snapshot,
            document,
            source="history",
        )
        ordered: list[dict[str, Any]] = []
        seen: set[str] = set()
        revision_id: str | None = snapshot.revision_id
        while revision_id is not None:
            if revision_id in seen:
                raise ShadowReaderScopeError("native revision history parent cycle is invalid")
            row = rows_by_id.get(revision_id)
            if row is None:
                raise ShadowReaderScopeError("native revision history parent is missing")
            if row.get("resource_id") != document.resource_id:
                raise ShadowReaderScopeError("native revision history crosses Resource scope")
            parent_id = row.get("parent_revision_id")
            if parent_id is not None and (
                not isinstance(parent_id, str) or _OID_RE.fullmatch(parent_id) is None
            ):
                raise ShadowReaderScopeError("native revision history parent is invalid")
            seen.add(revision_id)
            ordered.append(row)
            revision_id = parent_id
        if len(ordered) != len(copied):
            raise ShadowReaderScopeError(
                "native revision history has disconnected extra rows"
            )
        return ordered

    async def _verify_selector_bridge(
        self,
        document: LegacyInventoryDocument,
        *,
        native_revision_id: str,
        fixed_ref: str,
        history_rows: list[dict[str, Any]],
    ) -> None:
        async def resolve(selector: str, source: str):
            try:
                return await self.selector_bridge.resolve_selector(
                    resource_id=document.resource_id,
                    selector=selector,
                )
            except (AKBError, AttributeError, TypeError, ValueError) as exc:
                raise ShadowReaderScopeError(
                    f"{source} selector is not bound to the completed C9 bridge"
                ) from exc

        native = await resolve(native_revision_id, "native history head")
        if (
            native.kind != "native"
            or native.native_revision_id != native_revision_id
            or native.path_at_revision != document.current_path
        ):
            raise ShadowReaderScopeError("native history head is not bound to the C9 selector")
        native_lineage: list[str] = []
        for entry in document.lineage:
            resolution = await resolve(entry.legacy_git_oid, "retained")
            if (
                resolution.kind not in {"native", "bridge"}
                or resolution.legacy_git_oid != entry.legacy_git_oid
                or resolution.path_at_revision != entry.path_at_revision
                or not isinstance(resolution.fixed_git_oid, str)
                or _OID_RE.fullmatch(resolution.fixed_git_oid) is None
            ):
                raise ShadowReaderScopeError("retained selector is not bound to the C9 fixed-ref bridge")
            if resolution.kind == "native":
                if (
                    not isinstance(resolution.native_revision_id, str)
                    or _OID_RE.fullmatch(resolution.native_revision_id) is None
                ):
                    raise ShadowReaderScopeError("native history mapping has no Revision token")
                native_lineage.append(resolution.native_revision_id)
        current = await resolve(document.current_commit, "current retained")
        if (
            current.kind != "native"
            or current.native_revision_id != native_revision_id
            or current.fixed_git_oid != fixed_ref
        ):
            raise ShadowReaderScopeError("native history head has the wrong owning fixed ref")
        if [row["revision_id"] for row in history_rows] != list(reversed(native_lineage)):
            raise ShadowReaderScopeError("native revision history differs from completed mappings")

    async def get(
        self,
        document: LegacyInventoryDocument,
        *,
        selector: str,
        fixed_ref: str,
    ) -> dict[str, Any]:
        snapshot = await self._native_snapshot(document, selector=selector, fixed_ref=fixed_ref)
        metadata, body = _parsed_body(snapshot.text)
        title = metadata.get("title")
        if not isinstance(title, str) or not title:
            title = document.current_path.rsplit("/", 1)[-1]
        return {
            "kind": "document",
            "uri": doc_uri(self.vault_name, document.current_path),
            "vault": self.vault_name,
            "path": document.current_path,
            "title": title,
            "current_commit": selector,
            "content": body,
            "projection": {"revision": selector, "authoritative": False},
            "actor": {
                "id": document.activity.actor,
                "display": document.activity.actor,
            },
        }

    async def history(
        self,
        document: LegacyInventoryDocument,
        *,
        selector: str,
        fixed_ref: str,
    ) -> dict[str, Any]:
        snapshot = await self._native_snapshot(document, selector=selector, fixed_ref=fixed_ref)
        history_rows = await self._native_history_rows(document, snapshot)
        await self._verify_selector_bridge(
            document,
            native_revision_id=selector,
            fixed_ref=fixed_ref,
            history_rows=history_rows,
        )
        return {
            "history_source": "fixed-ref-bridge",
            "lineage_boundary": document.lineage[0].legacy_git_oid,
            "entries": _history_entries(document, native_revision_id=selector),
        }

    async def diff(
        self,
        document: LegacyInventoryDocument,
        *,
        selector: str,
        fixed_ref: str,
    ) -> dict[str, Any]:
        snapshot = await self._native_snapshot(document, selector=selector, fixed_ref=fixed_ref)
        raw_selected = await self.native_repository.get_revision(
            resource_id=document.resource_id,
            revision_id=selector,
        )
        if not isinstance(raw_selected, Mapping):
            raise ShadowReaderScopeError("native revision diff selection is missing")
        self._validate_selected_revision(
            raw_selected,
            snapshot,
            document,
            source="diff",
        )
        self._validate_revision_body_facts(
            raw_selected,
            snapshot,
            namespace_id=self.namespace_id,
            source="selected",
        )
        parent_text = ""
        if snapshot.action == "create":
            if snapshot.parent_revision_id is not None:
                raise ShadowReaderScopeError("native revision diff has an invalid create parent")
        elif snapshot.action in {"replace", "move"}:
            if snapshot.parent_revision_id is None:
                raise ShadowReaderScopeError("native revision diff has no parent")
            raw_parent = await self.native_repository.get_revision(
                resource_id=document.resource_id,
                revision_id=snapshot.parent_revision_id,
            )
            if not isinstance(raw_parent, Mapping):
                raise ShadowReaderScopeError("native revision diff parent row is missing")
            try:
                raw_parent_snapshot = await self.native_service.get_resource_revision(
                    namespace_id=self.namespace_id,
                    surface="document",
                    resource_id=document.resource_id,
                    revision_id=snapshot.parent_revision_id,
                )
            except NotFoundError as exc:
                raise ShadowReaderScopeError("native revision diff parent is missing") from exc
            parent_snapshot = self._materialization(raw_parent_snapshot)
            self._validate_revision_body_facts(
                raw_parent,
                parent_snapshot,
                namespace_id=self.namespace_id,
                source="parent",
            )
            parent_text = parent_snapshot.text
        else:
            raise ShadowReaderScopeError("native revision diff action is unsupported")
        return {
            "file": document.current_path,
            "commit": selector,
            "basis": "fixed-ref-snapshot",
            "text": _canonical_transition(parent_text, snapshot.text),
            "format": "unified",
        }

    async def audit_activity(
        self,
        document: LegacyInventoryDocument,
        *,
        selector: str,
        fixed_ref: str,
    ) -> dict[str, Any]:
        """Validate persisted native activity before a shadow projection."""

        return (await self.activity_evidence(
            document,
            selector=selector,
            fixed_ref=fixed_ref,
        )).binding_fact

    async def _activity_facts(
        self,
        document: LegacyInventoryDocument,
        *,
        selector: str,
        fixed_ref: str,
    ) -> tuple[_NativeMaterialization, Mapping[str, Any], Mapping[str, Any]]:
        snapshot = await self._native_snapshot(document, selector=selector, fixed_ref=fixed_ref)
        raw_selected = await self.native_repository.get_revision(
            resource_id=document.resource_id,
            revision_id=selector,
        )
        if not isinstance(raw_selected, Mapping):
            raise ShadowReaderScopeError("native revision activity selection is missing")
        if (
            raw_selected.get("namespace_id") != self.namespace_id
            or raw_selected.get("surface") != "document"
        ):
            raise ShadowReaderScopeError("native activity audit selected Revision is out of scope")
        self._validate_selected_revision(
            raw_selected,
            snapshot,
            document,
            source="activity",
        )
        raw_activity = await self.native_repository.get_activity_for_revision(
            namespace_id=self.namespace_id,
            surface="document",
            resource_id=document.resource_id,
            revision_id=selector,
        )
        if not isinstance(raw_activity, Mapping):
            raise ShadowReaderScopeError("native revision activity is missing")
        expected = (
            document.resource_id,
            selector,
            raw_selected.get("action"),
            raw_selected.get("actor"),
            raw_selected.get("subject"),
            raw_selected.get("summary"),
            raw_selected.get("path_from"),
            raw_selected.get("path_to"),
            raw_selected.get("occurred_at"),
            self._row_path(raw_selected),
        )
        observed = (
            raw_activity.get("resource_id"),
            raw_activity.get("revision_id"),
            raw_activity.get("action"),
            raw_activity.get("actor"),
            raw_activity.get("subject"),
            raw_activity.get("summary"),
            raw_activity.get("changed_path_from"),
            raw_activity.get("changed_path_to"),
            raw_activity.get("occurred_at"),
            raw_activity.get("path_at_revision"),
        )
        if observed != expected:
            raise ShadowReaderScopeError(
                "native revision activity differs from persisted Revision facts"
            )
        return snapshot, raw_selected, raw_activity

    async def _audit_activity_facts(
        self,
        document: LegacyInventoryDocument,
        *,
        snapshot: _NativeMaterialization,
        raw_selected: Mapping[str, Any],
        activity: Mapping[str, Any],
    ) -> dict[str, Any]:
        if not document.lineage:
            raise ShadowReaderScopeError("native activity audit has no frozen lineage head")
        frozen_time = document.lineage[-1].committed_at
        if (
            snapshot.occurred_at != frozen_time
            or raw_selected.get("occurred_at") != frozen_time
            or activity.get("occurred_at") != frozen_time
        ):
            raise ShadowReaderScopeError(
                "native activity audit timestamp differs from frozen lineage head"
            )

        parent_id = raw_selected.get("parent_revision_id")
        parent_binding: dict[str, Any] | None = None
        if parent_id is None:
            expected = {
                "action": "create",
                "actor": _NATIVE_ACTIVITY_MIGRATION_ACTOR,
                "subject": None,
                "summary": None,
                "path_from": None,
                "path_to": document.current_path,
            }
            observed_revision = {
                "action": raw_selected.get("action"),
                "actor": raw_selected.get("actor"),
                "subject": raw_selected.get("subject"),
                "summary": raw_selected.get("summary"),
                "path_from": raw_selected.get("path_from"),
                "path_to": raw_selected.get("path_to"),
            }
            observed_activity = {
                "action": activity.get("action"),
                "actor": activity.get("actor"),
                "subject": activity.get("subject"),
                "summary": activity.get("summary"),
                "path_from": activity.get("changed_path_from"),
                "path_to": activity.get("changed_path_to"),
            }
            if observed_revision != expected or observed_activity != expected:
                raise ShadowReaderScopeError(
                    "native activity audit genesis facts are invalid"
                )
        else:
            _require_oid(parent_id, "native activity parent revision")
            if parent_id == raw_selected.get("revision_id"):
                raise ShadowReaderScopeError("native activity audit parent is self-referential")
            raw_parent = await self.native_repository.get_revision(
                resource_id=document.resource_id,
                revision_id=parent_id,
            )
            if not isinstance(raw_parent, Mapping):
                raise ShadowReaderScopeError("native activity audit parent Revision is missing")
            if (
                raw_parent.get("namespace_id") != self.namespace_id
                or raw_parent.get("resource_id") != document.resource_id
                or raw_parent.get("surface") != "document"
                or raw_parent.get("revision_id") != parent_id
            ):
                raise ShadowReaderScopeError(
                    "native activity audit parent Revision is outside the Resource scope"
                )
            parent_binding = await self._completed_parent_mapping(
                document,
                parent_revision_id=parent_id,
            )
            parent_path = self._row_path(raw_parent)
            current_path = self._row_path(raw_selected)
            if (
                parent_path != parent_binding["path_at_revision"]
                or not isinstance(parent_path, str)
                or not parent_path
                or not isinstance(current_path, str)
                or not current_path
            ):
                raise ShadowReaderScopeError(
                    "native activity audit parent path differs from its completed legacy mapping"
                )
            action = "move" if parent_path != current_path else "replace"
            expected = {
                "action": action,
                "actor": document.activity.actor,
                "subject": document.activity.subject,
                "summary": document.activity.summary,
                "path_from": parent_path if action == "move" else None,
                "path_to": current_path if action == "move" else None,
            }
            observed_revision = {
                "action": raw_selected.get("action"),
                "actor": raw_selected.get("actor"),
                "subject": raw_selected.get("subject"),
                "summary": raw_selected.get("summary"),
                "path_from": raw_selected.get("path_from"),
                "path_to": raw_selected.get("path_to"),
            }
            observed_activity = {
                "action": activity.get("action"),
                "actor": activity.get("actor"),
                "subject": activity.get("subject"),
                "summary": activity.get("summary"),
                "path_from": activity.get("changed_path_from"),
                "path_to": activity.get("changed_path_to"),
            }
            if observed_revision != expected or observed_activity != expected:
                raise ShadowReaderScopeError(
                    "native activity audit reconcile facts are invalid"
                )

        occurred_at = raw_selected.get("occurred_at")
        if not isinstance(occurred_at, datetime):
            raise ShadowReaderScopeError("native activity audit timestamp is invalid")
        return {
            "profile": _NATIVE_ACTIVITY_AUDIT_PROFILE,
            "selected_revision": {
                "namespace_id": str(raw_selected.get("namespace_id")),
                "resource_id": str(raw_selected.get("resource_id")),
                "revision_id": raw_selected.get("revision_id"),
                "parent_revision_id": parent_id,
                "action": raw_selected.get("action"),
                "path_at_revision": self._row_path(raw_selected),
                "path_from": raw_selected.get("path_from"),
                "path_to": raw_selected.get("path_to"),
                "actor": raw_selected.get("actor"),
                "subject": raw_selected.get("subject"),
                "summary": raw_selected.get("summary"),
                "occurred_at": occurred_at.isoformat(),
            },
            "activity": {
                "resource_id": str(activity.get("resource_id")),
                "revision_id": activity.get("revision_id"),
                "action": activity.get("action"),
                "path_at_revision": activity.get("path_at_revision"),
                "path_from": activity.get("changed_path_from"),
                "path_to": activity.get("changed_path_to"),
                "actor": activity.get("actor"),
                "subject": activity.get("subject"),
                "summary": activity.get("summary"),
                "occurred_at": occurred_at.isoformat(),
            },
            "completed_parent_mapping": parent_binding,
        }

    async def _completed_parent_mapping(
        self,
        document: LegacyInventoryDocument,
        *,
        parent_revision_id: str,
    ) -> dict[str, Any]:
        matches: list[dict[str, Any]] = []
        for entry in document.lineage:
            try:
                resolution = await self.selector_bridge.resolve_selector(
                    resource_id=document.resource_id,
                    selector=entry.legacy_git_oid,
                )
            except (AKBError, AttributeError, TypeError, ValueError) as exc:
                raise ShadowReaderScopeError(
                    "native activity parent has no completed legacy mapping"
                ) from exc
            if resolution.kind != "native" or resolution.native_revision_id != parent_revision_id:
                continue
            if (
                resolution.legacy_git_oid != entry.legacy_git_oid
                or resolution.path_at_revision != entry.path_at_revision
                or not isinstance(resolution.fixed_git_oid, str)
                or _OID_RE.fullmatch(resolution.fixed_git_oid) is None
                or resolution.run_id is None
            ):
                raise ShadowReaderScopeError(
                    "native activity parent mapping differs from the frozen lineage"
                )
            matches.append(
                {
                    "legacy_git_oid": entry.legacy_git_oid,
                    "path_at_revision": entry.path_at_revision,
                    "native_revision_id": parent_revision_id,
                    "fixed_git_oid": resolution.fixed_git_oid,
                    "run_id": str(resolution.run_id),
                }
            )
        if len(matches) != 1:
            raise ShadowReaderScopeError(
                "native activity parent must have exactly one completed legacy mapping"
            )
        return matches[0]

    async def activity_evidence(
        self,
        document: LegacyInventoryDocument,
        *,
        selector: str,
        fixed_ref: str,
    ) -> NativeActivityEvidence:
        snapshot, raw_selected, activity = await self._activity_facts(
            document,
            selector=selector,
            fixed_ref=fixed_ref,
        )
        binding_fact = await self._audit_activity_facts(
            document,
            snapshot=snapshot,
            raw_selected=raw_selected,
            activity=activity,
        )
        actor = activity.get("actor")
        if not isinstance(actor, str) or not actor:
            raise ShadowReaderScopeError("native revision activity actor is invalid")
        return NativeActivityEvidence(
            envelope={
                "events": [
                    {
                        "hash": selector,
                        "subject": activity.get("subject"),
                        "author": {"id": actor, "display": actor},
                        "action": activity.get("action"),
                        "summary": activity.get("summary"),
                        "projection_revision": selector,
                    }
                ]
            },
            binding_fact=binding_fact,
        )

    async def activity(
        self,
        document: LegacyInventoryDocument,
        *,
        selector: str,
        fixed_ref: str,
    ) -> dict[str, Any]:
        return (await self.activity_evidence(
            document,
            selector=selector,
            fixed_ref=fixed_ref,
        )).envelope
