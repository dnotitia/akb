from types import SimpleNamespace

from app.exceptions import ConflictError
from app.services import skill_reservation_backfill as backfill
from app.services.skill_reservation_backfill import classify_violation, _move_target

ACTOR = backfill._ACTOR


def test_ordinary_doc_in_overview():
    row = {"path": "overview/notes.md", "doc_type": "note"}
    assert classify_violation(row) == "move_out"


def test_stray_skill_type():
    row = {"path": "notes/guide.md", "doc_type": "skill"}
    assert classify_violation(row) == "retype"


def test_canonical_retyped():
    row = {"path": "overview/vault-skill.md", "doc_type": "note"}
    assert classify_violation(row) == "restore_type"


def test_canonical_correct_is_clean():
    row = {"path": "overview/vault-skill.md", "doc_type": "skill"}
    assert classify_violation(row) is None


def test_normal_doc_clean():
    row = {"path": "notes/a.md", "doc_type": "note"}
    assert classify_violation(row) is None


def test_move_target_nested():
    assert _move_target("overview/a/b.md") == ("a", "b")


def test_move_target_root():
    assert _move_target("overview/x.md") == (None, "x")


# ── execute path ──────────────────────────────────────────────────────
# Fakes only: the scans are monkeypatched and the document service is a
# recorder, so the treatment/dispatch logic is covered without a database.


class _FakeDocService:
    """Records every mutator call; raises from a per-method queue when primed."""

    def __init__(self, *, move_raises=(), update_raises=(), put_raises=()):
        self.calls: list[tuple] = []
        self._move_raises = list(move_raises)
        self._update_raises = list(update_raises)
        self._put_raises = list(put_raises)

    @staticmethod
    def _pop(queue):
        return queue.pop(0) if queue else None

    async def move(self, vault, doc_ref, *, collection=None, slug=None,
                   message=None, agent_id=None, skill_internal=False):
        self.calls.append(
            ("move", vault, doc_ref, collection, slug, agent_id, skill_internal)
        )
        exc = self._pop(self._move_raises)
        if exc is not None:
            raise exc
        return SimpleNamespace(
            path=f"{collection}/{slug}.md" if collection else f"{slug}.md"
        )

    async def update(self, vault, doc_ref, req, agent_id=None):
        self.calls.append(
            ("update", vault, doc_ref, req.type, req.message, agent_id)
        )
        exc = self._pop(self._update_raises)
        if exc is not None:
            raise exc
        return SimpleNamespace(path=doc_ref)

    async def put(self, req, agent_id=None, *, skill_internal=False):
        self.calls.append(
            ("put", req.vault, req.collection, req.slug, req.type,
             agent_id, skill_internal)
        )
        exc = self._pop(self._put_raises)
        if exc is not None:
            raise exc
        return SimpleNamespace(path=f"{req.collection}/{req.slug}.md")


_NO_RESOURCES = {
    "resource_violations": {"files": 0, "tables": 0},
    "reserved_subcollections": 0,
}


def _patch_scans(monkeypatch, *, rows=(), missing=(),
                 archived_rows=(), archived_missing=(),
                 files=0, tables=0, subcollections=0):
    """Route every scan by vault-status scope, so the archived-vault exclusion
    is exercised through the real dispatch rather than stubbed away."""
    async def fake_violations(scope):
        source = archived_rows if scope == backfill._ARCHIVED_ONLY else rows
        return [dict(r) for r in source]

    async def fake_missing(scope):
        source = archived_missing if scope == backfill._ARCHIVED_ONLY else missing
        return list(source)

    async def fake_resources(scope):
        return {"files": files, "tables": tables}

    async def fake_subcollections(scope):
        return subcollections

    monkeypatch.setattr(backfill, "_scan_violations", fake_violations)
    monkeypatch.setattr(backfill, "_scan_missing", fake_missing)
    monkeypatch.setattr(backfill, "_count_reserved_resources", fake_resources)
    monkeypatch.setattr(backfill, "_count_reserved_subcollections", fake_subcollections)


async def test_execute_move_out_uses_bypass_and_root_or_nested_collection(monkeypatch):
    _patch_scans(monkeypatch, rows=[
        {"path": "overview/x.md", "doc_type": "note", "vault_name": "v1"},
        {"path": "overview/a/b.md", "doc_type": "note", "vault_name": "v1"},
    ])
    svc = _FakeDocService()

    result = await backfill.run(svc, execute=True)

    assert result["done"]["move_out"] == 2
    assert result["errors"] == []
    assert svc.calls == [
        ("move", "v1", "overview/x.md", "", "x", ACTOR, True),
        ("move", "v1", "overview/a/b.md", "a", "b", ACTOR, True),
    ]


async def test_execute_move_out_retries_once_with_suffix_on_any_conflict(monkeypatch):
    # The orphan-publication refusal, deliberately: the retry is not keyed to
    # the "already exists" wording.
    _patch_scans(monkeypatch, rows=[
        {"path": "overview/x.md", "doc_type": "note", "vault_name": "v1"},
    ])
    svc = _FakeDocService(move_raises=[ConflictError("claimed by a public link")])

    result = await backfill.run(svc, execute=True)

    assert result["done"]["move_out"] == 1
    assert result["errors"] == []
    assert [c[4] for c in svc.calls] == ["x", "x-from-overview"]


async def test_execute_second_conflict_is_recorded_and_batch_continues(monkeypatch):
    _patch_scans(monkeypatch, rows=[
        {"path": "overview/x.md", "doc_type": "note", "vault_name": "v1"},
        {"path": "overview/y.md", "doc_type": "note", "vault_name": "v1"},
    ])
    svc = _FakeDocService(move_raises=[
        ConflictError("Document already exists at path: x.md"),
        ConflictError("Document already exists at path: x-from-overview.md"),
    ])

    result = await backfill.run(svc, execute=True)

    assert result["done"]["move_out"] == 1  # the next item still ran
    assert len(result["errors"]) == 1
    assert result["errors"][0]["path"] == "v1:overview/x.md"
    assert [c[2] for c in svc.calls] == [
        "overview/x.md", "overview/x.md", "overview/y.md",
    ]


async def test_execute_retype_and_restore_type_update_requests(monkeypatch):
    _patch_scans(monkeypatch, rows=[
        {"path": "notes/guide.md", "doc_type": "skill", "vault_name": "v1"},
        {"path": "overview/vault-skill.md", "doc_type": "note", "vault_name": "v2"},
    ])
    svc = _FakeDocService()

    result = await backfill.run(svc, execute=True)

    assert result["done"]["retype"] == 1
    assert result["done"]["restore_type"] == 1
    assert svc.calls == [
        ("update", "v1", "notes/guide.md", "note",
         "skill-reservation backfill: retype", ACTOR),
        ("update", "v2", "overview/vault-skill.md", "skill",
         "skill-reservation backfill: restore", ACTOR),
    ]


async def test_execute_reseed_puts_seed_with_bypass(monkeypatch):
    _patch_scans(monkeypatch, missing=["v3"])
    svc = _FakeDocService()

    result = await backfill.run(svc, execute=True)

    assert result["done"]["reseed"] == 1
    assert svc.calls == [
        ("put", "v3", "overview", "vault-skill", "skill", ACTOR, True),
    ]


async def test_execute_move_out_of_skill_typed_doc_also_retypes(monkeypatch):
    # Both violations at once: classified move_out (path rule wins), and the
    # stray skill type is closed at the NEW path in the same pass.
    _patch_scans(monkeypatch, rows=[
        {"path": "overview/rogue.md", "doc_type": "skill", "vault_name": "v1"},
    ])
    svc = _FakeDocService()

    result = await backfill.run(svc, execute=True)

    assert result["done"]["move_out"] == 1
    assert result["done"]["retype"] == 0
    assert svc.calls == [
        ("move", "v1", "overview/rogue.md", "", "rogue", ACTOR, True),
        ("update", "v1", "rogue.md", "note",
         "skill-reservation backfill: retype", ACTOR),
    ]


async def test_dry_run_reports_counts_and_mutates_nothing(monkeypatch):
    _patch_scans(
        monkeypatch,
        rows=[
            {"path": "overview/x.md", "doc_type": "note", "vault_name": "v1"},
            {"path": "notes/g.md", "doc_type": "skill", "vault_name": "v1"},
            {"path": "overview/vault-skill.md", "doc_type": "note", "vault_name": "v2"},
            {"path": "overview/vault-skill.md", "doc_type": "skill", "vault_name": "v1"},
        ],
        missing=["v3"],
        archived_rows=[
            {"path": "overview/old.md", "doc_type": "note", "vault_name": "arc"},
            {"path": "overview/vault-skill.md", "doc_type": "skill", "vault_name": "arc"},
        ],
        archived_missing=["arc2"],
    )
    svc = _FakeDocService()

    result = await backfill.run(svc, execute=False)

    # The healthy canonical rows count toward nothing, in either scope.
    assert result == {
        "dry_run": True, "move_out": 1, "retype": 1, "restore_type": 1,
        "reseed": 1, "archived_excluded": 2, **_NO_RESOURCES,
    }
    assert svc.calls == []


async def test_include_archived_widens_scope_and_zeroes_the_excluded_count(monkeypatch):
    _patch_scans(
        monkeypatch,
        rows=[{"path": "overview/x.md", "doc_type": "note", "vault_name": "arc"}],
        archived_rows=[{"path": "overview/never-read.md", "doc_type": "note",
                        "vault_name": "arc"}],
    )
    svc = _FakeDocService()

    result = await backfill.run(svc, execute=False, include_archived=True)

    assert result == {
        "dry_run": True, "move_out": 1, "retype": 0, "restore_type": 0,
        "reseed": 0, "archived_excluded": 0, **_NO_RESOURCES,
    }


# ── Resource violations (report-only) ─────────────────────────────────
# Files and tables under overview/ have no move operation, so the backfill
# can only COUNT them. Reporting the count is what stops a clean doc run from
# reading as "the namespace is clean".


async def test_dry_run_reports_files_and_tables_under_overview(monkeypatch):
    _patch_scans(monkeypatch, files=3, tables=2, subcollections=4)
    svc = _FakeDocService()

    result = await backfill.run(svc, execute=False)

    assert result["resource_violations"] == {"files": 3, "tables": 2}
    assert result["reserved_subcollections"] == 4
    assert svc.calls == []  # counted, never treated


async def test_execute_reports_resource_violations_without_treating_them(monkeypatch):
    _patch_scans(
        monkeypatch,
        rows=[{"path": "overview/x.md", "doc_type": "note", "vault_name": "v1"}],
        files=1, tables=0, subcollections=2,
    )
    svc = _FakeDocService()

    result = await backfill.run(svc, execute=True)

    assert result["done"]["move_out"] == 1
    assert result["resource_violations"] == {"files": 1, "tables": 0}
    assert result["reserved_subcollections"] == 2
    # Only the document move — no file/table/collection mutation exists.
    assert [c[0] for c in svc.calls] == ["move"]


async def test_execute_clean_run_reports_zero_resource_violations(monkeypatch):
    _patch_scans(monkeypatch)
    svc = _FakeDocService()

    result = await backfill.run(svc, execute=True)

    assert result["resource_violations"] == {"files": 0, "tables": 0}
    assert result["reserved_subcollections"] == 0
