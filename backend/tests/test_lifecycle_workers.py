"""Kill-switch gating for external_git in the worker startup path.

`start_workers` must NOT start the external-git mirror poller when the feature
is disabled (kill-switch off → poller 미기동, no claim, no outbound).
Every worker start hook is stubbed so the test exercises ONLY the gating logic —
no real asyncio tasks, thread pools, or DB are created.

`app.services.lifecycle` is imported lazily inside each test (not at module top)
and `git_storage_path` is redirected to a writable tmp dir first, as a belt. No
module should mkdir storage at *import*: `external_git_poller` used to build
`ExternalGitService()` → `GitService()` at import (which mkdir'd
`git_storage_path`, default `/data/vaults`), but that construction is now lazy
(built on first poll). The redirect stays so collection never touches `/data`
even if some transitively-imported module regresses to an import-time mkdir.
"""

from __future__ import annotations

import types


def _import_lifecycle(monkeypatch, tmp_path):
    from app.config import settings

    # Point the import-time GitService mkdir at a writable dir (see module docstring).
    monkeypatch.setattr(settings, "git_storage_path", str(tmp_path / "vaults"))
    from app.services import lifecycle

    return lifecycle


def _stub_workers(monkeypatch, lifecycle, started: list[str]) -> None:
    """Replace every worker start hook `start_workers` touches with a recorder."""

    # The production lifecycle receives the process-selected revision backend
    # from the composition root. Keep these direct lifecycle tests on the
    # legacy path unless a test explicitly overrides it.
    monkeypatch.setattr(
        lifecycle,
        "selected_document_revision_backend",
        lambda: "bare_git",
        raising=False,
    )

    def rec(label: str):
        return lambda *a, **k: started.append(label)

    monkeypatch.setattr(lifecycle.embed_worker, "start", rec("embed_worker"))
    monkeypatch.setattr(lifecycle.delete_worker, "start", rec("delete_worker"))
    monkeypatch.setattr(lifecycle.external_git_poller, "start", rec("external_git_poller"))
    monkeypatch.setattr(lifecycle.metadata_worker, "start", rec("metadata_worker"))
    monkeypatch.setattr(lifecycle.vault_backfill, "start", rec("vault_backfill"))
    monkeypatch.setattr(lifecycle.sparse_encoder, "start_tokenizer_pool", rec("tokenizer_pool"))
    monkeypatch.setattr(lifecycle.sparse_encoder, "start_stats_refresher", rec("stats_refresher"))
    monkeypatch.setattr(lifecycle.write_lane, "start_commit_pool", rec("git_commit_pool"))
    # tool_usage maintenance runner is started unconditionally (a real start
    # would asyncio.create_task off a running loop); stub it so this test stays
    # loop-free and exercises only the external_git gate.
    monkeypatch.setattr(lifecycle.tool_usage, "start", rec("tool_usage_maintenance"))


def _settings(
    external_git_enabled: bool, *, llm_configured: bool = False
) -> types.SimpleNamespace:
    """Settings that turn OFF every optional worker so `start_workers` runs only
    the always-on set + the gate under test. `llm_configured` flips the LLM
    creds on so the metadata_worker gate can be exercised independently of the
    external_git gate."""
    return types.SimpleNamespace(
        external_git_enabled=external_git_enabled,
        bm25_recompute_interval_secs=3600,
        s3_endpoint_url=None,
        llm_base_url="http://llm.local/v1" if llm_configured else None,
        llm_api_key="sk-test" if llm_configured else None,  # pragma: allowlist secret
        redis_url=None,
        audit=types.SimpleNamespace(enabled=False, bucket=None),
        tool_usage=types.SimpleNamespace(enabled=False),
        role_sync_reconcile_interval_secs=0,
    )


def test_start_workers_starts_poller_when_enabled(monkeypatch, tmp_path):
    lifecycle = _import_lifecycle(monkeypatch, tmp_path)
    started: list[str] = []
    _stub_workers(monkeypatch, lifecycle, started)
    monkeypatch.setattr(lifecycle, "settings", _settings(external_git_enabled=True))

    lifecycle.start_workers()

    assert "external_git_poller" in started


def test_start_workers_skips_poller_when_disabled(monkeypatch, tmp_path):
    lifecycle = _import_lifecycle(monkeypatch, tmp_path)
    started: list[str] = []
    _stub_workers(monkeypatch, lifecycle, started)
    monkeypatch.setattr(lifecycle, "settings", _settings(external_git_enabled=False))

    lifecycle.start_workers()

    # The poller is gated off …
    assert "external_git_poller" not in started
    # … but the always-on workers still start (the gate is surgical).
    assert "embed_worker" in started
    assert "delete_worker" in started


def test_start_workers_starts_metadata_worker_when_enabled_and_llm(monkeypatch, tmp_path):
    lifecycle = _import_lifecycle(monkeypatch, tmp_path)
    started: list[str] = []
    _stub_workers(monkeypatch, lifecycle, started)
    monkeypatch.setattr(
        lifecycle, "settings", _settings(external_git_enabled=True, llm_configured=True)
    )

    lifecycle.start_workers()

    assert "metadata_worker" in started


def test_start_workers_skips_metadata_worker_when_external_git_disabled(monkeypatch, tmp_path):
    """metadata_worker only fills LLM metadata on external_git mirror imports, so
    the external-git kill-switch gates it OFF even when the LLM is configured (a
    disabled deployment issues zero LLM outbound). The always-on workers
    are untouched; the LLM-not-configured skip still applies independently."""
    lifecycle = _import_lifecycle(monkeypatch, tmp_path)
    started: list[str] = []
    _stub_workers(monkeypatch, lifecycle, started)
    monkeypatch.setattr(
        lifecycle, "settings", _settings(external_git_enabled=False, llm_configured=True)
    )

    lifecycle.start_workers()

    # LLM is configured, yet external-git is off → metadata_worker must NOT start.
    assert "metadata_worker" not in started
    # … the poller is off too, while the always-on set still starts.
    assert "external_git_poller" not in started
    assert "embed_worker" in started


def test_start_workers_skips_git_only_workers_for_postgres_native(monkeypatch, tmp_path):
    lifecycle = _import_lifecycle(monkeypatch, tmp_path)
    started: list[str] = []
    _stub_workers(monkeypatch, lifecycle, started)
    monkeypatch.setattr(lifecycle, "selected_document_revision_backend", lambda: "postgres_native")
    monkeypatch.setattr(
        lifecycle,
        "settings",
        _settings(external_git_enabled=True, llm_configured=True),
    )

    lifecycle.start_workers()

    assert "external_git_poller" not in started
    assert "metadata_worker" not in started
    assert "embed_worker" in started


def test_external_git_poller_service_is_lazy_not_constructed_at_import(monkeypatch):
    """Regression (MINOR): the shared ExternalGitService must be built lazily on
    first poll, never at *import*. Import-time construction also builds
    GitService(), whose __init__ mkdir's git_storage_path/_worktrees — a
    filesystem side effect on a disabled/read-only deploy and a test-isolation
    hazard.

    Spy on the constructor and re-execute the module body (importlib.reload == a
    fresh import): the reload must NOT call it; the first _get_service() builds
    it exactly once and memoizes."""
    import importlib

    from app.services import external_git_poller as poller
    from app.services import external_git_service as egs

    constructed: list[int] = []
    real_init = egs.ExternalGitService.__init__

    def spy_init(self, *a, **k):
        constructed.append(1)
        self.git = None  # don't build a real GitService (which would mkdir)

    monkeypatch.setattr(egs.ExternalGitService, "__init__", spy_init)
    try:
        importlib.reload(poller)  # re-run the module top-level == a fresh import
        assert constructed == []  # import constructed nothing
        assert poller._service is None

        svc = poller._get_service()  # first use builds it …
        assert constructed == [1]
        assert poller._service is svc

        assert poller._get_service() is svc  # … and memoizes (no rebuild)
        assert constructed == [1]
    finally:
        # Restore a clean module (real ctor, _service reset) for other tests.
        egs.ExternalGitService.__init__ = real_init
        importlib.reload(poller)
