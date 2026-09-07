"""Vault-name conflicts share one non-disclosing contract across create paths."""

from __future__ import annotations

import json

import asyncpg
import pytest

from app.exceptions import VaultNameUnavailableError
from app.repositories import vault_repo as vault_repo_module
from app.repositories.vault_repo import VaultRepository
from app.services import document_service as document_service_module
from app.services.document_service import DocumentService
from app.services.external_git_service import ExternalGitService


class _AsyncContext:
    def __init__(self, value):
        self.value = value

    async def __aenter__(self):
        return self.value

    async def __aexit__(self, exc_type, exc, traceback):
        return False


class _Pool:
    def __init__(self, connection):
        self.connection = connection

    def acquire(self):
        return _AsyncContext(self.connection)


@pytest.mark.asyncio
async def test_repository_maps_only_vault_name_unique_constraint(monkeypatch):
    class _FakeUniqueViolation(Exception):
        def __init__(self, constraint_name: str):
            self.constraint_name = constraint_name

    class _Connection:
        async def execute(self, sql, *args):
            raise _FakeUniqueViolation("vaults_name_key")

    monkeypatch.setattr(
        vault_repo_module.asyncpg,
        "UniqueViolationError",
        _FakeUniqueViolation,
    )

    with pytest.raises(VaultNameUnavailableError) as caught:
        await VaultRepository(_Pool(_Connection())).create(
            "reserved", "", "/tmp/reserved.git"
        )

    assert caught.value.code == "vault_name_unavailable"
    assert caught.value.details is None
    assert "reserved" not in str(caught.value)


@pytest.mark.asyncio
async def test_repository_does_not_mask_an_unrelated_unique_constraint(monkeypatch):
    class _FakeUniqueViolation(Exception):
        def __init__(self, constraint_name: str):
            self.constraint_name = constraint_name

    class _Connection:
        async def execute(self, sql, *args):
            raise _FakeUniqueViolation("some_other_key")

    monkeypatch.setattr(
        vault_repo_module.asyncpg,
        "UniqueViolationError",
        _FakeUniqueViolation,
    )

    with pytest.raises(_FakeUniqueViolation):
        await VaultRepository(_Pool(_Connection())).create(
            "candidate", "", "/tmp/candidate.git"
        )


@pytest.mark.asyncio
async def test_standard_git_race_rolls_back_and_returns_stable_conflict(monkeypatch):
    class _Git:
        @staticmethod
        def vault_exists(name: str) -> bool:
            return False

        @staticmethod
        def init_vault(name: str) -> str:
            raise FileExistsError("storage path exists")

    async def _direct_git_write(fn, /, *args, **kwargs):
        return fn(*args, **kwargs)

    monkeypatch.setattr(document_service_module, "run_git_write", _direct_git_write)
    service = DocumentService(git=_Git())
    rollback_calls: list[dict] = []

    async def _record_rollback(**kwargs):
        rollback_calls.append(kwargs)

    monkeypatch.setattr(service, "_rollback_vault_create", _record_rollback)

    with pytest.raises(VaultNameUnavailableError) as caught:
        await service._create_vault_standard(
            name="raced-name",
            description="",
            template=None,
            public_access="none",
            owner_id=None,
            uid=None,
            external_git=None,
            vault_repo=object(),
            coll_repo=object(),
        )

    assert caught.value.code == "vault_name_unavailable"
    assert "raced-name" not in str(caught.value)
    # FileExists means this request created no directory. Even when its earlier
    # probe saw "absent", an external mirror or another out-of-band writer may
    # have materialised the path, so deleting it would destroy the winner.
    assert rollback_calls == []


def test_external_git_bootstrap_joins_creation_lock_and_rechecks_absence():
    calls: list[object] = []

    class _Git:
        def __init__(self):
            self.exists_calls = 0

        def vault_exists(self, name: str) -> bool:
            self.exists_calls += 1
            calls.append(("exists", self.exists_calls, name))
            return self.exists_calls >= 2

        @staticmethod
        def acquire_vault_creation_lock(name: str) -> int:
            calls.append(("acquire", name))
            return 73

        @staticmethod
        def release_vault_creation_lock(fd: int) -> None:
            calls.append(("release", fd))

        @staticmethod
        def clone_mirror(*args):
            raise AssertionError("the path appeared while waiting; do not clone over it")

        @staticmethod
        def _is_mirror(name: str) -> bool:
            return True

        @staticmethod
        def inspect_mirror_structure(name: str, remote_url: str, branch: str) -> list:
            return []

        @staticmethod
        def is_healthy_repo(name: str) -> bool:
            return True

        @staticmethod
        def materialized_sha(name: str, branch: str) -> str:
            return "abc123"

    result = ExternalGitService(git=_Git()).ensure_local_bare(
        "raced-name",
        "abc123",
        "abc123",
        "https://git.example.test/team/repo.git",
        "main",
        None,
    )

    assert result == ("unchanged", "abc123")
    assert calls[:4] == [
        ("exists", 1, "raced-name"),
        ("acquire", "raced-name"),
        ("exists", 2, "raced-name"),
        ("release", 73),
    ]


def test_real_asyncpg_type_remains_available_after_monkeypatch_cleanup():
    """The test double above must not leak into later integration tests."""
    assert issubclass(asyncpg.UniqueViolationError, asyncpg.PostgresError)


@pytest.mark.asyncio
async def test_rest_handler_preserves_the_stable_409_without_resource_metadata(
    monkeypatch, tmp_path
):
    from app.config import settings

    monkeypatch.setattr(settings, "git_storage_path", str(tmp_path / "vaults"))
    from app.main import akb_error_handler

    response = await akb_error_handler(None, VaultNameUnavailableError())
    body = json.loads(response.body)

    assert response.status_code == 409
    assert body["code"] == "vault_name_unavailable"
    assert body["message"] == "Vault name is unavailable. Choose a different name."
    assert not ({"owner", "visibility", "public_access", "archived"} & set(body))
    assert body["detail"] == {
        "message": "Vault name is unavailable. Choose a different name.",
        "code": "vault_name_unavailable",
    }
