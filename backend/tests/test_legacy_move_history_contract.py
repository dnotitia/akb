"""Public bare-Git contract for document moves and historical selectors."""

from __future__ import annotations

import subprocess
import sys
from dataclasses import dataclass

import pytest
from git import Repo

from app.services.git_service import (
    GitHistoryBoundError,
    GitHistoryCommandError,
    GitService,
)


@dataclass(frozen=True)
class _MovedDocument:
    git: GitService
    vault: str
    old_path: str
    new_path: str
    created: str
    updated: str
    moved: str
    latest: str


@pytest.fixture
def moved_document(tmp_path) -> _MovedDocument:
    git = GitService(storage_path=str(tmp_path / "vaults"))
    vault = "move-history"
    old_path = "drafts/contract.md"
    new_path = "published/contract.md"
    git.init_vault(vault)

    created = git.commit_file(
        vault_name=vault,
        file_path=old_path,
        content="old body\n",
        message="create",
    )
    updated = git.commit_file(
        vault_name=vault,
        file_path=old_path,
        content="updated old body\n",
        message="update",
    )
    moved = git.move_file(
        vault_name=vault,
        old_path=old_path,
        new_path=new_path,
        message="move",
    )
    latest = git.commit_file(
        vault_name=vault,
        file_path=new_path,
        content="latest body\n",
        message="update after move",
    )
    return _MovedDocument(
        git=git,
        vault=vault,
        old_path=old_path,
        new_path=new_path,
        created=created,
        updated=updated,
        moved=moved,
        latest=latest,
    )


def test_current_moved_path_preserves_bare_git_history_contract(
    moved_document: _MovedDocument,
) -> None:
    doc = moved_document

    assert doc.git.path_at_revision(doc.vault, doc.new_path, doc.updated[:7]) == doc.old_path
    assert doc.git.read_file(doc.vault, doc.new_path, doc.updated[:7]) == "updated old body\n"

    history = doc.git.file_log(doc.vault, doc.new_path, max_count=10)
    assert [entry["hash"] for entry in history] == [
        doc.latest[:12],
        doc.moved[:12],
        doc.updated[:12],
        doc.created[:12],
    ]
    diff = doc.git.file_diff(doc.vault, doc.new_path, doc.updated[:7])
    assert diff["file"] == doc.new_path
    assert diff["type"] == "modified"
    assert "-old body" in diff["diff"]
    assert "+updated old body" in diff["diff"]


def test_non_ascii_moved_paths_resolve_for_historical_get_and_diff(tmp_path) -> None:
    git = GitService(storage_path=str(tmp_path / "vaults"))
    vault = "unicode-move-history"
    old_path = "초안/계약서.md"
    new_path = "게시/계약서.md"
    git.init_vault(vault)

    created = git.commit_file(
        vault_name=vault,
        file_path=old_path,
        content="초안\n",
        message="create",
    )
    updated = git.commit_file(
        vault_name=vault,
        file_path=old_path,
        content="수정된 초안\n",
        message="update",
    )
    git.move_file(
        vault_name=vault,
        old_path=old_path,
        new_path=new_path,
        message="move",
    )
    git.commit_file(
        vault_name=vault,
        file_path=new_path,
        content="최신 계약서\n",
        message="update after move",
    )

    assert len(created) == 40
    assert git.path_at_revision(vault, new_path, updated[:7]) == old_path
    assert git.read_file(vault, new_path, updated[:7]) == "수정된 초안\n"
    diff = git.file_diff(vault, new_path, updated[:7])
    assert diff["file"] == new_path
    assert diff["type"] == "modified"
    assert "-초안" in diff["diff"]
    assert "+수정된 초안" in diff["diff"]


def test_path_at_revision_stream_stops_after_target(
    moved_document: _MovedDocument,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    doc = moved_document
    repo = Repo(str(doc.git._bare_path(doc.vault)))
    marker = tmp_path / "tail-consumed"
    output = (
        f"{doc.latest}\x000\nM\t{doc.new_path}\n"
        f"{doc.updated}\x000\nM\t{doc.old_path}\n"
    )
    script = (
        "import pathlib, sys, time; "
        f"sys.stdout.write({output!r}); sys.stdout.flush(); "
        "time.sleep(2); "
        f"pathlib.Path({str(marker)!r}).write_text('tail consumed')"
    )
    process = subprocess.Popen(
        [sys.executable, "-c", script],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )

    git_type = type(repo.git)
    original_execute = git_type.execute

    def fake_execute(self, command, *args, **kwargs):
        if any(str(item) == "log" for item in command):
            assert kwargs["as_process"] is True
            return process
        return original_execute(self, command, *args, **kwargs)

    monkeypatch.setattr(doc.git, "_get_repo", lambda _vault: repo)
    monkeypatch.setattr(git_type, "execute", fake_execute)

    assert doc.git.path_at_revision(doc.vault, doc.new_path, doc.updated[:7]) == doc.old_path
    assert not marker.exists()


def test_path_at_revision_entry_cap_is_explicit(
    moved_document: _MovedDocument,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.services import git_service as gs

    monkeypatch.setattr(gs, "_PATH_AT_REVISION_MAX_ENTRIES", 1, raising=False)

    with pytest.raises(GitHistoryBoundError, match="entry cap") as exc_info:
        moved_document.git.path_at_revision(
            moved_document.vault,
            moved_document.new_path,
            moved_document.updated[:7],
        )
    assert exc_info.value.code == "git_history_entry_capped"


def test_path_at_revision_output_cap_is_explicit(
    moved_document: _MovedDocument,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.services import git_service as gs

    monkeypatch.setattr(gs, "_PATH_AT_REVISION_MAX_OUTPUT_BYTES", 1, raising=False)

    with pytest.raises(GitHistoryBoundError, match="output cap") as exc_info:
        moved_document.git.path_at_revision(
            moved_document.vault,
            moved_document.new_path,
            moved_document.updated[:7],
        )
    assert exc_info.value.code == "git_history_output_capped"


def test_path_at_revision_timeout_is_explicit(
    moved_document: _MovedDocument,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.services import git_service as gs

    repo = Repo(str(moved_document.git._bare_path(moved_document.vault)))
    process = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(2)"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )

    git_type = type(repo.git)
    original_execute = git_type.execute

    def fake_execute(self, command, *args, **kwargs):
        if any(str(item) == "log" for item in command):
            assert kwargs["as_process"] is True
            return process
        return original_execute(self, command, *args, **kwargs)

    monkeypatch.setattr(moved_document.git, "_get_repo", lambda _vault: repo)
    monkeypatch.setattr(git_type, "execute", fake_execute)
    monkeypatch.setattr(gs.settings, "git_write_timeout_secs", 0.05)

    with pytest.raises(GitHistoryBoundError, match="timed out") as exc_info:
        moved_document.git.path_at_revision(
            moved_document.vault,
            moved_document.new_path,
            moved_document.updated[:7],
        )
    assert exc_info.value.code == "git_history_timeout"


def test_path_at_revision_nonzero_exit_is_not_a_missing_path(
    moved_document: _MovedDocument,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = Repo(str(moved_document.git._bare_path(moved_document.vault)))
    process = subprocess.Popen(
        [sys.executable, "-c", "import sys; sys.exit(2)"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )

    git_type = type(repo.git)
    original_execute = git_type.execute

    def fake_execute(self, command, *args, **kwargs):
        if any(str(item) == "log" for item in command):
            assert kwargs["as_process"] is True
            return process
        return original_execute(self, command, *args, **kwargs)

    monkeypatch.setattr(moved_document.git, "_get_repo", lambda _vault: repo)
    monkeypatch.setattr(git_type, "execute", fake_execute)

    with pytest.raises(GitHistoryCommandError) as exc_info:
        moved_document.git.path_at_revision(
            moved_document.vault,
            moved_document.new_path,
            moved_document.updated[:7],
        )
    assert exc_info.value.status_code == 503
    assert exc_info.value.code == "git_history_failed"


def test_path_at_revision_unknown_selector_remains_none(moved_document: _MovedDocument) -> None:
    assert (
        moved_document.git.path_at_revision(
            moved_document.vault,
            moved_document.new_path,
            "0" * 40,
        )
        is None
    )
