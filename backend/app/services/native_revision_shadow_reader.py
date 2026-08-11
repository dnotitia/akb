"""Product-owned, read-only readers for the P2 shadow comparison.

The readers deliberately expose the small C10 envelopes rather than calling a
public route or registering a second request path.  The legacy reader is
bounded by the immutable fixed-ref inventory; the native reader is bounded by
the Resource identity and the native genesis Revision published by C9.
"""

from __future__ import annotations

import asyncio
import hashlib
import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Protocol
from uuid import UUID

import asyncpg

from app.exceptions import ConflictError, NotFoundError
from app.services.document_service import _parse_markdown
from app.services.git_service import FixedRefHistoryError, GitService
from app.services.legacy_revision_bridge import (
    LegacyInventoryDocument,
    LegacyRevisionBridge,
)
from app.services.native_revision_service import NativeRevisionService
from app.services.uri_service import doc_uri


_OID_RE = re.compile(r"^[0-9a-f]{40}$")


class ShadowReaderScopeError(ConflictError):
    """A product shadow read was not bound to its frozen C9 scope."""

    def __init__(self, message: str = "native revision shadow reader scope is invalid"):
        super().__init__(message)
        self.code = "native_revision_shadow_reader_scope_invalid"


class NativeRevisionReadService(Protocol):
    async def get_resource_revision(
        self,
        *,
        namespace_id,
        surface: str,
        resource_id,
        revision_id: str,
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
    """Render one deterministic content snapshot for both read models.

    C10 compares a Git parent-based diff with the native fixed-ref snapshot
    basis.  The basis is the approved semantic difference; the selected
    current body must still be the same.  This deliberately avoids making the
    native genesis pretend it has a legacy parent.
    """
    normalized = text.replace("\r\n", "\n")
    lines = normalized.splitlines()
    if not lines:
        return "@@ snapshot @@\n"
    return "@@ snapshot @@\n" + "\n".join(f" {line}" for line in lines)


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
            materialized_fixed_ref = raw_snapshot.get("fixed_ref")
            materialized_current_commit = raw_snapshot.get("current_commit")
            if not isinstance(body, bytes):
                raise ShadowReaderScopeError("legacy fixed-ref materialization returned no body")
            if not isinstance(materialized_fixed_ref, str) or not isinstance(
                materialized_current_commit,
                str,
            ):
                raise ShadowReaderScopeError("legacy fixed-ref materialization has invalid scope")
            snapshot = _LegacyMaterialization(
                fixed_ref=materialized_fixed_ref,
                current_commit=materialized_current_commit,
                body=bytes(body),
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
        _, body = _parsed_body(snapshot.body.decode("utf-8"))
        return {
            "file": document.current_path,
            "commit": selector,
            "basis": "git-parent",
            "text": _snapshot_diff(body),
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
        selector_bridge: LegacyRevisionBridge | None = None,
    ):
        if native_service is None and pool is None:
            raise ValueError("pool or native_service is required")
        if not isinstance(vault_name, str) or not vault_name.strip():
            raise ValueError("vault_name must be non-empty")
        self.pool = pool
        self.namespace_id = namespace_id
        self.vault_name = vault_name
        self.native_service = native_service or NativeRevisionService(pool)  # type: ignore[arg-type]
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
            text = _value(raw_snapshot, "text")
            if not isinstance(text, str):
                raise ShadowReaderScopeError("native revision materialization returned no text")
            action = _value(raw_snapshot, "action")
            snapshot = _NativeMaterialization(
                resource_id=_value(raw_snapshot, "resource_id"),
                revision_id=_value(raw_snapshot, "revision_id"),
                surface=_value(raw_snapshot, "surface"),
                path=_value(raw_snapshot, "path"),
                text=text,
                digest=_value(raw_snapshot, "digest"),
                byte_size=_value(raw_snapshot, "byte_size"),
                action=action if isinstance(action, str) and action else "create",
            )
            self._validate_materialization(snapshot, document, selector=selector)
            self._snapshot_cache = (key, snapshot)
            return snapshot

    async def _verify_selector_bridge(
        self,
        document: LegacyInventoryDocument,
        *,
        native_revision_id: str,
        fixed_ref: str,
    ) -> None:
        if self.selector_bridge is None:
            return
        native = await self.selector_bridge.resolve_selector(
            resource_id=document.resource_id,
            selector=native_revision_id,
        )
        if native.kind != "native" or native.native_revision_id != native_revision_id:
            raise ShadowReaderScopeError("native history head is not bound to the C9 selector")
        for entry in document.lineage[:-1]:
            resolution = await self.selector_bridge.resolve_selector(
                resource_id=document.resource_id,
                selector=entry.legacy_git_oid,
            )
            if (
                resolution.kind != "bridge"
                or resolution.fixed_git_oid != fixed_ref
                or resolution.legacy_git_oid != entry.legacy_git_oid
                or resolution.path_at_revision != entry.path_at_revision
            ):
                raise ShadowReaderScopeError("retained selector is not bound to the C9 fixed-ref bridge")

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
        await self._native_snapshot(document, selector=selector, fixed_ref=fixed_ref)
        await self._verify_selector_bridge(
            document,
            native_revision_id=selector,
            fixed_ref=fixed_ref,
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
        _, body = _parsed_body(snapshot.text)
        return {
            "file": document.current_path,
            "commit": selector,
            "basis": "fixed-ref-snapshot",
            "text": _snapshot_diff(body),
            "format": "unified",
        }

    async def activity(
        self,
        document: LegacyInventoryDocument,
        *,
        selector: str,
        fixed_ref: str,
    ) -> dict[str, Any]:
        snapshot = await self._native_snapshot(document, selector=selector, fixed_ref=fixed_ref)
        return {
            "events": [
                {
                    "hash": selector,
                    "subject": None,
                    "author": {
                        "id": "akb-native-revision-migration",
                        "display": "Migration",
                    },
                    "action": snapshot.action,
                    "summary": "C9 fixed-ref native genesis",
                    "projection_revision": selector,
                }
            ]
        }
