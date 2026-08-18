from __future__ import annotations

import os
import time

import pytest

from app import worker_health
from app import worker_main
from app.process_role import runtime_process_role


def test_process_role_defaults_to_all(monkeypatch):
    monkeypatch.delenv("AKB_PROCESS_ROLE", raising=False)
    assert runtime_process_role() == "all"


def test_process_role_rejects_unknown_value(monkeypatch):
    monkeypatch.setenv("AKB_PROCESS_ROLE", "surprise")
    with pytest.raises(RuntimeError, match="all, api, worker"):
        runtime_process_role()


def test_worker_health_accepts_fresh_and_rejects_stale_heartbeat(monkeypatch, tmp_path):
    heartbeat = tmp_path / "heartbeat"
    heartbeat.touch()
    monkeypatch.setenv("AKB_WORKER_HEARTBEAT_PATH", str(heartbeat))
    monkeypatch.setenv("AKB_WORKER_HEARTBEAT_MAX_AGE_SECS", "30")

    worker_health.main()

    old = time.time() - 60
    os.utime(heartbeat, (old, old))
    with pytest.raises(SystemExit, match="stale"):
        worker_health.main()


def test_worker_entrypoint_selects_revision_backend_before_composition(monkeypatch):
    calls: list[str] = []

    monkeypatch.setattr(
        worker_main,
        "get_revision_backend",
        lambda: calls.append("selected"),
    )
    monkeypatch.setattr(
        worker_main,
        "selected_document_revision_backend",
        lambda: "bare_git" if calls else None,
    )

    assert worker_main._select_revision_backend() == "bare_git"
    assert calls == ["selected"]


def test_worker_entrypoint_fails_closed_when_selection_does_not_complete(monkeypatch):
    monkeypatch.setattr(worker_main, "get_revision_backend", lambda: None)
    monkeypatch.setattr(
        worker_main,
        "selected_document_revision_backend",
        lambda: None,
    )

    with pytest.raises(RuntimeError, match="selection did not complete"):
        worker_main._select_revision_backend()
