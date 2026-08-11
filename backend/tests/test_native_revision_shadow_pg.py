"""Focused A5 shadow-comparator checks."""

from __future__ import annotations

import hashlib
import json
import os
import uuid
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import Any

import asyncpg
import pytest

from app.repositories.native_revision_migration_repo import (
    LegacyRevisionMapping,
    MigrationInventoryDriftError,
    MigrationItem,
    MigrationRun,
)
from app.services.legacy_revision_bridge import (
    LegacyActivitySemantics,
    LegacyInventory,
    LegacyInventoryDocument,
    LegacyLineageEntry,
)
from app.services.native_revision_shadow import (
    ShadowComparisonError,
    ShadowRunIncompleteError,
    NativeRevisionShadowComparator,
)


pytestmark = pytest.mark.asyncio

_DSN = os.environ.get(
    "AKB_TEST_DSN",
    "postgresql://akb:akb@127.0.0.1:55433/akb",  # pragma: allowlist secret
)


class _ReadRepository:
    def __init__(self, run: MigrationRun, items: list[MigrationItem]):
        self.run = run
        self.items = items
        self.get_run_calls = 0
        self.list_items_calls = 0

    async def get_run(self, run_id: uuid.UUID) -> MigrationRun:
        self.get_run_calls += 1
        assert run_id == self.run.run_id
        return self.run

    async def list_items(self, run_id: uuid.UUID) -> list[MigrationItem]:
        self.list_items_calls += 1
        assert run_id == self.run.run_id
        return list(self.items)

    async def exact_mapping(
        self,
        *,
        resource_id: uuid.UUID,
        legacy_git_oid: str,
    ) -> LegacyRevisionMapping | None:
        item = next(
            (
                item
                for item in self.items
                if item.legacy_document_id == resource_id
                and item.legacy_head_oid == legacy_git_oid
            ),
            None,
        )
        if item is None:
            return None
        return LegacyRevisionMapping(
            namespace_id=self.run.namespace_id,
            resource_id=resource_id,
            legacy_git_oid=legacy_git_oid,
            path_at_revision=item.captured_path,
            resolution="native",
            native_revision_id=item.native_head_revision_id,
            run_id=self.run.run_id,
            lineage_ordinal=1,
            fixed_git_oid=self.run.fixed_git_oid,
        )


class _Bridge:
    def __init__(self, inventory: LegacyInventory):
        self.inventory = inventory
        self.calls = 0
        self.drift = False

    async def inventory_for_run(self, run: MigrationRun) -> LegacyInventory:
        self.calls += 1
        if self.drift:
            raise MigrationInventoryDriftError()
        assert run.inventory_digest == self.inventory.inventory_digest
        return self.inventory


def _oid(char: str) -> str:
    return char * 40


def _doc(index: int) -> LegacyInventoryDocument:
    resource_id = uuid.UUID(f"{index:08d}-1111-4111-8111-111111111111")
    legacy_head = _oid(str(index + 1))
    retained = _oid(str(index + 3))
    body = f"# Document {index}\n\nbody at fixed ref\n"
    at = datetime(2026, 8, 10, 1, index, tzinfo=UTC)
    path = f"notes/document-{index}.md"
    activity = LegacyActivitySemantics(
        legacy_git_oid=legacy_head,
        committed_at=at,
        actor=f"actor-{index}",
        subject=f"akb://p2-vault/coll/{path}",
        summary=f"update document {index}",
        action="update",
        path_from=None,
        path_to=path,
        changed_paths=({"change": "update", "path_from": None, "path_to": path},),
    )
    return LegacyInventoryDocument(
        resource_id=resource_id,
        current_path=path,
        current_commit=legacy_head,
        created_at=at - timedelta(days=1),
        body=body.encode(),
        body_digest=hashlib.sha256(body.encode()).hexdigest(),
        byte_size=len(body.encode()),
        lineage=(
            LegacyLineageEntry(retained, path, at - timedelta(days=2)),
            LegacyLineageEntry(legacy_head, path, at),
        ),
        activity=activity,
        aliases=(),
    )


def _fixtures() -> tuple[MigrationRun, LegacyInventory, list[MigrationItem], dict[uuid.UUID, str]]:
    namespace_id = uuid.UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa")
    run_id = uuid.UUID("bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb")
    documents = tuple(_doc(index) for index in (1, 2))
    digest = "d" * 64
    run = MigrationRun(
        run_id=run_id,
        namespace_id=namespace_id,
        fixed_git_oid=_oid("f"),
        coverage_version="c9-test-v1",
        inventory_digest=digest,
        status="complete",
        created_at=datetime(2026, 8, 10, tzinfo=UTC),
        started_at=datetime(2026, 8, 10, tzinfo=UTC),
        completed_at=datetime(2026, 8, 10, 1, tzinfo=UTC),
        error=None,
    )
    inventory = LegacyInventory(
        namespace_id=namespace_id,
        fixed_git_oid=run.fixed_git_oid,
        coverage_version=run.coverage_version,
        documents=documents,
        inventory_digest=digest,
    )
    native_ids = {
        document.resource_id: _oid(str(index + 5))
        for index, document in enumerate(documents)
    }
    items = [
        MigrationItem(
            run_id=run_id,
            namespace_id=namespace_id,
            legacy_document_id=document.resource_id,
            native_resource_id=document.resource_id,
            captured_path=document.current_path,
            legacy_head_oid=document.current_commit,
            native_head_revision_id=native_ids[document.resource_id],
            body_digest=document.body_digest,
            byte_size=document.byte_size,
            status="complete",
            error_code=None,
        )
        for document in documents
    ]
    return run, inventory, items, native_ids


class _Reader:
    def __init__(
        self,
        native_ids: dict[uuid.UUID, str],
        *,
        candidate: bool,
        fixed_refs: dict[uuid.UUID, str] | None = None,
    ):
        self.native_ids = native_ids
        self.candidate = candidate
        self.fixed_refs = fixed_refs or {
            resource_id: _oid("f") for resource_id in native_ids
        }
        self.calls: list[tuple[str, uuid.UUID]] = []
        self.unknown_delta = False

    async def get(self, document: LegacyInventoryDocument, *, selector: str, fixed_ref: str) -> dict[str, Any]:
        self.calls.append(("get", document.resource_id))
        assert fixed_ref == self.fixed_refs[document.resource_id]
        native_id = self.native_ids[document.resource_id]
        if self.candidate:
            selector = native_id
        else:
            selector = document.current_commit
        body = document.body.decode()
        if not self.candidate:
            body = body.replace("\n", "\r\n")
        result = {
            "kind": "document",
            "uri": document.activity.subject,
            "vault": "p2-vault",
            "path": document.current_path,
            "title": document.current_path.rsplit("/", 1)[-1],
            "current_commit": selector,
            "content": body,
            "projection": {"revision": selector, "authoritative": False},
            "actor": {"id": document.activity.actor, "display": "Legacy Writer"},
        }
        if not self.candidate:
            result["actor"] = {"id": document.activity.actor, "display": "Legacy Writer"}
        if self.unknown_delta:
            result["uri"] = "akb://p2-vault/coll/notes/other.md"
        return result

    async def history(self, document: LegacyInventoryDocument, *, selector: str, fixed_ref: str) -> dict[str, Any]:
        self.calls.append(("history", document.resource_id))
        assert selector == (self.native_ids[document.resource_id] if self.candidate else document.current_commit)
        entries = []
        for index, entry in enumerate(reversed(document.lineage)):
            entry_selector = self.native_ids[document.resource_id] if self.candidate and index == 0 else entry.legacy_git_oid
            projection = entry_selector if index == 0 else None
            entries.append(
                {
                    "selector": entry_selector,
                    "payload_sha256": hashlib.sha256(entry.legacy_git_oid.encode()).hexdigest(),
                    "projection_revision": projection,
                    "summary": document.activity.summary if index == 0 else "retained history",
                }
            )
        return {
            "uri": document.activity.subject,
            "history_source": "fixed-ref-bridge" if self.candidate else "legacy-git-log",
            "lineage_boundary": document.lineage[0].legacy_git_oid if self.candidate else "legacy-document-start",
            "entries": entries,
        }

    async def diff(self, document: LegacyInventoryDocument, *, selector: str, fixed_ref: str) -> dict[str, Any]:
        self.calls.append(("diff", document.resource_id))
        assert selector == (self.native_ids[document.resource_id] if self.candidate else document.current_commit)
        return {
            "file": document.current_path,
            "commit": selector,
            "basis": "fixed-ref-snapshot" if self.candidate else "git-parent",
            "text": "@@ -1 +1 @@\n body at fixed ref",
            "format": "unified",
        }

    async def activity(self, document: LegacyInventoryDocument, *, selector: str, fixed_ref: str) -> dict[str, Any]:
        self.calls.append(("activity", document.resource_id))
        native_id = self.native_ids[document.resource_id]
        if self.candidate:
            return {
                "events": [
                    {
                        "hash": native_id,
                        "subject": "internal migration subject",
                        "author": {"id": "akb-native-revision-migration", "display": "Migration"},
                        "action": "create",
                        "summary": "C9 fixed-ref native genesis",
                        "projection_revision": native_id,
                    }
                ]
            }
        return {
            "events": [
                {
                    "hash": document.current_commit,
                    "subject": document.activity.subject,
                    "author": {"id": document.activity.actor, "display": "Legacy Writer (Git)"},
                    "action": document.activity.action,
                    "summary": document.activity.summary,
                    "projection_revision": document.current_commit,
                }
            ]
        }


class _CrossRunReadRepository:
    def __init__(
        self,
        final_run: MigrationRun,
        owner_run: MigrationRun,
        item: MigrationItem,
        mappings: dict[uuid.UUID, LegacyRevisionMapping],
    ):
        self.runs = {final_run.run_id: final_run, owner_run.run_id: owner_run}
        self.final_run = final_run
        self.item = item
        self.mappings = mappings

    async def get_run(self, run_id: uuid.UUID) -> MigrationRun | None:
        return self.runs.get(run_id)

    async def list_items(self, run_id: uuid.UUID) -> list[MigrationItem]:
        assert run_id == self.final_run.run_id
        return [self.item]

    async def exact_mapping(
        self,
        *,
        resource_id: uuid.UUID,
        legacy_git_oid: str,
    ) -> LegacyRevisionMapping | None:
        mapping = self.mappings.get(resource_id)
        if mapping is None or mapping.legacy_git_oid != legacy_git_oid:
            return None
        return mapping


async def _table_counts() -> tuple[tuple[str, int], ...]:
    try:
        conn = await asyncpg.connect(_DSN, timeout=2)
    except (OSError, asyncpg.PostgresError):
        pytest.skip(f"Postgres not reachable at {_DSN}")
    try:
        rows = await conn.fetch(
            """
            SELECT table_name
              FROM information_schema.tables
             WHERE table_schema = 'public'
             ORDER BY table_name
            """
        )
        counts = []
        for row in rows:
            table = row["table_name"].replace('"', '""')
            count = await conn.fetchval(f'SELECT count(*) FROM "{table}"')
            counts.append((row["table_name"], int(count)))
        return tuple(counts)
    finally:
        await conn.close()


async def test_completed_run_compares_every_resource_and_is_immutable():
    run, inventory, items, native_ids = _fixtures()
    repository = _ReadRepository(run, items)
    bridge = _Bridge(inventory)
    legacy = _Reader(native_ids, candidate=False)
    candidate = _Reader(native_ids, candidate=True)
    comparator = NativeRevisionShadowComparator(
        repository=repository,
        bridge=bridge,
        legacy_reader=legacy,
        candidate_reader=candidate,
    )

    before = await _table_counts()
    receipt = await comparator.compare_run(run.run_id)
    after = await _table_counts()

    assert before == after
    assert receipt["status"] == "passed"
    assert receipt["claim_scope"] == "semantic_candidate_evidence"
    assert receipt["write_authority"] == "legacy_only"
    assert receipt["final_p2_coverage_claim"] is False
    assert receipt["cutover_claim"] is False
    assert len(receipt["resources"]) == 2
    assert set(receipt["summary"]["used_rules"]) == {f"BR-0{index}" for index in range(1, 8)}
    for resource in receipt["resources"]:
        assert set(resource["operations"]) == {"get", "history", "diff", "activity"}
        for operation in resource["operations"].values():
            assert operation["normalized_equal"] is True
            assert operation["mismatch_count"] == sum(
                mismatch["count"] for mismatch in operation["classified_mismatches"]
            )
            for mismatch in operation["classified_mismatches"]:
                assert set(mismatch) == {"rule_id", "classification", "count"}
    encoded = json.dumps(receipt, sort_keys=True)
    assert all(document.body.decode() not in encoded for document in inventory.documents)
    assert all(str(document.resource_id) not in encoded for document in inventory.documents)
    assert {name for name, _ in legacy.calls} == {"get", "history", "diff", "activity"}
    assert len(legacy.calls) == len(inventory.documents) * 4
    assert len(candidate.calls) == len(inventory.documents) * 4
    assert receipt["receipt_digest"] == (await comparator.compare_run(run.run_id))["receipt_digest"]


async def test_completed_reconcile_resolves_an_unchanged_mapping_from_its_owner_run():
    run, inventory, items, native_ids = _fixtures()
    changed, unchanged = inventory.documents
    owner_run = replace(
        run,
        run_id=uuid.UUID("cccccccc-cccc-4ccc-8ccc-cccccccccccc"),
        fixed_git_oid=_oid("a"),
        coverage_version="c9-owner-v1",
        inventory_digest="e" * 64,
    )
    mappings = {
        changed.resource_id: LegacyRevisionMapping(
            namespace_id=run.namespace_id,
            resource_id=changed.resource_id,
            legacy_git_oid=changed.current_commit,
            path_at_revision=changed.current_path,
            resolution="native",
            native_revision_id=native_ids[changed.resource_id],
            run_id=run.run_id,
            lineage_ordinal=1,
            fixed_git_oid=run.fixed_git_oid,
        ),
        unchanged.resource_id: LegacyRevisionMapping(
            namespace_id=run.namespace_id,
            resource_id=unchanged.resource_id,
            legacy_git_oid=unchanged.current_commit,
            path_at_revision=unchanged.current_path,
            resolution="native",
            native_revision_id=native_ids[unchanged.resource_id],
            run_id=owner_run.run_id,
            lineage_ordinal=1,
            fixed_git_oid=owner_run.fixed_git_oid,
        ),
    }
    fixed_refs = {
        changed.resource_id: run.fixed_git_oid,
        unchanged.resource_id: owner_run.fixed_git_oid,
    }
    comparator = NativeRevisionShadowComparator(
        repository=_CrossRunReadRepository(run, owner_run, items[0], mappings),
        bridge=_Bridge(inventory),
        legacy_reader=_Reader(
            native_ids,
            candidate=False,
            fixed_refs=fixed_refs,
        ),
        candidate_reader=_Reader(
            native_ids,
            candidate=True,
            fixed_refs=fixed_refs,
        ),
    )

    receipt = await comparator.compare_run(run.run_id)

    assert receipt["status"] == "passed"
    assert receipt["summary"]["resource_count"] == 2
    assert receipt["summary"]["operation_count"] == 8


async def test_incomplete_run_and_inventory_drift_are_rejected_before_reads():
    run, inventory, items, native_ids = _fixtures()
    repository = _ReadRepository(replace(run, status="running"), items)
    bridge = _Bridge(inventory)
    legacy = _Reader(native_ids, candidate=False)
    candidate = _Reader(native_ids, candidate=True)
    comparator = NativeRevisionShadowComparator(
        repository=repository,
        bridge=bridge,
        legacy_reader=legacy,
        candidate_reader=candidate,
    )

    with pytest.raises(ShadowRunIncompleteError):
        await comparator.compare_run(run.run_id)
    assert bridge.calls == 0
    assert legacy.calls == []
    assert candidate.calls == []

    repository.run = run
    bridge.drift = True
    with pytest.raises(MigrationInventoryDriftError):
        await comparator.compare_run(run.run_id)
    assert legacy.calls == []
    assert candidate.calls == []


async def test_unknown_delta_is_a_non_passing_defect_not_a_new_bridge_rule():
    run, inventory, items, native_ids = _fixtures()
    repository = _ReadRepository(run, items)
    bridge = _Bridge(inventory)
    legacy = _Reader(native_ids, candidate=False)
    candidate = _Reader(native_ids, candidate=True)
    candidate.unknown_delta = True
    comparator = NativeRevisionShadowComparator(
        repository=repository,
        bridge=bridge,
        legacy_reader=legacy,
        candidate_reader=candidate,
    )

    with pytest.raises(ShadowComparisonError, match="unapproved mismatch"):
        await comparator.compare_run(run.run_id)


@pytest.mark.parametrize(
    "stale_item",
    [
        lambda item: replace(item, run_id=uuid.uuid4()),
        lambda item: replace(item, captured_path="notes/stale.md"),
        lambda item: replace(item, body_digest="0" * 64),
    ],
    ids=("cross-run", "stale-path", "stale-digest"),
)
async def test_stale_or_cross_run_item_binding_fails_before_any_reader(
    stale_item,
):
    run, inventory, items, native_ids = _fixtures()
    items[0] = stale_item(items[0])
    repository = _ReadRepository(run, items)
    bridge = _Bridge(inventory)
    legacy = _Reader(native_ids, candidate=False)
    candidate = _Reader(native_ids, candidate=True)
    comparator = NativeRevisionShadowComparator(
        repository=repository,
        bridge=bridge,
        legacy_reader=legacy,
        candidate_reader=candidate,
    )

    with pytest.raises(ShadowComparisonError, match="item binding"):
        await comparator.compare_run(run.run_id)
    assert legacy.calls == []
    assert candidate.calls == []
