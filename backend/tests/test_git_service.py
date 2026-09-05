"""Unit tests for GitService write behaviour.

GitService writes to `git_storage_path` from settings. Passing a tmp
`storage_path` to the constructor bypasses that entirely, so these
tests never touch the real `/data/vaults` directory.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor

import pytest
from git import Repo

from app.services.external_git_service import ExternalGitService
from app.services.git_service import FixedRefHistoryError, GitService
from tests.extgit_http import build_runner


def _mirror_git(git_http, tmp_path) -> GitService:
    """A GitService whose external-git runner is pinned to the local smart-HTTP
    git fixture's test policy (fake host `mirror.test` → 127.0.0.1 on the
    fixture port). Mirror clone/fetch/ls-remote therefore run through the REAL
    hermetic ``ExternalGitRunner`` + a real git transport — the validator's
    scheme/host fail-closed policy is satisfied by the fixture's (host, CIDR,
    port) allowlist rule, not bypassed."""
    return GitService(
        storage_path=str(tmp_path / "vaults"),
        ext_runner=build_runner(git_http.port),
    )


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


def _commit_file_at(
    git_service: GitService,
    vault_name: str,
    file_path: str,
    content: str | None,
    message: str,
    date: str,
) -> str:
    """Create a deterministic fixture commit through the persistent worktree."""
    worktree = git_service._worktree_path(vault_name)
    if not worktree.exists():
        git_service.commit_file(
            vault_name=vault_name,
            file_path=".fixture-seed.md",
            content="",
            message="fixture seed",
        )
        git_service.commit_file(
            vault_name=vault_name,
            file_path=".fixture-seed.md",
            content="",
            message="fixture seed",
        )
    repo = Repo(str(worktree))
    try:
        repo.git.reset("--hard", "HEAD")
        target = worktree / file_path
        if content is None:
            repo.git.rm("--", file_path)
        else:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8")
            repo.git.add("--", file_path)
        with repo.git.custom_environment(
            GIT_AUTHOR_NAME="Fixture",
            GIT_AUTHOR_EMAIL="fixture@example.dev",
            GIT_COMMITTER_NAME="Fixture",
            GIT_COMMITTER_EMAIL="fixture@example.dev",
            GIT_AUTHOR_DATE=date,
            GIT_COMMITTER_DATE=date,
        ):
            repo.git.commit("-m", message)
        return repo.git.rev_parse("HEAD").strip()
    finally:
        repo.close()


def _move_file_at(
    git_service: GitService,
    vault_name: str,
    old_path: str,
    new_path: str,
    message: str,
    date: str,
) -> str:
    """Create a deterministic rename fixture through the persistent worktree."""
    worktree = git_service._worktree_path(vault_name)
    if not worktree.exists():
        git_service.commit_file(
            vault_name=vault_name,
            file_path=".fixture-seed.md",
            content="",
            message="fixture seed",
        )
        git_service.commit_file(
            vault_name=vault_name,
            file_path=".fixture-seed.md",
            content="",
            message="fixture seed",
        )
    repo = Repo(str(worktree))
    try:
        repo.git.reset("--hard", "HEAD")
        (worktree / new_path).parent.mkdir(parents=True, exist_ok=True)
        repo.git.mv("--", old_path, new_path)
        with repo.git.custom_environment(
            GIT_AUTHOR_NAME="Fixture",
            GIT_AUTHOR_EMAIL="fixture@example.dev",
            GIT_COMMITTER_NAME="Fixture",
            GIT_COMMITTER_EMAIL="fixture@example.dev",
            GIT_AUTHOR_DATE=date,
            GIT_COMMITTER_DATE=date,
        ):
            repo.git.commit("-m", message)
        return repo.git.rev_parse("HEAD").strip()
    finally:
        repo.close()


def _block_chdir(monkeypatch: pytest.MonkeyPatch) -> None:
    def fail_chdir(path: str) -> None:
        raise AssertionError(f"os.chdir must not be used by GitService writes: {path}")

    monkeypatch.setattr(os, "chdir", fail_chdir)


def _direct_cat_file_children() -> set[int]:
    """Return this pytest process's persistent GitPython cat-file children."""
    try:
        result = subprocess.run(
            ["ps", "-axo", "pid=,ppid=,command="],
            check=True,
            capture_output=True,
            text=True,
            timeout=5,
        )
    except FileNotFoundError, subprocess.SubprocessError:
        pytest.skip("process-table inspection is unavailable")

    children: set[int] = set()
    for line in result.stdout.splitlines():
        fields = line.strip().split(maxsplit=2)
        if len(fields) != 3:
            continue
        pid, parent_pid, command = fields
        if parent_pid == str(os.getpid()) and command.startswith("git cat-file --batch"):
            children.add(int(pid))
    return children


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


def test_manual_fixed_ref_history_infers_plain_git_create_activity(
    git_service: GitService,
) -> None:
    """An imported Git commit need not carry AKB's private activity footer."""
    name = f"plain_import_{uuid.uuid4().hex[:8]}"
    git_service.init_vault(name)
    current_oid = git_service.commit_file(
        vault_name=name,
        file_path="notes/imported.md",
        content="# Imported\n\nPlain Git history.\n",
        message="Import documentation",
        author_name="Fixture Collector",
        author_email="collector@example.dev",
    )
    fixed_ref = git_service.commit_file(
        vault_name=name,
        file_path="notes/unrelated.md",
        content="# Unrelated\n",
        message="Add unrelated document",
    )
    current = Repo(str(git_service._bare_path(name))).commit(current_oid)

    snapshot = git_service.manual_fixed_ref_history(
        name,
        fixed_ref,
        "notes/imported.md",
        current_commit=current_oid,
        since_epoch=current.committed_date + 1,
    )

    assert snapshot["body"] == b"# Imported\n\nPlain Git history.\n"
    assert snapshot["history"][0]["action"] == "create"
    assert snapshot["activity"] == {
        "legacy_git_oid": current_oid,
        "committed_at": current.committed_datetime,
        "actor": "Fixture Collector",
        "subject": "Import documentation",
        "summary": "",
        "action": "create",
        "path_from": None,
        "path_to": "notes/imported.md",
        "changed_paths": [
            {
                "change": "create",
                "path_from": None,
                "path_to": "notes/imported.md",
            }
        ],
    }


def test_manual_fixed_ref_history_infers_plain_git_update_activity(
    git_service: GitService,
) -> None:
    name = f"plain_update_{uuid.uuid4().hex[:8]}"
    git_service.init_vault(name)
    git_service.commit_file(
        vault_name=name,
        file_path="notes/imported.md",
        content="# Imported\n",
        message="Import documentation",
    )
    current_oid = git_service.commit_file(
        vault_name=name,
        file_path="notes/imported.md",
        content="# Imported\n\nRevised.\n",
        message="Revise documentation",
        author_name="Fixture Collector",
        author_email="collector@example.dev",
    )
    current = Repo(str(git_service._bare_path(name))).commit(current_oid)

    snapshot = git_service.manual_fixed_ref_history(
        name,
        current_oid,
        "notes/imported.md",
        current_commit=current_oid,
        since_epoch=current.committed_date + 1,
    )

    assert [entry["action"] for entry in snapshot["history"]] == [
        "update",
        "create",
    ]
    assert snapshot["activity"]["action"] == "update"
    assert snapshot["activity"]["actor"] == "Fixture Collector"
    assert snapshot["activity"]["subject"] == "Revise documentation"
    assert snapshot["activity"]["changed_paths"] == [
        {
            "change": "update",
            "path_from": None,
            "path_to": "notes/imported.md",
        }
    ]


def test_manual_fixed_ref_history_normalizes_legacy_edit_action(
    git_service: GitService,
) -> None:
    """Pre-v2 commits used ``edit`` for the public ``update`` action."""
    name = f"legacy_edit_{uuid.uuid4().hex[:8]}"
    git_service.init_vault(name)
    git_service.commit_file(
        vault_name=name,
        file_path="notes/imported.md",
        content="# Imported\n",
        message="Import documentation",
    )
    current_oid = git_service.commit_file(
        vault_name=name,
        file_path="notes/imported.md",
        content="# Imported\n\nRevised.\n",
        message=(
            "Revise documentation\n\n"
            "agent: fixture-collector\n"
            "action: edit\n"
            "summary: historical edit vocabulary"
        ),
    )

    snapshot = git_service.manual_fixed_ref_history(
        name,
        current_oid,
        "notes/imported.md",
        current_commit=current_oid,
    )
    batched = git_service.manual_fixed_ref_history_batch(
        name,
        current_oid,
        [
            {
                "file_path": "notes/imported.md",
                "current_commit": current_oid,
                "since_epoch": None,
            }
        ],
    )[0]

    assert snapshot["history"][0]["action"] == "update"
    assert snapshot["activity"]["action"] == "update"
    assert batched == snapshot


def test_manual_fixed_ref_history_batch_preserves_unicode_activity_path(
    git_service: GitService,
) -> None:
    name = f"unicode_activity_{uuid.uuid4().hex[:8]}"
    git_service.init_vault(name)
    path = "research/dynamo/전체-아키텍처.md"
    current_oid = git_service.commit_file(
        vault_name=name,
        file_path=path,
        content="# 전체 아키텍처\n",
        message=(
            "Import documentation\n\n"
            "agent: fixture-collector\n"
            "action: create\n"
            "summary: unicode path"
        ),
    )

    snapshot = git_service.manual_fixed_ref_history_batch(
        name,
        current_oid,
        [
            {
                "file_path": path,
                "current_commit": current_oid,
                "since_epoch": None,
            }
        ],
    )[0]

    assert snapshot["activity"]["action"] == "create"
    assert snapshot["activity"]["path_to"] == path


def test_manual_fixed_ref_history_rejects_declared_activity_that_conflicts_with_git(
    git_service: GitService,
) -> None:
    name = f"invalid_activity_{uuid.uuid4().hex[:8]}"
    git_service.init_vault(name)
    current_oid = git_service.commit_file(
        vault_name=name,
        file_path="notes/imported.md",
        content="# Imported\n",
        message=(
            "Import documentation\n\n"
            "agent: fixture-collector\n"
            "action: update\n"
            "summary: claims an update for a newly added file"
        ),
    )

    with pytest.raises(
        FixedRefHistoryError,
        match="activity does not match the file action",
    ):
        git_service.manual_fixed_ref_history(
            name,
            current_oid,
            "notes/imported.md",
            current_commit=current_oid,
        )


def test_manual_fixed_ref_history_batch_matches_per_path_history_for_lifecycle(
    git_service: GitService,
) -> None:
    name = f"indexed_lifecycle_{uuid.uuid4().hex[:8]}"
    git_service.init_vault(name)
    created = _commit_file_at(
        git_service,
        name,
        "notes/draft.md",
        "one\n",
        "create",
        "2026-01-01T00:00:00+0000",
    )
    updated = _commit_file_at(
        git_service,
        name,
        "notes/draft.md",
        "two\n",
        "update",
        "2026-01-01T00:00:01+0000",
    )
    moved = _move_file_at(
        git_service,
        name,
        "notes/draft.md",
        "published/draft.md",
        "move",
        "2026-01-01T00:00:02+0000",
    )
    latest = _commit_file_at(
        git_service,
        name,
        "published/draft.md",
        "three\n",
        "update after move",
        "2026-01-01T00:00:03+0000",
    )
    fixed_ref = _commit_file_at(
        git_service,
        name,
        "unrelated.md",
        "unrelated\n",
        "unrelated tip",
        "2026-01-01T00:00:04+0000",
    )
    requests = [
        {
            "file_path": "notes/draft.md",
            "current_commit": created,
            "since_epoch": None,
        },
        {
            "file_path": "notes/draft.md",
            "current_commit": updated,
            "since_epoch": None,
        },
        {
            "file_path": "published/draft.md",
            "current_commit": moved,
            "since_epoch": None,
        },
        {
            "file_path": "published/draft.md",
            "current_commit": latest,
            "since_epoch": None,
        },
    ]

    batched = git_service.manual_fixed_ref_history_batch(name, fixed_ref, requests)
    expected = [
        git_service.manual_fixed_ref_history(
            name,
            fixed_ref,
            request["file_path"],
            current_commit=request["current_commit"],
            since_epoch=request["since_epoch"],
        )
        for request in requests
    ]

    assert batched == expected

    metadata_only = git_service.manual_fixed_ref_history_batch(
        name,
        fixed_ref,
        requests,
        include_bodies=False,
    )
    assert [
        {key: value for key, value in snapshot.items() if key not in {"body_digest", "byte_size"}}
        for snapshot in metadata_only
    ] == [
        {key: value for key, value in snapshot.items() if key != "body"}
        for snapshot in expected
    ]
    for compact, full in zip(metadata_only, expected, strict=True):
        assert "body" not in compact
        assert compact["body_digest"] == hashlib.sha256(full["body"]).hexdigest()
        assert compact["byte_size"] == len(full["body"])


def test_manual_fixed_ref_history_batch_compact_inventory_rejects_nul(
    git_service: GitService,
) -> None:
    name = f"indexed_nul_{uuid.uuid4().hex[:8]}"
    git_service.init_vault(name)
    git_service.commit_file(
        vault_name=name,
        file_path="binary.md",
        content="initial text",
        message="initial body",
    )
    git_service.commit_file(
        vault_name=name,
        file_path="binary.md",
        content="worktree text",
        message="materialize worktree",
    )
    worktree = git_service._worktree_path(name)
    repo = Repo(str(worktree))
    (worktree / "binary.md").write_bytes(b"before\x00after")
    repo.git.add("--", "binary.md")
    repo.index.commit("binary body")
    fixed_ref = repo.git.rev_parse("HEAD").strip()

    with pytest.raises(FixedRefHistoryError, match="contains NUL bytes"):
        git_service.manual_fixed_ref_history_batch(
            name,
            fixed_ref,
            [
                {
                    "file_path": "binary.md",
                    "current_commit": fixed_ref,
                    "since_epoch": None,
                }
            ],
            include_bodies=False,
        )


def test_manual_fixed_ref_history_batch_respects_same_path_recreate_boundary(
    git_service: GitService,
) -> None:
    name = f"indexed_recreate_{uuid.uuid4().hex[:8]}"
    git_service.init_vault(name)
    _commit_file_at(
        git_service,
        name,
        "notes/reused.md",
        "old document\n",
        "old create",
        "2026-01-01T00:00:00+0000",
    )
    _commit_file_at(
        git_service,
        name,
        "notes/reused.md",
        None,
        "old delete",
        "2026-01-01T00:00:01+0000",
    )
    recreated = _commit_file_at(
        git_service,
        name,
        "notes/reused.md",
        "new document\n",
        "new create",
        "2026-01-01T00:00:02+0000",
    )
    fixed_ref = _commit_file_at(
        git_service,
        name,
        "unrelated.md",
        "unrelated\n",
        "unrelated tip",
        "2026-01-01T00:00:03+0000",
    )
    current = Repo(str(git_service._bare_path(name))).commit(recreated)
    try:
        since_epoch = current.committed_date
    finally:
        current.repo.close()
    request = {
        "file_path": "notes/reused.md",
        "current_commit": recreated,
        "since_epoch": since_epoch,
    }

    batched = git_service.manual_fixed_ref_history_batch(name, fixed_ref, [request])
    expected = git_service.manual_fixed_ref_history(
        name,
        fixed_ref,
        request["file_path"],
        current_commit=request["current_commit"],
        since_epoch=request["since_epoch"],
    )

    assert batched == [expected]
    assert [entry["legacy_git_oid"] for entry in batched[0]["history"]] == [recreated]


def test_manual_fixed_ref_history_batch_keeps_same_second_commits(
    git_service: GitService,
) -> None:
    name = f"indexed_same_second_{uuid.uuid4().hex[:8]}"
    git_service.init_vault(name)
    created = _commit_file_at(
        git_service,
        name,
        "notes/same-second.md",
        "one\n",
        "create",
        "2026-01-01T00:00:00+0000",
    )
    updated = _commit_file_at(
        git_service,
        name,
        "notes/same-second.md",
        "two\n",
        "update",
        "2026-01-01T00:00:00+0000",
    )
    fixed_ref = _commit_file_at(
        git_service,
        name,
        "unrelated.md",
        "unrelated\n",
        "unrelated tip",
        "2026-01-01T00:00:00+0000",
    )
    current = Repo(str(git_service._bare_path(name))).commit(updated)
    try:
        since_epoch = current.committed_date
    finally:
        current.repo.close()
    request = {
        "file_path": "notes/same-second.md",
        "current_commit": updated,
        "since_epoch": since_epoch,
    }

    batched = git_service.manual_fixed_ref_history_batch(name, fixed_ref, [request])
    expected = git_service.manual_fixed_ref_history(
        name,
        fixed_ref,
        request["file_path"],
        current_commit=request["current_commit"],
        since_epoch=request["since_epoch"],
    )

    assert batched == [expected]
    assert [entry["legacy_git_oid"] for entry in batched[0]["history"]] == [updated, created]


def test_manual_fixed_ref_history_batch_traverses_once_per_batch(
    git_service: GitService,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    name = f"indexed_cache_{uuid.uuid4().hex[:8]}"
    git_service.init_vault(name)
    current = git_service.commit_file(
        vault_name=name,
        file_path="notes/cached.md",
        content="cached\n",
        message="create",
    )
    fixed_ref = git_service.commit_file(
        vault_name=name,
        file_path="unrelated.md",
        content="unrelated\n",
        message="unrelated tip",
    )
    repo = Repo(str(git_service._bare_path(name)))
    git_type = type(repo.git)
    original_execute = git_type.execute
    calls = {"log": 0, "diff_tree": 0}

    def counting_execute(self, command, *args, **kwargs):
        command_names = {str(item) for item in command}
        if "log" in command_names:
            calls["log"] += 1
        if "diff-tree" in command_names:
            calls["diff_tree"] += 1
        return original_execute(self, command, *args, **kwargs)

    monkeypatch.setattr(git_type, "execute", counting_execute)
    request = {
        "file_path": "notes/cached.md",
        "current_commit": current,
        "since_epoch": None,
    }
    first = git_service.manual_fixed_ref_history_batch(name, fixed_ref, [request, request])
    second = git_service.manual_fixed_ref_history_batch(name, fixed_ref, [request])
    repo.close()

    assert first[0] == first[1] == second[0]
    assert calls == {"log": 2, "diff_tree": 2}


@pytest.mark.parametrize(
    "invalid_field",
    [
        "fixed_format",
        "current_format",
        "fixed_unknown",
        "current_unknown",
    ],
)
def test_manual_fixed_ref_history_batch_rejects_invalid_fixed_or_current_oid(
    git_service: GitService,
    invalid_field: str,
) -> None:
    name = f"indexed_invalid_oid_{uuid.uuid4().hex[:8]}"
    git_service.init_vault(name)
    valid_commit = git_service.commit_file(
        vault_name=name,
        file_path="notes/invalid.md",
        content="invalid\n",
        message="create",
    )
    fixed_ref = valid_commit
    current_commit = valid_commit
    if invalid_field == "fixed_format":
        fixed_ref = "not-a-commit"
    elif invalid_field == "current_format":
        current_commit = "not-a-commit"
    elif invalid_field == "fixed_unknown":
        fixed_ref = "0" * 40
    else:
        current_commit = "0" * 40

    with pytest.raises(FixedRefHistoryError):
        git_service.manual_fixed_ref_history_batch(
            name,
            fixed_ref,
            [
                {
                    "file_path": "notes/invalid.md",
                    "current_commit": current_commit,
                    "since_epoch": None,
                }
            ],
        )


def test_read_file_closes_repo_while_caller_still_references_it(
    git_service: GitService,
    vault: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The hot read path must not depend on ``Repo.__del__`` or cyclic GC."""
    bare_repo = Repo(str(git_service._bare_path(vault)))
    original_close = bare_repo.close
    close_calls = 0

    def close() -> None:
        nonlocal close_calls
        close_calls += 1
        original_close()

    monkeypatch.setattr(bare_repo, "close", close)
    monkeypatch.setattr(git_service, "_get_repo", lambda _vault_name: bare_repo)

    assert git_service.read_file(vault, "seed.md") == "seed\n"
    assert close_calls == 1


def test_parallel_reads_leave_no_cat_file_children(
    git_service: GitService,
    vault: str,
) -> None:
    """Concurrent reads release GitPython's two persistent helper processes."""
    before = _direct_cat_file_children()

    with ThreadPoolExecutor(max_workers=32) as executor:
        results = list(
            executor.map(
                lambda _index: git_service.read_file(vault, "seed.md"),
                range(96),
            )
        )

    assert results == ["seed\n"] * 96
    deadline = time.monotonic() + 2
    after = _direct_cat_file_children()
    while after - before and time.monotonic() < deadline:
        time.sleep(0.02)
        after = _direct_cat_file_children()
    assert after - before == set()


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


def test_ensure_local_bare_clones_when_absent(git_http, tmp_path):
    """No local repo → fresh clone of the upstream head over the REAL hermetic
    runner + git transport. Returns (action, materialized_sha)."""
    git = _mirror_git(git_http, tmp_path)
    svc = ExternalGitService(git=git)
    url, head = git_http.add_repo("up1", {"doc.md": "# Hello\n"})
    name = f"mirror_{uuid.uuid4().hex[:8]}"

    assert not git.vault_exists(name)
    action, sha = svc.ensure_local_bare(name, None, head, url, "main", None)

    assert action == "cloned"
    assert sha == head  # materialized SHA == upstream head
    assert git.vault_exists(name)
    assert "doc.md" in git.ls_tree(name, sha)


def test_ensure_local_bare_reclones_untrusted_stale_dir(git_http, tmp_path):
    """A bare dir present for a NEVER-synced mirror (last_synced_sha=None)
    is untrusted — a stale leftover (e.g. a prior same-named vault whose
    delete cleanup raced an in-flight clone) or a clone that crashed before
    recording success. ensure_local_bare must REMOVE it and clone fresh,
    never adopt it.

    Reproduces the failure the old `vault_exists()`-only bootstrap caused:
    the path existed → clone was skipped → fetch ran against a broken repo
    → the mirror retried forever with document_count stuck at 0.
    """
    git = _mirror_git(git_http, tmp_path)
    svc = ExternalGitService(git=git)
    url, head = git_http.add_repo("up2", {"doc.md": "# Hello\n"})
    name = f"mirror_{uuid.uuid4().hex[:8]}"

    # Plant a stale / corrupt bare dir where the vault's repo would live.
    stale = git._bare_path(name)
    stale.mkdir(parents=True)
    (stale / "garbage").write_text("not a git repo")
    assert git.vault_exists(name)  # path present → old code would SKIP clone

    action, sha = svc.ensure_local_bare(
        name,
        None,
        head,
        url,
        "main",
        None,
        allow_unmarked_never_synced=True,
    )

    assert action == "cloned"
    assert sha == head
    # Stale garbage gone, replaced by a valid clone at the upstream head.
    assert not (git._bare_path(name) / "garbage").exists()
    assert "doc.md" in git.ls_tree(name, sha)


def test_never_synced_reclone_holds_exact_active_sidecar_authority(
    git_service, monkeypatch,
):
    """The exceptional unmarked cleanup runs while retirement is DB-blocked."""
    import asyncio

    state = {"transaction_open": False, "ensure_calls": 0}

    class _Transaction:
        async def __aenter__(self):
            state["transaction_open"] = True

        async def __aexit__(self, *_args):
            state["transaction_open"] = False

    class _Connection:
        def transaction(self):
            return _Transaction()

        async def fetchval(self, query, *args):
            assert "FOR SHARE" in query
            assert args[0] == vault_id
            return 1

    class _Acquire:
        async def __aenter__(self):
            return _Connection()

        async def __aexit__(self, *_args):
            return None

    class _Pool:
        def acquire(self):
            return _Acquire()

    vault_id = uuid.uuid4()
    vault_name = f"never-synced-{uuid.uuid4().hex[:8]}"
    git_service._bare_path(vault_name).mkdir()
    service = ExternalGitService(git=git_service)

    def _ensure(*args, **kwargs):
        assert state["transaction_open"] is True
        assert kwargs == {"allow_unmarked_never_synced": True}
        state["ensure_calls"] += 1
        return ("cloned", "a" * 40)

    monkeypatch.setattr(service, "ensure_local_bare", _ensure)
    cfg = {
        "last_synced_sha": None,
        "remote_url": "https://example.invalid/repo.git",
        "remote_branch": "main",
        "auth_token": None,
    }
    result = asyncio.run(
        service._ensure_local_bare_for_reconcile(
            _Pool(),
            vault_id=vault_id,
            vault_name=vault_name,
            cfg=cfg,
            new_sha="a" * 40,
        )
    )

    assert result == ("cloned", "a" * 40)
    assert state == {"transaction_open": False, "ensure_calls": 1}


def test_ensure_local_bare_unchanged_when_synced_and_sha_matches(git_http, tmp_path):
    """A trusted, clean repo (last_synced_sha set) at the current head → no git
    work, just 'unchanged' with the materialized SHA read from the local ref."""
    git = _mirror_git(git_http, tmp_path)
    svc = ExternalGitService(git=git)
    url, head = git_http.add_repo("up3", {"doc.md": "# Hello\n"})
    name = f"mirror_{uuid.uuid4().hex[:8]}"

    action, sha = svc.ensure_local_bare(name, None, head, url, "main", None)
    assert action == "cloned" and sha == head
    # Now synced at `head`: a matching sha must short-circuit.
    action, sha = svc.ensure_local_bare(name, head, head, url, "main", None)
    assert action == "unchanged"
    assert sha == head


def test_ensure_local_bare_fetches_when_upstream_advances(git_http, tmp_path):
    """A trusted repo behind the upstream → incremental fetch; the returned
    materialized SHA is the NEW local ref, and ls_tree sees the new content."""
    git = _mirror_git(git_http, tmp_path)
    svc = ExternalGitService(git=git)
    url, head1 = git_http.add_repo("up_fetch", {"doc.md": "# One\n"})
    name = f"mirror_{uuid.uuid4().hex[:8]}"

    action, sha = svc.ensure_local_bare(name, None, head1, url, "main", None)
    assert action == "cloned" and sha == head1

    head2 = git_http.publish_change("up_fetch", "extra.md", "# Two\n")
    assert head2 != head1
    action, sha = svc.ensure_local_bare(name, head1, head2, url, "main", None)
    assert action == "fetched"
    assert sha == head2  # materialized SHA follows the fetched local ref
    tree = git.ls_tree(name, sha)
    assert "doc.md" in tree and "extra.md" in tree


def test_fetch_remote_refuses_stale_poller_promotion_after_retirement(
    git_http, tmp_path, monkeypatch,
):
    """A fetch staged before retirement must never advance the retained HEAD.

    ``fetch_to_ref`` is the poller's network/staging boundary.  Pause at that
    seam, finish the offline marker retirement, then resume the stale poller:
    only its temporary ref may exist; the manual vault's published ``HEAD``
    must remain the frozen ref.
    """
    from app.exceptions import MirrorMarkerError

    git = _mirror_git(git_http, tmp_path)
    url, head1 = git_http.add_repo("retired-fetch", {"doc.md": "one\n"})
    vault_name = f"retired-fetch-{uuid.uuid4().hex[:8]}"
    git.clone_mirror(vault_name, url, "main", None)
    head2 = git_http.publish_change("retired-fetch", "doc.md", "two\n")
    assert head2 != head1

    original_fetch = git._ext_runner.fetch_to_ref

    def fetch_then_finish_retirement(*args, **kwargs):
        original_fetch(*args, **kwargs)
        git.quarantine_external_mirror_marker(vault_name, expected_ref=head1)
        git.finalize_external_mirror_retirement(vault_name, expected_ref=head1)

    monkeypatch.setattr(git._ext_runner, "fetch_to_ref", fetch_then_finish_retirement)

    with pytest.raises(MirrorMarkerError, match="external-git mirror is no longer active"):
        git.fetch_remote(vault_name, url, "main", None)

    bare = _mirror_bare(tmp_path, vault_name)
    assert git.current_commit(vault_name) == head1
    assert not (bare / "akb-external-mirror").exists()
    assert not (bare / "akb-external-mirror-retiring").exists()


def test_stale_sync_cannot_rearm_a_completed_retired_mirror(git_http, tmp_path):
    """A later stale reconcile cannot recreate the mirror marker after handoff."""
    from app.exceptions import MirrorMarkerError

    git = _mirror_git(git_http, tmp_path)
    service = ExternalGitService(git=git)
    url, head1 = git_http.add_repo("retired-rearm", {"doc.md": "one\n"})
    vault_name = f"retired-rearm-{uuid.uuid4().hex[:8]}"
    assert service.ensure_local_bare(vault_name, None, head1, url, "main", None) == (
        "cloned",
        head1,
    )
    git.quarantine_external_mirror_marker(vault_name, expected_ref=head1)
    git.finalize_external_mirror_retirement(vault_name, expected_ref=head1)
    head2 = git_http.publish_change("retired-rearm", "doc.md", "two\n")

    with pytest.raises(MirrorMarkerError, match="external-git mirror marker is missing"):
        service.ensure_local_bare(vault_name, head1, head2, url, "main", None)

    bare = _mirror_bare(tmp_path, vault_name)
    assert git.current_commit(vault_name) == head1
    assert not (bare / "akb-external-mirror").exists()
    assert not (bare / "akb-external-mirror-retiring").exists()


def test_stale_reclone_cannot_replace_a_completed_retired_mirror(
    git_http, tmp_path, monkeypatch,
):
    """A health decision made before retirement has no destructive authority.

    Pause at the re-clone publication seam, complete offline retirement, then
    resume the stale poller. Its staged clone must be discarded: the retained
    repository, frozen HEAD/history, and marker-free manual-vault shape survive.
    """
    from app.exceptions import MirrorMarkerError

    git = _mirror_git(git_http, tmp_path)
    service = ExternalGitService(git=git)
    url, head1 = git_http.add_repo("retired-reclone", {"doc.md": "one\n"})
    vault_name = f"retired-reclone-{uuid.uuid4().hex[:8]}"
    assert service.ensure_local_bare(vault_name, None, head1, url, "main", None) == (
        "cloned",
        head1,
    )
    head2 = git_http.publish_change("retired-reclone", "doc.md", "two\n")
    assert head2 != head1

    real_reclone = git.reclone_active_mirror

    def _retire_then_resume(*args, **kwargs):
        git.quarantine_external_mirror_marker(vault_name, expected_ref=head1)
        git.finalize_external_mirror_retirement(vault_name, expected_ref=head1)
        return real_reclone(*args, **kwargs)

    monkeypatch.setattr(git, "reclone_active_mirror", _retire_then_resume)
    monkeypatch.setattr(
        git,
        "inspect_mirror_structure",
        lambda *args, **kwargs: ["disallowed-config"],
    )

    with pytest.raises(MirrorMarkerError, match="external-git mirror is no longer active"):
        service.ensure_local_bare(vault_name, head1, head2, url, "main", None)

    bare = _mirror_bare(tmp_path, vault_name)
    assert git.current_commit(vault_name) == head1
    assert git.read_file(vault_name, "doc.md") == "one\n"
    assert [item.hexsha for item in Repo(str(bare)).iter_commits()] == [head1]
    assert not (bare / "akb-external-mirror").exists()
    assert not (bare / "akb-external-mirror-retiring").exists()


def test_stale_first_sync_snapshot_cannot_replace_a_completed_retired_mirror(
    git_http, tmp_path, monkeypatch,
):
    """A stale ``last_synced_sha=None`` snapshot is not publication authority."""
    from app.exceptions import MirrorMarkerError

    git = _mirror_git(git_http, tmp_path)
    service = ExternalGitService(git=git)
    url, head = git_http.add_repo("retired-first-sync", {"doc.md": "one\n"})
    vault_name = f"retired-first-sync-{uuid.uuid4().hex[:8]}"
    service.ensure_local_bare(vault_name, None, head, url, "main", None)
    git.quarantine_external_mirror_marker(vault_name, expected_ref=head)
    git.finalize_external_mirror_retirement(vault_name, expected_ref=head)
    monkeypatch.setattr(
        git,
        "inspect_mirror_structure",
        lambda *args, **kwargs: ["disallowed-config"],
    )

    with pytest.raises(MirrorMarkerError, match="external-git mirror is no longer active"):
        service.ensure_local_bare(vault_name, None, head, url, "main", None)

    bare = _mirror_bare(tmp_path, vault_name)
    assert git.current_commit(vault_name) == head
    assert git.read_file(vault_name, "doc.md") == "one\n"
    assert not (bare / "akb-external-mirror").exists()
    assert not (bare / "akb-external-mirror-retiring").exists()


def test_is_healthy_repo(git_http, tmp_path):
    """Structural soundness probe: absent → False, healthy clone → True,
    objects dropped → False."""
    git = _mirror_git(git_http, tmp_path)
    svc = ExternalGitService(git=git)
    url, head = git_http.add_repo("up4", {"doc.md": "# Hello\n"})
    name = f"mirror_{uuid.uuid4().hex[:8]}"

    assert git.is_healthy_repo(name) is False  # absent
    svc.ensure_local_bare(name, None, head, url, "main", None)
    assert git.is_healthy_repo(name) is True  # healthy clone
    shutil.rmtree(git._bare_path(name) / "objects")  # corrupt
    assert git.is_healthy_repo(name) is False


def test_ensure_local_bare_reclones_corrupt_synced_repo(git_http, tmp_path):
    """A previously-synced repo (last_synced_sha set) that is now corrupt
    must be re-cloned, not fetched-into. Closes the 'post-sync corruption'
    self-heal gap: keying on last_synced_sha alone would take the fetch /
    unchanged branch and never recover the broken repo.
    """
    git = _mirror_git(git_http, tmp_path)
    svc = ExternalGitService(git=git)
    url, head = git_http.add_repo("up5", {"doc.md": "# Hello\n"})
    name = f"mirror_{uuid.uuid4().hex[:8]}"

    action, sha = svc.ensure_local_bare(name, None, head, url, "main", None)
    assert action == "cloned"
    assert git.is_healthy_repo(name)

    # Corrupt the now-synced repo (partial fetch / disk error shape).
    shutil.rmtree(git._bare_path(name) / "objects")
    assert not git.is_healthy_repo(name)

    # last_synced_sha set AND sha unchanged, but the repo is broken — the
    # integrity gate must still force a clean re-clone.
    action, sha = svc.ensure_local_bare(name, head, head, url, "main", None)
    assert action == "cloned"
    assert sha == head
    assert git.is_healthy_repo(name)
    assert "doc.md" in git.ls_tree(name, sha)


def _repoint_head_to_missing_tree(bare_path) -> str:
    """Make a tmp_path mirror bare's HEAD commit PRESENT but its ROOT TREE object
    ABSENT (a partial-fetch / disk-loss shape) WITHOUT disturbing packed objects:
    store a fresh commit object whose ``tree`` points at a non-existent oid, then
    repoint refs/heads/main (HEAD's symref target) at it. tmp_path bare only —
    never a real repo/.git; no chmod, no recursive walk."""
    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0"}
    ghost_tree = "0123456789abcdef0123456789abcdef01234567"  # pragma: allowlist secret
    body = (
        f"tree {ghost_tree}\n"
        "author T <t@akb.local> 0 +0000\n"
        "committer T <t@akb.local> 0 +0000\n\nghost\n"
    )
    ghost = subprocess.run(
        ["git", f"--git-dir={bare_path}", "hash-object", "-t", "commit", "-w", "--stdin"],
        input=body, capture_output=True, text=True, env=env, check=True,
    ).stdout.strip()
    subprocess.run(
        ["git", f"--git-dir={bare_path}", "update-ref", "refs/heads/main", ghost],
        capture_output=True, text=True, env=env, check=True,
    )
    return ghost


def test_is_healthy_repo_catches_root_tree_loss(git_http, tmp_path):
    """A bare whose HEAD commit is PRESENT but whose ROOT TREE
    object is LOST (partial fetch / disk error) must read as UNHEALTHY — verifying
    HEAD^{commit} alone missed this, so the self-heal re-clone never converged and
    every blob read failed. Adding the HEAD^{tree} peel catches it, and the
    self-heal path re-clones the untrusted repo back to health."""
    git = _mirror_git(git_http, tmp_path)
    svc = ExternalGitService(git=git)
    url, head = git_http.add_repo("uptree", {"doc.md": "# Hi\n"})
    name = f"mirror_{uuid.uuid4().hex[:8]}"

    svc.ensure_local_bare(name, None, head, url, "main", None)
    assert git.is_healthy_repo(name) is True

    # Commit stays present; its root tree object goes missing.
    _repoint_head_to_missing_tree(git._bare_path(name))
    assert git.is_healthy_repo(name) is False  # tree loss now caught (was True)

    # last_synced_sha set AND sha unchanged, but the tree is gone — the integrity
    # gate must still force a clean re-clone back to a healthy, readable repo.
    action, sha = svc.ensure_local_bare(name, head, head, url, "main", None)
    assert action == "cloned"
    assert sha == head
    assert git.is_healthy_repo(name) is True
    assert "doc.md" in git.ls_tree(name, sha)


# ══ Mirror READ paths are hermetic (runner, not GitPython) ══
def _mirror_bare(tmp_path, name: str):
    return tmp_path / "vaults" / f"{name}.git"


def test_clone_mirror_marks_vault_and_non_mirror_is_not_flagged(git_http, tmp_path, git_service, vault):
    git = _mirror_git(git_http, tmp_path)
    url, _ = git_http.add_repo("markme", {"doc.md": "x\n"})
    git.clone_mirror("mk", url, "main", None)
    assert git._is_mirror("mk") is True
    assert (_mirror_bare(tmp_path, "mk") / "akb-external-mirror").is_file()
    # A normal (non-mirror) vault carries no marker.
    assert git_service._is_mirror(vault) is False


def test_mirror_reads_route_through_hermetic_runner(git_http, tmp_path, monkeypatch):
    git = _mirror_git(git_http, tmp_path)
    url, head = git_http.add_repo("mreads", {"doc.md": "# T\n\nhi\n"})
    git.clone_mirror("mr", url, "main", None)

    # If ANY mirror read fell back to GitPython, _get_repo would blow up — so a
    # green run proves every read routed through the hermetic runner.
    def _boom(*a, **k):
        raise AssertionError("GitPython _get_repo used on a mirror read path")

    monkeypatch.setattr(git, "_get_repo", _boom)

    assert git.read_file("mr", "doc.md") == "# T\n\nhi\n"
    assert git.read_file("mr", "doc.md", commit=head) == "# T\n\nhi\n"
    assert git.read_file("mr", "missing.md") is None
    log = git.file_log("mr", "doc.md")
    assert log and log[0]["hash"] == head[:12]
    vl = git.vault_log("mr")
    assert vl and vl[0]["hash"] == head[:12]
    assert any(f["path"] == "doc.md" for f in vl[0]["files"])
    assert git.file_diff("mr", "doc.md", head)["type"] == "added"


def test_mirror_read_unknown_commit_is_not_found_not_error(git_http, tmp_path):
    """Companion to the MAJOR fail-closed fix (fix-4): ``read_file`` with a
    genuinely unknown commit (short OR full 40-hex) stays a not-found → None
    (404), NOT a 502. Because ``resolve_blob_oid`` now PROPAGATES an unresolvable
    rev instead of masking it as absent, ``_read_file_mirror`` pre-resolves the
    caller's commit and maps an unknown/​malformed one to None here — a present
    commit still reads normally, and a genuinely absent path is still None."""
    git = _mirror_git(git_http, tmp_path)
    url, head = git_http.add_repo("uc", {"doc.md": "hi\n"})
    git.clone_mirror("uc", url, "main", None)
    # Present commit + present path → content; present commit + absent path → None.
    assert git.read_file("uc", "doc.md", commit=head) == "hi\n"
    assert git.read_file("uc", "doc.md") == "hi\n"
    assert git.read_file("uc", "missing.md") is None
    # A well-formed but UNKNOWN full 40-hex commit → not-found (None), not a raise.
    assert git.read_file("uc", "doc.md", commit="0" * 40) is None
    # A short/opaque unknown ref → not-found (None), unchanged.
    assert git.read_file("uc", "doc.md", commit="deadbeef") is None


def test_mirror_reads_zero_network_even_with_promisor_and_rewrite_config(git_http, tmp_path):
    git = _mirror_git(git_http, tmp_path)
    url, head = git_http.add_repo("prom", {"doc.md": "hi\n"})
    git.clone_mirror("pr", url, "main", None)
    # Plant promisor / partial-clone / insteadOf-rewrite into the mirror config.
    with open(_mirror_bare(tmp_path, "pr") / "config", "a", encoding="utf-8") as f:
        f.write(
            '\n[remote "origin"]\n\tpromisor = true\n'
            "\tpartialclonefilter = blob:none\n"
            '[url "http://127.0.0.1:1/"]\n\tinsteadOf = http://mirror.test/\n'
        )
    before = git_http.request_count
    assert git.read_file("pr", "doc.md") == "hi\n"
    assert git.file_log("pr", "doc.md")
    assert git.vault_log("pr")
    assert git.file_diff("pr", "doc.md", head)["type"] == "added"
    # GIT_NO_LAZY_FETCH + sealed env → the reads never touch the network, even
    # with a promisor + rewrite config planted in the repo.
    assert git_http.request_count == before


# ══ Unchanged fast-path requires local-ref agreement ════════
def test_ensure_local_bare_refetches_on_local_ref_mismatch(git_http, tmp_path):
    git = _mirror_git(git_http, tmp_path)
    svc = ExternalGitService(git=git)
    url, head1 = git_http.add_repo("mm", {"doc.md": "one\n"})
    action, sha = svc.ensure_local_bare("mmv", None, head1, url, "main", None)
    assert action == "cloned" and sha == head1

    # Move the LOCAL ref forward (as a prior partial reconcile would) while the
    # recorded cursor stays at head1.
    git_http.publish_change("mm", "doc.md", "one\ntwo\n")
    head2 = git.fetch_remote("mmv", url, "main", None)
    assert head2 != head1 and git.materialized_sha("mmv", "main") == head2

    # hint == cursor == head1, but the LOCAL ref is head2 → MUST fetch/reconcile,
    # never take the stale 'unchanged' fast path.
    action, _ = svc.ensure_local_bare("mmv", head1, head1, url, "main", None)
    assert action == "fetched"


def test_ensure_local_bare_unchanged_requires_all_three_to_agree(git_http, tmp_path):
    git = _mirror_git(git_http, tmp_path)
    svc = ExternalGitService(git=git)
    url, head1 = git_http.add_repo("agree", {"doc.md": "one\n"})
    svc.ensure_local_bare("agv", None, head1, url, "main", None)
    # hint == cursor == local ref == head1 → genuinely unchanged.
    action, sha = svc.ensure_local_bare("agv", head1, head1, url, "main", None)
    assert action == "unchanged" and sha == head1


# ══ A structure finding forces an actual sterile re-clone ══
def test_ensure_local_bare_reclones_after_structure_finding(git_http, tmp_path):
    git = _mirror_git(git_http, tmp_path)
    svc = ExternalGitService(git=git)
    url, head1 = git_http.add_repo("ster", {"doc.md": "hi\n"})
    svc.ensure_local_bare("stv", None, head1, url, "main", None)

    # Plant a transport-redirect key → the structure default-deny flags it.
    with open(_mirror_bare(tmp_path, "stv") / "config", "a", encoding="utf-8") as f:
        f.write('\n[remote "origin"]\n\tproxy = http://evil:8080\n')
    assert git.inspect_mirror_structure("stv", url, "main")  # dirty

    # Even though hint == cursor (would otherwise be 'unchanged'), the finding
    # forces a sterile re-clone that yields a structurally CLEAN repo.
    action, _ = svc.ensure_local_bare("stv", head1, head1, url, "main", None)
    assert action == "cloned"
    assert git.inspect_mirror_structure("stv", url, "main") == []
    assert git._is_mirror("stv") is True  # marker re-established by the re-clone


# ══ Finding #1: marker backfill for pre-fix mirrors ═════════
# `clone_mirror` only writes the marker on a fresh clone, so a mirror created
# before the marker existed has none — `_is_mirror` is False and its reads
# fall through to GitPython (fail-open). The DB (`vault_external_git`) is the
# authoritative mirror list; the startup backfill re-stamps the on-disk marker.
# `ensure_local_bare` deliberately refuses a missing marker rather than letting
# a stale poller re-arm a retired manual vault. Tests use `git init --bare` via
# the local in-process git fixture (no real network); the fixture host pins
# GIT_TERMINAL_PROMPT=0 in the runner's sealed env, and we also set it for the
# fixture's own non-runner git calls as a belt.
def test_mark_as_mirror_is_idempotent_and_needs_existing_bare(git_http, tmp_path, monkeypatch):
    monkeypatch.setenv("GIT_TERMINAL_PROMPT", "0")
    git = _mirror_git(git_http, tmp_path)
    url, _ = git_http.add_repo("mm", {"doc.md": "x\n"})

    # Absent bare → no-op (nothing to mark).
    assert git.mark_as_mirror("ghost") is False
    assert git._is_mirror("ghost") is False

    git.clone_mirror("mmv", url, "main", None)
    marker = _mirror_bare(tmp_path, "mmv") / "akb-external-mirror"
    assert marker.is_file()  # clone wrote it

    # Already-marked mirror → no-op.
    assert git.mark_as_mirror("mmv") is False

    # Pre-fix state (marker stripped) → first mark writes, second is a no-op.
    marker.unlink()
    assert git._is_mirror("mmv") is False
    assert git.mark_as_mirror("mmv") is True
    assert marker.is_file()
    assert git.mark_as_mirror("mmv") is False


def test_backfill_stamps_marker_less_mirror_and_makes_reads_hermetic(git_http, tmp_path, monkeypatch):
    from app.services.external_git_service import _stamp_mirror_markers

    monkeypatch.setenv("GIT_TERMINAL_PROMPT", "0")
    git = _mirror_git(git_http, tmp_path)
    url, head = git_http.add_repo(
        "bf", {"doc.md": "# T\n\nhi\n", "sub/nested.md": "# N\n"}
    )
    git.clone_mirror("bfv", url, "main", None)
    # Simulate a PRE-FIX mirror: strip the marker clone_mirror wrote, so
    # _is_mirror is False and reads would fall through to GitPython (the gap).
    marker = _mirror_bare(tmp_path, "bfv") / "akb-external-mirror"
    marker.unlink()
    assert git._is_mirror("bfv") is False

    # The backfill enumerates the DB's mirror vault names; the sync seam takes
    # them directly (the DB query itself is exercised at app startup, not by this
    # DB-less unit gate). Returns (marked, failed); marking is idempotent — a
    # re-run writes nothing and reports no failures.
    assert _stamp_mirror_markers(git, ["bfv"]) == (1, [])
    assert marker.is_file()
    assert git._is_mirror("bfv") is True
    assert _stamp_mirror_markers(git, ["bfv"]) == (0, [])

    # Every read now routes through the hermetic runner: if ANY fell back to
    # GitPython, _get_repo would blow up — a green run proves fail-closed reads.
    def _boom(*a, **k):
        raise AssertionError("GitPython _get_repo used on a mirror read path")

    monkeypatch.setattr(git, "_get_repo", _boom)

    assert git.read_file("bfv", "doc.md") == "# T\n\nhi\n"
    assert git.file_log("bfv", "doc.md")[0]["hash"] == head[:12]
    assert git.vault_log("bfv")[0]["hash"] == head[:12]
    assert git.file_diff("bfv", "doc.md", head)["type"] == "added"
    # Finding #5 read paths are hermetic on a mirror too.
    assert git.current_commit("bfv") == head
    assert set(git.list_files("bfv")) == {"doc.md", "sub/nested.md"}
    assert git.list_files("bfv", "sub") == ["sub/nested.md"]
    assert git.list_directories("bfv") == ["sub"]
    # Finding #5: whole-repo diff has no hermetic primitive yet → refuse on a
    # mirror (never GitPython) with a clean typed 4xx, not a 500.
    from app.exceptions import AKBError

    with pytest.raises(AKBError) as diff_err:
        git.diff("bfv", head)
    assert diff_err.value.status_code == 400


def test_backfill_skips_non_mirror_vault(git_http, tmp_path, monkeypatch):
    from app.services.external_git_service import _stamp_mirror_markers

    monkeypatch.setenv("GIT_TERMINAL_PROMPT", "0")
    git = _mirror_git(git_http, tmp_path)
    # A plain (non-mirror) vault: normal init + commit, never a clone_mirror, so
    # no marker and NOT in the DB's mirror list.
    git.init_vault("plainv")
    git.commit_file(vault_name="plainv", file_path="a.md", content="a\n", message="seed")
    assert git._is_mirror("plainv") is False

    # The DB mirror list drives the backfill; a plain vault is never in it, so it
    # is not a backfill target and keeps _is_mirror False → GitPython read path.
    assert _stamp_mirror_markers(git, []) == (0, [])
    assert git._is_mirror("plainv") is False
    assert git.read_file("plainv", "a.md") == "a\n"


def test_stamp_mirror_markers_collects_write_failures(git_http, tmp_path, monkeypatch):
    """Fail-fast contract: the sweep tries every vault and DISTINGUISHES a normal
    skip (bare absent — not yet cloned) from a real marker-WRITE fault on an
    existing bare, collecting only the latter so the caller can refuse to start."""
    from app.services.external_git_service import _stamp_mirror_markers

    monkeypatch.setenv("GIT_TERMINAL_PROMPT", "0")
    git = _mirror_git(git_http, tmp_path)
    url, _ = git_http.add_repo("cf", {"doc.md": "x\n"})
    git.clone_mirror("okv", url, "main", None)
    (_mirror_bare(tmp_path, "okv") / "akb-external-mirror").unlink()  # needs a write

    # Simulate a disk/permission fault on ONE existing mirror's marker write.
    real_mark = git.mark_as_mirror

    def flaky_mark(name):
        if name == "failv":
            raise OSError("simulated disk fault")
        return real_mark(name)

    monkeypatch.setattr(git, "mark_as_mirror", flaky_mark)

    # "absentv" has no bare → normal skip (NOT a failure); "okv" writes; "failv"
    # write faults → collected. Order of failures follows input order.
    marked, failed = _stamp_mirror_markers(git, ["okv", "absentv", "failv"])
    assert marked == 1
    assert failed == ["failv"]


def test_backfill_mirror_markers_unconditional_and_fail_fast(git_http, tmp_path, monkeypatch):
    """The backfill entry point (`backfill_mirror_markers`) is UNCONDITIONAL and
    fail-fast (external-git kill-switch consistency):
      - it runs even when external_git is disabled — the marker is the
        fail-closed safety net that lets the read paths REFUSE a disabled mirror,
        so a partial kill-switch that skipped the backfill would leave
        marker-less mirrors reading fail-OPEN. `external_git_service` therefore
        no longer imports `settings`, and enumerates + stamps regardless.
      - a marker WRITE failure on an existing bare → raises so the lifespan
        aborts boot before serving (fail-fast, zero fail-open window).
    Runs the async entry synchronously via asyncio.run; the DB (get_pool /
    VaultExternalGitRepository) is stubbed so the gate needs no live Postgres.
    """
    import asyncio

    from app.services import external_git_service as egs

    monkeypatch.setenv("GIT_TERMINAL_PROMPT", "0")
    git = _mirror_git(git_http, tmp_path)

    # `external_git_service` must not reference `settings` at all anymore: the
    # backfill is unconditional, so there is nothing left to gate on.
    assert not hasattr(egs, "settings")

    # (1) A pre-fix mirror (marker stripped) → the backfill enumerates the DB
    # and STAMPS the marker, regardless of the (removed) kill-switch.
    url, _ = git_http.add_repo("kill", {"doc.md": "x\n"})
    git.clone_mirror("dv", url, "main", None)
    (_mirror_bare(tmp_path, "dv") / "akb-external-mirror").unlink()
    assert git._is_mirror("dv") is False

    class _RepoOneMirror:
        def __init__(self, _pool):
            pass

        async def list_mirror_vault_names(self):
            return ["dv"]

    async def _fake_pool():
        return object()  # not really used — the repo is stubbed below

    monkeypatch.setattr(egs, "get_pool", _fake_pool)
    monkeypatch.setattr(egs, "VaultExternalGitRepository", _RepoOneMirror)

    assert asyncio.run(egs.backfill_mirror_markers(git=git)) == 1
    assert git._is_mirror("dv") is True

    # (2) An existing mirror whose marker write faults → RuntimeError (boot
    # abort). Stub the repo to report one mirror; force its write to fault.
    class _RepoFail:
        def __init__(self, _pool):
            pass

        async def list_mirror_vault_names(self):
            return ["failv"]

    def _always_fail(_name):
        raise OSError("simulated disk fault")

    monkeypatch.setattr(egs, "VaultExternalGitRepository", _RepoFail)
    monkeypatch.setattr(git, "mark_as_mirror", _always_fail)

    with pytest.raises(RuntimeError) as boot_err:
        asyncio.run(egs.backfill_mirror_markers(git=git))
    assert "failv" in str(boot_err.value)


def test_ensure_local_bare_requires_authoritative_backfill_for_a_missing_marker(
    git_http, tmp_path, monkeypatch,
):
    from app.exceptions import MirrorMarkerError
    from app.services.external_git_service import _stamp_mirror_markers

    monkeypatch.setenv("GIT_TERMINAL_PROMPT", "0")
    git = _mirror_git(git_http, tmp_path)
    svc = ExternalGitService(git=git)
    url, head = git_http.add_repo("heal", {"doc.md": "hi\n"})

    action, sha = svc.ensure_local_bare("healv", None, head, url, "main", None)
    assert action == "cloned" and sha == head
    # Simulate a pre-fix mirror: strip the marker the clone wrote.
    (_mirror_bare(tmp_path, "healv") / "akb-external-mirror").unlink()
    assert git._is_mirror("healv") is False

    # A stale reconcile may not recreate a missing marker itself: completed
    # retirement intentionally leaves the retained manual vault marker-less.
    with pytest.raises(MirrorMarkerError, match="marker is missing"):
        svc.ensure_local_bare("healv", head, head, url, "main", None)

    # The DB-authoritative startup sweep is the sole marker-restoration path.
    assert _stamp_mirror_markers(git, ["healv"]) == (1, [])
    action, sha2 = svc.ensure_local_bare("healv", head, head, url, "main", None)
    assert action == "unchanged" and sha2 == head
    assert git._is_mirror("healv") is True


# ══ Marker robustness — fail-CLOSED on an abnormal entry ══
def test_is_mirror_fail_closed_on_abnormal_marker(git_http, tmp_path):
    """An ambiguous marker entry (directory / broken symlink) must fail CLOSED:
    `_is_mirror`, `mark_as_mirror`, AND the read paths all RAISE
    MirrorMarkerError rather than collapse to "not a mirror" (which would let
    the read fall open to GitPython). A valid regular-file marker → mirror; an
    absent marker (manual-vault shape) → not a mirror, GitPython read works."""
    from app.exceptions import MirrorMarkerError

    git = _mirror_git(git_http, tmp_path)
    url, _ = git_http.add_repo("ab", {"doc.md": "x\n"})
    git.clone_mirror("abv", url, "main", None)
    marker = _mirror_bare(tmp_path, "abv") / "akb-external-mirror"

    # Valid regular-file marker → mirror.
    assert marker.is_file() and git._is_mirror("abv") is True

    # (1) A DIRECTORY at the marker path → fail-CLOSED raise everywhere.
    marker.unlink()
    marker.mkdir()
    with pytest.raises(MirrorMarkerError):
        git._is_mirror("abv")
    with pytest.raises(MirrorMarkerError):
        git.mark_as_mirror("abv")
    # A read must propagate the raise (fail-closed), never fall to GitPython.
    with pytest.raises(MirrorMarkerError):
        git.read_file("abv", "doc.md")
    # The cat_blob choke-point shares _use_mirror_reader, so the abnormal-marker
    # raise propagates there too (never a silent GitPython/runner fallback).
    with pytest.raises(MirrorMarkerError):
        git.cat_blob("abv", "0" * 40)
    marker.rmdir()

    # (2) A BROKEN SYMLINK at the marker path → fail-CLOSED raise (lstat, not
    # followed).
    marker.symlink_to(_mirror_bare(tmp_path, "abv") / "does-not-exist")
    with pytest.raises(MirrorMarkerError):
        git._is_mirror("abv")
    with pytest.raises(MirrorMarkerError):
        git.mark_as_mirror("abv")
    marker.unlink()

    # (3) Absent marker (manual-vault shape) → not a mirror, and a read falls to
    # GitPython cleanly (no raise, no regression).
    assert git._is_mirror("abv") is False
    assert git.read_file("abv", "doc.md") == "x\n"


def test_mark_as_mirror_no_clobber_on_race(git_http, tmp_path, monkeypatch):
    """O_EXCL no-clobber: an entry that appears in the create race window is
    NOT overwritten. Simulate the race by planting a DIFFERENT regular file at
    the marker path between the absent-check and the create — the create must
    fail-closed on it rather than clobber it. A valid pre-existing marker is an
    idempotent no-op (True→False across two runs)."""
    from app.exceptions import MirrorMarkerError

    monkeypatch.setenv("GIT_TERMINAL_PROMPT", "0")
    git = _mirror_git(git_http, tmp_path)
    url, _ = git_http.add_repo("nc", {"doc.md": "x\n"})
    git.clone_mirror("ncv", url, "main", None)
    marker = _mirror_bare(tmp_path, "ncv") / "akb-external-mirror"

    # Already a valid marker → idempotent no-op.
    assert git.mark_as_mirror("ncv") is False
    # Strip → first mark writes, second is a no-op (no clobber of its own write).
    marker.unlink()
    assert git.mark_as_mirror("ncv") is True
    assert marker.read_text().strip() == "akb external-git mirror"
    assert git.mark_as_mirror("ncv") is False

    # Race: a foreign entry appears where the marker would be created. lstat
    # classifies it first, so a foreign DIRECTORY is refused fail-closed rather
    # than clobbered.
    marker.unlink()
    marker.mkdir()
    with pytest.raises(MirrorMarkerError):
        git.mark_as_mirror("ncv")
    # The foreign entry is untouched.
    assert marker.is_dir()


# ══ Mirror READ refused when the kill-switch is off ══
def test_mirror_read_refused_when_disabled(git_http, tmp_path, monkeypatch):
    """With `external_git_enabled` off, a mirror read is REFUSED (503) by every
    read path — never served by the runner OR GitPython. The marker
    still identifies the vault as a mirror (backfill is unconditional). A manual
    (non-mirror) vault is unaffected by the kill-switch."""
    import types

    from app.exceptions import AKBError
    from app.services import git_service as gs

    git = _mirror_git(git_http, tmp_path)
    url, _ = git_http.add_repo("ks", {"doc.md": "hi\n"})
    git.clone_mirror("ksv", url, "main", None)
    # A plain vault for the no-regression check.
    git.init_vault("plain")
    git.commit_file(vault_name="plain", file_path="a.md", content="a\n", message="seed")

    # Enabled (default): mirror reads serve normally, down to the lowest-level
    # object read (cat_blob) — the choke-point is transparent when on.
    assert git.read_file("ksv", "doc.md") == "hi\n"
    head = git.materialized_sha("ksv", "main")
    blob_sha = git.ls_tree("ksv", head)["doc.md"]
    assert git.cat_blob("ksv", blob_sha) == b"hi\n"

    # Disabled kill-switch: mirror reads are refused with 503, on every path —
    # including the cat_blob choke-point that metadata_worker (and any future
    # indexer) funnels its mirror-object reads through. It refuses BEFORE any
    # git I/O, so a disabled deployment performs zero mirror read / outbound.
    monkeypatch.setattr(gs, "settings", types.SimpleNamespace(external_git_enabled=False))
    for call in (
        lambda: git.read_file("ksv", "doc.md"),
        lambda: git.list_files("ksv"),
        lambda: git.list_directories("ksv"),
        lambda: git.current_commit("ksv"),
        lambda: git.file_log("ksv", "doc.md"),
        lambda: git.vault_log("ksv"),
        lambda: git.file_diff("ksv", "doc.md", "HEAD"),
        lambda: git.cat_blob("ksv", blob_sha),
    ):
        with pytest.raises(AKBError) as err:
            call()
        assert err.value.status_code == 503

    # The cat_blob refusal is specifically the kill-switch guard (not an
    # incidental git failure): it carries the external_git_disabled code.
    with pytest.raises(AKBError) as cb_err:
        git.cat_blob("ksv", blob_sha)
    assert cb_err.value.code == "external_git_disabled"

    # A manual (non-mirror) vault is unaffected by the kill-switch.
    assert git.read_file("plain", "a.md") == "a\n"
    assert git.list_files("plain") == ["a.md"]


# ══ A systemically-dirty fresh re-clone breaks the loop ══
def test_ensure_local_bare_breaks_reclone_loop_on_systemic_findings(git_http, tmp_path, monkeypatch):
    """If a FRESH sterile re-clone STILL trips the structure inspector, the
    findings are systemic (git-version / inspector incompatibility) — re-cloning
    every poll would loop forever. ensure_local_bare must fail loudly with a
    compat error after exactly ONE re-clone, not loop."""
    from app.services.external_git_service import ExternalGitCompatError

    git = _mirror_git(git_http, tmp_path)
    svc = ExternalGitService(git=git)
    url, head = git_http.add_repo("loop", {"doc.md": "hi\n"})
    action, _ = svc.ensure_local_bare("lpv", None, head, url, "main", None)
    assert action == "cloned"

    # EVERY structure inspection — including on our own fresh sterile re-clone —
    # reports a finding.
    clones: list[str] = []
    real_clone = git.reclone_active_mirror

    def _counting_clone(name, *a, **k):
        clones.append(name)
        return real_clone(name, *a, **k)

    monkeypatch.setattr(git, "reclone_active_mirror", _counting_clone)
    monkeypatch.setattr(
        git, "inspect_mirror_structure", lambda *a, **k: ["disallowed config entry"]
    )

    # The finding forces a re-clone; the fresh clone STILL reports it → fail
    # loudly instead of looping. Exactly ONE re-clone happened.
    with pytest.raises(ExternalGitCompatError):
        svc.ensure_local_bare("lpv", head, head, url, "main", None)
    assert clones == ["lpv"]


def test_ensure_local_bare_fails_closed_on_abnormal_marker(git_http, tmp_path):
    """The unchanged/self-heal fast path must fail CLOSED when the marker is
    abnormal: mark_as_mirror raises on a directory marker rather than serving a
    mirror whose on-disk signal is ambiguous."""
    from app.exceptions import MirrorMarkerError

    git = _mirror_git(git_http, tmp_path)
    svc = ExternalGitService(git=git)
    url, head = git_http.add_repo("selfheal", {"doc.md": "hi\n"})
    svc.ensure_local_bare("shv", None, head, url, "main", None)

    # Replace the marker with a directory (abnormal). The unchanged fast path
    # calls mark_as_mirror → fail-closed rather than serve.
    marker = _mirror_bare(tmp_path, "shv") / "akb-external-mirror"
    marker.unlink()
    marker.mkdir()
    with pytest.raises(MirrorMarkerError):
        svc.ensure_local_bare("shv", head, head, url, "main", None)


# ══ Oversized-blob gate — size WITHOUT reading, gated on the cap ══
def test_blob_exceeds_max_sizes_without_reading_and_gates_on_cap(git_http, tmp_path, monkeypatch):
    """`blob_exceeds_max` returns the EXACT byte size (via `cat-file -s`, no
    content read) and compares it to `external_git_blob_max_bytes` with a strict
    `>` (at the cap is NOT oversized)."""
    import types

    from app.services import git_service as gs

    git = _mirror_git(git_http, tmp_path)
    content = "# Doc\n\nhello world\n"
    nbytes = len(content.encode("utf-8"))
    url, head = git_http.add_repo("sz", {"doc.md": content})
    git.clone_mirror("szv", url, "main", None)
    blob_sha = git.ls_tree("szv", head)["doc.md"]

    def _settings(cap):
        return types.SimpleNamespace(
            external_git_enabled=True, external_git_blob_max_bytes=cap
        )

    # Well under the cap → not oversized; size is exact.
    monkeypatch.setattr(gs, "settings", _settings(10 * 1024 * 1024))
    assert git.blob_exceeds_max("szv", blob_sha) == (nbytes, False)
    # Cap below the blob → oversized (size unchanged).
    monkeypatch.setattr(gs, "settings", _settings(nbytes - 1))
    assert git.blob_exceeds_max("szv", blob_sha) == (nbytes, True)
    # Exactly at the cap is NOT oversized (strict `>`).
    monkeypatch.setattr(gs, "settings", _settings(nbytes))
    assert git.blob_exceeds_max("szv", blob_sha) == (nbytes, False)


def test_blob_exceeds_max_refused_when_disabled(git_http, tmp_path, monkeypatch):
    """The oversized gate shares `cat_blob`'s kill-switch choke-point: a disabled
    mirror is refused with a 503 BEFORE any git I/O, so a kill-switched
    deployment sizes zero mirror objects."""
    import types

    from app.exceptions import AKBError
    from app.services import git_service as gs

    git = _mirror_git(git_http, tmp_path)
    url, head = git_http.add_repo("szks", {"doc.md": "hi\n"})
    git.clone_mirror("szksv", url, "main", None)
    blob_sha = git.ls_tree("szksv", head)["doc.md"]

    monkeypatch.setattr(
        gs, "settings",
        types.SimpleNamespace(external_git_enabled=True, external_git_blob_max_bytes=10),
    )
    assert git.blob_exceeds_max("szksv", blob_sha)[1] is False

    monkeypatch.setattr(
        gs, "settings",
        types.SimpleNamespace(external_git_enabled=False, external_git_blob_max_bytes=10),
    )
    with pytest.raises(AKBError) as err:
        git.blob_exceeds_max("szksv", blob_sha)
    assert err.value.status_code == 503
    assert err.value.code == "external_git_disabled"


# ══ Containment backstop — vault-name path-safety + storage-root + symlinks ══
def test_cleanup_vault_dirs_rejects_unsafe_vault_name(git_service, tmp_path):
    """A DIRECT cleanup entry with an unsafe vault name is refused BEFORE
    any rmtree — a `..` / separator / absolute name could otherwise drive rmtree
    OUTSIDE the storage root."""
    # A real dir outside the storage root that an unsafe name resolves to:
    # _bare_path("../pwned") == <storage>/../pwned.git == tmp_path/pwned.git.
    outside = tmp_path / "pwned.git"
    outside.mkdir()
    (outside / "keep.txt").write_text("do not delete")

    for bad in ("..", "../pwned", "a/b", "/abs", "", "foo/../pwned"):
        with pytest.raises(ValueError):
            git_service.cleanup_vault_dirs(bad)

    # The outside-root target survived — the name check refused before any rmtree.
    assert outside.exists() and (outside / "keep.txt").exists()


def test_cleanup_vault_dirs_is_symlink_safe(git_service, tmp_path):
    """A symlink planted at the bare path is NOT rmtree'd THROUGH: its
    (out-of-root) target survives and only the link is removed."""
    external = tmp_path / "external_target"
    external.mkdir()
    (external / "precious.txt").write_text("do not delete")

    name = "linkvault"
    bare = git_service._bare_path(name)
    bare.parent.mkdir(parents=True, exist_ok=True)
    os.symlink(external, bare)  # the bare PATH itself is a symlink → external dir
    assert bare.is_symlink()

    git_service.cleanup_vault_dirs(name)

    # Link removed; the target dir and its file are untouched (never followed).
    assert not bare.is_symlink() and not bare.exists()
    assert external.exists() and (external / "precious.txt").exists()


def test_cleanup_stale_locks_is_symlink_safe(git_service, tmp_path):
    """A symlinked `index.lock` is never followed: the target's mtime is not read
    for the age gate and the target is never removed — only real regular lock
    files are cleared."""
    import time as _time

    name = "locksym"
    # The linked-worktree lock location the cleaner scans:
    #   <bare>/worktrees/<name>/index.lock
    lock_dir = git_service._bare_path(name) / "worktrees" / name
    lock_dir.mkdir(parents=True)
    external = tmp_path / "lock_target.txt"
    external.write_text("do not delete")
    # Age the TARGET so the gate would pass if its mtime were (wrongly) read.
    old = _time.time() - 3600
    os.utime(external, (old, old))
    lock = lock_dir / "index.lock"
    os.symlink(external, lock)
    assert lock.is_symlink()

    cleared = git_service.cleanup_stale_locks(max_age_seconds=60.0)

    assert cleared == 0  # the symlinked lock is skipped, not counted
    assert lock.is_symlink()  # link left in place — never acted through
    assert external.exists()  # target untouched


# ══ Oversized blob is refused on EVERY mirror content read ═══
# The reconcile gate is only an ingestion gate; a direct cat_blob / historical
# read_file / root-commit file_diff must ALSO refuse an over-cap blob BEFORE
# materializing it, so an on-demand read can't blow up memory.
def test_mirror_reads_refuse_oversized_blob_before_materializing(
    git_http, tmp_path, monkeypatch
):
    from app.services import git_service as gs

    git = _mirror_git(git_http, tmp_path)
    url, head = git_http.add_repo("big", {"big.md": "X" * 5000})
    git.clone_mirror("bigv", url, "main", None)
    tree = git.ls_tree("bigv", head)
    blob_sha = tree["big.md"]

    # Cap far below the blob size → every content read must refuse it.
    monkeypatch.setattr(gs.settings, "external_git_blob_max_bytes", 100)

    # Prove NO content is materialized: bomb the runner's content readers. The
    # size pre-check (cat-file -s) must short-circuit BEFORE either is reached.
    def _boom(*a, **k):
        raise AssertionError("content materialized despite the oversized gate")

    monkeypatch.setattr(git._ext_runner, "cat_blob", _boom)
    monkeypatch.setattr(git._ext_runner, "cat_path", _boom)

    with pytest.raises(gs.ExternalGitOversizedError):
        git.cat_blob("bigv", blob_sha)  # by-sha choke-point (reconcile/metadata)
    with pytest.raises(gs.ExternalGitOversizedError):
        git.read_file("bigv", "big.md")  # HEAD read
    with pytest.raises(gs.ExternalGitOversizedError):
        git.read_file("bigv", "big.md", commit=head)  # historical read
    with pytest.raises(gs.ExternalGitOversizedError):
        git.file_diff("bigv", "big.md", head)  # root-commit full-content diff


def test_mirror_reads_normal_size_unaffected_by_oversized_gate(
    git_http, tmp_path, monkeypatch
):
    """A blob UNDER the cap is unaffected — the gate never fires and content is
    served exactly as before (no regression)."""
    from app.services import git_service as gs

    git = _mirror_git(git_http, tmp_path)
    url, head = git_http.add_repo("small", {"ok.md": "hi\n"})
    git.clone_mirror("smallv", url, "main", None)
    tree = git.ls_tree("smallv", head)

    # Cap comfortably above the 3-byte blob.
    monkeypatch.setattr(gs.settings, "external_git_blob_max_bytes", 100)

    assert git.read_file("smallv", "ok.md") == "hi\n"
    assert git.read_file("smallv", "ok.md", commit=head) == "hi\n"
    assert git.cat_blob("smallv", tree["ok.md"]) == b"hi\n"
    assert git.file_diff("smallv", "ok.md", head)["type"] == "added"


def test_read_file_missing_path_is_still_none_under_gate(git_http, tmp_path, monkeypatch):
    """A path absent at the rev sizes as None → 404 (None), never an oversized
    error — the gate must not turn a missing file into a refusal."""
    from app.services import git_service as gs

    git = _mirror_git(git_http, tmp_path)
    url, _head = git_http.add_repo("miss", {"ok.md": "hi\n"})
    git.clone_mirror("missv", url, "main", None)
    monkeypatch.setattr(gs.settings, "external_git_blob_max_bytes", 1)
    assert git.read_file("missv", "does-not-exist.md") is None


# ══ Diff refuses an oversized PRE-image, not only post ═══
def test_mirror_file_diff_refuses_oversized_preimage_on_shrink(
    git_http, tmp_path, monkeypatch
):
    """A commit that SHRINKS a large file has a small POST-image but a large
    PRE-image; ``diff-tree -p`` would buffer the large pre-image whole. The mirror
    diff must size BOTH images and refuse when EITHER is over the cap
    — the post-image alone would pass here."""
    from app.services import git_service as gs

    git = _mirror_git(git_http, tmp_path)
    url, _head1 = git_http.add_repo("shrink", {"big.md": "X" * 5000})
    git.clone_mirror("shrinkv", url, "main", None)
    git_http.publish_change("shrink", "big.md", "x\n")  # 5000 B → 2 B
    head2 = git.fetch_remote("shrinkv", url, "main", None)

    # Cap BETWEEN the post-image (2 B, would pass) and the pre-image (5000 B) — the
    # large pre-image must force the refusal.
    monkeypatch.setattr(gs.settings, "external_git_blob_max_bytes", 100)

    # No patch may be materialized: bomb the runner's content/patch readers. The
    # pre/post size pre-check must short-circuit BEFORE file_diff_entry runs.
    def _boom(*a, **k):
        raise AssertionError("diff materialized despite the oversized pre-image gate")

    monkeypatch.setattr(git._ext_runner, "cat_path", _boom)
    monkeypatch.setattr(git._ext_runner, "file_diff_entry", _boom)

    with pytest.raises(gs.ExternalGitOversizedError):
        git.file_diff("shrinkv", "big.md", head2)


def test_mirror_file_diff_small_modification_unaffected(git_http, tmp_path, monkeypatch):
    """A modification where BOTH images are under the cap diffs normally — the
    pre/post gate never false-trips (no regression)."""
    from app.services import git_service as gs

    git = _mirror_git(git_http, tmp_path)
    url, _head1 = git_http.add_repo("mod", {"a.md": "one\n"})
    git.clone_mirror("modv", url, "main", None)
    git_http.publish_change("mod", "a.md", "one\ntwo\n")
    head2 = git.fetch_remote("modv", url, "main", None)
    monkeypatch.setattr(gs.settings, "external_git_blob_max_bytes", 10_000)
    d = git.file_diff("modv", "a.md", head2)
    assert d["type"] == "modified" and "+two" in d["diff"]


# ══ Symlink containment on the READ paths + cleanup ═════
def test_mirror_reads_refuse_symlinked_bare(git_http, tmp_path):
    """A mirror whose bare is (or was swapped to be) a symlink is refused on
    EVERY mirror READ path — reading through it could leave the storage root
    (partial). The link is never followed."""
    from app.exceptions import MirrorMarkerError

    git = _mirror_git(git_http, tmp_path)
    url, head = git_http.add_repo("symread", {"doc.md": "hi\n"})
    git.clone_mirror("symv", url, "main", None)
    blob = git.ls_tree("symv", head)["doc.md"]

    # Move the real bare aside and replace its path with a symlink to it. The
    # marker still resolves THROUGH the link (so _is_mirror stays True), but the
    # read paths must fail-closed on the symlinked bare itself.
    bare = _mirror_bare(tmp_path, "symv")
    real = tmp_path / "real_symv.git"
    os.rename(bare, real)
    os.symlink(real, bare)
    assert bare.is_symlink()

    for call in (
        lambda: git.cat_blob("symv", blob),
        lambda: git.blob_exceeds_max("symv", blob),
        lambda: git.read_file("symv", "doc.md"),
        lambda: git.read_file("symv", "doc.md", commit=head),
        lambda: git.file_diff("symv", "doc.md", head),
        lambda: git.file_log("symv", "doc.md"),
        lambda: git.vault_log("symv"),
    ):
        with pytest.raises(MirrorMarkerError):
            call()
    # The real bare (out of the link) is never mutated by the refused reads.
    assert (real / "akb-external-mirror").is_file()


def test_cleanup_stale_locks_skips_symlinked_bare(git_service, tmp_path):
    """A symlinked ``<name>.git`` in the storage root is never FOLLOWED for lock
    cleanup — enumerating / unlinking ``index.lock`` under it would act OUTSIDE
    the storage root. Only real directories are vaults."""
    import time as _time

    # An external "bare" holding a stale-looking lock the cleaner must NOT touch.
    external = tmp_path / "external_bare"
    lock_dir = external / "worktrees" / "evil"
    lock_dir.mkdir(parents=True)
    lock = lock_dir / "index.lock"
    lock.write_text("x")
    old = _time.time() - 3600
    os.utime(lock, (old, old))

    # Plant a symlinked `<name>.git` in the storage root pointing at it.
    git_service.storage_path.mkdir(parents=True, exist_ok=True)
    link = git_service.storage_path / "evil.git"
    os.symlink(external, link)
    assert link.is_symlink()

    cleared = git_service.cleanup_stale_locks(max_age_seconds=60.0)
    assert cleared == 0  # the symlinked bare is skipped, never followed
    assert lock.exists()  # the external lock is untouched


# ══ Containment backstop on EVERY DB-derived-name path ═══════
# The name guard now lives in _bare_path/_worktree_path (the shared choke-point),
# not only cleanup — so marker/clone/fetch/read can never interpolate an unsafe
# name out of the storage root.
@pytest.mark.parametrize(
    "bad", ["../evil", "/abs/evil", "a/b", "..", ".", "", "x\x00y", "a\nb"]
)
def test_bare_and_worktree_paths_reject_unsafe_names(git_service, bad):
    with pytest.raises(ValueError):
        git_service._bare_path(bad)
    with pytest.raises(ValueError):
        git_service._worktree_path(bad)


def test_mutation_entries_reject_unsafe_name_before_touching_disk(git_service):
    """A DB-derived unsafe name is refused at the shared path choke-point —
    BEFORE any create / write / rename / network — so nothing lands outside the
    storage root."""
    with pytest.raises(ValueError):
        git_service.mark_as_mirror("../escape")
    with pytest.raises(ValueError):
        git_service.clone_mirror("../escape", "https://mirror.test/x.git", "main", None)
    with pytest.raises(ValueError):
        git_service.fetch_remote("../escape", "https://mirror.test/x.git", "main", None)


def test_contained_rejects_path_resolving_outside_root(git_service, tmp_path):
    """The resolve-under-root backstop behind the mutation-point checks: a
    symlink INSIDE the root that points OUT of it makes any path THROUGH it
    resolve outside — _contained must reject that, and accept a genuine in-root
    path."""
    outside = tmp_path / "outside"
    outside.mkdir()
    link = git_service.storage_path / "escape"
    link.symlink_to(outside, target_is_directory=True)
    with pytest.raises(ValueError):
        git_service._contained(link / "x.git")
    # A genuine in-root path resolves fine (returns the resolved path).
    assert git_service._contained(git_service.storage_path / "ok.git")


def test_mark_as_mirror_refuses_symlinked_bare_out_of_root(git_service, tmp_path):
    """A safe NAME whose on-disk bare is a symlink pointing OUT of the storage
    root is refused (the lstat guard catches the symlink; the containment resolve
    is the deeper backstop) and nothing is written into the external target."""
    from app.exceptions import MirrorMarkerError

    name = "sneaky"
    outside = tmp_path / "outside_root"
    outside.mkdir()
    bare = git_service._bare_path(name)
    bare.parent.mkdir(parents=True, exist_ok=True)
    bare.symlink_to(outside, target_is_directory=True)
    with pytest.raises(MirrorMarkerError):
        git_service.mark_as_mirror(name)
    assert not (outside / "akb-external-mirror").exists()


# ══ The POLL-PATH mirror reads share the symlink-checked resolver ══
def test_poll_path_mirror_reads_refuse_symlinked_bare(git_http, tmp_path):
    """The reconcile / poll-path mirror reads (is_healthy_repo, materialized_sha,
    inspect_mirror_structure, ls_tree, last_commit_for_path) route through the
    SAME symlink-checking ``_mirror_bare`` resolver as the on-demand reads: a
    symlinked bare fails CLOSED with MirrorMarkerError BEFORE any git/config read
    through the link. ``ensure_local_bare`` inherits the fail-closed
    (it reaches inspect_mirror_structure / is_healthy_repo). The real bare (out of
    the link) is never read through or mutated."""
    from app.exceptions import MirrorMarkerError

    git = _mirror_git(git_http, tmp_path)
    svc = ExternalGitService(git=git)
    url, head = git_http.add_repo("psym", {"doc.md": "hi\n"})
    git.clone_mirror("psym", url, "main", None)

    # Swap the real bare for a symlink to it (a restored / tampered layout).
    bare = _mirror_bare(tmp_path, "psym")
    real = tmp_path / "real_psym.git"
    os.rename(bare, real)
    os.symlink(real, bare)
    assert bare.is_symlink()

    for call in (
        lambda: git.is_healthy_repo("psym"),
        lambda: git.materialized_sha("psym", "main"),
        lambda: git.inspect_mirror_structure("psym", url, "main"),
        lambda: git.ls_tree("psym", head),
        lambda: git.last_commit_for_path("psym", "doc.md"),
        # ensure_local_bare reaches inspect_mirror_structure before any read.
        lambda: svc.ensure_local_bare("psym", head, head, url, "main", None),
    ):
        with pytest.raises(MirrorMarkerError):
            call()
    assert (real / "akb-external-mirror").is_file()  # real bare never mutated


# ══ Diff binds the RESOLVED full commit OID (checked == read) ══
def test_file_diff_binds_resolved_full_oid(git_http, tmp_path, monkeypatch):
    """``_file_diff_mirror`` passes the ALREADY-RESOLVED full commit OID to the
    renderer — never the abbreviated hash the renderer would re-resolve — so the
    oversized size-check OID and the read OID are the SAME object, and the
    streaming cap is the conservative diff bound."""
    import types

    from app.services import git_service as gs

    git = _mirror_git(git_http, tmp_path)
    url, head = git_http.add_repo("oidb", {"doc.md": "hi\n"})
    git.clone_mirror("oidb", url, "main", None)  # real settings for the clone

    captured: dict = {}
    real_entry = git._ext_runner.file_diff_entry

    def _spy(bare, commit, path, *, max_output_bytes=None):
        captured["commit"] = commit
        captured["cap"] = max_output_bytes
        return real_entry(bare, commit, path, max_output_bytes=max_output_bytes)

    monkeypatch.setattr(git._ext_runner, "file_diff_entry", _spy)
    cap = 100_000
    monkeypatch.setattr(
        gs, "settings",
        types.SimpleNamespace(external_git_enabled=True, external_git_blob_max_bytes=cap),
    )

    out = git.file_diff("oidb", "doc.md", head[:12])  # pass the ABBREVIATED hash
    assert out["type"] == "added"
    # The renderer received the FULL 40-hex OID, not the 12-hex short hash.
    assert captured["commit"] == head and len(captured["commit"]) == 40
    # ...and the streaming cap is the conservative 4×cap+margin diff bound.
    assert captured["cap"] == gs._DIFF_OUTPUT_FACTOR * cap + gs._OUTPUT_MARGIN_BYTES


# ══ A near-cap short-line FULL REWRITE renders without a false 413 ══
def test_file_diff_allows_near_cap_full_rewrite(git_http, tmp_path, monkeypatch):
    """A legitimate FULL REWRITE of a near-cap file made of many SHORT lines
    renders a unified diff whose per-line +/- prefixes push it past 2×cap. The old
    2×cap streaming bound wrongly 413'd it; the conservative 4×cap bound
    lets it through, while the per-image size checks still gate oversized CONTENT.
    """
    import types

    from app.services import git_service as gs
    from app.services.git_service import _OUTPUT_MARGIN_BYTES

    cap = 100_000
    n = 49_000  # 49k lines of "a\n" = 98 000 bytes < cap
    pre = "a\n" * n
    post = "b\n" * n  # every line differs → a genuine full rewrite (empty LCS)
    assert len(pre.encode()) < cap and len(post.encode()) < cap

    git = _mirror_git(git_http, tmp_path)
    url, _head = git_http.add_repo("fullrw", {"big.md": pre})
    git.clone_mirror("fullrw", url, "main", None)
    head2 = git_http.publish_change("fullrw", "big.md", post)
    git.fetch_remote("fullrw", url, "main", None)  # setup done with real settings

    # Small cap in effect ONLY for the diff call.
    monkeypatch.setattr(
        gs, "settings",
        types.SimpleNamespace(external_git_enabled=True, external_git_blob_max_bytes=cap),
    )
    out = git.file_diff("fullrw", "big.md", head2)
    assert out["type"] == "modified"
    # The rendered diff EXCEEDS the old 2×cap+margin bound → the old bound would
    # have wrongly 413'd this legitimate full rewrite; the new bound renders it.
    assert len(out["diff"]) > 2 * cap + _OUTPUT_MARGIN_BYTES
    assert "+b" in out["diff"] and "-a" in out["diff"]
