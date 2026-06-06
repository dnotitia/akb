"""Unit tests for the compliance-grade audit producer (`audit_log`).

Covers the properties that make the stream "audit" rather than "logs":
append + stable schema, monotonic seq, hash-chain integrity + tamper
detection, chain re-seed across a simulated restart, the read-skip /
write-always policy, the never-raise contract, and the upload→prune
local-file lifecycle (with a fake S3 so no boto3/network is touched).
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

from app.config import settings
from app.services import audit_log


def _today() -> str:
    return datetime.now(timezone.utc).date().isoformat()


def _day_offset(days: int) -> str:
    return (datetime.now(timezone.utc).date() + timedelta(days=days)).isoformat()


@pytest.fixture
def audit_dir(tmp_path, monkeypatch):
    """Enable audit into an isolated tmp dir and seed a fresh chain."""
    monkeypatch.setattr(settings.audit, "enabled", True)
    monkeypatch.setattr(settings.audit, "log_dir", str(tmp_path))
    monkeypatch.setattr(settings.audit, "log_reads", True)
    monkeypatch.setattr(settings.audit, "bucket", "")
    monkeypatch.setattr(settings.audit, "local_retention_days", 2)
    audit_log.init()
    return tmp_path


def _read_lines(path) -> list[str]:
    return [ln for ln in path.read_text(encoding="utf-8").splitlines() if ln.strip()]


def test_record_appends_with_stable_schema(audit_dir):
    audit_log.record(action="akb_put", actor="alice", actor_id="u1",
                     vault="v", target="path=docs/x.md")
    f = audit_dir / f"akb-audit-{_today()}.jsonl"
    lines = _read_lines(f)
    assert len(lines) == 1
    row = json.loads(lines[0])
    assert row["action"] == "akb_put"
    assert row["actor"] == "alice"
    assert row["seq"] == 1
    assert row["outcome"] == "ok"
    assert len(row["h"]) == 64           # sha256 hex
    assert set(("v", "ts", "seq", "action", "h")) <= set(row)


def test_seq_monotonic_and_chain_verifies(audit_dir):
    for i in range(5):
        audit_log.record(action="akb_put", actor="a", target=f"id=d-{i}")
    lines = _read_lines(audit_dir / f"akb-audit-{_today()}.jsonl")
    seqs = [json.loads(ln)["seq"] for ln in lines]
    assert seqs == [1, 2, 3, 4, 5]
    ok, bad = audit_log.verify_chain(lines)
    assert ok and bad == -1


def test_tamper_breaks_chain(audit_dir):
    for i in range(3):
        audit_log.record(action="akb_delete", actor="a", target=f"id=d-{i}")
    lines = _read_lines(audit_dir / f"akb-audit-{_today()}.jsonl")
    # Mutate the middle line's target without recomputing its hash.
    middle = json.loads(lines[1])
    middle["target"] = "id=tampered"
    lines[1] = json.dumps(middle, ensure_ascii=False)
    ok, bad = audit_log.verify_chain(lines)
    assert not ok
    assert bad == 2                       # seq of the tampered line


def test_chain_reseeds_after_restart(audit_dir):
    audit_log.record(action="akb_put", actor="a", target="id=1")
    audit_log.record(action="akb_put", actor="a", target="id=2")
    # Simulate a process restart: re-init re-seeds seq + prev from disk.
    audit_log.init()
    audit_log.record(action="akb_put", actor="a", target="id=3")
    lines = _read_lines(audit_dir / f"akb-audit-{_today()}.jsonl")
    assert [json.loads(ln)["seq"] for ln in lines] == [1, 2, 3]
    ok, _ = audit_log.verify_chain(lines)
    assert ok                              # chain unbroken across the restart


def test_reads_skipped_when_disabled_but_writes_kept(audit_dir, monkeypatch):
    monkeypatch.setattr(settings.audit, "log_reads", False)

    class _U:
        username, user_id = "bob", "u2"

    audit_log.record_tool("akb_search", {"vault": "v", "query": "hi"}, _U(), {"results": []})
    audit_log.record_tool("akb_put", {"vault": "v", "path": "p"}, _U(), {"ok": True})
    # Unknown (non-read) tool must always be logged.
    audit_log.record_tool("akb_some_new_write", {"vault": "v"}, _U(), {"ok": True})

    lines = _read_lines(audit_dir / f"akb-audit-{_today()}.jsonl")
    actions = [json.loads(ln)["action"] for ln in lines]
    assert "akb_search" not in actions
    assert "akb_put" in actions
    assert "akb_some_new_write" in actions


def test_record_tool_marks_error_outcome(audit_dir):
    class _U:
        username, user_id = "bob", "u2"

    audit_log.record_tool("akb_get", {"vault": "v", "id": "d-x"}, _U(),
                          {"error": "not found", "code": "NOT_FOUND"})
    row = json.loads(_read_lines(audit_dir / f"akb-audit-{_today()}.jsonl")[0])
    assert row["outcome"] == "error"
    assert row["code"] == "NOT_FOUND"


def test_never_raises_on_unwritable_dir(tmp_path, monkeypatch):
    blocker = tmp_path / "afile"
    blocker.write_text("not a dir")
    monkeypatch.setattr(settings.audit, "enabled", True)
    monkeypatch.setattr(settings.audit, "log_dir", str(blocker / "sub"))
    audit_log.init()                       # mkdir fails, swallowed
    # Must not raise even though the file can't be written.
    audit_log.record(action="akb_put", actor="a", target="id=1")


def test_disabled_is_noop(tmp_path, monkeypatch):
    monkeypatch.setattr(settings.audit, "enabled", False)
    monkeypatch.setattr(settings.audit, "log_dir", str(tmp_path))
    audit_log.record(action="akb_put", actor="a")
    assert list(tmp_path.glob("akb-audit-*.jsonl")) == []


def test_process_uploads_entrypoint_is_awaitable():
    # BackfillRunner `await`s its callback — the uploader entrypoint MUST be
    # a coroutine function, else the worker loop crashes on `await <int>`.
    import asyncio
    import inspect
    assert inspect.iscoroutinefunction(audit_log._process_uploads)
    # Disabled (default) → returns 0 without touching the filesystem/S3.
    assert asyncio.run(audit_log._process_uploads()) == 0


def test_upload_then_prune_lifecycle(audit_dir, monkeypatch):
    monkeypatch.setattr(settings.audit, "bucket", "audit-bucket")
    uploaded: list[str] = []
    monkeypatch.setattr(audit_log, "_upload",
                        lambda path, day: uploaded.append(path.name))

    # A file from yesterday (age 1, kept after upload) and one from 3 days
    # ago (age 3 ≥ retention 2, pruned after upload).
    y1 = audit_dir / f"akb-audit-{_day_offset(-1)}.jsonl"
    y3 = audit_dir / f"akb-audit-{_day_offset(-3)}.jsonl"
    for f in (y1, y3):
        f.write_text('{"seq":1,"h":"x"}\n', encoding="utf-8")

    done = audit_log._process_uploads_sync()
    assert done >= 2
    assert sorted(uploaded) == sorted([y1.name, y3.name])
    assert y1.exists()                     # uploaded, within retention → kept
    assert not y3.exists()                 # uploaded, past retention → pruned
    assert (audit_dir / f".uploaded-{_day_offset(-1)}").exists()


def test_upload_failure_keeps_file(audit_dir, monkeypatch):
    monkeypatch.setattr(settings.audit, "bucket", "audit-bucket")

    def _boom(path, day):
        raise RuntimeError("bucket down")

    monkeypatch.setattr(audit_log, "_upload", _boom)
    old = audit_dir / f"akb-audit-{_day_offset(-5)}.jsonl"
    old.write_text('{"seq":1,"h":"x"}\n', encoding="utf-8")

    audit_log._process_uploads_sync()
    # Upload failed → no marker → NOT pruned even though it's old.
    assert old.exists()
    assert not (audit_dir / f".uploaded-{_day_offset(-5)}").exists()
