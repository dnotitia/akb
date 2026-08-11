"""Focused tests for the product-owned P2 shadow-read seam."""

from __future__ import annotations

import asyncio
import difflib
import hashlib
import importlib.util
import json
import os
import uuid
from contextlib import asynccontextmanager
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import asyncpg
import pytest
from git import Repo

from app.repositories.native_revision_migration_repo import (
    LegacyRevisionMapping,
    MigrationItem,
    MigrationRun,
)
from app.services.git_service import GitService
from app.services.legacy_revision_bridge import (
    LegacyActivitySemantics,
    LegacyInventory,
    LegacyInventoryDocument,
    LegacyLineageEntry,
    LegacyRevisionBridge,
    SelectorResolution,
    SelectorUnknownError,
)
from app.services.native_revision_shadow import (
    NativeRevisionShadowComparator,
    ShadowComparisonError,
)
from app.services.native_revision_shadow_reader import (
    _apply_unified_patch,
    _canonical_transition,
    LegacyFixedRefShadowReader,
    NativeActivityEvidence,
    NativeRevisionShadowReader,
    ShadowReaderScopeError,
)
from app.services.native_revision_backfill import NativeRevisionBackfill
from app.services.native_revision_reconcile import NativeRevisionReconcile


pytestmark = pytest.mark.asyncio

_BACKEND = Path(__file__).resolve().parents[1]
_INIT_SQL = (_BACKEND / "app" / "db" / "init.sql").read_text(encoding="utf-8")
_MIGRATIONS = _BACKEND / "app" / "db" / "migrations"
_DSN = os.environ.get(
    "AKB_TEST_DSN",
    "postgresql://akb:akb@127.0.0.1:54913/akb_p2_native_revision_test",  # pragma: allowlist secret
)


async def _reachable() -> bool:
    try:
        conn = await asyncpg.connect(_DSN, timeout=2)
    except OSError, asyncpg.PostgresError:
        return False
    await conn.close()
    return True


def _database_dsn(name: str) -> str:
    return f"{_DSN.rsplit('/', 1)[0]}/{name}"


def _load_migration(filename: str):
    path = _MIGRATIONS / filename
    spec = importlib.util.spec_from_file_location(f"shadow_reader_{filename}", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@asynccontextmanager
async def _fresh_schema(tmp_path: Path):
    if not await _reachable():
        pytest.skip(f"Postgres not reachable at {_DSN}")
    name = f"akb_shadow_reader_{uuid.uuid4().hex[:12]}"
    admin = await asyncpg.connect(_DSN)
    connection = None
    pool = None
    try:
        await admin.execute(f'CREATE DATABASE "{name}"')
        dsn = _database_dsn(name)
        connection = await asyncpg.connect(dsn)
        await connection.execute(_INIT_SQL)
        for filename in (
            "010_external_git_mirror.py",
            "048_native_revision_core.py",
            "053_native_revision_m1_pg_body.py",
            "060_native_revision_migration_bridge.py",
        ):
            await _load_migration(filename).migrate(conn=connection)
        await connection.close()
        connection = None
        pool = await asyncpg.create_pool(dsn, min_size=1, max_size=4)
        yield pool
    finally:
        if pool is not None:
            await pool.close()
        if connection is not None and not connection.is_closed():
            await connection.close()
        await admin.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
        await admin.close()


async def _authority_counts(pool) -> tuple[tuple[str, int], ...]:
    tables = (
        "native_resources",
        "native_revisions",
        "native_revision_activity",
        "native_revision_migration_runs",
        "native_revision_migration_items",
        "legacy_revision_mappings",
    )
    async with pool.acquire() as conn:
        return tuple([(table, int(await conn.fetchval(f'SELECT count(*) FROM "{table}"'))) for table in tables])


def _oid(char: str) -> str:
    return char * 40


def _document_body(document: LegacyInventoryDocument) -> bytes:
    if document.resource_id == uuid.UUID("11111111-1111-4111-8111-111111111111"):
        return b"# Migration candidate\n\nsecret body\n"
    if document.resource_id == uuid.UUID("44444444-4444-4444-8444-444444444444"):
        return b"# Second candidate\n\nother secret body\n"
    raise AssertionError(f"unexpected synthetic document: {document.resource_id}")


@pytest.mark.parametrize(
    ("parent_text", "patch", "expected"),
    [
        pytest.param(
            "text\n",
            "@@ -1 +1 @@\n-text\n+text\n\\ No newline at end of file\n",
            "text",
            id="newline-to-no-newline",
        ),
        pytest.param(
            "text",
            "@@ -1 +1 @@\n-text\n\\ No newline at end of file\n+text\n",
            "text\n",
            id="no-newline-to-newline",
        ),
    ],
)
async def test_apply_unified_patch_preserves_terminal_newline_state(
    parent_text: str,
    patch: str,
    expected: str,
):
    assert _apply_unified_patch(parent_text, patch, "modified") == expected


async def test_apply_unified_patch_rejects_forged_parent_line():
    patch = "@@ -1 +1 @@\n-forged\n+text\n"

    with pytest.raises(ShadowReaderScopeError, match="differs from parent body"):
        _apply_unified_patch("text\n", patch, "modified")


def _document() -> LegacyInventoryDocument:
    resource_id = uuid.UUID("11111111-1111-4111-8111-111111111111")
    current = _oid("a")
    retained = _oid("b")
    path = "notes/migration-candidate.md"
    body = "# Migration candidate\n\nsecret body\n"
    at = datetime(2026, 8, 10, tzinfo=UTC)
    return LegacyInventoryDocument(
        resource_id=resource_id,
        current_path=path,
        current_commit=current,
        created_at=at,
        body_digest=hashlib.sha256(body.encode()).hexdigest(),
        byte_size=len(body.encode()),
        aliases=(),
        lineage=(
            LegacyLineageEntry(retained, path, at),
            LegacyLineageEntry(current, path, at),
        ),
        activity=LegacyActivitySemantics(
            legacy_git_oid=current,
            committed_at=at,
            actor="actor-legacy-writer",
            subject="akb://p2-manual/coll/notes/migration-candidate.md",
            summary="update migration candidate at fixed ref",
            action="update",
            path_from=None,
            path_to=path,
            changed_paths=({"change": "update", "path_from": None, "path_to": path},),
        ),
    )


def _second_document() -> LegacyInventoryDocument:
    first = _document()
    current = _oid("d")
    retained = _oid("e")
    path = "notes/second-candidate.md"
    body = b"# Second candidate\n\nother secret body\n"
    at = first.created_at + timedelta(seconds=1)
    return replace(
        first,
        resource_id=uuid.UUID("44444444-4444-4444-8444-444444444444"),
        current_path=path,
        current_commit=current,
        created_at=at,
        body_digest=hashlib.sha256(body).hexdigest(),
        byte_size=len(body),
        lineage=(
            LegacyLineageEntry(retained, path, at),
            LegacyLineageEntry(current, path, at),
        ),
        activity=replace(
            first.activity,
            legacy_git_oid=current,
            committed_at=at,
            subject="akb://p2-manual/coll/notes/second-candidate.md",
            summary="update second candidate at fixed ref",
            path_to=path,
            changed_paths=({"change": "update", "path_from": None, "path_to": path},),
        ),
    )


async def test_inventory_document_schema_does_not_retain_body_text():
    document = _document()

    assert not hasattr(document, "body")


def _run_and_item(
    document: LegacyInventoryDocument,
) -> tuple[MigrationRun, MigrationItem, LegacyInventory, str]:
    namespace_id = uuid.UUID("22222222-2222-4222-8222-222222222222")
    run_id = uuid.UUID("33333333-3333-4333-8333-333333333333")
    native_id = _oid("c")
    fixed_ref = _oid("f")
    digest = "d" * 64
    run = MigrationRun(
        run_id=run_id,
        namespace_id=namespace_id,
        fixed_git_oid=fixed_ref,
        coverage_version="p2-test-v1",
        inventory_digest=digest,
        status="complete",
        created_at=document.created_at,
        started_at=document.created_at,
        completed_at=document.created_at,
        error=None,
    )
    inventory = LegacyInventory(
        namespace_id=namespace_id,
        fixed_git_oid=fixed_ref,
        coverage_version=run.coverage_version,
        documents=(document,),
        inventory_digest=digest,
    )
    item = MigrationItem(
        run_id=run_id,
        namespace_id=namespace_id,
        legacy_document_id=document.resource_id,
        native_resource_id=document.resource_id,
        captured_path=document.current_path,
        legacy_head_oid=document.current_commit,
        native_head_revision_id=native_id,
        body_digest=document.body_digest,
        byte_size=document.byte_size,
        status="complete",
        error_code=None,
    )
    return run, item, inventory, native_id


class _ReadRepository:
    def __init__(self, run: MigrationRun, item: MigrationItem):
        self.run = run
        self.item = item

    async def get_run(self, run_id: uuid.UUID) -> MigrationRun:
        assert run_id == self.run.run_id
        return self.run

    async def list_items(self, run_id: uuid.UUID) -> list[MigrationItem]:
        assert run_id == self.run.run_id
        return [self.item]

    async def exact_mapping(
        self,
        *,
        resource_id: uuid.UUID,
        legacy_git_oid: str,
    ) -> LegacyRevisionMapping | None:
        if resource_id != self.item.legacy_document_id or legacy_git_oid != self.item.legacy_head_oid:
            return None
        return LegacyRevisionMapping(
            namespace_id=self.run.namespace_id,
            resource_id=resource_id,
            legacy_git_oid=legacy_git_oid,
            path_at_revision=self.item.captured_path,
            resolution="native",
            native_revision_id=self.item.native_head_revision_id,
            run_id=self.run.run_id,
            lineage_ordinal=1,
            fixed_git_oid=self.run.fixed_git_oid,
        )

    async def list_resource_mappings(
        self,
        *,
        namespace_id: uuid.UUID,
        resource_id: uuid.UUID,
    ) -> list[LegacyRevisionMapping]:
        assert namespace_id == self.run.namespace_id
        document = _document()
        assert resource_id == document.resource_id
        return [
            LegacyRevisionMapping(
                namespace_id=namespace_id,
                resource_id=resource_id,
                legacy_git_oid=entry.legacy_git_oid,
                path_at_revision=entry.path_at_revision,
                resolution="native" if index == len(document.lineage) - 1 else "bridge",
                native_revision_id=(self.item.native_head_revision_id if index == len(document.lineage) - 1 else None),
                run_id=self.run.run_id,
                lineage_ordinal=index,
                fixed_git_oid=self.run.fixed_git_oid,
            )
            for index, entry in enumerate(document.lineage)
        ]


class _InventoryBridge:
    def __init__(self, inventory: LegacyInventory):
        self.inventory = inventory

    async def inventory_for_run(self, run: MigrationRun) -> LegacyInventory:
        assert run.inventory_digest == self.inventory.inventory_digest
        return self.inventory


def _activity_binding_fact(
    document: LegacyInventoryDocument,
    *,
    selector: str,
    fixed_ref: str,
) -> dict[str, Any]:
    namespace_id = "22222222-2222-4222-8222-222222222222"
    resource_id = str(document.resource_id)
    occurred_at = document.lineage[-1].committed_at.isoformat()
    current_mapping = {
        "namespace_id": namespace_id,
        "resource_id": resource_id,
        "legacy_git_oid": document.current_commit,
        "path_at_revision": document.current_path,
        "resolution": "native",
        "native_revision_id": selector,
        "fixed_git_oid": fixed_ref,
        "run_id": "33333333-3333-4333-8333-333333333333",
    }
    selected_revision = {
        "namespace_id": namespace_id,
        "resource_id": resource_id,
        "revision_id": selector,
        "parent_revision_id": None,
        "surface": "document",
        "action": "create",
        "path_at_revision": document.current_path,
        "path_from": None,
        "path_to": document.current_path,
        "actor": "akb-native-revision-migration",
        "subject": None,
        "summary": None,
        "occurred_at": occurred_at,
        "digest": document.body_digest,
        "byte_size": document.byte_size,
    }
    return {
        "profile": "akb-native-revision-p2-activity-audit/v1",
        "selector": selector,
        "fixed_ref": fixed_ref,
        "current_mapping": current_mapping,
        "selected_revision": selected_revision,
        "activity": {
            key: value
            for key, value in selected_revision.items()
            if key not in {"parent_revision_id", "digest", "byte_size"}
        },
        "completed_parent_mapping": None,
    }


class _WireReader:
    def __init__(self, native_id: str, *, candidate: bool):
        self.native_id = native_id
        self.candidate = candidate

    def _selector(self, document: LegacyInventoryDocument) -> str:
        return self.native_id if self.candidate else document.current_commit

    async def get(self, document: LegacyInventoryDocument, *, selector: str, fixed_ref: str) -> dict[str, Any]:
        del fixed_ref
        body = _document_body(document).decode()
        if not self.candidate:
            body = body.replace("\n", "\r\n")
        return {
            "kind": "document",
            "uri": "akb://p2-manual/coll/notes/migration-candidate.md",
            "vault": "p2-manual",
            "path": document.current_path,
            "resource_id": str(document.resource_id),
            "current_commit": selector,
            "content": body,
            "projection": {"revision": selector, "authoritative": False},
        }

    async def history(self, document: LegacyInventoryDocument, *, selector: str, fixed_ref: str) -> dict[str, Any]:
        del fixed_ref
        entries = []
        for index, entry in enumerate(reversed(document.lineage)):
            entry_selector = self.native_id if self.candidate and index == 0 else entry.legacy_git_oid
            entries.append(
                {
                    "selector": entry_selector,
                    "payload_sha256": hashlib.sha256(entry.legacy_git_oid.encode()).hexdigest(),
                    "projection_revision": entry_selector if index == 0 else None,
                    "summary": "current" if index == 0 else "retained history",
                }
            )
        return {
            "history_source": "fixed-ref-bridge" if self.candidate else "legacy-git-log",
            "lineage_boundary": document.lineage[0].legacy_git_oid if self.candidate else "legacy-document-start",
            "entries": entries,
        }

    async def diff(self, document: LegacyInventoryDocument, *, selector: str, fixed_ref: str) -> dict[str, Any]:
        del fixed_ref
        return {
            "file": document.current_path,
            "commit": selector,
            "basis": "fixed-ref-snapshot" if self.candidate else "git-parent",
            "text": "@@ snapshot @@\n secret body",
            "format": "unified",
        }

    async def activity(self, document: LegacyInventoryDocument, *, selector: str, fixed_ref: str) -> dict[str, Any]:
        del fixed_ref
        activity = document.activity
        actor = "akb-native-revision-migration" if self.candidate else activity.actor
        return {
            "events": [
                {
                    "hash": selector,
                    "subject": None if self.candidate else activity.subject,
                    "author": {
                        "id": actor,
                        "display": actor if self.candidate else "Legacy Writer (Git)",
                    },
                    "action": activity.action if not self.candidate else "create",
                    "summary": activity.summary if not self.candidate else None,
                    "projection_revision": selector,
                }
            ]
        }

    async def activity_evidence(
        self,
        document: LegacyInventoryDocument,
        *,
        selector: str,
        fixed_ref: str,
    ) -> NativeActivityEvidence:
        envelope = await self.activity(
            document,
            selector=selector,
            fixed_ref=fixed_ref,
        )
        return NativeActivityEvidence(
            envelope=envelope,
            binding_fact=_activity_binding_fact(
                document,
                selector=selector,
                fixed_ref=fixed_ref,
            ),
        )


class _NoWriteGit:
    def __init__(self, *documents: LegacyInventoryDocument):
        self.documents = documents
        self.bodies = {
            (document.current_path, document.current_commit): _document_body(document)
            for document in documents
        }
        for document in documents:
            for entry in document.lineage[:-1]:
                self.bodies[(entry.path_at_revision, entry.legacy_git_oid)] = _document_body(document).replace(
                    b"secret body", b"old body"
                )
        self.calls: list[tuple[str, tuple[Any, ...]]] = []
        self.snapshot_override: dict[str, Any] | None = None
        self.diff_override: dict[str, Any] | None = None

    def manual_fixed_ref_history(self, *args, **kwargs) -> dict[str, Any]:
        self.calls.append(("manual_fixed_ref_history", args))
        document = next(document for document in self.documents if document.current_path == args[2])
        result = {
            "fixed_ref": args[1],
            "current_commit": kwargs["current_commit"],
            "body": self.bodies[(args[2], kwargs["current_commit"])],
            "history": [
                {
                    "legacy_git_oid": entry.legacy_git_oid,
                    "path_at_revision": entry.path_at_revision,
                    "committed_at": entry.committed_at,
                }
                for entry in reversed(document.lineage)
            ],
            "activity": {
                "legacy_git_oid": document.activity.legacy_git_oid,
                "committed_at": document.activity.committed_at,
                "actor": document.activity.actor,
                "subject": document.activity.subject,
                "summary": document.activity.summary,
                "action": document.activity.action,
                "path_from": document.activity.path_from,
                "path_to": document.activity.path_to,
                "changed_paths": [dict(change) for change in document.activity.changed_paths],
            },
        }
        return self.snapshot_override if self.snapshot_override is not None else result

    def file_diff(self, *args) -> dict[str, Any]:
        self.calls.append(("file_diff", args))
        document = next(document for document in self.documents if document.current_path == args[1])
        previous = document.lineage[-2]
        parent = self.bodies[(previous.path_at_revision, previous.legacy_git_oid)].decode()
        current = self.bodies[(document.current_path, document.current_commit)].decode()
        result = {
            "file": args[1],
            "commit": args[2],
            "type": "modified",
            "diff": "\n".join(
                difflib.unified_diff(
                    parent.splitlines(),
                    current.splitlines(),
                    fromfile=f"a/{previous.path_at_revision}",
                    tofile=f"b/{document.current_path}",
                    lineterm="",
                )
            ),
        }
        return self.diff_override if self.diff_override is not None else result

    def read_file(self, *args, **kwargs) -> str | None:
        self.calls.append(("read_file", args))
        body = self.bodies.get((args[1], kwargs.get("commit")))
        return body.decode() if body is not None else None


class _GenesisDiffGit(_NoWriteGit):
    def __init__(self, document: LegacyInventoryDocument, *, diff: dict[str, Any], body: str):
        super().__init__(document)
        self.diff_override = diff
        self.parent_reads: list[tuple[tuple[Any, ...], dict[str, Any]]] = []
        self.bodies[(document.current_path, document.current_commit)] = body.encode()

    def file_diff(self, *args) -> dict[str, Any]:
        self.calls.append(("file_diff", args))
        return self.diff_override

    def read_file(self, *args, **kwargs) -> str:
        if (args[1], kwargs.get("commit")) != (
            self.documents[0].current_path,
            self.documents[0].current_commit,
        ):
            self.parent_reads.append((args, kwargs))
            raise AssertionError("genesis diff read outside logical lineage")
        self.calls.append(("read_file", args))
        return self.bodies[(args[1], kwargs["commit"])].decode()


@dataclass
class _Snapshot:
    resource_id: uuid.UUID
    revision_id: str
    surface: str
    path: str
    text: str
    digest: str
    byte_size: int
    action: str = "create"
    parent_revision_id: str | None = None
    occurred_at: datetime = datetime(2026, 8, 10, tzinfo=UTC)


class _NoWriteNative:
    def __init__(
        self,
        *entries: tuple[LegacyInventoryDocument, str],
    ):
        self.entries = {document.resource_id: (document, native_id) for document, native_id in entries}
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.snapshot_overrides: dict[tuple[uuid.UUID, str], _Snapshot] = {}

    async def get_resource_revision(self, **kwargs) -> _Snapshot:
        self.calls.append(("get_resource_revision", kwargs))
        document, native_id = self.entries[kwargs["resource_id"]]
        override = self.snapshot_overrides.get((kwargs["resource_id"], kwargs["revision_id"]))
        if override is not None:
            return override
        assert kwargs["revision_id"] == native_id
        body = _document_body(document)
        return _Snapshot(
            resource_id=document.resource_id,
            revision_id=native_id,
            surface="document",
            path=document.current_path,
            text=body.decode(),
            digest=document.body_digest,
            byte_size=len(body),
            occurred_at=document.activity.committed_at,
        )


class _NoWriteNativeRepository:
    def __init__(self, *entries: tuple[LegacyInventoryDocument, str]):
        self.entries = {document.resource_id: (document, native_id) for document, native_id in entries}
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.history_overrides: dict[uuid.UUID, list[dict[str, Any]]] = {}
        self.revision_overrides: dict[uuid.UUID, dict[str, Any] | None] = {}
        self.revision_selector_overrides: dict[tuple[uuid.UUID, str], dict[str, Any] | None] = {}
        self.activity_override: dict[str, Any] | None = None

    def _revision(self, resource_id: uuid.UUID) -> dict[str, Any]:
        document, native_id = self.entries[resource_id]
        return {
            "namespace_id": uuid.UUID("22222222-2222-4222-8222-222222222222"),
            "resource_id": document.resource_id,
            "revision_id": native_id,
            "parent_revision_id": None,
            "surface": "document",
            "action": "create",
            "path_at_revision": document.current_path,
            "path_from": None,
            "path_to": document.current_path,
            "message": "C9 fixed-ref native genesis",
            "subject": None,
            "summary": None,
            "actor": "akb-native-revision-migration",
            "occurred_at": document.activity.committed_at,
            "digest": document.body_digest,
            "byte_size": document.byte_size,
        }

    async def list_history(self, **kwargs) -> list[dict[str, Any]]:
        self.calls.append(("list_history", kwargs))
        if kwargs["resource_id"] in self.history_overrides:
            return self.history_overrides[kwargs["resource_id"]]
        return [self._revision(kwargs["resource_id"])]

    async def get_revision(self, **kwargs) -> dict[str, Any] | None:
        self.calls.append(("get_revision", kwargs))
        key = (kwargs["resource_id"], kwargs["revision_id"])
        if key in self.revision_selector_overrides:
            return self.revision_selector_overrides[key]
        if kwargs["resource_id"] in self.revision_overrides:
            return self.revision_overrides[kwargs["resource_id"]]
        row = self._revision(kwargs["resource_id"])
        return row if row["revision_id"] == kwargs["revision_id"] else None

    async def get_activity_for_revision(self, **kwargs) -> dict[str, Any] | None:
        self.calls.append(("get_activity_for_revision", kwargs))
        if self.activity_override is not None:
            return self.activity_override
        row = self._revision(kwargs["resource_id"])
        if row["revision_id"] != kwargs["revision_id"]:
            return None
        return {
            "resource_id": row["resource_id"],
            "revision_id": row["revision_id"],
            "action": row["action"],
            "actor": row["actor"],
            "subject": row["subject"],
            "summary": row["summary"],
            "changed_path_from": row["path_from"],
            "changed_path_to": row["path_to"],
            "occurred_at": row["occurred_at"],
            "path_at_revision": row["path_at_revision"],
        }


class _CompletedSelectorBridge:
    def __init__(
        self,
        document: LegacyInventoryDocument,
        native_id: str,
        fixed_ref: str,
        *,
        native_mappings: dict[str, str] | None = None,
    ):
        self.document = document
        self.native_id = native_id
        self.fixed_ref = fixed_ref
        self.native_mappings = {
            document.current_commit: native_id,
            **(native_mappings or {}),
        }
        self.deleted: set[str] = set()
        self.corrupt_paths: set[str] = set()

    async def resolve_selector(
        self,
        *,
        resource_id: uuid.UUID,
        selector: str,
    ) -> SelectorResolution:
        assert resource_id == self.document.resource_id
        if selector in self.deleted:
            raise SelectorUnknownError(selector)
        if selector == self.native_id or selector in self.native_mappings.values():
            return SelectorResolution(
                resource_id=resource_id,
                selector=selector,
                kind="native",
                native_revision_id=selector,
                fixed_git_oid=None,
                legacy_git_oid=None,
                path_at_revision=self.document.current_path,
                run_id=None,
            )
        entry = next(entry for entry in self.document.lineage if entry.legacy_git_oid == selector)
        mapped_native = self.native_mappings.get(selector)
        return SelectorResolution(
            resource_id=resource_id,
            selector=selector,
            kind="native" if mapped_native is not None else "bridge",
            native_revision_id=mapped_native,
            fixed_git_oid=self.fixed_ref,
            legacy_git_oid=selector,
            path_at_revision=(
                "notes/corrupt-retained.md" if selector in self.corrupt_paths else entry.path_at_revision
            ),
            run_id=uuid.UUID("33333333-3333-4333-8333-333333333333"),
        )


class _SelectorBridgeRouter:
    def __init__(self, *bridges: _CompletedSelectorBridge):
        self.bridges = {bridge.document.resource_id: bridge for bridge in bridges}

    async def resolve_selector(self, *, resource_id: uuid.UUID, selector: str) -> SelectorResolution:
        return await self.bridges[resource_id].resolve_selector(
            resource_id=resource_id,
            selector=selector,
        )


def _activity_for_revision(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "resource_id": row["resource_id"],
        "revision_id": row["revision_id"],
        "action": row["action"],
        "actor": row["actor"],
        "subject": row["subject"],
        "summary": row["summary"],
        "changed_path_from": row["path_from"],
        "changed_path_to": row["path_to"],
        "occurred_at": row["occurred_at"],
        "path_at_revision": row["path_at_revision"],
    }


def _native_activity_reader(
    document: LegacyInventoryDocument,
    native_id: str,
    fixed_ref: str,
    service: _NoWriteNative,
    repository: _NoWriteNativeRepository,
    *,
    native_mappings: dict[str, str] | None = None,
) -> NativeRevisionShadowReader:
    return NativeRevisionShadowReader(
        pool=None,
        namespace_id=uuid.UUID("22222222-2222-4222-8222-222222222222"),
        vault_name="p2-manual",
        native_service=service,
        native_repository=repository,
        selector_bridge=_CompletedSelectorBridge(
            document,
            native_id,
            fixed_ref,
            native_mappings=native_mappings,
        ),
    )


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("action", "replace"),
        ("actor", "forged-actor"),
        ("subject", "forged-subject"),
        ("summary", "forged-summary"),
        ("path_from", "notes/forged-parent.md"),
        ("path_to", "notes/forged-current.md"),
        ("occurred_at", datetime(2026, 8, 11, tzinfo=UTC)),
    ],
)
async def test_genesis_activity_audit_rejects_forged_persisted_facts(field, value):
    document = _document()
    native_id = _oid("c")
    fixed_ref = _oid("f")
    service = _NoWriteNative((document, native_id))
    repository = _NoWriteNativeRepository((document, native_id))
    row = repository._revision(document.resource_id)
    forged = {**row, field: value}
    repository.revision_selector_overrides[(document.resource_id, native_id)] = forged
    repository.activity_override = _activity_for_revision(forged)
    snapshot = _Snapshot(
        resource_id=document.resource_id,
        revision_id=native_id,
        surface="document",
        path=document.current_path,
        text=_document_body(document).decode(),
        digest=document.body_digest,
        byte_size=document.byte_size,
        action=forged["action"],
        occurred_at=forged["occurred_at"],
    )
    service.snapshot_overrides[(document.resource_id, native_id)] = snapshot

    with pytest.raises(ShadowReaderScopeError, match="activity audit"):
        await _native_activity_reader(
            document,
            native_id,
            fixed_ref,
            service,
            repository,
        ).activity(document, selector=native_id, fixed_ref=fixed_ref)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("action", "replace"),
        ("actor", "forged-actor"),
        ("summary", "forged-summary"),
    ],
)
async def test_reconcile_activity_audit_derives_action_from_parent_and_current_paths(
    field,
    value,
):
    document = _document()
    native_id = _oid("c")
    parent_id = _oid("e")
    fixed_ref = _oid("f")
    old_path = "notes/old-migration-candidate.md"
    document = replace(
        document,
        lineage=(replace(document.lineage[0], path_at_revision=old_path), document.lineage[1]),
    )
    service = _NoWriteNative((document, native_id))
    repository = _NoWriteNativeRepository((document, native_id))
    selected = {
        **repository._revision(document.resource_id),
        "action": "move",
        "parent_revision_id": parent_id,
        "path_from": old_path,
        "path_to": document.current_path,
        "actor": document.activity.actor,
        "subject": document.activity.subject,
        "summary": document.activity.summary,
    }
    if field == "action":
        selected = {
            **selected,
            "action": value,
            "path_from": None,
            "path_to": None,
        }
    else:
        selected = {**selected, field: value}
    parent = {
        **repository._revision(document.resource_id),
        "revision_id": parent_id,
        "path_at_revision": old_path,
        "path_from": None,
        "path_to": old_path,
    }
    repository.revision_selector_overrides[(document.resource_id, native_id)] = selected
    repository.revision_selector_overrides[(document.resource_id, parent_id)] = parent
    repository.activity_override = _activity_for_revision(selected)
    repository.history_overrides[document.resource_id] = [selected, parent]
    service.snapshot_overrides[(document.resource_id, native_id)] = _Snapshot(
        resource_id=document.resource_id,
        revision_id=native_id,
        surface="document",
        path=document.current_path,
        text=_document_body(document).decode(),
        digest=document.body_digest,
        byte_size=document.byte_size,
        action=selected["action"],
        parent_revision_id=parent_id,
        occurred_at=selected["occurred_at"],
    )

    with pytest.raises(ShadowReaderScopeError, match="activity audit"):
        await _native_activity_reader(
            document,
            native_id,
            fixed_ref,
            service,
            repository,
            native_mappings={document.lineage[0].legacy_git_oid: parent_id},
        ).activity(document, selector=native_id, fixed_ref=fixed_ref)


async def test_reconcile_activity_audit_rejects_correlated_parent_path_tampering():
    document = _document()
    native_id = _oid("c")
    parent_id = _oid("e")
    fixed_ref = _oid("f")
    service = _NoWriteNative((document, native_id))
    repository = _NoWriteNativeRepository((document, native_id))
    forged_parent_path = "notes/forged-parent.md"
    selected = {
        **repository._revision(document.resource_id),
        "action": "move",
        "parent_revision_id": parent_id,
        "path_from": forged_parent_path,
        "path_to": document.current_path,
        "actor": document.activity.actor,
        "subject": document.activity.subject,
        "summary": document.activity.summary,
    }
    parent = {
        **repository._revision(document.resource_id),
        "revision_id": parent_id,
        "path_at_revision": forged_parent_path,
        "path_from": None,
        "path_to": forged_parent_path,
    }
    repository.revision_selector_overrides[(document.resource_id, native_id)] = selected
    repository.revision_selector_overrides[(document.resource_id, parent_id)] = parent
    repository.activity_override = _activity_for_revision(selected)
    service.snapshot_overrides[(document.resource_id, native_id)] = _Snapshot(
        resource_id=document.resource_id,
        revision_id=native_id,
        surface="document",
        path=document.current_path,
        text=_document_body(document).decode(),
        digest=document.body_digest,
        byte_size=document.byte_size,
        action="move",
        parent_revision_id=parent_id,
        occurred_at=selected["occurred_at"],
    )

    with pytest.raises(
        ShadowReaderScopeError,
        match="parent path differs from its completed legacy mapping",
    ):
        await _native_activity_reader(
            document,
            native_id,
            fixed_ref,
            service,
            repository,
            native_mappings={document.lineage[0].legacy_git_oid: parent_id},
        ).activity(document, selector=native_id, fixed_ref=fixed_ref)


async def test_activity_audit_failure_prevents_shadow_projection(monkeypatch):
    document = _document()
    run, item, inventory, native_id = _run_and_item(document)
    service = _NoWriteNative((document, native_id))
    repository = _NoWriteNativeRepository((document, native_id))
    forged = {**repository._revision(document.resource_id), "actor": "forged-actor"}
    repository.revision_selector_overrides[(document.resource_id, native_id)] = forged
    repository.activity_override = _activity_for_revision(forged)
    native_candidate = _native_activity_reader(
        document,
        native_id,
        run.fixed_git_oid,
        service,
        repository,
    )
    candidate = _WireReader(native_id, candidate=True)
    candidate.activity_evidence = native_candidate.activity_evidence
    comparator = NativeRevisionShadowComparator(
        repository=_ReadRepository(run, item),
        bridge=_InventoryBridge(inventory),
        legacy_reader=_WireReader(native_id, candidate=False),
        candidate_reader=candidate,
    )
    projection_calls: list[object] = []

    def project(*args):
        del args
        projection_calls.append(object())
        raise AssertionError("activity projection ran after audit failure")

    monkeypatch.setattr(comparator, "_project_candidate_activity", project)

    with pytest.raises(ShadowReaderScopeError, match="activity audit"):
        await comparator.compare_run(run.run_id)
    assert projection_calls == []


async def test_product_readers_are_scoped_and_read_only():
    document = _document()
    second = _second_document()
    fixed_ref = _oid("f")
    native_id = _oid("c")
    second_native_id = _oid("9")
    git = _NoWriteGit(document, second)
    legacy = LegacyFixedRefShadowReader(git=git, vault_name="p2-manual")

    result = await legacy.get(document, selector=document.current_commit, fixed_ref=fixed_ref)
    await legacy.history(document, selector=document.current_commit, fixed_ref=fixed_ref)
    await legacy.diff(document, selector=document.current_commit, fixed_ref=fixed_ref)
    await legacy.activity(document, selector=document.current_commit, fixed_ref=fixed_ref)
    assert result["current_commit"] == document.current_commit
    assert result["content"] == "# Migration candidate\n\nsecret body"
    assert [name for name, _ in git.calls] == [
        "manual_fixed_ref_history",
        "file_diff",
        "read_file",
        "read_file",
    ]

    result["projection"]["revision"] = "caller-mutation"
    result["content"] = "caller-mutation"
    reread = await legacy.get(document, selector=document.current_commit, fixed_ref=fixed_ref)
    assert reread["projection"]["revision"] == document.current_commit
    assert reread["content"] == "# Migration candidate\n\nsecret body"
    assert len(git.calls) == 4

    await legacy.get(second, selector=second.current_commit, fixed_ref=fixed_ref)
    await legacy.history(second, selector=second.current_commit, fixed_ref=fixed_ref)
    await legacy.diff(second, selector=second.current_commit, fixed_ref=fixed_ref)
    await legacy.activity(second, selector=second.current_commit, fixed_ref=fixed_ref)
    assert len(git.calls) == 8

    await legacy.get(document, selector=document.current_commit, fixed_ref=fixed_ref)
    assert len(git.calls) == 9

    with pytest.raises(ShadowReaderScopeError):
        await legacy.get(document, selector=_oid("e"), fixed_ref=fixed_ref)
    assert len(git.calls) == 9

    native_service = _NoWriteNative(
        (document, native_id),
        (second, second_native_id),
    )
    native_repository = _NoWriteNativeRepository(
        (document, native_id),
        (second, second_native_id),
    )
    native = NativeRevisionShadowReader(
        pool=None,
        namespace_id=uuid.UUID("22222222-2222-4222-8222-222222222222"),
        vault_name="p2-manual",
        native_service=native_service,
        native_repository=native_repository,
        selector_bridge=_SelectorBridgeRouter(
            _CompletedSelectorBridge(document, native_id, fixed_ref),
            _CompletedSelectorBridge(second, second_native_id, fixed_ref),
        ),
    )
    candidate = await native.get(document, selector=native_id, fixed_ref=fixed_ref)
    await native.history(document, selector=native_id, fixed_ref=fixed_ref)
    await native.diff(document, selector=native_id, fixed_ref=fixed_ref)
    await native.activity(document, selector=native_id, fixed_ref=fixed_ref)
    assert candidate["current_commit"] == native_id
    assert [name for name, _ in native_service.calls] == ["get_resource_revision"]
    assert [name for name, _ in native_repository.calls] == [
        "list_history",
        "get_revision",
        "get_revision",
        "get_activity_for_revision",
    ]

    candidate["projection"]["revision"] = "caller-mutation"
    candidate["content"] = "caller-mutation"
    candidate_reread = await native.get(document, selector=native_id, fixed_ref=fixed_ref)
    assert candidate_reread["projection"]["revision"] == native_id
    assert candidate_reread["content"] == "# Migration candidate\n\nsecret body"
    assert len(native_service.calls) == 1
    assert len(native_repository.calls) == 4

    await native.get(second, selector=second_native_id, fixed_ref=fixed_ref)
    await native.history(second, selector=second_native_id, fixed_ref=fixed_ref)
    await native.diff(second, selector=second_native_id, fixed_ref=fixed_ref)
    await native.activity(second, selector=second_native_id, fixed_ref=fixed_ref)
    assert len(native_service.calls) == 2
    assert len(native_repository.calls) == 8

    await native.get(document, selector=native_id, fixed_ref=fixed_ref)
    assert len(native_service.calls) == 3


def _genesis_document(*, action: str, body: str) -> LegacyInventoryDocument:
    base = _document()
    current = base.lineage[-1]
    return replace(
        base,
        body_digest=hashlib.sha256(body.encode()).hexdigest(),
        byte_size=len(body.encode()),
        lineage=(current,),
        activity=replace(
            base.activity,
            action=action,
            summary=f"{action} migration candidate at fixed ref",
            changed_paths=({"change": action, "path_from": None, "path_to": base.current_path},),
        ),
    )


async def test_legacy_reader_treats_modified_singleton_lineage_as_logical_genesis():
    body = "# Migration candidate\n\nline 1\nline 2\nline 3\nline 4\nline 5\nline 6\nline 7\nline 8\nline 9\n"
    document = _genesis_document(action="update", body=body)
    path = document.current_path
    raw_diff = {
        "file": path,
        "commit": document.current_commit,
        "type": "modified",
        "diff": "\n".join(
            [
                f"--- a/{path}",
                f"+++ b/{path}",
                "@@ -5,7 +5,7 @@",
                *(f"-out-of-lineage-{index}" for index in range(1, 8)),
                *(f"+line {index}" for index in range(3, 10)),
            ]
        ),
    }
    git = _GenesisDiffGit(document, diff=raw_diff, body=body)

    result = await LegacyFixedRefShadowReader(git=git, vault_name="p2-manual").diff(
        document,
        selector=document.current_commit,
        fixed_ref=_oid("f"),
    )

    assert result["text"] == _canonical_transition("", body)
    assert git.parent_reads == []
    assert [name for name, _ in git.calls] == [
        "manual_fixed_ref_history",
        "file_diff",
        "read_file",
    ]


async def test_legacy_reader_rejects_malformed_added_genesis_patch():
    body = "# Migration candidate\n\ncurrent body\n"
    document = _genesis_document(action="create", body=body)
    path = document.current_path
    lines = body.splitlines()
    raw_diff = {
        "file": path,
        "commit": document.current_commit,
        "type": "added",
        "diff": "\n".join(
            [
                f"@@ -5,0 +1,{len(lines)} @@",
                *(f"+{line}" for line in lines),
            ]
        ),
    }
    git = _GenesisDiffGit(document, diff=raw_diff, body=body)

    with pytest.raises(ShadowReaderScopeError, match="invalid parent range"):
        await LegacyFixedRefShadowReader(git=git, vault_name="p2-manual").diff(
            document,
            selector=document.current_commit,
            fixed_ref=_oid("f"),
        )


async def test_legacy_reader_matches_inventory_after_subsecond_lineage_boundary():
    document = _document()
    created_at = document.created_at.replace(microsecond=487_280)
    current_at = created_at.replace(microsecond=0) + timedelta(seconds=1)
    document = replace(
        document,
        created_at=created_at,
        lineage=(
            LegacyLineageEntry(
                document.current_commit,
                document.current_path,
                current_at,
            ),
        ),
        activity=replace(document.activity, committed_at=current_at),
    )
    git = _NoWriteGit(document)
    raw = git.manual_fixed_ref_history(
        "p2-manual",
        _oid("f"),
        document.current_path,
        current_commit=document.current_commit,
    )
    raw["history"].append(
        {
            "legacy_git_oid": _oid("b"),
            "path_at_revision": document.current_path,
            # Git's second-resolution --since boundary can include this row,
            # while the C9 inventory excludes it against precise created_at.
            "committed_at": created_at.replace(microsecond=0),
        }
    )
    git.snapshot_override = raw

    history = await LegacyFixedRefShadowReader(
        git=git,
        vault_name="p2-manual",
    ).history(
        document,
        selector=document.current_commit,
        fixed_ref=_oid("f"),
    )

    assert [entry["selector"] for entry in history["entries"]] == [
        document.current_commit
    ]


async def test_legacy_reader_rejects_precreation_row_before_current_head():
    document = _document()
    fixed_ref = _oid("f")
    git = _NoWriteGit(document)
    raw = git.manual_fixed_ref_history(
        "p2-manual",
        fixed_ref,
        document.current_path,
        current_commit=document.current_commit,
    )
    raw["history"].insert(
        0,
        {
            "legacy_git_oid": _oid("0"),
            "path_at_revision": document.current_path,
            "committed_at": document.created_at - timedelta(microseconds=1),
        },
    )
    git.snapshot_override = raw

    with pytest.raises(ShadowReaderScopeError, match="history"):
        await LegacyFixedRefShadowReader(
            git=git,
            vault_name="p2-manual",
        ).history(document, selector=document.current_commit, fixed_ref=fixed_ref)


async def test_legacy_reader_fails_closed_on_missing_or_corrupt_product_facts():
    document = _document()
    fixed_ref = _oid("f")

    missing_history_git = _NoWriteGit(document)
    raw = missing_history_git.manual_fixed_ref_history(
        "p2-manual",
        fixed_ref,
        document.current_path,
        current_commit=document.current_commit,
    )
    missing_history_git.snapshot_override = {**raw, "history": raw["history"][:-1]}
    with pytest.raises(ShadowReaderScopeError, match="history"):
        await LegacyFixedRefShadowReader(
            git=missing_history_git,
            vault_name="p2-manual",
        ).history(document, selector=document.current_commit, fixed_ref=fixed_ref)

    corrupt_activity_git = _NoWriteGit(document)
    raw = corrupt_activity_git.manual_fixed_ref_history(
        "p2-manual",
        fixed_ref,
        document.current_path,
        current_commit=document.current_commit,
    )
    corrupt_activity_git.snapshot_override = {
        **raw,
        "activity": {**raw["activity"], "actor": "wrong-actor"},
    }
    with pytest.raises(ShadowReaderScopeError, match="activity"):
        await LegacyFixedRefShadowReader(
            git=corrupt_activity_git,
            vault_name="p2-manual",
        ).activity(document, selector=document.current_commit, fixed_ref=fixed_ref)

    corrupt_diff_git = _NoWriteGit(document)
    corrupt_diff_git.diff_override = {
        "file": document.current_path,
        "commit": document.current_commit,
        "type": "modified",
        "diff": "@@ -1,3 +1,3 @@\n # Migration candidate\n \n-old body\n+forged body",
    }
    with pytest.raises(ShadowReaderScopeError, match="diff"):
        await LegacyFixedRefShadowReader(
            git=corrupt_diff_git,
            vault_name="p2-manual",
        ).diff(document, selector=document.current_commit, fixed_ref=fixed_ref)


async def test_native_reader_requires_completed_selector_bridge_and_retained_mappings():
    document = _document()
    native_id = _oid("c")
    fixed_ref = _oid("f")
    namespace_id = uuid.UUID("22222222-2222-4222-8222-222222222222")
    service = _NoWriteNative((document, native_id))
    repository = _NoWriteNativeRepository((document, native_id))

    with pytest.raises(ValueError, match="selector_bridge"):
        NativeRevisionShadowReader(
            pool=None,
            namespace_id=namespace_id,
            vault_name="p2-manual",
            native_service=service,
            native_repository=repository,
        )

    retained = document.lineage[0].legacy_git_oid
    deleted = _CompletedSelectorBridge(document, native_id, fixed_ref)
    deleted.deleted.add(retained)
    with pytest.raises(ShadowReaderScopeError, match="retained selector"):
        await NativeRevisionShadowReader(
            pool=None,
            namespace_id=namespace_id,
            vault_name="p2-manual",
            native_service=service,
            native_repository=repository,
            selector_bridge=deleted,
        ).history(document, selector=native_id, fixed_ref=fixed_ref)

    corrupt = _CompletedSelectorBridge(document, native_id, fixed_ref)
    corrupt.corrupt_paths.add(retained)
    with pytest.raises(ShadowReaderScopeError, match="retained selector"):
        await NativeRevisionShadowReader(
            pool=None,
            namespace_id=namespace_id,
            vault_name="p2-manual",
            native_service=service,
            native_repository=repository,
            selector_bridge=corrupt,
        ).history(document, selector=native_id, fixed_ref=fixed_ref)


async def test_native_reader_fails_closed_on_missing_corrupt_or_reordered_product_facts():
    document = _document()
    native_id = _oid("c")
    fixed_ref = _oid("f")
    namespace_id = uuid.UUID("22222222-2222-4222-8222-222222222222")
    service = _NoWriteNative((document, native_id))

    missing_history = _NoWriteNativeRepository((document, native_id))
    missing_history.history_overrides[document.resource_id] = []
    with pytest.raises(ShadowReaderScopeError, match="history"):
        await NativeRevisionShadowReader(
            pool=None,
            namespace_id=namespace_id,
            vault_name="p2-manual",
            native_service=service,
            native_repository=missing_history,
            selector_bridge=_CompletedSelectorBridge(document, native_id, fixed_ref),
        ).history(document, selector=native_id, fixed_ref=fixed_ref)

    reordered_history = _NoWriteNativeRepository((document, native_id))
    current = reordered_history._revision(document.resource_id)
    older = {
        **current,
        "revision_id": _oid("9"),
        "parent_revision_id": None,
        "occurred_at": current["occurred_at"] - timedelta(seconds=1),
    }
    current = {**current, "parent_revision_id": older["revision_id"]}
    reordered_history.history_overrides[document.resource_id] = [older, current]
    with pytest.raises(ShadowReaderScopeError, match="history"):
        await NativeRevisionShadowReader(
            pool=None,
            namespace_id=namespace_id,
            vault_name="p2-manual",
            native_service=service,
            native_repository=reordered_history,
            selector_bridge=_CompletedSelectorBridge(document, native_id, fixed_ref),
        ).history(document, selector=native_id, fixed_ref=fixed_ref)

    corrupt_diff = _NoWriteNativeRepository((document, native_id))
    corrupt_diff.revision_overrides[document.resource_id] = {
        **corrupt_diff._revision(document.resource_id),
        "path_at_revision": "wrong.md",
    }
    with pytest.raises(ShadowReaderScopeError, match="diff"):
        await NativeRevisionShadowReader(
            pool=None,
            namespace_id=namespace_id,
            vault_name="p2-manual",
            native_service=service,
            native_repository=corrupt_diff,
            selector_bridge=_CompletedSelectorBridge(document, native_id, fixed_ref),
        ).diff(document, selector=native_id, fixed_ref=fixed_ref)

    corrupt_activity = _NoWriteNativeRepository((document, native_id))
    activity = await corrupt_activity.get_activity_for_revision(
        namespace_id=namespace_id,
        surface="document",
        resource_id=document.resource_id,
        revision_id=native_id,
    )
    assert activity is not None
    corrupt_activity.activity_override = {**activity, "action": "move"}
    with pytest.raises(ShadowReaderScopeError, match="activity"):
        await NativeRevisionShadowReader(
            pool=None,
            namespace_id=namespace_id,
            vault_name="p2-manual",
            native_service=service,
            native_repository=corrupt_activity,
            selector_bridge=_CompletedSelectorBridge(document, native_id, fixed_ref),
        ).activity(document, selector=native_id, fixed_ref=fixed_ref)


def _native_replace_fakes(
    document: LegacyInventoryDocument,
    native_id: str,
    parent_id: str,
) -> tuple[_NoWriteNative, _NoWriteNativeRepository, dict[str, Any], dict[str, Any]]:
    service = _NoWriteNative((document, native_id))
    repository = _NoWriteNativeRepository((document, native_id))
    old_text = _document_body(document).decode().replace("secret body", "old body")
    old_bytes = old_text.encode()
    selected_row = {
        **repository._revision(document.resource_id),
        "action": "replace",
        "parent_revision_id": parent_id,
    }
    parent_row = {
        **repository._revision(document.resource_id),
        "revision_id": parent_id,
        "action": "create",
        "parent_revision_id": None,
        "digest": hashlib.sha256(old_bytes).hexdigest(),
        "byte_size": len(old_bytes),
    }
    repository.revision_selector_overrides[(document.resource_id, native_id)] = selected_row
    repository.revision_selector_overrides[(document.resource_id, parent_id)] = parent_row
    repository.history_overrides[document.resource_id] = [selected_row, parent_row]
    service.snapshot_overrides[(document.resource_id, native_id)] = _Snapshot(
        resource_id=document.resource_id,
        revision_id=native_id,
        surface="document",
        path=document.current_path,
        text=_document_body(document).decode(),
        digest=document.body_digest,
        byte_size=document.byte_size,
        action="replace",
        parent_revision_id=parent_id,
        occurred_at=document.activity.committed_at,
    )
    service.snapshot_overrides[(document.resource_id, parent_id)] = _Snapshot(
        resource_id=document.resource_id,
        revision_id=parent_id,
        surface="document",
        path=document.current_path,
        text=old_text,
        digest=parent_row["digest"],
        byte_size=parent_row["byte_size"],
        action="create",
        parent_revision_id=None,
        occurred_at=document.activity.committed_at,
    )
    return service, repository, selected_row, parent_row


async def test_native_history_follows_parent_lineage_with_equal_timestamps():
    document = _document()
    native_id = _oid("c")
    parent_id = _oid("e")
    fixed_ref = _oid("f")
    service, repository, _, _ = _native_replace_fakes(document, native_id, parent_id)
    bridge = _CompletedSelectorBridge(
        document,
        native_id,
        fixed_ref,
        native_mappings={document.lineage[0].legacy_git_oid: parent_id},
    )

    result = await NativeRevisionShadowReader(
        pool=None,
        namespace_id=uuid.UUID("22222222-2222-4222-8222-222222222222"),
        vault_name="p2-manual",
        native_service=service,
        native_repository=repository,
        selector_bridge=bridge,
    ).history(document, selector=native_id, fixed_ref=fixed_ref)

    assert result["history_source"] == "fixed-ref-bridge"


async def test_native_history_rejects_invalid_parent_graph_shapes():
    document = _document()
    native_id = _oid("c")
    parent_id = _oid("e")
    fixed_ref = _oid("f")
    namespace_id = uuid.UUID("22222222-2222-4222-8222-222222222222")
    bridge = _CompletedSelectorBridge(document, native_id, fixed_ref)

    for expected, shape in (
        ("cycle", "cycle"),
        ("missing", "missing"),
        ("invalid selectors", "duplicate"),
        ("disconnected", "extra"),
    ):
        service = _NoWriteNative((document, native_id))
        repository = _NoWriteNativeRepository((document, native_id))
        selected = repository._revision(document.resource_id)
        if shape in {"cycle", "missing"}:
            selected = {
                **selected,
                "action": "replace",
                "parent_revision_id": native_id if shape == "cycle" else parent_id,
            }
            service.snapshot_overrides[(document.resource_id, native_id)] = _Snapshot(
                resource_id=document.resource_id,
                revision_id=native_id,
                surface="document",
                path=document.current_path,
                text=_document_body(document).decode(),
                digest=document.body_digest,
                byte_size=document.byte_size,
                action="replace",
                parent_revision_id=selected["parent_revision_id"],
                occurred_at=document.activity.committed_at,
            )
        if shape == "duplicate":
            history = [selected, dict(selected)]
        elif shape == "extra":
            history = [
                selected,
                {
                    **selected,
                    "revision_id": parent_id,
                },
            ]
        else:
            history = [selected]
        repository.history_overrides[document.resource_id] = history

        with pytest.raises(ShadowReaderScopeError, match=expected):
            await NativeRevisionShadowReader(
                pool=None,
                namespace_id=namespace_id,
                vault_name="p2-manual",
                native_service=service,
                native_repository=repository,
                selector_bridge=bridge,
            ).history(document, selector=native_id, fixed_ref=fixed_ref)


async def test_native_diff_binds_selected_and_parent_rows_to_persisted_bodies():
    document = _document()
    native_id = _oid("c")
    parent_id = _oid("e")
    fixed_ref = _oid("f")
    namespace_id = uuid.UUID("22222222-2222-4222-8222-222222222222")
    bridge = _CompletedSelectorBridge(
        document,
        native_id,
        fixed_ref,
        native_mappings={document.lineage[0].legacy_git_oid: parent_id},
    )

    corrupt_current_service, corrupt_current_repo, selected_row, _ = _native_replace_fakes(
        document, native_id, parent_id
    )
    corrupt_current_repo.revision_selector_overrides[(document.resource_id, native_id)] = {
        **selected_row,
        "digest": "0" * 64,
    }
    with pytest.raises(ShadowReaderScopeError, match="selected.*body facts"):
        await NativeRevisionShadowReader(
            pool=None,
            namespace_id=namespace_id,
            vault_name="p2-manual",
            native_service=corrupt_current_service,
            native_repository=corrupt_current_repo,
            selector_bridge=bridge,
        ).diff(document, selector=native_id, fixed_ref=fixed_ref)

    corrupt_parent_service, corrupt_parent_repo, _, parent_row = _native_replace_fakes(document, native_id, parent_id)
    corrupt_parent_service.snapshot_overrides[(document.resource_id, parent_id)] = replace(
        corrupt_parent_service.snapshot_overrides[(document.resource_id, parent_id)],
        text="# Migration candidate\n\nforged parent\n",
        digest=parent_row["digest"],
        byte_size=parent_row["byte_size"],
    )
    with pytest.raises(ShadowReaderScopeError, match="parent.*body facts"):
        await NativeRevisionShadowReader(
            pool=None,
            namespace_id=namespace_id,
            vault_name="p2-manual",
            native_service=corrupt_parent_service,
            native_repository=corrupt_parent_repo,
            selector_bridge=bridge,
        ).diff(document, selector=native_id, fixed_ref=fixed_ref)


async def test_failed_cache_replacement_evicts_the_prior_resource():
    document = _document()
    invalid = replace(_second_document(), body_digest="0" * 64)
    fixed_ref = _oid("f")
    native_id = _oid("c")
    invalid_native_id = _oid("9")

    git = _NoWriteGit(document, invalid)
    legacy = LegacyFixedRefShadowReader(git=git, vault_name="p2-manual")
    await legacy.get(document, selector=document.current_commit, fixed_ref=fixed_ref)
    with pytest.raises(ShadowReaderScopeError, match="differs from C9 inventory"):
        await legacy.get(invalid, selector=invalid.current_commit, fixed_ref=fixed_ref)
    await legacy.get(document, selector=document.current_commit, fixed_ref=fixed_ref)
    assert len(git.calls) == 3

    native_service = _NoWriteNative(
        (document, native_id),
        (invalid, invalid_native_id),
    )
    native = NativeRevisionShadowReader(
        pool=None,
        namespace_id=uuid.UUID("22222222-2222-4222-8222-222222222222"),
        vault_name="p2-manual",
        native_service=native_service,
        native_repository=_NoWriteNativeRepository(
            (document, native_id),
            (invalid, invalid_native_id),
        ),
        selector_bridge=_CompletedSelectorBridge(document, native_id, fixed_ref),
    )
    await native.get(document, selector=native_id, fixed_ref=fixed_ref)
    with pytest.raises(ShadowReaderScopeError, match="body differs from C9 inventory"):
        await native.get(invalid, selector=invalid_native_id, fixed_ref=fixed_ref)
    await native.get(document, selector=native_id, fixed_ref=fixed_ref)
    assert len(native_service.calls) == 3


async def test_legacy_shadow_reader_retains_at_most_one_materialized_body():
    first = _document()
    second = _second_document()
    fixed_ref = _oid("f")
    git = _NoWriteGit(first, second)
    reader = LegacyFixedRefShadowReader(git=git, vault_name="p2-manual")

    await reader.get(first, selector=first.current_commit, fixed_ref=fixed_ref)
    assert reader._snapshot_cache is not None
    assert reader._snapshot_cache[0].resource_id == first.resource_id

    await reader.get(second, selector=second.current_commit, fixed_ref=fixed_ref)
    assert reader._snapshot_cache is not None
    assert reader._snapshot_cache[0].resource_id == second.resource_id
    assert len(git.calls) == 2


async def test_comparator_receipt_is_redacted_and_classified():
    document = _document()
    run, item, inventory, native_id = _run_and_item(document)
    comparator = NativeRevisionShadowComparator(
        repository=_ReadRepository(run, item),
        bridge=_InventoryBridge(inventory),
        legacy_reader=_WireReader(native_id, candidate=False),
        candidate_reader=_WireReader(native_id, candidate=True),
    )

    receipt = await comparator.compare_run(run.run_id)
    encoded = json.dumps(receipt, sort_keys=True)

    assert _document_body(document).decode() not in encoded
    assert document.current_path not in encoded
    assert str(document.resource_id) not in encoded
    assert document.activity.actor not in encoded
    assert document.activity.subject not in encoded
    assert document.activity.summary not in encoded
    assert str(run.run_id) not in encoded
    assert "raw_candidate" not in receipt
    assert receipt["schema_version"] == 4
    assert receipt["protocol_version"] == "akb-native-revision-p2-w1-c10/v4"
    assert "legacy" not in receipt["resources"][0]["operations"]["get"]
    assert receipt["summary"]["unexplained_mismatch_count"] == 0
    assert receipt["summary"]["mismatch_count"] == 12
    assert receipt["summary"]["raw_activity_audit_count"] == 1
    assert receipt["evidence_binding"]["mapping_count"] == 1
    assert receipt["evidence_binding"]["retained_mapping_count"] == 1
    assert receipt["evidence_binding"]["native_parent_binding_count"] == 1
    assert receipt["evidence_binding"]["owner_run_count"] == 1
    assert receipt["evidence_binding"]["scheme"] == "sha256"
    assert receipt["evidence_binding"]["canonicalization"] == ("utf8-json-sort-keys-no-whitespace-v1")
    assert receipt["evidence_binding"]["domain"] == ("akb-native-revision-p2-w1-c10/evidence-binding/v1")
    components = receipt["evidence_binding"]["components"]
    assert set(components) == {
        "comparison_run",
        "mapping_owner_activity",
        "retained_mapping_closure",
        "native_parent_bindings",
        "owner_runs",
    }
    assert len(components["comparison_run"]) == 64
    assert len(components["mapping_owner_activity"]) == 1
    assert len(components["retained_mapping_closure"]) == 1
    assert len(components["native_parent_bindings"]) == 1
    assert components["owner_runs"] == sorted(set(components["owner_runs"]))
    assert receipt["evidence_binding"]["owner_run_count"] == len(
        components["owner_runs"]
    )
    assert all(
        len(commitment) == 64 and set(commitment) <= set("0123456789abcdef")
        for commitment in components["owner_runs"]
    )
    recomputed = hashlib.sha256(
        (receipt["evidence_binding"]["domain"] + "\0").encode()
        + json.dumps(
            components,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()
    assert receipt["evidence_binding"]["commitment"] == recomputed
    assert len(receipt["evidence_binding"]["commitment"]) == 64
    assert all(
        resource["operations"]["activity"]["raw_activity_audit"]
        == {
            "profile": "akb-native-revision-p2-activity-audit/v1",
            "status": "passed",
        }
        for resource in receipt["resources"]
    )
    assert receipt["resources"][0]["operations"]["get"]["classified_mismatches"] == [
        {
            "rule_id": "BR-01",
            "classification": "revision_token",
            "count": 1,
        },
        {
            "rule_id": "BR-04",
            "classification": "projection_revision",
            "count": 1,
        },
        {
            "rule_id": "BR-05",
            "classification": "formatting_only",
            "count": 1,
        },
    ]
    assert document.current_commit not in encoded
    assert run.fixed_git_oid not in encoded
    assert run.inventory_digest not in encoded


async def test_owner_run_count_tamper_cannot_survive_correlated_recompute():
    document = _document()
    run, item, inventory, native_id = _run_and_item(document)
    comparator = NativeRevisionShadowComparator(
        repository=_ReadRepository(run, item),
        bridge=_InventoryBridge(inventory),
        legacy_reader=_WireReader(native_id, candidate=False),
        candidate_reader=_WireReader(native_id, candidate=True),
    )
    receipt = await comparator.compare_run(run.run_id)

    count_tamper = json.loads(json.dumps(receipt))
    count_tamper["evidence_binding"]["owner_run_count"] += 1
    count_tamper.pop("receipt_digest")
    count_tamper["receipt_digest"] = hashlib.sha256(
        json.dumps(
            count_tamper,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()
    assert count_tamper["evidence_binding"]["owner_run_count"] != len(
        count_tamper["evidence_binding"]["components"]["owner_runs"]
    )

    component_tamper = json.loads(json.dumps(receipt))
    component_tamper["evidence_binding"]["components"]["owner_runs"].append("0" * 64)
    component_tamper["evidence_binding"]["commitment"] = hashlib.sha256(
        (component_tamper["evidence_binding"]["domain"] + "\0").encode()
        + json.dumps(
            component_tamper["evidence_binding"]["components"],
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()
    component_tamper.pop("receipt_digest")
    component_tamper["receipt_digest"] = hashlib.sha256(
        json.dumps(
            component_tamper,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()
    assert component_tamper["evidence_binding"]["owner_run_count"] != len(
        component_tamper["evidence_binding"]["components"]["owner_runs"]
    )


async def test_comparator_rejects_correlated_activity_fact_tamper():
    document = _document()
    run, item, inventory, native_id = _run_and_item(document)
    changed_candidate = _WireReader(native_id, candidate=True)
    original_evidence = changed_candidate.activity_evidence

    async def changed_evidence(*args, **kwargs):
        evidence = await original_evidence(*args, **kwargs)
        return NativeActivityEvidence(
            envelope=evidence.envelope,
            binding_fact={
                **evidence.binding_fact,
                "selected_revision": {
                    **evidence.binding_fact["selected_revision"],
                    "resource_id": str(uuid.uuid4()),
                },
            },
        )

    changed_candidate.activity_evidence = changed_evidence

    comparator = NativeRevisionShadowComparator(
        repository=_ReadRepository(run, item),
        bridge=_InventoryBridge(inventory),
        legacy_reader=_WireReader(native_id, candidate=False),
        candidate_reader=changed_candidate,
    )

    with pytest.raises(ShadowComparisonError, match="not correlated"):
        await comparator.compare_run(run.run_id)


async def test_comparator_requires_a_candidate_raw_activity_auditor():
    document = _document()
    run, item, inventory, native_id = _run_and_item(document)
    candidate = _WireReader(native_id, candidate=True)
    candidate.activity_evidence = None
    comparator = NativeRevisionShadowComparator(
        repository=_ReadRepository(run, item),
        bridge=_InventoryBridge(inventory),
        legacy_reader=_WireReader(native_id, candidate=False),
        candidate_reader=candidate,
    )

    with pytest.raises(
        ShadowComparisonError,
        match="cannot prove the raw native activity audit",
    ):
        await comparator.compare_run(run.run_id)


async def test_comparator_rejects_profile_only_activity_evidence():
    document = _document()
    run, item, inventory, native_id = _run_and_item(document)
    candidate = _WireReader(native_id, candidate=True)
    original_evidence = candidate.activity_evidence

    async def profile_only(*args, **kwargs):
        evidence = await original_evidence(*args, **kwargs)
        return NativeActivityEvidence(
            envelope=evidence.envelope,
            binding_fact={"profile": "akb-native-revision-p2-activity-audit/v1"},
        )

    candidate.activity_evidence = profile_only
    comparator = NativeRevisionShadowComparator(
        repository=_ReadRepository(run, item),
        bridge=_InventoryBridge(inventory),
        legacy_reader=_WireReader(native_id, candidate=False),
        candidate_reader=candidate,
    )

    with pytest.raises(ShadowComparisonError, match="activity audit schema"):
        await comparator.compare_run(run.run_id)


async def test_product_readers_compare_a_completed_pg_backfill_without_writes(tmp_path):
    async with _fresh_schema(tmp_path) as pool:
        git = GitService(storage_path=str(tmp_path / "git"))
        vault_name = f"shadow-{uuid.uuid4().hex}"
        git.init_vault(vault_name)
        old_body = "---\ntitle: Migration candidate\n---\n\nold body"
        current_body = "---\ntitle: Migration candidate\n---\n\ncurrent body"
        git.commit_file(
            vault_name,
            "notes/migration-candidate.md",
            old_body,
            "[create] migration-candidate.md\n\nagent: legacy-writer\naction: create\nsummary: create migration candidate",
        )
        await asyncio.sleep(1.05)
        current_oid = git.commit_file(
            vault_name,
            "notes/migration-candidate.md",
            current_body,
            "[update] migration-candidate.md\n\nagent: legacy-writer\naction: update\nsummary: update migration candidate",
        )
        current_at = Repo(str(git._bare_path(vault_name))).commit(current_oid).committed_datetime
        await asyncio.sleep(1.05)
        fixed_ref = git.commit_file(vault_name, "unrelated.md", "unrelated\n", "unrelated fixed-ref tip")
        resource_id = uuid.uuid4()
        async with pool.acquire() as conn:
            namespace_id = await conn.fetchval(
                """
                INSERT INTO vaults (name, git_path, status)
                VALUES ($1, $2, 'active')
                RETURNING id
                """,
                vault_name,
                str(git._bare_path(vault_name)),
            )
            await conn.execute(
                """
                INSERT INTO documents
                    (id, vault_id, path, title, created_at, updated_at, current_commit, source)
                VALUES ($1, $2, $3, $4, $5, $5, $6, 'manual')
                """,
                resource_id,
                namespace_id,
                "notes/migration-candidate.md",
                "Migration candidate",
                current_at - timedelta(seconds=2),
                current_oid,
            )

        bridge = LegacyRevisionBridge(pool, git=git)
        backfill = NativeRevisionBackfill(pool, git=git, bridge=bridge)
        run, inventory = await backfill.prepare_run(
            namespace_id=namespace_id,
            fixed_ref=fixed_ref,
            coverage_version="p2-shadow-reader-test-v1",
        )
        assert len(inventory.documents) == 1
        result = await backfill.backfill_run(run.run_id)
        assert result.status == "complete"
        item = await backfill.repository.get_item(run.run_id, resource_id)
        assert item is not None and item.native_head_revision_id is not None

        legacy = LegacyFixedRefShadowReader(git=git, vault_name=vault_name)
        candidate = NativeRevisionShadowReader(
            pool,
            namespace_id=namespace_id,
            vault_name=vault_name,
            selector_bridge=bridge,
        )
        before = await _authority_counts(pool)
        comparator = NativeRevisionShadowComparator(
            pool=pool,
            bridge=bridge,
            legacy_reader=legacy,
            candidate_reader=candidate,
        )
        receipt = await comparator.compare_run(run.run_id)
        after = await _authority_counts(pool)

        assert before == after
        assert receipt["status"] == "passed"
        assert receipt["summary"]["resource_count"] == 1
        assert receipt["summary"]["operation_count"] == 4
        assert receipt["summary"]["unexplained_mismatch_count"] == 0
        encoded = json.dumps(receipt, sort_keys=True)
        assert current_body not in encoded
        assert "notes/migration-candidate.md" not in encoded
        assert str(resource_id) not in encoded
        assert str(run.run_id) not in encoded
        assert fixed_ref not in encoded


async def test_reconcile_c10_resolves_changed_and_unchanged_heads_across_runs(tmp_path):
    async with _fresh_schema(tmp_path) as pool:
        git = GitService(storage_path=str(tmp_path / "git"))
        vault_name = f"shadow-reconcile-{uuid.uuid4().hex}"
        git.init_vault(vault_name)
        first_oid = git.commit_file(
            vault_name,
            "first.md",
            "first r1\n",
            "[create] first.md\n\nagent: legacy-writer\naction: create\nsummary: create first",
        )
        second_oid = git.commit_file(
            vault_name,
            "second.md",
            "second r1\n",
            "[create] second.md\n\nagent: legacy-writer\naction: create\nsummary: create second",
        )
        fixed_r1 = git.commit_file(vault_name, "r1-tip.md", "tip\n", "fixed R1 tip")
        bare = Repo(str(git._bare_path(vault_name)))
        committed = {
            "first.md": bare.commit(first_oid).committed_datetime,
            "second.md": bare.commit(second_oid).committed_datetime,
        }
        resource_ids = {path: uuid.uuid4() for path in committed}
        async with pool.acquire() as conn:
            namespace_id = await conn.fetchval(
                """
                INSERT INTO vaults (name, git_path, status)
                VALUES ($1, $2, 'active')
                RETURNING id
                """,
                vault_name,
                str(git._bare_path(vault_name)),
            )
            for path, oid in (("first.md", first_oid), ("second.md", second_oid)):
                await conn.execute(
                    """
                    INSERT INTO documents
                        (id, vault_id, path, title, created_at, updated_at,
                         current_commit, source)
                    VALUES ($1, $2, $3, $3, $4, $4, $5, 'manual')
                    """,
                    resource_ids[path],
                    namespace_id,
                    path,
                    committed[path] - timedelta(seconds=1),
                    oid,
                )

        bridge = LegacyRevisionBridge(pool, git=git)
        backfill = NativeRevisionBackfill(pool, git=git, bridge=bridge)
        initial_run, initial_inventory = await backfill.prepare_run(
            namespace_id=namespace_id,
            fixed_ref=fixed_r1,
            coverage_version="p2-shadow-reconcile-initial-v1",
        )
        assert len(initial_inventory.documents) == 2
        assert (await backfill.backfill_run(initial_run.run_id)).status == "complete"

        changed_oid = git.commit_file(
            vault_name,
            "first.md",
            "first r2\n",
            "[update] first.md\n\nagent: legacy-writer\naction: update\nsummary: update first",
        )
        fixed_r2 = git.commit_file(vault_name, "r2-tip.md", "tip\n", "fixed R2 tip")
        async with pool.acquire() as conn:
            await conn.execute(
                """
                UPDATE documents
                   SET current_commit = $2, updated_at = NOW()
                 WHERE id = $1
                """,
                resource_ids["first.md"],
                changed_oid,
            )

        reconciler = NativeRevisionReconcile(pool, git=git, bridge=bridge)
        final = await reconciler.reconcile(namespace_id=namespace_id, fixed_ref=fixed_r2)
        assert final.status == "complete"
        assert final.changed_items == 1
        assert final.unchanged_items == 1
        assert len(await backfill.repository.list_items(final.run_id)) == 1

        before = await _authority_counts(pool)
        comparator = NativeRevisionShadowComparator(
            pool=pool,
            bridge=bridge,
            legacy_reader=LegacyFixedRefShadowReader(git=git, vault_name=vault_name),
            candidate_reader=NativeRevisionShadowReader(
                pool,
                namespace_id=namespace_id,
                vault_name=vault_name,
                selector_bridge=bridge,
            ),
        )
        receipt = await comparator.compare_run(final.run_id)
        assert await _authority_counts(pool) == before
        assert receipt["status"] == "passed"
        assert receipt["summary"]["resource_count"] == 2
        assert receipt["summary"]["operation_count"] == 8
