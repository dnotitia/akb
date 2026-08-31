"""Crash-safe mirror-marker retirement checks."""

from __future__ import annotations

import pytest

from app.exceptions import MirrorMarkerError
from app.services.git_service import GitService


def test_external_mirror_marker_retires_through_a_fail_closed_tombstone(tmp_path) -> None:
    git = GitService(storage_path=str(tmp_path / "git"))
    vault = "retirement-marker"
    git.init_vault(vault)
    fixed_ref = git.commit_file(vault, "overview.md", "# Overview\n", "seed")
    assert git.mark_as_mirror(vault) is True

    git.quarantine_external_mirror_marker(vault, expected_ref=fixed_ref)

    bare = git._bare_path(vault)
    assert not (bare / "akb-external-mirror").exists()
    assert (bare / "akb-external-mirror-retiring").is_file()
    with pytest.raises(MirrorMarkerError, match="retirement is incomplete"):
        git.current_commit(vault)

    # Both phases are recovery-safe when a process restarts after either side
    # of the non-transactional filesystem transition.
    git.quarantine_external_mirror_marker(vault, expected_ref=fixed_ref)
    git.finalize_external_mirror_retirement(vault, expected_ref=fixed_ref)
    git.finalize_external_mirror_retirement(vault, expected_ref=fixed_ref)

    assert not (bare / "akb-external-mirror").exists()
    assert not (bare / "akb-external-mirror-retiring").exists()
    assert git.current_commit(vault) == fixed_ref


def test_external_mirror_marker_refuses_a_ref_drift_before_removal(tmp_path) -> None:
    git = GitService(storage_path=str(tmp_path / "git"))
    vault = "retirement-ref-drift"
    git.init_vault(vault)
    fixed_ref = git.commit_file(vault, "overview.md", "# Overview\n", "seed")
    assert git.mark_as_mirror(vault) is True

    with pytest.raises(MirrorMarkerError, match="fixed ref did not match"):
        git.quarantine_external_mirror_marker(vault, expected_ref="a" * 40)

    assert (git._bare_path(vault) / "akb-external-mirror").is_file()
    assert git.current_commit(vault) == fixed_ref
