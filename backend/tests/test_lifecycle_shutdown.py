"""Runtime safety contracts for the all-in-one backend lifecycle."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from app.config import Settings


def test_runtime_safety_defaults_are_explicit_and_bounded():
    configured = Settings()

    assert configured.tokenizer_processes == 2
    assert configured.worker_shutdown_timeout_secs == 35.0
    with pytest.raises(ValueError):
        Settings(tokenizer_processes=0)
    with pytest.raises(ValueError):
        Settings(tokenizer_processes=5)


def test_kiwi_uses_one_native_worker_inside_each_process(monkeypatch):
    from app.services import sparse_encoder

    created: list[dict] = []

    class FakeKiwi:
        def __init__(self, **kwargs):
            created.append(kwargs)

    monkeypatch.setattr(sparse_encoder, "Kiwi", FakeKiwi)
    monkeypatch.setattr(sparse_encoder, "_kiwi", None)

    sparse_encoder._get_kiwi()

    assert created == [{"num_workers": 1}]


async def test_stop_workers_broadcasts_then_joins_every_component(monkeypatch):
    from app.services import lifecycle

    entered: list[str] = []
    release = asyncio.Event()

    async def stop(name: str) -> None:
        entered.append(name)
        await release.wait()

    class RoleSync:
        async def stop_reconcile_timer(self) -> None:
            await stop("role_sync")

    names = {
        lifecycle.m1_file_transfer_reaper: "m1",
        lifecycle.loop_monitor: "loop_monitor",
        lifecycle.events_publisher: "events",
        lifecycle.metadata_worker: "metadata",
        lifecycle.external_git_poller: "external_git",
        lifecycle.asset_gc_worker: "asset_gc",
        lifecycle.s3_delete_worker: "s3_delete",
        lifecycle.app_rollout_worker: "rollout",
        lifecycle.delete_worker: "vector_delete",
        lifecycle.embed_worker: "embed",
        lifecycle.vault_backfill: "vault_backfill",
        lifecycle.queue_rescuer: "queue_rescuer",
    }
    for module, name in names.items():
        monkeypatch.setattr(module, "stop", lambda name=name: stop(name))
    monkeypatch.setattr(lifecycle.audit_log, "stop_uploader", lambda: stop("audit"))
    monkeypatch.setattr(lifecycle.tool_usage, "stop", lambda: stop("tool_usage"))
    monkeypatch.setattr(
        lifecycle.sparse_encoder,
        "stop_stats_refresher",
        lambda: stop("bm25"),
    )
    monkeypatch.setattr(lifecycle, "get_role_sync", lambda: RoleSync())
    monkeypatch.setattr(lifecycle.settings, "worker_shutdown_timeout_secs", 1.0)

    ordering: list[str] = []
    monkeypatch.setattr(lifecycle, "request_stop_all", lambda: ordering.append("broadcast"))
    monkeypatch.setattr(
        lifecycle.sparse_encoder,
        "stop_tokenizer_pool",
        lambda: ordering.append("tokenizer_pool"),
    )
    monkeypatch.setattr(
        lifecycle.write_lane,
        "stop_commit_pool",
        lambda: ordering.append("commit_pool"),
    )

    task = asyncio.create_task(lifecycle.stop_workers())
    expected = len(names) + 4
    for _ in range(100):
        if len(entered) == expected:
            break
        await asyncio.sleep(0)

    assert ordering == ["broadcast"]
    assert len(entered) == expected
    release.set()
    await task
    assert ordering == ["broadcast", "tokenizer_pool", "commit_pool"]


async def test_one_stop_failure_does_not_skip_siblings(monkeypatch):
    from app.services import lifecycle

    called: list[str] = []

    async def ok(name: str) -> None:
        called.append(name)

    async def fail() -> None:
        called.append("failed")
        raise RuntimeError("boom")

    class RoleSync:
        async def stop_reconcile_timer(self) -> None:
            await ok("role_sync")

    modules = [
        lifecycle.m1_file_transfer_reaper,
        lifecycle.loop_monitor,
        lifecycle.events_publisher,
        lifecycle.metadata_worker,
        lifecycle.external_git_poller,
        lifecycle.asset_gc_worker,
        lifecycle.s3_delete_worker,
        lifecycle.app_rollout_worker,
        lifecycle.delete_worker,
        lifecycle.embed_worker,
        lifecycle.vault_backfill,
        lifecycle.queue_rescuer,
    ]
    for index, module in enumerate(modules):
        monkeypatch.setattr(module, "stop", lambda index=index: ok(f"module-{index}"))
    monkeypatch.setattr(lifecycle.audit_log, "stop_uploader", lambda: ok("audit"))
    monkeypatch.setattr(lifecycle.tool_usage, "stop", fail)
    monkeypatch.setattr(
        lifecycle.sparse_encoder,
        "stop_stats_refresher",
        lambda: ok("bm25"),
    )
    monkeypatch.setattr(lifecycle, "get_role_sync", lambda: RoleSync())
    monkeypatch.setattr(lifecycle, "request_stop_all", lambda: None)
    monkeypatch.setattr(lifecycle.sparse_encoder, "stop_tokenizer_pool", lambda: None)
    monkeypatch.setattr(lifecycle.write_lane, "stop_commit_pool", lambda: None)

    await lifecycle.stop_workers()

    assert "failed" in called
    assert "bm25" in called
    assert "module-9" in called


def test_kubernetes_timeout_contract_is_explicit():
    manifest = (
        Path(__file__).resolve().parents[2] / "deploy/k8s/backend.yaml"
    ).read_text()

    assert "terminationGracePeriodSeconds: 45" in manifest
    assert "UVICORN_TIMEOUT_KEEP_ALIVE" in manifest
    assert 'value: "65"' in manifest
    assert 'name: AKB_PROCESS_ROLE' in manifest
    assert 'value: "api"' in manifest
    assert '- name: worker' in manifest
    assert 'command: ["python", "-m", "app.worker_main"]' in manifest
    assert 'value: "worker"' in manifest
    assert 'command: ["python", "-m", "app.worker_health"]' in manifest
