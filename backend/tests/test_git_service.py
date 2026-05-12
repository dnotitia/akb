"""Unit tests for GitService bulk-delete behaviour.

GitService writes to `git_storage_path` from settings. Passing a tmp
`storage_path` to the constructor bypasses that entirely, so these
tests never touch the real `/data/vaults` directory.

The full write path requires a non-empty bare repo (otherwise
`_ensure_worktree` returns None and writes fall back to the clone
path). We seed each vault with one `commit_file` to get past that.
"""

from __future__ import annotations

import uuid

import pytest

from app.services.git_service import GitService


@pytest.fixture
def git_service(tmp_path):
    """GitService rooted at a per-test tmpdir."""
    return GitService(storage_path=str(tmp_path / "vaults"))


@pytest.fixture
def vault(git_service):
    """Initialise a vault with one seed commit so the worktree exists.

    Returns the vault name. The seed commit is on a single throwaway
    file ('seed.md') the bulk-delete tests do not touch.
    """
    name = f"test_vault_{uuid.uuid4().hex[:8]}"
    git_service.init_vault(name)
    git_service.commit_file(
        vault_name=name,
        file_path="seed.md",
        content="seed\n",
        message="seed",
    )
    return name


def _vault_commit_count(git_service: GitService, vault_name: str) -> int:
    return len(git_service.vault_log(vault_name, max_count=1000))


def test_delete_paths_bulk_removes_files_and_creates_one_commit(git_service, vault):
    # Add three files in three separate commits.
    for name, body in [("a.md", "A"), ("b.md", "B"), ("c.md", "C")]:
        git_service.commit_file(
            vault_name=vault,
            file_path=name,
            content=body,
            message=f"add {name}",
        )

    before = _vault_commit_count(git_service, vault)

    sha = git_service.delete_paths_bulk(
        vault_name=vault,
        file_paths=["a.md", "b.md"],
        message="bulk delete a + b",
    )

    after = _vault_commit_count(git_service, vault)

    assert sha is not None and len(sha) == 40
    # Exactly one new commit.
    assert after == before + 1
    # Deleted files are gone, untouched file remains.
    assert git_service.read_file(vault, "a.md") is None
    assert git_service.read_file(vault, "b.md") is None
    assert git_service.read_file(vault, "c.md") == "C"


def test_delete_paths_bulk_is_idempotent_on_missing(git_service, vault):
    git_service.commit_file(
        vault_name=vault,
        file_path="real.md",
        content="real",
        message="add real",
    )

    before = _vault_commit_count(git_service, vault)

    sha = git_service.delete_paths_bulk(
        vault_name=vault,
        file_paths=["real.md", "ghost.md"],
        message="bulk delete with ghost",
    )

    after = _vault_commit_count(git_service, vault)

    assert sha is not None and len(sha) == 40
    assert after == before + 1
    assert git_service.read_file(vault, "real.md") is None


def test_delete_paths_bulk_returns_none_when_all_missing(git_service, vault):
    before = _vault_commit_count(git_service, vault)

    sha = git_service.delete_paths_bulk(
        vault_name=vault,
        file_paths=["ghost1.md", "ghost2.md"],
        message="should not commit",
    )

    after = _vault_commit_count(git_service, vault)

    assert sha is None
    assert after == before


def test_delete_paths_bulk_dedupes_input(git_service, vault):
    """Passing the same path twice must not crash on the second remove."""
    git_service.commit_file(
        vault_name=vault,
        file_path="dup.md",
        content="d",
        message="add dup",
    )

    before = _vault_commit_count(git_service, vault)

    sha = git_service.delete_paths_bulk(
        vault_name=vault,
        file_paths=["dup.md", "dup.md"],
        message="bulk delete with duplicate",
    )

    after = _vault_commit_count(git_service, vault)

    assert sha is not None
    assert after == before + 1
    assert git_service.read_file(vault, "dup.md") is None
