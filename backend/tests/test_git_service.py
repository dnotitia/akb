"""Unit tests for GitService write behaviour.

GitService writes to `git_storage_path` from settings. Passing a tmp
`storage_path` to the constructor bypasses that entirely, so these
tests never touch the real `/data/vaults` directory.
"""

from __future__ import annotations

import os
import subprocess
import threading
import uuid

import pytest

from app.services.external_git_service import ExternalGitService
from app.services.git_service import GitService


def _make_upstream(tmp_path) -> tuple[str, str]:
    """Create a real on-disk git repo with one commit on `main`, usable as
    a clone source. Returns (clone_url, head_sha)."""
    up = tmp_path / "upstream"
    up.mkdir()

    def g(*args: str) -> None:
        subprocess.run(
            ["git", *args], cwd=str(up), check=True, capture_output=True, text=True
        )

    g("init", "-b", "main")
    g("config", "user.email", "t@example.com")
    g("config", "user.name", "Test")
    (up / "doc.md").write_text("# Hello\n")
    g("add", ".")
    g("commit", "-m", "init")
    head = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=str(up), check=True, capture_output=True, text=True,
    ).stdout.strip()
    return str(up), head


@pytest.fixture
def git_service(tmp_path):
    """GitService rooted at a per-test tmpdir."""
    return GitService(storage_path=str(tmp_path / "vaults"))


@pytest.fixture
def vault(git_service):
    """Initialise a vault with one seed commit so the worktree exists.

    Returns the vault name. The seed commit is on a single throwaway
    file ('seed.md') the write tests do not touch.
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


def _block_chdir(monkeypatch: pytest.MonkeyPatch) -> None:
    def fail_chdir(path: str) -> None:
        raise AssertionError(f"os.chdir must not be used by GitService writes: {path}")

    monkeypatch.setattr(os, "chdir", fail_chdir)


def test_commit_file_creates_initial_commit(git_service: GitService) -> None:
    name = f"test_vault_{uuid.uuid4().hex[:8]}"
    git_service.init_vault(name)

    sha = git_service.commit_file(
        vault_name=name,
        file_path="first.md",
        content="first\n",
        message="initial document",
        author_name="Ada Lovelace",
        author_email="ada@example.dev",
    )

    assert len(sha) == 40
    assert git_service.read_file(name, "first.md") == "first\n"
    assert _vault_commit_count(git_service, name) == 1


def test_commit_file_existing_worktree_preserves_author_and_message(git_service: GitService, vault: str) -> None:
    before = _vault_commit_count(git_service, vault)

    sha = git_service.commit_file(
        vault_name=vault,
        file_path="authored.md",
        content="body\n",
        message="custom subject\n\nbody line",
        author_name="Ada Lovelace",
        author_email="ada@example.dev",
    )

    after = _vault_commit_count(git_service, vault)
    latest = git_service.vault_log(vault, max_count=1)[0]

    assert len(sha) == 40
    assert after == before + 1
    assert latest["hash"] == sha[:12]
    assert latest["subject"] == "custom subject"
    assert latest["author"] == "Ada Lovelace"
    assert git_service.read_file(vault, "authored.md") == "body\n"


def test_delete_file_removes_file_and_creates_commit(git_service: GitService, vault: str) -> None:
    git_service.commit_file(
        vault_name=vault,
        file_path="delete-me.md",
        content="delete me",
        message="add delete-me",
    )
    before = _vault_commit_count(git_service, vault)

    sha = git_service.delete_file(
        vault_name=vault,
        file_path="delete-me.md",
        message="delete delete-me",
    )

    after = _vault_commit_count(git_service, vault)
    latest = git_service.vault_log(vault, max_count=1)[0]

    assert len(sha) == 40
    assert after == before + 1
    assert latest["hash"] == sha[:12]
    assert latest["subject"] == "delete delete-me"
    assert git_service.read_file(vault, "delete-me.md") is None


def test_write_paths_do_not_call_os_chdir(
    git_service: GitService,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    name = f"test_vault_{uuid.uuid4().hex[:8]}"
    git_service.init_vault(name)

    _block_chdir(monkeypatch)

    git_service.commit_file(
        vault_name=name,
        file_path="seed.md",
        content="seed\n",
        message="seed",
    )
    git_service.commit_file(
        vault_name=name,
        file_path="delete-me.md",
        content="delete me",
        message="add delete-me",
    )
    git_service.delete_file(
        vault_name=name,
        file_path="delete-me.md",
        message="delete delete-me",
    )
    for path in ("bulk-a.md", "bulk-b.md"):
        git_service.commit_file(
            vault_name=name,
            file_path=path,
            content=path,
            message=f"add {path}",
        )

    sha = git_service.delete_paths_bulk(
        vault_name=name,
        file_paths=["bulk-a.md", "bulk-b.md"],
        message="bulk delete",
    )

    assert sha is not None


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


def test_cleanup_vault_dirs_serializes_with_vault_lock(git_service, vault):
    """On-disk teardown must hold `_vault_lock` so it cannot race an
    in-flight clone/fetch writing the same bare repo.

    `cleanup_vault_dirs` is the only git-touching op that mutates the
    on-disk repo; before this it ran lock-free, so a `delete_vault`
    rmtree could race a poller `clone_mirror` and leave a partial bare
    dir that a same-named recreate then adopts (`vault_exists()` True →
    bootstrap clone skipped → fetch into a broken repo, failing every
    retry). Here we hold the lock and assert teardown blocks until it is
    released — proving the serialization.
    """
    from app.services import git_service as gs

    assert git_service.vault_exists(vault)  # bare + worktree present

    cleanup_started = threading.Event()
    cleanup_done = threading.Event()

    def _cleanup() -> None:
        cleanup_started.set()
        git_service.cleanup_vault_dirs(vault)
        cleanup_done.set()

    lock = gs._vault_lock(vault)
    lock.acquire()
    worker = threading.Thread(target=_cleanup)
    try:
        worker.start()
        assert cleanup_started.wait(timeout=2.0)
        # Lock held → teardown is hard-blocked: dirs stay intact.
        assert not cleanup_done.wait(timeout=0.3)
        assert git_service.vault_exists(vault)
    finally:
        lock.release()

    # Lock released → teardown completes and removes everything.
    assert cleanup_done.wait(timeout=3.0)
    worker.join(timeout=3.0)
    assert not git_service.vault_exists(vault)


def test_ensure_local_bare_clones_when_absent(tmp_path):
    """No local repo → fresh clone of the upstream head."""
    git = GitService(storage_path=str(tmp_path / "vaults"))
    svc = ExternalGitService(git=git)
    url, head = _make_upstream(tmp_path)
    name = f"mirror_{uuid.uuid4().hex[:8]}"

    assert not git.vault_exists(name)
    action = svc.ensure_local_bare(name, None, head, url, "main", None)

    assert action == "cloned"
    assert git.vault_exists(name)
    assert "doc.md" in git.ls_tree(name, head)


def test_ensure_local_bare_reclones_untrusted_stale_dir(tmp_path):
    """A bare dir present for a NEVER-synced mirror (last_synced_sha=None)
    is untrusted — a stale leftover (e.g. a prior same-named vault whose
    delete cleanup raced an in-flight clone) or a clone that crashed before
    recording success. ensure_local_bare must REMOVE it and clone fresh,
    never adopt it.

    Reproduces the failure the old `vault_exists()`-only bootstrap caused:
    the path existed → clone was skipped → fetch ran against a broken repo
    → the mirror retried forever with document_count stuck at 0.
    """
    git = GitService(storage_path=str(tmp_path / "vaults"))
    svc = ExternalGitService(git=git)
    url, head = _make_upstream(tmp_path)
    name = f"mirror_{uuid.uuid4().hex[:8]}"

    # Plant a stale / corrupt bare dir where the vault's repo would live.
    stale = git._bare_path(name)
    stale.mkdir(parents=True)
    (stale / "garbage").write_text("not a git repo")
    assert git.vault_exists(name)  # path present → old code would SKIP clone

    action = svc.ensure_local_bare(name, None, head, url, "main", None)

    assert action == "cloned"
    # Stale garbage gone, replaced by a valid clone at the upstream head.
    assert not (git._bare_path(name) / "garbage").exists()
    assert "doc.md" in git.ls_tree(name, head)


def test_ensure_local_bare_unchanged_when_synced_and_sha_matches(tmp_path):
    """A trusted repo (last_synced_sha set) at the current head → no git
    work, just 'unchanged'."""
    git = GitService(storage_path=str(tmp_path / "vaults"))
    svc = ExternalGitService(git=git)
    url, head = _make_upstream(tmp_path)
    name = f"mirror_{uuid.uuid4().hex[:8]}"

    assert svc.ensure_local_bare(name, None, head, url, "main", None) == "cloned"
    # Now synced at `head`: a matching sha must short-circuit.
    action = svc.ensure_local_bare(name, head, head, url, "main", None)
    assert action == "unchanged"
