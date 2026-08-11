"""Focused tests for the product-owned P2 shadow-read seam."""

from __future__ import annotations

import asyncio
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
)
from app.services.native_revision_shadow import NativeRevisionShadowComparator
from app.services.native_revision_shadow_reader import (
    LegacyFixedRefShadowReader,
    NativeRevisionShadowReader,
    ShadowReaderScopeError,
)
from app.services.native_revision_backfill import NativeRevisionBackfill


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
    except (OSError, asyncpg.PostgresError):
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
        return tuple([
            (table, int(await conn.fetchval(f'SELECT count(*) FROM "{table}"')))
            for table in tables
        ])


def _oid(char: str) -> str:
    return char * 40


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
        body=body.encode(),
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
        body=body,
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
            changed_paths=(
                {"change": "update", "path_from": None, "path_to": path},
            ),
        ),
    )


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


class _InventoryBridge:
    def __init__(self, inventory: LegacyInventory):
        self.inventory = inventory

    async def inventory_for_run(self, run: MigrationRun) -> LegacyInventory:
        assert run.inventory_digest == self.inventory.inventory_digest
        return self.inventory


class _WireReader:
    def __init__(self, native_id: str, *, candidate: bool):
        self.native_id = native_id
        self.candidate = candidate

    def _selector(self, document: LegacyInventoryDocument) -> str:
        return self.native_id if self.candidate else document.current_commit

    async def get(self, document: LegacyInventoryDocument, *, selector: str, fixed_ref: str) -> dict[str, Any]:
        del fixed_ref
        body = document.body.decode()
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
        return {
            "events": [
                {
                    "hash": selector,
                    "subject": activity.subject,
                    "author": {
                        "id": activity.actor,
                        "display": "Legacy Writer (Git)" if not self.candidate else "Migration",
                    },
                    "action": activity.action if not self.candidate else "create",
                    "summary": activity.summary if not self.candidate else "C9 fixed-ref native genesis",
                    "projection_revision": selector,
                }
            ]
        }


class _NoWriteGit:
    def __init__(self, *documents: LegacyInventoryDocument):
        self.bodies = {
            (document.current_path, document.current_commit): document.body
            for document in documents
        }
        self.calls: list[tuple[str, tuple[Any, ...]]] = []

    def manual_fixed_ref_history(self, *args, **kwargs) -> dict[str, Any]:
        self.calls.append(("manual_fixed_ref_history", args))
        return {
            "fixed_ref": args[1],
            "current_commit": kwargs["current_commit"],
            "body": self.bodies[(args[2], kwargs["current_commit"])],
            "history": [],
            "activity": {},
        }


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
    occurred_at: datetime = datetime(2026, 8, 10, tzinfo=UTC)


class _NoWriteNative:
    def __init__(
        self,
        *entries: tuple[LegacyInventoryDocument, str],
    ):
        self.entries = {
            document.resource_id: (document, native_id)
            for document, native_id in entries
        }
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def get_resource_revision(self, **kwargs) -> _Snapshot:
        self.calls.append(("get_resource_revision", kwargs))
        document, native_id = self.entries[kwargs["resource_id"]]
        assert kwargs["revision_id"] == native_id
        body = document.body
        return _Snapshot(
            resource_id=document.resource_id,
            revision_id=native_id,
            surface="document",
            path=document.current_path,
            text=body.decode(),
            digest=document.body_digest,
            byte_size=len(body),
        )


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
    assert [name for name, _ in git.calls] == ["manual_fixed_ref_history"]

    result["projection"]["revision"] = "caller-mutation"
    result["content"] = "caller-mutation"
    reread = await legacy.get(document, selector=document.current_commit, fixed_ref=fixed_ref)
    assert reread["projection"]["revision"] == document.current_commit
    assert reread["content"] == "# Migration candidate\n\nsecret body"
    assert len(git.calls) == 1

    await legacy.get(second, selector=second.current_commit, fixed_ref=fixed_ref)
    await legacy.history(second, selector=second.current_commit, fixed_ref=fixed_ref)
    await legacy.diff(second, selector=second.current_commit, fixed_ref=fixed_ref)
    await legacy.activity(second, selector=second.current_commit, fixed_ref=fixed_ref)
    assert len(git.calls) == 2

    await legacy.get(document, selector=document.current_commit, fixed_ref=fixed_ref)
    assert len(git.calls) == 3

    with pytest.raises(ShadowReaderScopeError):
        await legacy.get(document, selector=_oid("e"), fixed_ref=fixed_ref)
    assert len(git.calls) == 3

    native_service = _NoWriteNative(
        (document, native_id),
        (second, second_native_id),
    )
    native = NativeRevisionShadowReader(
        pool=None,
        namespace_id=uuid.UUID("22222222-2222-4222-8222-222222222222"),
        vault_name="p2-manual",
        native_service=native_service,
    )
    candidate = await native.get(document, selector=native_id, fixed_ref=fixed_ref)
    await native.history(document, selector=native_id, fixed_ref=fixed_ref)
    await native.diff(document, selector=native_id, fixed_ref=fixed_ref)
    await native.activity(document, selector=native_id, fixed_ref=fixed_ref)
    assert candidate["current_commit"] == native_id
    assert [name for name, _ in native_service.calls] == ["get_resource_revision"]

    candidate["projection"]["revision"] = "caller-mutation"
    candidate["content"] = "caller-mutation"
    candidate_reread = await native.get(document, selector=native_id, fixed_ref=fixed_ref)
    assert candidate_reread["projection"]["revision"] == native_id
    assert candidate_reread["content"] == "# Migration candidate\n\nsecret body"
    assert len(native_service.calls) == 1

    await native.get(second, selector=second_native_id, fixed_ref=fixed_ref)
    await native.history(second, selector=second_native_id, fixed_ref=fixed_ref)
    await native.diff(second, selector=second_native_id, fixed_ref=fixed_ref)
    await native.activity(second, selector=second_native_id, fixed_ref=fixed_ref)
    assert len(native_service.calls) == 2

    await native.get(document, selector=native_id, fixed_ref=fixed_ref)
    assert len(native_service.calls) == 3


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
    )
    await native.get(document, selector=native_id, fixed_ref=fixed_ref)
    with pytest.raises(ShadowReaderScopeError, match="body differs from C9 inventory"):
        await native.get(invalid, selector=invalid_native_id, fixed_ref=fixed_ref)
    await native.get(document, selector=native_id, fixed_ref=fixed_ref)
    assert len(native_service.calls) == 3


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

    assert document.body.decode() not in encoded
    assert document.current_path not in encoded
    assert str(document.resource_id) not in encoded
    assert str(run.run_id) not in encoded
    assert "raw_candidate" not in receipt
    assert receipt["schema_version"] == 2
    assert receipt["protocol_version"].endswith("/v2")
    assert "legacy" not in receipt["resources"][0]["operations"]["get"]
    assert receipt["summary"]["unexplained_mismatch_count"] == 0
    assert receipt["summary"]["mismatch_count"] == 12
    assert receipt["resources"][0]["operations"]["get"]["mismatches"] == [
        {
            "path": "$.content",
            "rule_id": "BR-05",
            "classification": "formatting_only",
        },
        {
            "path": "$.current_commit",
            "rule_id": "BR-01",
            "classification": "revision_token",
        },
        {
            "path": "$.projection.revision",
            "rule_id": "BR-04",
            "classification": "projection_revision",
        },
    ]


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
        assert receipt["run"]["fixed_ref"] == fixed_ref
