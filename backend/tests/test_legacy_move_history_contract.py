"""Public bare-Git contract for document moves and historical selectors."""

from __future__ import annotations

from dataclasses import dataclass

import pytest

from app.services.git_service import GitService


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
    assert diff["file"] == doc.old_path
    assert diff["type"] == "modified"
    assert "-old body" in diff["diff"]
    assert "+updated old body" in diff["diff"]
