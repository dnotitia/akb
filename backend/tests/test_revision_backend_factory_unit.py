"""Process-scoped document revision backend selection guards."""

from __future__ import annotations

import ast
import importlib
from pathlib import Path
import sys

import pytest

from app.config import Settings
from app.services.document_service import DocumentService


def _settings(tmp_path, **overrides) -> Settings:
    return Settings(git_storage_path=str(tmp_path), **overrides)


@pytest.fixture(autouse=True)
def reset_revision_backend():
    from app.services import revision_backend

    revision_backend.reset_document_service_for_tests()
    yield
    revision_backend.reset_document_service_for_tests()
    sys.modules.pop("app.api.routes.activity", None)
    sys.modules.pop("mcp_server.server", None)
    routes_package = sys.modules.get("app.api.routes")
    if routes_package is not None:
        routes_package.__dict__.pop("activity", None)
    mcp_package = sys.modules.get("mcp_server")
    if mcp_package is not None:
        mcp_package.__dict__.pop("server", None)


def test_default_backend_is_legacy_document_service_once_per_process(monkeypatch, tmp_path):
    from app.services import revision_backend
    from app.services import git_service

    configured = _settings(tmp_path)
    monkeypatch.setattr(revision_backend, "settings", configured)
    monkeypatch.setattr(git_service, "settings", configured)

    first = revision_backend.get_document_service()
    second = revision_backend.get_document_service()

    assert isinstance(first, DocumentService)
    assert second is first
    assert revision_backend.selected_document_revision_backend() == "bare_git_current"


def test_native_backend_requires_explicit_measurement_opt_in(tmp_path):
    with pytest.raises(ValueError, match="native_revision_m1_measurement_only"):
        Settings(
            git_storage_path=str(tmp_path),
            document_revision_backend="native_ledger_m1",
        )


def test_native_backend_requires_dedicated_measurement_database(tmp_path):
    with pytest.raises(ValueError, match="akb_revision_m1_measurement"):
        Settings(
            git_storage_path=str(tmp_path),
            document_revision_backend="native_ledger_m1",
            native_revision_m1_measurement_only=True,
        )


def test_native_backend_uses_registered_factory_once(monkeypatch, tmp_path):
    from app.services import revision_backend

    configured = _settings(
        tmp_path,
        db_name="akb_revision_m1_measurement",
        document_revision_backend="native_ledger_m1",
        native_revision_m1_measurement_only=True,
    )
    native_service = object()
    native_backend = _NativeRevisionBackend(native_service)
    factory_calls = 0

    def make_native_backend():
        nonlocal factory_calls
        factory_calls += 1
        return native_backend

    monkeypatch.setattr(revision_backend, "settings", configured)
    revision_backend.register_native_revision_backend(make_native_backend)

    assert revision_backend.get_document_service() is native_service
    assert revision_backend.get_document_service() is native_service
    assert revision_backend.get_revision_backend() is native_backend
    assert factory_calls == 1
    assert revision_backend.selected_document_revision_backend() == "native_ledger_m1"


def test_native_backend_fails_closed_without_registered_implementation(monkeypatch, tmp_path):
    from app.services import revision_backend

    monkeypatch.setattr(
        revision_backend,
        "settings",
        _settings(
            tmp_path,
            db_name="akb_revision_m1_measurement",
            document_revision_backend="native_ledger_m1",
            native_revision_m1_measurement_only=True,
        ),
    )

    with pytest.raises(revision_backend.NativeRevisionBackendUnavailableError):
        revision_backend.get_document_service()


class _NativeRevisionBackend:
    def __init__(self, document_service):
        self.document_service = document_service

    async def vault_activity(self, vault, *, max_count, since, path):
        return []

    async def recent_changes(self, user_id, *, vault, limit):
        return []

    async def document_diff(self, vault, doc_ref, commit):
        return None

    async def document_version(self, vault, doc_ref, version):
        return None

    async def document_history(self, vault, doc_ref, *, limit):
        return {}


def test_native_activity_route_import_has_no_git_or_current_commit_escape(monkeypatch, tmp_path):
    from app.services import git_service, revision_backend

    configured = _settings(
        tmp_path,
        db_name="akb_revision_m1_measurement",
        document_revision_backend="native_ledger_m1",
        native_revision_m1_measurement_only=True,
    )
    monkeypatch.setattr(revision_backend, "settings", configured)
    revision_backend.register_native_revision_backend(
        lambda: _NativeRevisionBackend(object())
    )

    def fail_if_constructed(*args, **kwargs):
        raise AssertionError("native activity route must not construct GitService")

    monkeypatch.setattr(git_service, "GitService", fail_if_constructed)
    sys.modules.pop("app.api.routes.activity", None)
    activity = importlib.import_module("app.api.routes.activity")
    server = importlib.import_module("mcp_server.server")

    assert activity.revision_backend is revision_backend.get_revision_backend()
    assert server.revision_backend is revision_backend.get_revision_backend()
    source = Path(activity.__file__).read_text()
    assert "GitService" not in source
    assert "current_commit" not in source
    assert "GitService" not in Path(server.__file__).read_text()


def test_production_composition_roots_use_the_process_factory():
    backend = Path(__file__).resolve().parents[1]
    roots = {
        "app/api/routes/activity.py": "get_revision_backend",
        "app/api/routes/collections.py": "get_document_service",
        "app/api/routes/documents.py": "get_document_service",
        "app/api/routes/help.py": "get_document_service",
        "app/api/routes/knowledge_io.py": "get_document_service",
        "app/services/agent_memory_service.py": "get_document_service",
        "app/services/publication_service.py": "get_document_service",
        "mcp_server/server.py": "get_revision_backend",
    }

    for relative_path, factory_name in roots.items():
        tree = ast.parse((backend / relative_path).read_text())
        calls = [
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
        ]
        assert any(call.func.id == factory_name for call in calls), relative_path
        assert not any(call.func.id == "DocumentService" for call in calls), relative_path
