"""Native startup must not compose Bare-Git maintenance paths."""

from __future__ import annotations


def _patch_lifespan(monkeypatch, main, selected_backend: str, events: list[str]) -> None:
    monkeypatch.setattr(main, "selected_document_revision_backend", lambda: selected_backend)
    monkeypatch.setattr(main, "install_secret_redaction", lambda: None)

    async def init_storage() -> None:
        events.append("init_storage")

    async def backfill_mirror_markers() -> int:
        events.append("backfill_mirror_markers")
        return 1

    def check_external_git_capability(_settings) -> None:
        events.append("check_external_git_capability")

    def start_workers() -> None:
        events.append("start_workers")

    async def stop_workers() -> None:
        events.append("stop_workers")

    async def shutdown_storage() -> None:
        events.append("shutdown_storage")

    def audit_shutdown() -> None:
        events.append("audit_shutdown")

    monkeypatch.setattr(main, "init_storage", init_storage)
    monkeypatch.setattr(main.external_git_service, "backfill_mirror_markers", backfill_mirror_markers)
    monkeypatch.setattr(main, "check_external_git_capability", check_external_git_capability)
    monkeypatch.setattr(main, "start_workers", start_workers)
    monkeypatch.setattr(main, "stop_workers", stop_workers)
    monkeypatch.setattr(main, "shutdown_storage", shutdown_storage)
    monkeypatch.setattr(main.audit_log, "shutdown", audit_shutdown)


async def test_native_lifespan_skips_git_startup_maintenance(monkeypatch, tmp_path):
    from app.config import settings

    monkeypatch.setattr(settings, "git_storage_path", str(tmp_path / "vaults"))
    from app import main

    events: list[str] = []
    _patch_lifespan(monkeypatch, main, "postgres_native", events)

    async with main.lifespan(main.app):
        events.append("serving")

    assert "backfill_mirror_markers" not in events
    assert "check_external_git_capability" not in events
    assert events == [
        "init_storage",
        "start_workers",
        "serving",
        "stop_workers",
        "shutdown_storage",
        "audit_shutdown",
    ]


async def test_bare_git_lifespan_keeps_git_startup_maintenance(monkeypatch, tmp_path):
    from app.config import settings

    monkeypatch.setattr(settings, "git_storage_path", str(tmp_path / "vaults"))
    from app import main

    events: list[str] = []
    _patch_lifespan(monkeypatch, main, "bare_git", events)

    async with main.lifespan(main.app):
        events.append("serving")

    assert "backfill_mirror_markers" in events
    assert "check_external_git_capability" in events
