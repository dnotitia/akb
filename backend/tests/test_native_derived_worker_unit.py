from __future__ import annotations

import hashlib
import logging
import uuid
from contextlib import asynccontextmanager

import pytest

from app.services import native_derived_worker
from app.services._backfill import MAX_RETRIES
from app.services.index_service import MAX_CHUNK_SIZE, SOURCE_TYPES
from app.services.m1_pg_body_store import M1PgBodyStore
from app.services.m1_reference_payload_store import M1ReferencePayloadStore
from app.services.native_payload_verification import (
    NativePayloadPlacementError,
    verify_native_head_body,
)
from app.services.native_derived_worker import (
    DIRECT_GREP_DELIVERY,
    NATIVE_DOCUMENT_SOURCE,
    NATIVE_FILE_SOURCE,
    SELECTED_DELIVERY,
    NativeDerivedWorker,
    build_native_document_chunks,
    build_native_file_chunks,
    source_type_for_surface,
)


def test_native_document_chunks_are_real_body_chunks_with_search_metadata():
    raw = """---
title: Native title
type: runbook
summary: Derived summary
tags:
  - native
---
# Recovery
exact searchable body
"""

    chunks = build_native_document_chunks(
        vault_name="measure",
        path="ops/recovery.md",
        canonical_text=raw,
    )

    assert NATIVE_DOCUMENT_SOURCE == "native_document"
    assert len(chunks) == 1
    assert chunks[0].section_path == "# Recovery"
    assert "TITLE: Native title" in chunks[0].content
    assert "PATH: measure/ops/recovery.md" in chunks[0].content
    assert "exact searchable body" in chunks[0].content
    assert "title: Native title" not in chunks[0].content


def test_native_document_chunks_do_not_create_a_synthetic_empty_projection():
    raw = """---
title: Empty
---
"""

    assert build_native_document_chunks(
        vault_name="measure",
        path="empty.md",
        canonical_text=raw,
    ) == []


def test_native_file_chunks_carry_the_body_and_file_addressing():
    resource_id = uuid.UUID("11111111-2222-3333-4444-555555555555")

    chunks = build_native_file_chunks(
        vault_name="measure",
        path="src/app/main.py",
        resource_id=resource_id,
        canonical_text="def handler():\n    return 'exact file body'\n",
    )

    assert NATIVE_FILE_SOURCE == "native_file"
    assert len(chunks) == 1
    assert chunks[0].section_path == ""
    assert "TITLE: main.py" in chunks[0].content
    assert "TYPE: file" in chunks[0].content
    assert "VAULT: measure" in chunks[0].content
    # File addressing: the vault-relative path, and the canonical File URI —
    # never `PATH: measure/src/app/main.py` and never a doc:// locator.
    assert "PATH: src/app/main.py" in chunks[0].content
    assert (
        f"URI: akb://measure/coll/src/app/file/{resource_id}" in chunks[0].content
    )
    assert "akb://measure/doc/" not in chunks[0].content
    assert "return 'exact file body'" in chunks[0].content


def test_native_file_chunks_do_not_lose_text_before_a_comment_that_looks_like_a_heading():
    """A text File is not markdown.

    `chunk_markdown` treats any `# ` line as a section boundary and emits only
    the spans it recognizes, so a Python comment would silently swallow every
    preceding line. The File body must survive chunking intact.
    """
    body = "import os\n\n# TODO: replace the shim\n\nvalue = os.environ['A']\n"

    chunks = build_native_file_chunks(
        vault_name="measure",
        path="shim.py",
        resource_id=uuid.uuid4(),
        canonical_text=body,
    )

    assert len(chunks) == 1
    assert "import os" in chunks[0].content
    assert "# TODO: replace the shim" in chunks[0].content
    assert "value = os.environ['A']" in chunks[0].content
    assert chunks[0].section_path == ""


def test_native_file_chunks_split_a_large_body_within_the_embedding_bound():
    resource_id = uuid.UUID("aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee")
    body = "\n\n".join(f"paragraph {index} of the file body" for index in range(400))

    chunks = build_native_file_chunks(
        vault_name="measure",
        path="big.txt",
        resource_id=resource_id,
        canonical_text=body,
    )

    assert len(chunks) > 1
    assert all(len(chunk.content) <= MAX_CHUNK_SIZE for chunk in chunks)
    assert [chunk.chunk_index for chunk in chunks] == list(range(len(chunks)))
    # Every chunk carries the resource-level signal, not just chunk 0 — a chunk
    # from deep inside a large file is otherwise anonymous to both retrieval
    # legs.
    assert all(chunk.content.startswith("TITLE: big.txt\n") for chunk in chunks)
    assert all(f"URI: akb://measure/file/{resource_id}" in chunk.content for chunk in chunks)
    # And no body line is lost to the split.
    joined = "\n".join(chunk.content for chunk in chunks)
    assert all(f"paragraph {index} of the file body" in joined for index in range(400))


def test_native_file_chunks_do_not_create_a_synthetic_empty_projection():
    assert build_native_file_chunks(
        vault_name="measure",
        path="blank.txt",
        resource_id=uuid.uuid4(),
        canonical_text="\n  \n",
    ) == []


def test_derived_discriminators_are_distinct_and_schema_admitted():
    # A native text File's Resource id IS the public `vault_files.id`; sharing
    # the legacy `file` discriminator would make the two authorities collide on
    # one (source_type, source_id) key.
    assert source_type_for_surface("document") == NATIVE_DOCUMENT_SOURCE
    assert source_type_for_surface("file") == NATIVE_FILE_SOURCE
    assert NATIVE_FILE_SOURCE not in {"file", NATIVE_DOCUMENT_SOURCE}
    assert {NATIVE_DOCUMENT_SOURCE, NATIVE_FILE_SOURCE} <= set(SOURCE_TYPES)
    with pytest.raises(ValueError, match="unsupported native derived surface"):
        source_type_for_surface("table")


def test_both_surfaces_select_one_delivery_and_direct_grep_stays_readable():
    # `selected_delivery` names the delivery mechanism, not the surface. After
    # parity there is exactly one mechanism; the pre-parity File delivery stays
    # defined so historical rows can still be read rather than rewritten.
    assert SELECTED_DELIVERY == "native-searchable-derived-v1"
    assert DIRECT_GREP_DELIVERY == "native-direct-pg-grep-v1"
    assert SELECTED_DELIVERY != DIRECT_GREP_DELIVERY


@pytest.mark.parametrize(
    "placement",
    (M1ReferencePayloadStore.selected_placement, M1PgBodyStore.selected_placement),
)
def test_native_head_body_verification_dispatches_to_the_manifest_placement(placement):
    canonical = b"verified native body\n"
    assert verify_native_head_body(
        {
            "canonical_bytes": canonical,
            "digest": hashlib.sha256(canonical).hexdigest(),
            "byte_size": len(canonical),
            "encoding": "utf-8",
            "selected_placement": placement,
            "verification_profile": "sha256-size-utf8-v1",
        }
    ) == canonical


def test_native_head_body_verification_rejects_an_unknown_manifest_placement():
    with pytest.raises(NativePayloadPlacementError, match="Unsupported native payload placement"):
        verify_native_head_body(
            {
                "selected_placement": "unknown-placement-v1",
            }
        )


@pytest.mark.parametrize(
    "placement",
    (M1ReferencePayloadStore.selected_placement, M1PgBodyStore.selected_placement),
)
def test_native_head_body_verification_rejects_a_mismatched_placement_profile(placement):
    canonical = b"verified native body\n"
    with pytest.raises(RuntimeError, match="verification profile mismatch"):
        verify_native_head_body(
            {
                "canonical_bytes": canonical,
                "digest": hashlib.sha256(canonical).hexdigest(),
                "byte_size": len(canonical),
                "encoding": "utf-8",
                "selected_placement": placement,
                "verification_profile": "mismatched-profile-v1",
            }
        )


# ── terminal delivery must be countable and named ─────────────────────
#
# The failure these cover lost three documents from ranked search and was
# noticed only because someone happened to be watching `last_error` on an
# internal queue table during a migration (#527).  Both halves of the fix are
# asserted here without a database, because the queue state and the log line
# are what an operator has and neither of them is a query they should have to
# know to write.


class _StatsConn:
    """The two aggregate rows `_pending_stats` reads, in the order it reads them.

    The cumulative counters and the Head-scoped ones come from separate
    queries over different populations, so a fake that answered both from one
    row could not tell a repaired loss from a live one — which is the only
    thing these cases are about.
    """

    def __init__(self, rows):
        self.rows = list(rows)
        self.queries: list[str] = []

    async def fetchrow(self, query, *args):
        self.queries.append(query)
        self.args = args
        return self.rows.pop(0) if len(self.rows) > 1 else self.rows[0]


def _stats_pool(row, heads=None):
    conn = _StatsConn([row, heads if heads is not None else _heads()])

    class _Pool:
        def acquire(self):
            @asynccontextmanager
            async def _acquire():
                yield conn

            return _acquire()

    return _Pool(), conn


def _counts(**overrides):
    """The cumulative ledger row: everything this queue has ever recorded."""
    row = {
        "pending": 0,
        "retrying": 0,
        "exhausted": 0,
        "abandoned": 0,
        "applied": 0,
        "superseded": 0,
        "deleted": 0,
        "direct_grep": 0,
    }
    row.update(overrides)
    return row


def _heads(**overrides):
    """The subset still at a Resource Head: what is true of the corpus now."""
    row = {"pending": 0, "retrying": 0, "exhausted": 0, "abandoned": 0}
    row.update(overrides)
    return row


async def test_an_abandoned_intent_is_reported_even_though_the_queue_drained():
    """The bug in one assertion: progress at 100%, a document gone.

    `pending` is 0 and `applied` says the rest of the backfill succeeded, which
    is exactly what an operator watching progress sees when a Resource has been
    given up on. The count and the status are the only things that say so.
    """
    pool, _ = _stats_pool(_counts(applied=41, abandoned=1), _heads(abandoned=1))

    stats = await native_derived_worker._pending_stats(pool)

    assert stats["pending"] == 0
    assert stats["abandoned"] == 1
    assert stats["status"] == "degraded"


async def test_a_drained_queue_with_nothing_lost_is_ok():
    pool, _ = _stats_pool(_counts(applied=42))

    assert (await native_derived_worker._pending_stats(pool))["status"] == "ok"


async def test_work_still_in_flight_is_reconciling_not_ok():
    """`ok` must mean settled, or a poller stops waiting while work remains."""
    pool, _ = _stats_pool(_counts(pending=3, retrying=1))

    assert (await native_derived_worker._pending_stats(pool))["status"] == "reconciling"


async def test_a_claim_killed_on_its_final_attempt_is_exhausted_not_retrying():
    """The pre-terminal state `queue_rescuer` exists to close.

    The claim query skips `retry_count >= MAX_RETRIES`, so nothing will pick
    this row up again on its own. Counting it as ordinary retrying work would
    describe a stalled queue as a busy one.
    """
    pool, _ = _stats_pool(_counts(pending=1, exhausted=1), _heads(pending=1, exhausted=1))

    stats = await native_derived_worker._pending_stats(pool)

    assert stats["exhausted"] == 1
    assert stats["status"] == "degraded"


async def test_a_loss_that_a_later_revision_repaired_stops_being_a_fault():
    """The ledger keeps it; the verdict must not.

    A revision is abandoned, the author saves again, and the new Head indexes
    cleanly. Nothing is missing from ranked search, yet the cumulative counter
    can never go back down — so a verdict read from it reports the same repair
    as an outage for the life of the deployment.
    """
    pool, _ = _stats_pool(_counts(applied=42, abandoned=1), _heads())

    stats = await native_derived_worker._pending_stats(pool)

    assert stats["abandoned"] == 1, "the ledger still records what was given up on"
    assert stats["abandoned_at_head"] == 0
    assert stats["status"] == "ok"


async def test_a_stuck_final_attempt_on_a_superseded_revision_is_not_degraded():
    """Its Head has an intent of its own; this row is waiting on the rescuer."""
    pool, _ = _stats_pool(_counts(pending=1, exhausted=1), _heads())

    stats = await native_derived_worker._pending_stats(pool)

    assert stats["exhausted"] == 1
    assert stats["status"] == "reconciling"


async def test_a_live_loss_is_still_degraded_when_the_ledger_holds_repaired_ones():
    """One current Head lost, several historical — the verdict follows the one."""
    pool, _ = _stats_pool(_counts(applied=99, abandoned=7), _heads(abandoned=1))

    assert (await native_derived_worker._pending_stats(pool))["status"] == "degraded"


async def test_vault_scoped_stats_are_narrowed_by_namespace():
    namespace_id = uuid.uuid4()
    pool, conn = _stats_pool(_counts())

    await native_derived_worker._pending_stats(pool, namespace_id)

    assert "AND namespace_id = $2" in conn.queries[0]
    # Both populations are narrowed, or the verdict would be the deployment's.
    assert "AND i.namespace_id = $2" in conn.queries[1]
    assert conn.args == (MAX_RETRIES, namespace_id)


class _FailureConn:
    def __init__(self, located):
        self._located = located
        self.statements: list[str] = []

    async def execute(self, query, *args):
        self.statements.append(query)

    async def fetchrow(self, _query, *_args):
        return self._located


def _failure_worker(located):
    conn = _FailureConn(located)

    class _Pool:
        def acquire(self):
            @asynccontextmanager
            async def _acquire():
                yield conn

            return _acquire()

    return NativeDerivedWorker(_Pool()), conn


def _intent(retry_count):
    return {
        "intent_id": uuid.uuid4(),
        "resource_id": uuid.uuid4(),
        "revision_id": uuid.uuid4(),
        "retry_count": retry_count,
        "surface": "document",
    }


async def test_the_final_failure_names_the_resource_it_just_gave_up_on(caplog):
    """A count says something was lost; only a path says what to fix."""
    worker, conn = _failure_worker({"vault_name": "handbook", "current_path": "ops/extracted.md"})
    caplog.set_level(logging.ERROR, logger="akb.native_derived_worker")

    await worker._failure(_intent(MAX_RETRIES), ValueError("body bytes must not be logged"))

    assert "delivery_outcome = 'abandoned'" in conn.statements[0]
    record = next(r for r in caplog.records if r.levelno == logging.ERROR)
    message = record.getMessage()
    assert "ABANDONED" in message
    assert "vault=handbook" in message
    assert "path=ops/extracted.md" in message
    assert "error=ValueError" in message
    # The class, never the message: an exception can quote the body that
    # failed to store, exactly as `last_error` refuses to.
    assert "body bytes must not be logged" not in message


async def test_a_retryable_failure_stays_quiet_about_abandonment(caplog):
    """Attempt 1 of 8 is not a loss, and must not read as one."""
    worker, _ = _failure_worker({"vault_name": "handbook", "current_path": "ops/extracted.md"})
    caplog.set_level(logging.ERROR, logger="akb.native_derived_worker")

    await worker._failure(_intent(1), ValueError("transient"))

    assert [r for r in caplog.records if r.levelno == logging.ERROR] == []


async def test_an_unnameable_resource_still_reports_the_abandonment(caplog):
    """Diagnostics are best effort; the row is already terminal when this runs."""
    worker, _ = _failure_worker(None)
    caplog.set_level(logging.ERROR, logger="akb.native_derived_worker")

    await worker._failure(_intent(MAX_RETRIES), ValueError("boom"))

    message = next(r for r in caplog.records if r.levelno == logging.ERROR).getMessage()
    assert "ABANDONED" in message
    assert "vault=<unresolved>" in message


# ── Graph parity: the derived rewrite owns the document's implicit edges ──


def test_document_relations_are_read_from_frontmatter_and_body():
    raw = """---
title: Native title
depends_on:
  - akb://measure/coll/specs/doc/a.md
related_to: not-a-list
---
# Body
see [b](specs/b.md)
"""

    relations = native_derived_worker.build_native_document_relations(raw)

    assert relations.depends_on == ["akb://measure/coll/specs/doc/a.md"]
    assert relations.related_to == []          # a scalar is not a relation list
    assert relations.implements == []
    assert "specs/b.md" in relations.body
    assert "title: Native title" not in relations.body


class _RewriteConn:
    """Records the writes one derived rewrite performs."""

    def __init__(self, resource: dict, previous_path: str | None) -> None:
        self.resource = resource
        self.previous_path = previous_path
        self.executed: list[str] = []

    def transaction(self):
        @asynccontextmanager
        async def _tx():
            yield None

        return _tx()

    async def fetchrow(self, _query, *_args):
        return self.resource

    async def fetchval(self, _query, *_args):
        return self.previous_path

    async def execute(self, query, *_args):
        self.executed.append(query)
        return "OK"


def _rewrite_pool(conn):
    class _Pool:
        def acquire(self):
            @asynccontextmanager
            async def _acquire():
                yield conn

            return _acquire()

    return _Pool()


def _document_intent(resource_id, namespace_id, revision_id):
    return {
        "intent_id": uuid.uuid4(),
        "resource_id": resource_id,
        "namespace_id": namespace_id,
        "revision_id": revision_id,
        "surface": "document",
    }


@pytest.fixture
def _stubbed_rewrite(monkeypatch):
    """Isolate the graph half: real body verification and chunking are elsewhere."""
    calls: dict[str, list] = {"stored": [], "all_deleted": []}

    monkeypatch.setattr(
        native_derived_worker, "verify_native_head_body",
        lambda head: b"---\ntitle: T\n---\nbody [x](specs/x.md)\n",
    )

    async def _store(
        _conn, vault_id, vault_name, path, depends_on, related_to, implements, body,
        source_resource_id=None,
    ):
        calls["stored"].append(
            (vault_id, vault_name, path, depends_on, related_to, implements, body,
             source_resource_id)
        )
        return 1

    async def _delete_all(_conn, vault_id, vault_name, resource_id, path):
        calls["all_deleted"].append((vault_id, vault_name, resource_id, path))

    async def _enqueue(*_args, **_kwargs):
        return None

    monkeypatch.setattr(native_derived_worker, "store_document_relations", _store)
    monkeypatch.setattr(native_derived_worker, "delete_native_document_edges", _delete_all)
    monkeypatch.setattr(native_derived_worker.delete_worker, "enqueue_source_deletes", _enqueue)
    return calls


async def test_a_live_document_rewrite_stores_its_body_links(_stubbed_rewrite):
    resource_id, namespace_id, revision_id = uuid.uuid4(), uuid.uuid4(), "rev-1"
    conn = _RewriteConn(
        {"lifecycle": "live", "head_revision_id": revision_id, "current_path": "specs/a.md"},
        previous_path="specs/a.md",
    )
    worker = NativeDerivedWorker(pool=_rewrite_pool(conn))

    await worker._apply_live(
        _document_intent(resource_id, namespace_id, revision_id),
        {"vault_name": "measure", "current_path": "specs/a.md", "resource_id": resource_id},
    )

    assert len(_stubbed_rewrite["stored"]) == 1
    vault_id, vault_name, path, *_rest = _stubbed_rewrite["stored"][0]
    assert (vault_id, vault_name, path) == (namespace_id, "measure", "specs/a.md")
    # The rewrite hands over the resource identity, which is what scopes the
    # implicit clear to this document instead of to whatever answers at the path.
    assert _stubbed_rewrite["stored"][0][-1] == resource_id


async def test_a_moved_document_clears_its_old_links_by_identity_not_by_path(_stubbed_rewrite):
    """The rewrite no longer sweeps the previous PATH.

    It used to read `native_derived_heads.path` and delete the implicit rows
    there, which erased the links of whichever document had since taken that
    freed path. The identity handed to `store_document_relations` covers the
    rows this resource still owns under any previous path, so the sweep is
    both unnecessary and unsafe.
    """
    resource_id, namespace_id, revision_id = uuid.uuid4(), uuid.uuid4(), "rev-2"
    conn = _RewriteConn(
        {"lifecycle": "live", "head_revision_id": revision_id, "current_path": "new/a.md"},
        previous_path="old/a.md",
    )
    worker = NativeDerivedWorker(pool=_rewrite_pool(conn))

    await worker._apply_live(
        _document_intent(resource_id, namespace_id, revision_id),
        {"vault_name": "measure", "current_path": "new/a.md", "resource_id": resource_id},
    )

    assert _stubbed_rewrite["stored"][0][2] == "new/a.md"
    assert _stubbed_rewrite["stored"][0][-1] == resource_id
    assert not any("old/a.md" in q for q in conn.executed)


async def test_a_superseded_revision_rewrites_no_relations(_stubbed_rewrite):
    resource_id, namespace_id = uuid.uuid4(), uuid.uuid4()
    conn = _RewriteConn(
        {"lifecycle": "live", "head_revision_id": "rev-newer", "current_path": "specs/a.md"},
        previous_path=None,
    )
    worker = NativeDerivedWorker(pool=_rewrite_pool(conn))

    applied = await worker._apply_live(
        _document_intent(resource_id, namespace_id, "rev-old"),
        {"vault_name": "measure", "current_path": "specs/a.md", "resource_id": resource_id},
    )

    assert applied == 0
    assert _stubbed_rewrite["stored"] == []


async def test_a_deleted_document_stops_being_a_graph_endpoint(_stubbed_rewrite):
    resource_id, revision_id = uuid.uuid4(), "rev-3"
    conn = _RewriteConn(
        {
            "lifecycle": "deleted",
            "head_revision_id": revision_id,
            "current_path": "specs/a.md",
            "surface": "document",
            "vault_name": "measure",
        },
        previous_path=None,
    )
    worker = NativeDerivedWorker(pool=_rewrite_pool(conn))

    namespace_id = uuid.uuid4()
    await worker._apply_delete(_document_intent(resource_id, namespace_id, revision_id))

    # Keyed on the resource. The path comes along only so the cleanup can tell
    # whether it has been taken over (akb#654).
    assert _stubbed_rewrite["all_deleted"] == [
        (namespace_id, "measure", resource_id, "specs/a.md")
    ]


async def test_a_deleted_file_leaves_the_document_graph_alone(_stubbed_rewrite):
    resource_id, revision_id = uuid.uuid4(), "rev-4"
    conn = _RewriteConn(
        {
            "lifecycle": "deleted",
            "head_revision_id": revision_id,
            "current_path": "assets/a.txt",
            "surface": "file",
            "vault_name": "measure",
        },
        previous_path=None,
    )
    worker = NativeDerivedWorker(pool=_rewrite_pool(conn))
    intent = _document_intent(resource_id, uuid.uuid4(), revision_id) | {"surface": "file"}

    await worker._apply_delete(intent)

    assert _stubbed_rewrite["all_deleted"] == []
