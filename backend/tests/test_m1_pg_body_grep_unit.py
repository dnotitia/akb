from __future__ import annotations

import asyncio
import hashlib
import re
import threading
import time
import uuid

import pytest

from app.exceptions import AKBError, ValidationError
from app.services import m1_native_grep_service as native_grep
from app.services.m1_native_grep_service import HeadBody, M1NativeGrepService
from app.services import m1_pg_body_store
from app.services.m1_pg_body_store import M1PgBodyStore, PgBodyIntegrityError
from app.services.m1_reference_payload_store import M1ReferencePayloadStore
from app.services.native_payload_verification import NativePayloadPlacementError


def _row(body: bytes = b"hello\nneedle\n") -> dict:
    return {
        "canonical_bytes": body,
        "byte_size": len(body),
        "digest": hashlib.sha256(body).hexdigest(),
        "encoding": "utf-8",
        "selected_placement": "pg-bodystore-v1",
        "verification_profile": "sha256-size-utf8-v1",
    }


def test_pg_body_candidate_verifies_one_canonical_utf8_representation():
    row = _row()
    assert M1PgBodyStore._verify_row(row) == row["canonical_bytes"]
    assert M1PgBodyStore._verified_bytes("hello") == (
        b"hello",
        hashlib.sha256(b"hello").hexdigest(),
    )


@pytest.mark.parametrize("payload", [b"\xff"])
def test_pg_body_candidate_rejects_non_searchable_bytes(payload: bytes):
    with pytest.raises(ValidationError):
        M1PgBodyStore._verified_bytes(payload)


def test_pg_body_candidate_preserves_utf8_nul_bytes():
    payload = b"before\x00after"

    canonical, digest = M1PgBodyStore._verified_bytes(payload)

    assert canonical == payload
    assert digest == hashlib.sha256(payload).hexdigest()
    assert M1PgBodyStore._verify_row(_row(payload)) == payload


def test_pg_body_candidate_rejects_oversize_before_text_scan(monkeypatch):
    monkeypatch.setattr(m1_pg_body_store, "M1_PG_TEXT_MAX_BYTES", 4)

    with pytest.raises(ValidationError, match="10 MiB limit"):
        M1PgBodyStore._verified_bytes(b"12345")


def test_pg_body_candidate_rejects_manifest_drift():
    row = _row()
    row["digest"] = "0" * 64
    with pytest.raises(PgBodyIntegrityError, match="digest"):
        M1PgBodyStore._verify_row(row)


def test_pg_body_receipt_binds_exact_verified_bytes():
    row = {
        **_row(b"hello"),
        "payload_id": uuid.uuid4(),
        "namespace_id": uuid.uuid4(),
        "content_profile": "text",
    }

    receipt = M1PgBodyStore._receipt_from_row(row)

    assert receipt.payload_id == row["payload_id"]
    assert receipt.digest == row["digest"]
    assert receipt.byte_size == 5
    assert receipt.canonical_bytes == b"hello"


def test_native_grep_matcher_has_literal_regex_and_case_contract():
    insensitive = M1NativeGrepService._matcher("needle", regex=False, case_sensitive=False)
    sensitive = M1NativeGrepService._matcher("Needle", regex=False, case_sensitive=True)
    regex = M1NativeGrepService._matcher(r"need(le|ful)", regex=True, case_sensitive=False)

    assert insensitive("NEEDLE")
    assert sensitive("Needle")
    assert not sensitive("needle")
    assert regex("Needful")
    with pytest.raises(ValidationError, match="Invalid regex"):
        M1NativeGrepService._matcher("(", regex=True, case_sensitive=False)


@pytest.mark.asyncio
async def test_adversarial_regex_is_off_loop_and_fails_within_bound(monkeypatch):
    body = HeadBody(
        namespace_id=uuid.uuid4(),
        vault="measure",
        resource_id=uuid.uuid4(),
        surface="file",
        path="slow.txt",
        revision_id="a" * 40,
        digest="b" * 64,
        byte_size=27,
        canonical_bytes=(b"a" * 26) + b"!",
    )
    service = M1NativeGrepService(object())  # type: ignore[arg-type]

    async def head_bodies(**_kwargs):
        return [body]

    monkeypatch.setattr(service, "_head_bodies", head_bodies)
    monkeypatch.setattr(native_grep, "NATIVE_GREP_REGEX_TIMEOUT_SECONDS", 0.05)
    ticks = 0
    stop = False

    async def heartbeat():
        nonlocal ticks
        while not stop:
            await asyncio.sleep(0)
            ticks += 1

    ticker = asyncio.create_task(heartbeat())
    started = time.monotonic()
    try:
        with pytest.raises(AKBError, match="timed out") as error:
            await service.grep(
                r"(a+)+$", user_id=uuid.uuid4(), regex=True,
                include_text_files=True,
            )
    finally:
        stop = True
        await ticker

    assert error.value.status_code == 408
    assert time.monotonic() - started < 2
    assert ticks > 0


@pytest.mark.asyncio
async def test_bounded_regex_worker_preserves_exact_match_receipt(monkeypatch):
    body = HeadBody(
        namespace_id=uuid.uuid4(),
        vault="measure",
        resource_id=uuid.uuid4(),
        surface="file",
        path="src/main.py",
        revision_id="a" * 40,
        digest="b" * 64,
        byte_size=15,
        canonical_bytes=b"Needful\nneedle\n",
    )
    service = M1NativeGrepService(object())  # type: ignore[arg-type]

    async def head_bodies(**_kwargs):
        return [body]

    monkeypatch.setattr(service, "_head_bodies", head_bodies)
    result = await service.grep(
        r"need(le|ful)", user_id=uuid.uuid4(), regex=True,
        include_text_files=True,
    )

    assert result["searched_bytes"] == body.byte_size
    assert result["total_matches"] == 2
    assert result["results"] == [{
        "uri": body.uri,
        "resource_type": "file",
        "vault": "measure",
        "path": "src/main.py",
        "title": "main.py",
        "revision": body.revision_id,
        "content_hash": body.digest,
        "payload_placement": M1PgBodyStore.selected_placement,
        "matches": [
            {"line": 1, "text": "Needful"},
            {"line": 2, "text": "needle"},
        ],
    }]


@pytest.mark.asyncio
async def test_native_grep_rejects_unbounded_pattern_and_candidate_bytes(monkeypatch):
    service = M1NativeGrepService(object())  # type: ignore[arg-type]
    monkeypatch.setattr(native_grep, "NATIVE_GREP_MAX_PATTERN_BYTES", 8)

    with pytest.raises(ValidationError, match="pattern exceeds"):
        await service.grep("x" * 9, user_id=uuid.uuid4(), regex=True)

    body = HeadBody(
        namespace_id=uuid.uuid4(),
        vault="measure",
        resource_id=uuid.uuid4(),
        surface="document",
        path="large.md",
        revision_id="a" * 40,
        digest="b" * 64,
        byte_size=9,
        canonical_bytes=b"123456789",
    )

    async def head_bodies(**_kwargs):
        return [body]

    monkeypatch.setattr(service, "_head_bodies", head_bodies)
    monkeypatch.setattr(native_grep, "NATIVE_GREP_MAX_SEARCH_BYTES", 8)
    with pytest.raises(ValidationError, match="candidate bytes exceed"):
        await service.grep("x", user_id=uuid.uuid4())


@pytest.mark.asyncio
async def test_native_grep_rejects_file_replace_before_scan_or_mutation(monkeypatch):
    service = M1NativeGrepService(object())  # type: ignore[arg-type]
    scanned = False

    async def head_bodies(**_kwargs):
        nonlocal scanned
        scanned = True
        return []

    monkeypatch.setattr(service, "_head_bodies", head_bodies)

    with pytest.raises(ValidationError, match="does not support File resources"):
        await service.grep(
            "needle",
            user_id=uuid.uuid4(),
            replace="replacement",
            actor="tester",
            include_text_files=True,
        )
    assert scanned is False


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("selected_placement", "store_type"),
    (
        (M1PgBodyStore.selected_placement, M1PgBodyStore),
        (M1ReferencePayloadStore.selected_placement, M1ReferencePayloadStore),
    ),
)
async def test_native_grep_document_replace_uses_its_head_placement(
    monkeypatch,
    selected_placement,
    store_type,
):
    body_bytes = b"---\ntitle: Document\n---\nneedle body\n"
    row = {
        **_row(body_bytes),
        "selected_placement": selected_placement,
        "namespace_id": uuid.uuid4(),
        "vault": "measure",
        "resource_id": uuid.uuid4(),
        "surface": "document",
        "current_path": "notes/document.md",
        "head_revision_id": "a" * 40,
    }
    service = M1NativeGrepService(_Pool(_Conn(row)))
    calls = []
    stores = []

    async def require_write_access(**_kwargs):
        return None

    class _Native:
        async def replace_text(self, **kwargs):
            calls.append(kwargs)
            return type("Result", (), {"revision_id": "b" * 40})()

    def native_service(_pool, *, payload_store):
        stores.append(payload_store)
        return _Native()

    monkeypatch.setattr(service, "_require_write_access", require_write_access)
    monkeypatch.setattr(native_grep, "NativeRevisionService", native_service)

    result = await service.grep(
        "needle",
        user_id=uuid.uuid4(),
        replace="replacement",
        actor="tester",
    )

    assert calls[0]["surface"] == "document"
    assert "replacement body" in calls[0]["payload"]
    assert isinstance(stores[0], store_type)
    assert result["replaced_resources"] == 1


@pytest.mark.asyncio
async def test_native_grep_replace_uses_full_scope_beyond_response_limit(monkeypatch):
    body_bytes = b"---\ntitle: Document\n---\nTODO item\n"
    bodies = [
        HeadBody(
            namespace_id=uuid.uuid4(),
            vault="measure",
            resource_id=uuid.uuid4(),
            surface="document",
            path=f"notes/{index}.md",
            revision_id=str(index) * 40,
            digest=hashlib.sha256(body_bytes).hexdigest(),
            byte_size=len(body_bytes),
            canonical_bytes=body_bytes,
        )
        for index in range(1, 4)
    ]
    service = M1NativeGrepService(object())  # type: ignore[arg-type]
    calls = []

    async def head_bodies(**_kwargs):
        return bodies

    async def require_write_access(**_kwargs):
        return None

    class _Native:
        async def replace_text(self, **kwargs):
            calls.append(kwargs)
            return type(
                "Result",
                (),
                {
                    "revision_id": f"new-{len(calls)}",
                    "parent_revision_id": kwargs["expected_revision_id"],
                },
            )()

    monkeypatch.setattr(service, "_head_bodies", head_bodies)
    monkeypatch.setattr(service, "_require_write_access", require_write_access)
    monkeypatch.setattr(native_grep, "NativeRevisionService", lambda *_args, **_kwargs: _Native())

    replacement = r"C:\temp\1"
    result = await service.grep_public(
        "TODO",
        user_id=uuid.uuid4(),
        replace=replacement,
        actor="tester",
        limit=1,
        max_replacements=3,
    )

    assert result["returned_docs"] == 1
    assert result["total_docs"] == 3
    assert result["replaced_docs"] == 3
    assert len(result["replacements"]) == 3
    assert [row["previous_commit"] for row in result["replacements"]] == [
        body.revision_id for body in bodies
    ]
    assert len(calls) == 3
    assert all(f"{replacement} item" in call["payload"] for call in calls)


@pytest.mark.asyncio
async def test_native_grep_replace_budget_fails_before_mutation(monkeypatch):
    body_bytes = b"---\ntitle: Document\n---\nTODO item\n"
    bodies = [
        HeadBody(
            namespace_id=uuid.uuid4(),
            vault="measure",
            resource_id=uuid.uuid4(),
            surface="document",
            path=f"notes/{index}.md",
            revision_id=str(index) * 40,
            digest=hashlib.sha256(body_bytes).hexdigest(),
            byte_size=len(body_bytes),
            canonical_bytes=body_bytes,
        )
        for index in range(1, 4)
    ]
    service = M1NativeGrepService(object())  # type: ignore[arg-type]
    calls = []

    async def head_bodies(**_kwargs):
        return bodies

    class _Native:
        async def replace_text(self, **kwargs):
            calls.append(kwargs)

    monkeypatch.setattr(service, "_head_bodies", head_bodies)
    monkeypatch.setattr(native_grep, "NativeRevisionService", lambda *_args, **_kwargs: _Native())

    result = await service.grep_public(
        "TODO",
        user_id=uuid.uuid4(),
        replace="done",
        actor="tester",
        limit=1,
        max_replacements=2,
    )

    assert result["code"] == "bulk_too_large"
    assert result["replacement_complete"] is False
    assert result["replacements"] == []
    assert calls == []


@pytest.mark.asyncio
async def test_native_grep_prevalidates_all_head_placements_before_replacing(monkeypatch):
    body_bytes = b"---\ntitle: Document\n---\nneedle body\n"
    bodies = [
        HeadBody(
            namespace_id=uuid.uuid4(),
            vault="measure",
            resource_id=uuid.uuid4(),
            surface="document",
            path="notes/first.md",
            revision_id="a" * 40,
            digest=hashlib.sha256(body_bytes).hexdigest(),
            byte_size=len(body_bytes),
            canonical_bytes=body_bytes,
            selected_placement=M1PgBodyStore.selected_placement,
        ),
        HeadBody(
            namespace_id=uuid.uuid4(),
            vault="measure",
            resource_id=uuid.uuid4(),
            surface="document",
            path="notes/unknown.md",
            revision_id="b" * 40,
            digest=hashlib.sha256(body_bytes).hexdigest(),
            byte_size=len(body_bytes),
            canonical_bytes=body_bytes,
            selected_placement="unknown-placement-v1",
        ),
    ]
    service = M1NativeGrepService(object())  # type: ignore[arg-type]
    mutations = []

    async def head_bodies(**_kwargs):
        return bodies

    async def require_write_access(**_kwargs):
        return None

    class _Native:
        async def replace_text(self, **kwargs):
            mutations.append(kwargs)
            return type("Result", (), {"revision_id": "c" * 40})()

    monkeypatch.setattr(service, "_head_bodies", head_bodies)
    monkeypatch.setattr(service, "_require_write_access", require_write_access)
    monkeypatch.setattr(native_grep, "NativeRevisionService", lambda *_args, **_kwargs: _Native())

    with pytest.raises(NativePayloadPlacementError, match="Unsupported native payload placement"):
        await service.grep(
            "needle",
            user_id=uuid.uuid4(),
            replace="replacement",
            actor="tester",
        )

    assert mutations == []


def test_regex_process_start_is_inside_wall_clock_deadline(monkeypatch):
    release_start = threading.Event()
    terminated = threading.Event()
    slot_released = threading.Event()

    class _Slot:
        releases = 0

        @staticmethod
        def acquire(*, blocking):
            assert blocking is False
            return True

        def release(self):
            self.releases += 1
            slot_released.set()

    slot = _Slot()

    class _PipeEnd:
        def recv_bytes(self):
            return b'["ok", {}]'

        def send_bytes(self, _message):
            return None

        def close(self):
            return None

    class _SlowProcess:
        def __init__(self, **_kwargs):
            self.alive = False

        def start(self):
            release_start.wait(timeout=1)
            self.alive = True

        def is_alive(self):
            return self.alive

        def terminate(self):
            self.alive = False
            terminated.set()

        def kill(self):
            self.terminate()

        def join(self, _timeout=None):
            return None

    class _Context:
        @staticmethod
        def Pipe(*, duplex):
            assert duplex is False
            return _PipeEnd(), _PipeEnd()

        @staticmethod
        def Process(**kwargs):
            return _SlowProcess(**kwargs)

    monkeypatch.setattr(native_grep.multiprocessing, "get_context", lambda _method: _Context())
    monkeypatch.setattr(native_grep, "_REGEX_PROCESS_SLOTS", slot)
    timer = threading.Timer(0.2, release_start.set)
    timer.start()
    started = time.monotonic()
    try:
        with pytest.raises(native_grep._RegexScanTimedOut):
            native_grep._run_regex_bounded(lambda: {}, (), {}, 0.03)
        assert time.monotonic() - started < 0.15
        assert slot.releases == 0
    finally:
        release_start.set()
        timer.cancel()
    assert terminated.wait(timeout=1)
    assert slot_released.wait(timeout=1)
    assert slot.releases == 1


class _Acquire:
    def __init__(self, conn):
        self.conn = conn

    async def __aenter__(self):
        return self.conn

    async def __aexit__(self, *_args):
        return None


class _Conn:
    def __init__(self, row, *, aggregate=None):
        self.row = row
        self.aggregate = aggregate or {
            "resource_count": 1,
            "total_bytes": row["byte_size"],
        }
        self.sql = ""
        self.aggregate_sql = ""
        self.params = ()
        self.body_fetches = 0

    def transaction(self, **_kwargs):
        return _Acquire(self)

    async def fetchrow(self, sql, *params):
        self.aggregate_sql = sql
        self.params = params
        return self.aggregate

    async def fetch(self, sql, *params):
        self.sql = sql
        self.params = params
        self.body_fetches += 1
        return [self.row]


class _Pool:
    def __init__(self, conn):
        self.conn = conn

    def acquire(self):
        return _Acquire(self.conn)


@pytest.mark.asyncio
async def test_head_body_query_intersects_acl_and_pins_current_revision():
    body = b"needle\n"
    resource_id = uuid.uuid4()
    row = {
        **_row(body),
        "namespace_id": uuid.uuid4(),
        "vault": "engineering",
        "resource_id": resource_id,
        "surface": "file",
        "current_path": "src/main.py",
        "head_revision_id": "a" * 40,
    }
    conn = _Conn(row)
    service = M1NativeGrepService(_Pool(conn))

    bodies = await service._head_bodies(
        user_id=uuid.uuid4(),
        vaults=["engineering"],
        collection="src",
        resource_id=resource_id,
        surfaces=("document", "file"),
    )

    assert len(bodies) == 1
    assert bodies[0].revision_id == "a" * 40
    assert bodies[0].selected_placement == M1PgBodyStore.selected_placement
    assert bodies[0].text == "needle\n"
    assert "rs.head_revision_id" in conn.sql
    assert "vault_access" in conn.sql
    assert "pm.selected_placement =" not in conn.sql
    assert "substring(" in conn.sql
    assert "ESCAPE" in conn.sql
    assert conn.params[3:] == ("src", "src/%", resource_id)
    assert "rs.surface = ANY" in conn.sql


@pytest.mark.asyncio
async def test_head_body_query_accepts_reference_payload_documents_for_public_grep():
    """Public native documents may retain the approved reference placement."""
    body = b"needle\n"
    row = {
        **_row(body),
        "selected_placement": M1ReferencePayloadStore.selected_placement,
        "namespace_id": uuid.uuid4(),
        "vault": "engineering",
        "resource_id": uuid.uuid4(),
        "surface": "document",
        "current_path": "r5/literal.md",
        "head_revision_id": "a" * 40,
    }
    conn = _Conn(row)
    service = M1NativeGrepService(_Pool(conn))

    result = await service.grep_public(
        "needle",
        user_id=uuid.uuid4(),
        vaults=["engineering"],
        collection=None,
    )

    assert result["total_docs"] == 1
    assert [item["path"] for item in result["results"]] == ["r5/literal.md"]
    assert "pm.selected_placement =" not in conn.sql


@pytest.mark.asyncio
async def test_head_body_query_rejects_unknown_manifest_placement():
    row = {
        **_row(),
        "selected_placement": "unknown-placement-v1",
        "namespace_id": uuid.uuid4(),
        "vault": "engineering",
        "resource_id": uuid.uuid4(),
        "surface": "document",
        "current_path": "r5/unknown.md",
        "head_revision_id": "a" * 40,
    }

    with pytest.raises(NativePayloadPlacementError, match="Unsupported native payload placement"):
        await M1NativeGrepService(_Pool(_Conn(row)))._head_bodies(
            user_id=uuid.uuid4(),
            vaults=["engineering"],
            collection=None,
            resource_id=None,
            surfaces=("document",),
        )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("aggregate", "message"),
    [
        ({"resource_count": 10_001, "total_bytes": 10_001}, "candidate resources exceed"),
        ({"resource_count": 1, "total_bytes": 128 * 1024 * 1024 + 1}, "candidate bytes exceed"),
    ],
)
async def test_head_body_query_rejects_aggregate_before_materializing_bodies(
    aggregate, message,
):
    conn = _Conn(_row(), aggregate=aggregate)
    service = M1NativeGrepService(_Pool(conn))

    with pytest.raises(ValidationError, match=message):
        await service._head_bodies(
            user_id=uuid.uuid4(),
            vaults=None,
            collection=None,
            resource_id=None,
            surfaces=("document", "file"),
        )

    assert conn.body_fetches == 0
    assert "canonical_bytes" not in conn.aggregate_sql


def test_public_surface_selection_keeps_w3a_document_only_and_guards_w3b():
    assert M1NativeGrepService._selected_surfaces(include_text_files=False) == ("document",)
    assert M1NativeGrepService._selected_surfaces(include_text_files=True) == (
        "document",
        "file",
    )


def _grep_body(*, text: str, path: str = "bounded.txt") -> HeadBody:
    body = text.encode()
    return HeadBody(
        namespace_id=uuid.uuid4(),
        vault="measure",
        resource_id=uuid.uuid4(),
        surface="file",
        path=path,
        revision_id="a" * 40,
        digest=hashlib.sha256(body).hexdigest(),
        byte_size=len(body),
        canonical_bytes=body,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("regex", [False, True])
async def test_native_grep_rejects_empty_patterns_before_scanning(monkeypatch, regex):
    service = M1NativeGrepService(object())  # type: ignore[arg-type]
    scanned = False

    async def head_bodies(**_kwargs):
        nonlocal scanned
        scanned = True
        return []

    monkeypatch.setattr(service, "_head_bodies", head_bodies)

    with pytest.raises(ValidationError, match="pattern must not be empty"):
        await service.grep("", user_id=uuid.uuid4(), regex=regex)
    assert scanned is False


@pytest.mark.asyncio
async def test_literal_grep_caps_materialized_matches_but_keeps_exact_totals(monkeypatch):
    first = _grep_body(text="needle one\nneedle two\nneedle three\n", path="first.txt")
    second = _grep_body(text="needle four\nneedle five\nneedle six\n", path="second.txt")
    service = M1NativeGrepService(object())  # type: ignore[arg-type]

    async def head_bodies(**_kwargs):
        return [first, second]

    monkeypatch.setattr(service, "_head_bodies", head_bodies)
    monkeypatch.setattr(native_grep, "NATIVE_GREP_MAX_MATCHES_PER_RESOURCE", 2)
    monkeypatch.setattr(native_grep, "NATIVE_GREP_MAX_TOTAL_MATCHES", 3)

    result = await service.grep(
        "needle", user_id=uuid.uuid4(), include_text_files=True,
    )

    assert result["total_resources"] == 2
    assert result["total_matches"] == 6
    assert result["returned_matches"] == 3
    assert [len(row["matches"]) for row in result["results"]] == [2, 1]
    assert result["truncated"] is True
    assert result["truncation"]["reasons"] == [
        "per_resource_match_limit",
        "total_match_limit",
    ]
    assert result["truncation"]["limits"]["matches_per_resource"] == 2
    assert result["truncation"]["limits"]["total_matches"] == 3


@pytest.mark.asyncio
async def test_grep_caps_each_snippet_and_per_resource_and_total_snippet_bytes(monkeypatch):
    first = _grep_body(text=("needle " + "x" * 100 + "\n") * 3, path="first.txt")
    second = _grep_body(text=("needle " + "y" * 100 + "\n") * 3, path="second.txt")
    service = M1NativeGrepService(object())  # type: ignore[arg-type]

    async def head_bodies(**_kwargs):
        return [first, second]

    monkeypatch.setattr(service, "_head_bodies", head_bodies)
    monkeypatch.setattr(native_grep, "NATIVE_GREP_MAX_SNIPPET_BYTES", 8)
    monkeypatch.setattr(native_grep, "NATIVE_GREP_MAX_SNIPPET_BYTES_PER_RESOURCE", 16)
    monkeypatch.setattr(native_grep, "NATIVE_GREP_MAX_TOTAL_SNIPPET_BYTES", 24)

    result = await service.grep(
        "needle", user_id=uuid.uuid4(), include_text_files=True,
    )
    snippets = [
        match["text"].encode()
        for row in result["results"]
        for match in row["matches"]
    ]

    assert result["total_matches"] == 6
    assert sum(map(len, snippets)) <= 24
    assert all(len(snippet) <= 8 for snippet in snippets)
    assert result["truncation"]["reasons"] == [
        "per_resource_snippet_byte_limit",
        "snippet_byte_limit",
        "total_snippet_byte_limit",
    ]


@pytest.mark.asyncio
async def test_count_only_counts_without_materializing_snippets(monkeypatch):
    body = _grep_body(text=("needle " + "x" * 10_000 + "\n") * 5)
    service = M1NativeGrepService(object())  # type: ignore[arg-type]

    async def head_bodies(**_kwargs):
        return [body]

    monkeypatch.setattr(service, "_head_bodies", head_bodies)
    monkeypatch.setattr(native_grep, "NATIVE_GREP_MAX_MATCHES_PER_RESOURCE", 0)
    monkeypatch.setattr(native_grep, "NATIVE_GREP_MAX_TOTAL_MATCHES", 0)
    monkeypatch.setattr(native_grep, "NATIVE_GREP_MAX_SNIPPET_BYTES", 0)
    monkeypatch.setattr(native_grep, "NATIVE_GREP_MAX_SNIPPET_BYTES_PER_RESOURCE", 0)
    monkeypatch.setattr(native_grep, "NATIVE_GREP_MAX_TOTAL_SNIPPET_BYTES", 0)

    result = await service.grep(
        "needle", user_id=uuid.uuid4(), include_text_files=True, count_only=True,
    )

    assert result == {
        "pattern": "needle",
        "regex": False,
        "searched_resources": 1,
        "searched_bytes": body.byte_size,
        "total_resources": 1,
        "total_matches": 5,
        "by_resource": {body.uri: 5},
    }


@pytest.mark.asyncio
async def test_regex_worker_returns_bounded_materialization(monkeypatch):
    body = _grep_body(text="".join(f"line {number}\n" for number in range(20)))
    service = M1NativeGrepService(object())  # type: ignore[arg-type]

    async def head_bodies(**_kwargs):
        return [body]

    monkeypatch.setattr(service, "_head_bodies", head_bodies)
    monkeypatch.setattr(native_grep, "NATIVE_GREP_MAX_MATCHES_PER_RESOURCE", 2)
    monkeypatch.setattr(native_grep, "NATIVE_GREP_MAX_TOTAL_MATCHES", 2)

    result = await service.grep(
        ".*", user_id=uuid.uuid4(), regex=True, include_text_files=True,
    )

    assert result["total_matches"] == 20
    assert result["returned_matches"] == 2
    assert len(result["results"][0]["matches"]) == 2
    assert result["truncated"] is True


@pytest.mark.asyncio
async def test_regex_worker_rejects_serialized_output_over_hard_cap(monkeypatch):
    body = _grep_body(text="needle\n")
    service = M1NativeGrepService(object())  # type: ignore[arg-type]

    async def head_bodies(**_kwargs):
        return [body]

    monkeypatch.setattr(service, "_head_bodies", head_bodies)
    monkeypatch.setattr(native_grep, "NATIVE_GREP_MAX_CHILD_RESULT_BYTES", 64)

    with pytest.raises(ValidationError, match="bounded worker output"):
        await service.grep(
            "needle", user_id=uuid.uuid4(), regex=True, include_text_files=True,
        )


@pytest.mark.asyncio
async def test_grep_items_report_each_head_placement_without_any_locator(monkeypatch):
    """Placement is observable per resource; addresses never are."""
    unified = HeadBody(
        namespace_id=uuid.uuid4(),
        vault="measure",
        resource_id=uuid.uuid4(),
        surface="document",
        path="notes/unified.md",
        revision_id="a" * 40,
        digest="b" * 64,
        byte_size=len(b"needle unified\n"),
        canonical_bytes=b"needle unified\n",
        selected_placement=M1PgBodyStore.selected_placement,
    )
    historical = HeadBody(
        namespace_id=unified.namespace_id,
        vault="measure",
        resource_id=uuid.uuid4(),
        surface="document",
        path="notes/historical.md",
        revision_id="c" * 40,
        digest="d" * 64,
        byte_size=len(b"needle historical\n"),
        canonical_bytes=b"needle historical\n",
        selected_placement=M1ReferencePayloadStore.selected_placement,
    )
    text_file = HeadBody(
        namespace_id=unified.namespace_id,
        vault="measure",
        resource_id=uuid.uuid4(),
        surface="file",
        path="src/main.py",
        revision_id="e" * 40,
        digest="f" * 64,
        byte_size=len(b"needle file\n"),
        canonical_bytes=b"needle file\n",
        selected_placement=M1PgBodyStore.selected_placement,
    )
    service = M1NativeGrepService(object())  # type: ignore[arg-type]

    async def head_bodies(**_kwargs):
        return [unified, historical, text_file]

    monkeypatch.setattr(service, "_head_bodies", head_bodies)
    internal = await service.grep(
        "needle", user_id=uuid.uuid4(), include_text_files=True,
    )
    public = M1NativeGrepService._public_response(
        pattern="needle", regex=False, native=internal,
    )

    assert [row["payload_placement"] for row in internal["results"]] == [
        M1PgBodyStore.selected_placement,
        M1ReferencePayloadStore.selected_placement,
        M1PgBodyStore.selected_placement,
    ]
    assert [row["payload_placement"] for row in public["results"]] == [
        M1PgBodyStore.selected_placement,
        M1ReferencePayloadStore.selected_placement,
        M1PgBodyStore.selected_placement,
    ]
    forbidden = {"payload_id", "private_locator", "payload_manifest_id", "namespace_id"}
    for row in internal["results"] + public["results"]:
        assert forbidden.isdisjoint(row)


@pytest.mark.asyncio
async def test_namespace_placement_totals_group_without_ids_or_digest_values():
    class _AggregateConn:
        def __init__(self):
            self.sql = ""
            self.params = ()

        async def fetch(self, sql, *params):
            self.sql = sql
            self.params = params
            return [
                {
                    "selected_placement": M1ReferencePayloadStore.selected_placement,
                    "bodies": 2,
                    "body_bytes": 30,
                    "distinct_digests": 1,
                },
                {
                    "selected_placement": M1PgBodyStore.selected_placement,
                    "bodies": 3,
                    "body_bytes": 44,
                    "distinct_digests": 3,
                },
            ]

    conn = _AggregateConn()
    namespace_id = uuid.uuid4()

    totals = await M1PgBodyStore(_Pool(conn)).namespace_placement_totals(namespace_id)

    assert [(row.selected_placement, row.bodies, row.body_bytes, row.distinct_digests) for row in totals] == [
        (M1ReferencePayloadStore.selected_placement, 2, 30, 1),
        (M1PgBodyStore.selected_placement, 3, 44, 3),
    ]
    assert conn.params == (namespace_id,)
    assert "GROUP BY selected_placement" in conn.sql
    # The projection must stay an aggregate: no addresses, no bodies, and the
    # digest column only ever reduced to a cardinality.
    for forbidden in ("payload_id", "private_locator", "canonical_bytes", "prepared_at"):
        assert forbidden not in conn.sql
    assert re.findall(r"\bdigest\b", conn.sql) == ["digest"]
    assert "COUNT(DISTINCT digest)" in conn.sql


def test_regex_worker_rejects_when_process_slots_are_exhausted(monkeypatch):
    class NoSlot:
        def acquire(self, *, blocking):
            assert blocking is False
            return False

    monkeypatch.setattr(native_grep, "_REGEX_PROCESS_SLOTS", NoSlot())

    with pytest.raises(AKBError, match="capacity exhausted") as error:
        native_grep._run_regex_bounded(lambda: None, (), {}, 0.1)
    assert error.value.status_code == 429
