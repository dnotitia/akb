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
    """One row of `native_invalidation_intents` aggregates."""

    def __init__(self, row):
        self.row = row
        self.queries: list[str] = []

    async def fetchrow(self, query, *args):
        self.queries.append(query)
        self.args = args
        return self.row


def _stats_pool(row):
    conn = _StatsConn(row)

    class _Pool:
        def acquire(self):
            @asynccontextmanager
            async def _acquire():
                yield conn

            return _acquire()

    return _Pool(), conn


def _counts(**overrides):
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


async def test_an_abandoned_intent_is_reported_even_though_the_queue_drained():
    """The bug in one assertion: progress at 100%, a document gone.

    `pending` is 0 and `applied` says the rest of the backfill succeeded, which
    is exactly what an operator watching progress sees when a Resource has been
    given up on. The count and the status are the only things that say so.
    """
    pool, _ = _stats_pool(_counts(applied=41, abandoned=1))

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
    pool, _ = _stats_pool(_counts(pending=1, exhausted=1))

    stats = await native_derived_worker._pending_stats(pool)

    assert stats["exhausted"] == 1
    assert stats["status"] == "degraded"


async def test_vault_scoped_stats_are_narrowed_by_namespace():
    namespace_id = uuid.uuid4()
    pool, conn = _stats_pool(_counts())

    await native_derived_worker._pending_stats(pool, namespace_id)

    assert "AND namespace_id = $2" in conn.queries[0]
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
