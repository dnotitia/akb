"""Two OS processes may never corrupt one vault's shared worktree."""

from __future__ import annotations

import multiprocessing
from pathlib import Path

from git import Repo

from app.services.git_service import GitService


def _commit_in_process(
    storage: str,
    ready,
    results,
    path: str,
    content: str,
) -> None:
    try:
        service = GitService(storage)
        ready.wait(timeout=10)
        commit = service.commit_file(
            "shared",
            path,
            content,
            f"write {path}",
        )
        results.put(("ok", commit))
    except BaseException as exc:  # noqa: BLE001 — child reports to parent
        results.put(("error", f"{type(exc).__name__}: {exc}"))


def test_shared_worktree_serializes_across_processes(tmp_path: Path):
    service = GitService(str(tmp_path))
    service.init_vault("shared")
    service.commit_file("shared", "seed.md", "seed", "seed")

    context = multiprocessing.get_context("spawn")
    ready = context.Event()
    results = context.Queue()
    processes = [
        context.Process(
            target=_commit_in_process,
            args=(str(tmp_path), ready, results, "a.md", "alpha"),
        ),
        context.Process(
            target=_commit_in_process,
            args=(str(tmp_path), ready, results, "b.md", "bravo"),
        ),
    ]
    for process in processes:
        process.start()
    ready.set()
    for process in processes:
        process.join(timeout=20)
        assert process.exitcode == 0

    outcomes = [results.get(timeout=2) for _ in processes]
    assert [status for status, _ in outcomes] == ["ok", "ok"]
    assert service.read_file("shared", "a.md") == "alpha"
    assert service.read_file("shared", "b.md") == "bravo"

    bare = Repo(str(tmp_path / "shared.git"))
    head = bare.git.rev_parse("HEAD").strip()
    for _status, commit in outcomes:
        bare.git.merge_base("--is-ancestor", commit, head)
    assert bare.git.fsck("--full").strip() == ""
