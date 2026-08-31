"""R0/R1 stable selector and authority-configuration contract tests."""

from __future__ import annotations

import sys
import uuid

import pytest

from app.config import NATIVE_REVISION_M1_MEASUREMENT_DATABASE_NAME, Settings
from app.services.document_service import DocumentService


_DATABASE_ID = uuid.UUID("11111111-1111-4111-8111-111111111111")
_IMAGE_DIGEST = "sha256:" + "a" * 64


def _native_settings(tmp_path, **overrides) -> Settings:
    values = {
        "git_storage_path": str(tmp_path),
        "document_revision_backend": "postgres_native",
        "document_revision_tenant_id": "tenant-001",
        "document_revision_namespace": "tenant-001",
        "document_revision_database_id": _DATABASE_ID,
        "document_revision_runtime_image_digest": _IMAGE_DIGEST,
    }
    values.update(overrides)
    return Settings(**values)


def test_omitted_selector_defaults_to_stable_bare_git(tmp_path):
    from app.services.revision_backend import canonical_document_revision_backend

    configured = Settings(git_storage_path=str(tmp_path))

    assert configured.document_revision_backend == "bare_git"
    assert canonical_document_revision_backend(configured.document_revision_backend) == "bare_git"


@pytest.mark.parametrize(
    ("selector", "canonical"),
    [
        ("bare_git", "bare_git"),
        ("bare_git_current", "bare_git"),
        ("postgres_native", "postgres_native"),
        ("native_ledger_m1", "postgres_native"),
    ],
)
def test_stable_selectors_and_legacy_bare_alias_have_canonical_names(selector, canonical):
    from app.services.revision_backend import canonical_document_revision_backend

    assert canonical_document_revision_backend(selector) == canonical


def test_native_selector_requires_frozen_bootstrap_identity(tmp_path):
    with pytest.raises(ValueError, match="document_revision_tenant_id"):
        Settings(
            git_storage_path=str(tmp_path),
            document_revision_backend="postgres_native",
        )


@pytest.mark.parametrize(
    "overrides",
    [
        {"db_name": NATIVE_REVISION_M1_MEASUREMENT_DATABASE_NAME},
        {"native_revision_m1_measurement_only": True},
        {"native_revision_m1_file_driver": "fscas"},
    ],
)
def test_native_selector_rejects_reserved_measurement_configuration(tmp_path, overrides):
    with pytest.raises(ValueError, match="postgres_native"):
        _native_settings(tmp_path, **overrides)


def test_native_selector_constructs_the_native_facade_without_git(monkeypatch, tmp_path):
    from app.services import git_service, revision_backend
    from app.services.native_document_service import NativeDocumentService

    configured = _native_settings(tmp_path)
    monkeypatch.setattr(revision_backend, "settings", configured)
    monkeypatch.setattr(git_service, "settings", configured)

    def fail_if_constructed(*_args, **_kwargs):
        raise AssertionError("postgres_native composition must not construct GitService")

    monkeypatch.setattr(revision_backend, "GitService", fail_if_constructed)

    selected = revision_backend.get_revision_backend()

    assert isinstance(selected.document_service, NativeDocumentService)
    assert selected.document_service.move.__func__ is NativeDocumentService.move
    assert revision_backend.selected_document_revision_backend() == "postgres_native"


def test_native_backend_with_injected_document_service_does_not_construct_legacy_git(monkeypatch):
    from app.services import native_revision_backend
    from app.services.native_document_service import NativeDocumentService

    document_service = NativeDocumentService()

    def fail_if_constructed(*_args, **_kwargs):
        raise AssertionError("current Native backend construction must not construct GitService")

    monkeypatch.setattr(native_revision_backend, "GitService", fail_if_constructed)

    backend = native_revision_backend.NativeRevisionBackend(document_service=document_service)

    assert backend.document_service is document_service


def test_legacy_native_measurement_alias_remains_guarded(tmp_path):
    with pytest.raises(ValueError, match="native_revision_m1_measurement_only"):
        Settings(
            git_storage_path=str(tmp_path),
            document_revision_backend="native_ledger_m1",
        )


def test_legacy_default_alias_still_builds_the_bare_git_service(monkeypatch, tmp_path):
    from app.services import git_service, revision_backend

    configured = Settings(
        git_storage_path=str(tmp_path),
        document_revision_backend="bare_git_current",
    )
    monkeypatch.setattr(revision_backend, "settings", configured)
    monkeypatch.setattr(git_service, "settings", configured)

    selected = revision_backend.get_revision_backend()

    assert isinstance(selected.document_service, DocumentService)
    assert selected.document_service.move.__func__ is DocumentService.move
    assert revision_backend.selected_document_revision_backend() == "bare_git"


@pytest.fixture(autouse=True)
def reset_revision_selection():
    from app.services import revision_backend

    revision_backend.reset_document_service_for_tests()
    yield
    revision_backend.reset_document_service_for_tests()
    for module in (
        "app.main",
        "app.api.routes.activity",
        "app.api.routes.documents",
        "mcp_server.server",
    ):
        sys.modules.pop(module, None)
