"""Git operations for Vault management.

Each Vault is a Git bare repo. This service handles:
- Vault (bare repo) initialization
- Reading files from HEAD
- Committing file changes (add/update/delete)
- Log and diff queries
- Cloning / fetching from an external remote (read-only mirror vaults)

Writes go through a persistent per-vault worktree linked to the bare repo
(`git worktree add`). No clone-per-commit, no push. The worktree shares
the object store with bare, so commits in the worktree update the bare's
refs directly. Concurrent writes against the same worktree are serialized
by both a per-process threading lock and a storage-backed ``flock``; the final
ref update is compare-and-swap against the exact parent commit.

Every external-mirror git command — the three network sinks (clone_mirror /
fetch_remote / ls_remote_head) *and* every local read on a mirror bare repo
(ls_tree / cat_blob / last_commit_for_path / is_healthy_repo) — is executed
through the single hermetic `ExternalGitRunner` boundary. The auth
token is passed only as an `Authorization` header via `--config-env`, never in
the URL, argv, or the bare's `.git/config`; the runner also seals the child
environment, pins DNS, and blocks non-https transports. See
`app/services/external_git_runner.py`.
"""

from __future__ import annotations

import fcntl
import logging
import os
import re
import selectors
import shutil
import stat
import subprocess
import threading
import time
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from git import Blob, Repo
from git.exc import BadName, BadObject, GitError

from app.config import settings
from app.exceptions import AKBError, MirrorMarkerError
from app.services.external_git_runner import ExternalGitRunner
from app.services.external_git_validation import validate_branch
from app.util.errors import (
    GIT_HISTORY_ENTRY_CAPPED,
    GIT_HISTORY_FAILED,
    GIT_HISTORY_OUTPUT_CAPPED,
    GIT_HISTORY_TIMEOUT,
)

logger = logging.getLogger("akb.git")


@contextmanager
def _managed_repo(repo: Repo) -> Iterator[Repo]:
    """Deterministically release GitPython's persistent command processes.

    GitPython keeps ``git cat-file --batch`` helpers in each ``Repo`` object.
    Relying on ``Repo.__del__`` leaves those helpers alive until cyclic garbage
    collection happens, which is unbounded in a long-running API process.  A
    cleanup failure is logged instead of replacing the result of a completed
    mutation with an ambiguous error response.
    """
    try:
        yield repo
    finally:
        try:
            repo.close()
        except Exception:  # noqa: BLE001 - cleanup must not mask the operation
            logger.warning(
                "Failed to close GitPython repository handle for %s",
                getattr(repo, "working_dir", None) or getattr(repo, "git_dir", "unknown"),
                exc_info=True,
            )


# Sentinel file dropped at the bare-repo root by ``clone_mirror`` to mark a vault
# as an external-git mirror. It is written by us — never by upstream
# repo content, which can only populate git objects/refs, not arbitrary files at
# the bare root — so it is an unforgeable, DB-free signal that lets EVERY
# GitService read path self-route to the hermetic runner for mirror vaults with
# no per-call DB lookup. It travels with the repo through the atomic
# clone/rename and is removed with it by ``cleanup_vault_dirs``. The name is not
# a git-recognised file, so it never perturbs git operations or the structure
# default-deny inspector.
_MIRROR_MARKER = "akb-external-mirror"

# Slack over ``external_git_blob_max_bytes`` for the runner's STREAMING output
# cap. The per-blob size pre-check already refuses an
# over-cap blob, so a passed blob's streamed read is at most ``cap`` bytes; this
# margin only keeps a blob sized EXACTLY at the cap from tripping the
# strictly-greater-than backstop. A diff is bounded separately (see below), plus
# this margin.
_OUTPUT_MARGIN_BYTES = 64 * 1024

# Multiplier for a RENDERED unified diff's streaming cap. A unified diff
# prefixes EVERY line with one byte (`+`/`-`/` `), and a
# full rewrite renders BOTH the pre- AND the post-image. Worst case per side is
# `cap` content bytes plus one prefix byte per line (≤`cap` lines) = 2×cap; both
# sides = 4×cap. The diff/index/`---`/`+++`/`@@` headers (a few path-length lines
# plus one hunk header for a full rewrite) fit comfortably inside
# ``_OUTPUT_MARGIN_BYTES``. The old 2×cap bound counted only the content, not the
# per-line prefixes, so a near-cap file of many SHORT lines rendered a legitimate
# full-rewrite diff above 2×cap and was falsely 413'd; 4×cap+margin never
# false-trips a legitimate diff of two ≤cap images while still bounding memory.
# The per-image size checks refuse genuinely oversized content up front; this
# streaming cap stays the backstop for a pathological many-hunk patch.
_DIFF_OUTPUT_FACTOR = 4

# ``path_at_revision`` only needs the prefix of a path-following walk through
# the requested target.  Keep the prefix itself bounded as a backstop for a
# target that is not in the path's history, and stream the command so a large
# suffix is never materialized.  The timeout reuses the existing configured Git
# command bound (the same setting used by the write lane); the entry/output
# limits are code-owned safety floors because this internal lookup has no user
# supplied page size.
_PATH_AT_REVISION_MAX_ENTRIES = 10_000
_PATH_AT_REVISION_MAX_OUTPUT_BYTES = 4 * 1024 * 1024
_PATH_AT_REVISION_READ_CHUNK_BYTES = 64 * 1024
_PATH_AT_REVISION_DEFAULT_TIMEOUT_SECS = 30.0


def _marker_state(marker: Path) -> str:
    """Classify the on-disk ``_MIRROR_MARKER`` entry, fail-CLOSED.

    Returns ``"absent"`` when nothing exists at the marker path (the normal
    manual-vault shape — a non-mirror vault has no entry here) or ``"valid"``
    when it is a regular, non-symlink file (a genuine marker). ANY other
    entry — a directory, a symlink (including a broken one), a device / fifo /
    socket, or an unexpected ``OSError`` while stat-ing — is AMBIGUOUS and
    therefore fail-closed: it raises :class:`MirrorMarkerError` rather than
    being collapsed to a boolean, so a planted / abnormal entry can never make
    a mirror read silently fall through to GitPython (the fail-OPEN gap this
    closes).

    ``os.lstat`` (never ``os.stat``) so a symlink is detected, not followed —
    a symlink planted here must be a finding, not silently resolved.
    """
    try:
        st = os.lstat(marker)
    except FileNotFoundError:
        return "absent"
    except OSError as e:
        raise MirrorMarkerError(
            f"external-git mirror marker {marker.name!r} is unreadable "
            f"({e.__class__.__name__})"
        ) from e
    mode = st.st_mode
    if stat.S_ISLNK(mode):
        raise MirrorMarkerError(
            f"external-git mirror marker {marker.name!r} is a symlink"
        )
    if stat.S_ISDIR(mode):
        raise MirrorMarkerError(
            f"external-git mirror marker {marker.name!r} is a directory"
        )
    if not stat.S_ISREG(mode):
        raise MirrorMarkerError(
            f"external-git mirror marker {marker.name!r} is not a regular file"
        )
    return "valid"


# Per-vault serialization for worktree writes. asyncio.to_thread dispatches
# to a shared ThreadPoolExecutor, so two concurrent commits on the same
# vault can land on different worker threads — threading.Lock (not
# asyncio.Lock) is the right primitive here.
_VAULT_LOCKS_GUARD = threading.Lock()
_VAULT_LOCKS: dict[str, threading.Lock] = {}


def _vault_lock(vault_name: str) -> threading.Lock:
    with _VAULT_LOCKS_GUARD:
        lock = _VAULT_LOCKS.get(vault_name)
        if lock is None:
            lock = threading.Lock()
            _VAULT_LOCKS[vault_name] = lock
        return lock


def _write_kt() -> dict:
    """`kill_after_timeout` kwarg for write-path git commands.

    A wedged git process (disk stall, huge `reset --hard` stat sweep)
    otherwise pins the vault lock + a commit-executor thread + the caller's
    pool connection until PG's 60s idle-in-transaction reaper fires. Killing
    the command fails just that one request (transaction rolls back; the
    next write's `reset --hard` reconciles the worktree) and frees the lane.
    """
    return {"kill_after_timeout": settings.git_write_timeout_secs}


class ExternalGitOversizedError(AKBError):
    """A mirror blob exceeds ``external_git_blob_max_bytes`` and must NOT be
    materialized on a READ path.

    The reconciler SKIPS/tombstones an oversized blob before it is ever read,
    so it never reaches a read here; this is the READ-side backstop. A direct
    ``cat_blob`` / historical ``read_file`` / root-commit ``file_diff`` on an
    over-cap blob is REFUSED before its bytes are read into memory, so an
    oversized upstream file cannot blow up memory through an on-demand read
    (the reconcile gate is only an ingestion gate). Unlike the reconcile skip
    (silent, internal), a read refusal is surfaced to the caller as a clean
    413. The message is value-less (byte counts only) — safe to log."""

    def __init__(self, size: int, max_bytes: int):
        super().__init__(
            f"external-git mirror blob is {size} bytes, over the "
            f"{max_bytes}-byte cap; refusing to materialize it on a read",
            status_code=413,
            code="external_git_blob_oversized",
        )


class FixedRefHistoryError(RuntimeError):
    """A manual-vault fixed-ref history read could not be completed."""


class GitHistoryBoundError(AKBError, RuntimeError):
    """A bounded manual-vault history lookup could not finish safely.

    Unlike an unknown commit/path (which keeps the historical ``None`` result),
    hitting a resource bound is explicit so callers cannot mistake a truncated
    walk for a missing historical file.  The class is also a ``RuntimeError``
    for legacy service callers that already classify Git history failures that
    way, while ``AKBError`` gives public reads the normal error envelope.
    """

    def __init__(self, bound: str, limit: int | float):
        if bound == "timeout":
            message = f"git path history timed out after {limit:g}s"
            status_code = 503
            code = GIT_HISTORY_TIMEOUT
        elif bound == "output":
            message = f"git path history exceeded the {limit}-byte output cap"
            status_code = 413
            code = GIT_HISTORY_OUTPUT_CAPPED
        else:
            message = f"git path history exceeded the {limit}-entry cap"
            status_code = 503
            code = GIT_HISTORY_ENTRY_CAPPED
        super().__init__(message, status_code=status_code, code=code)
        self.bound = bound
        self.limit = limit


class GitHistoryCommandError(AKBError, RuntimeError):
    """A streamed manual-vault history command exited unsuccessfully."""

    def __init__(self) -> None:
        super().__init__(
            "git path history command failed",
            status_code=503,
            code=GIT_HISTORY_FAILED,
        )


class GitService:
    def __init__(
        self,
        storage_path: str | None = None,
        *,
        ext_runner: ExternalGitRunner | None = None,
    ):
        self.storage_path = Path(storage_path or settings.git_storage_path)
        self.storage_path.mkdir(parents=True, exist_ok=True)
        self.worktrees_path = self.storage_path / "_worktrees"
        self.worktrees_path.mkdir(parents=True, exist_ok=True)
        self.write_locks_path = self.storage_path / ".akb-write-locks"
        self.write_locks_path.mkdir(parents=True, exist_ok=True)
        self.creation_locks_path = self.storage_path / ".akb-create-locks"
        self.creation_locks_path.mkdir(parents=True, exist_ok=True)
        # The sole hermetic execution boundary for external-mirror git commands.
        # Injectable so tests can supply a policy/resolver that
        # accepts a local fixture host.
        self._ext_runner = ext_runner or ExternalGitRunner()

    def _bare_path(self, vault_name: str) -> Path:
        # Path-safety backstop on EVERY entry that resolves a bare path from a
        # DB-derived name: the cheap name guard here is the
        # single choke-point that every marker/clone/fetch/read path shares, so a
        # traversal / absolute / separator-bearing name can never interpolate OUT
        # of the storage root — not only at cleanup. Legitimate (incl. underscored
        # legacy/test) names pass unchanged; the deeper resolve-under-root check
        # (``_contained``) is applied at the create/write/rename points.
        self._require_safe_vault_name(vault_name)
        return self.storage_path / f"{vault_name}.git"

    def _worktree_path(self, vault_name: str) -> Path:
        # Same universal name guard as ``_bare_path``.
        self._require_safe_vault_name(vault_name)
        return self.worktrees_path / vault_name

    @contextmanager
    def _vault_write_lock(self, vault_name: str):
        """Serialize one shared worktree across threads and processes.

        Ref CAS cannot protect files or the shared index between ``reset`` and
        ``write-tree``. The lock file lives on the same mounted storage as the
        worktree, making that full interval mutually exclusive for every AKB
        process with write access to the volume.
        """
        self._require_safe_vault_name(vault_name)
        lock_path = self.write_locks_path / f"{vault_name}.lock"
        flags = os.O_CREAT | os.O_RDWR | getattr(os, "O_CLOEXEC", 0)
        flags |= getattr(os, "O_NOFOLLOW", 0)
        with _vault_lock(vault_name):
            fd = os.open(lock_path, flags, 0o600)
            try:
                fcntl.flock(fd, fcntl.LOCK_EX)
                yield
            finally:
                try:
                    fcntl.flock(fd, fcntl.LOCK_UN)
                finally:
                    os.close(fd)

    def acquire_vault_creation_lock(self, vault_name: str) -> int:
        """Acquire the cross-process lock spanning create *and rollback*."""
        self._require_safe_vault_name(vault_name)
        lock_path = self.creation_locks_path / f"{vault_name}.lock"
        flags = os.O_CREAT | os.O_RDWR | getattr(os, "O_CLOEXEC", 0)
        flags |= getattr(os, "O_NOFOLLOW", 0)
        fd = os.open(lock_path, flags, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
        except BaseException:
            os.close(fd)
            raise
        return fd

    @staticmethod
    def release_vault_creation_lock(fd: int) -> None:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)

    def _mirror_bare(self, vault_name: str) -> Path:
        """Resolve a mirror's bare path for a READ, fail-CLOSED on a symlinked
        bare (partial containment).

        A bare that is — or was swapped to be — a symlink could redirect a mirror
        read OUT of the storage root (a restored / tampered layout, or a
        post-startup swap of the bare for an external symlink). Reject it rather
        than read through it. ``os.lstat`` (never ``stat``) so the link is
        DETECTED, not followed. An ABSENT bare is returned unchanged — the
        caller's own ``exists()`` handles the not-yet-cloned / 404 case; only a
        PRESENT symlink is refused. This is the cheap read-side half of the
        containment work; the full openat2/O_NOFOLLOW binding of the
        clone-rename / fetch-write TOCTOU is a deferred follow-up."""
        bare = self._bare_path(vault_name)
        try:
            st = os.lstat(bare)
        except FileNotFoundError:
            return bare
        except OSError as e:
            raise MirrorMarkerError(
                f"external-git mirror bare for {vault_name!r} is unreadable "
                f"({e.__class__.__name__})"
            ) from e
        if stat.S_ISLNK(st.st_mode):
            raise MirrorMarkerError(
                f"external-git mirror bare for {vault_name!r} is a symlink; "
                "refusing to read through it"
            )
        return bare

    # ── Containment backstop ────────────────────────────
    @staticmethod
    def _require_safe_vault_name(vault_name: str) -> str:
        """Reject a vault name that could traverse OUT of the storage root when
        interpolated into ``_bare_path`` / ``_worktree_path``.

        This is a PATH-SAFETY guard for the DIRECT DB / restore / cleanup
        backstops — an entry that may not have passed the MCP create-time
        validator (``document_service.validate_vault_name``) but still drives a
        destructive filesystem op (rmtree / rename). It is deliberately MORE
        permissive than that create policy: the git layer has always accepted
        names the MCP grammar rejects (underscored test / legacy vaults), so this
        must refuse ONLY traversal shapes — an empty name, ``.`` / ``..``, an
        absolute / rooted name, an embedded path separator, or a NUL / control
        character — never tighten the naming grammar itself. ``_contained`` is the
        definitive backstop behind it (resolve-under-root)."""
        if (
            not vault_name
            or vault_name in (".", "..")
            or os.path.isabs(vault_name)
            or os.path.basename(vault_name) != vault_name
            or any(ord(ch) < 0x20 for ch in vault_name)
        ):
            raise ValueError(
                f"unsafe vault name for a storage-path operation: {vault_name!r}"
            )
        return vault_name

    def _contained(self, path: Path) -> Path:
        """Resolve ``path`` and confirm it stays INSIDE ``storage_path``
        (storage-root containment). Rejects a ``..`` segment, an absolute
        segment, and a symlink whose target redirects the resolved path
        out of the storage root. Returns the resolved path for the caller to act
        on; raises :class:`ValueError` otherwise. This is the defence-in-depth
        backstop behind :meth:`_require_safe_vault_name` — even if a name check is
        bypassed, a destructive op never lands outside the storage root."""
        root = self.storage_path.resolve()
        resolved = path.resolve()
        if resolved != root and root not in resolved.parents:
            raise ValueError(
                f"path {path} resolves outside the storage root {root}"
            )
        return resolved

    def _get_repo(self, vault_name: str) -> Repo:
        bare_path = self._bare_path(vault_name)
        if not bare_path.exists():
            raise FileNotFoundError(f"Vault repo not found: {vault_name}")
        return Repo(str(bare_path))

    def _is_mirror(self, vault_name: str) -> bool:
        """True iff this vault is an external-git mirror (a regular-file marker
        sits at the bare-repo root — see ``_MIRROR_MARKER``).

        Fail-CLOSED: the marker is resolved with ``os.lstat`` via
        :func:`_marker_state`, which RAISES :class:`MirrorMarkerError` for any
        ambiguous entry (a directory, a symlink incl. broken, another type, or
        an unexpected stat error) — it never collapses those to ``False``. A
        manual (non-mirror) vault has NO entry at the marker path, so it reads
        cleanly as ``False`` with no false positive. Mirror reads MUST route
        through the hermetic runner, never GitPython, so a planted
        promisor/rewrite config cannot re-open lazy-fetch on a plain public
        READ; returning ``False`` on an ambiguous entry would re-open exactly
        that (fail-open), hence the raise."""
        return _marker_state(self._bare_path(vault_name) / _MIRROR_MARKER) == "valid"

    def _use_mirror_reader(self, vault_name: str) -> bool:
        """Decide how a READ on ``vault_name`` must be served, fail-CLOSED.

        Returns True to route through the hermetic runner (an external-git
        mirror) or False for a plain GitPython read (a manual vault). It folds
        the whole kill-switch contract into one place so every
        read path shares it:

        * an ABNORMAL marker entry makes ``_is_mirror`` RAISE
          (:class:`MirrorMarkerError`) — never a silent GitPython fallback;
        * a genuine mirror while the feature is DISABLED
          (``external_git_enabled`` False) is REFUSED with a 503 rather than
          served by ANY path (runner or GitPython), so a kill-switched
          deployment performs zero mirror I/O;
        * a manual (non-mirror) vault is unaffected — it reads via GitPython
          whether the feature is on or off (no regression)."""
        is_mirror = self._is_mirror(vault_name)
        if is_mirror and not settings.external_git_enabled:
            raise AKBError(
                "external-git mirror reads are disabled",
                status_code=503,
                code="external_git_disabled",
            )
        return is_mirror

    def mark_as_mirror(self, vault_name: str) -> bool:
        """Backfill the ``_MIRROR_MARKER`` onto an existing bare repo the DB
        already knows to be an external-git mirror but that predates the marker.

        ``clone_mirror`` only writes the marker on a *fresh* clone, so a mirror
        created before the marker existed carries none — leaving ``_is_mirror``
        False and letting its reads fall through to GitPython (the fail-open
        gap this closes). This re-establishes the on-disk signal from the
        authoritative DB record (``vault_external_git``); see
        ``external_git_service.backfill_mirror_markers``.

        Returns True when it wrote the marker, False when nothing needed doing
        (the bare repo is absent, or a VALID regular-file marker already exists
        — idempotent). Fail-CLOSED: an ABNORMAL pre-existing entry
        at the marker path (a directory, a symlink, or any non-regular file)
        raises :class:`MirrorMarkerError` rather than being skipped — a skipped
        abnormal entry is precisely the fail-open the caller's backfill
        fail-fast must catch (`_stamp_mirror_markers` collects it).

        No-clobber and TOCTOU-free: the marker is created with
        ``os.open(O_CREAT | O_EXCL | O_WRONLY | O_NOFOLLOW)`` — an entry that
        appears in the race window makes the create fail (``FileExistsError``)
        and is re-classified rather than overwritten, and ``O_NOFOLLOW`` refuses
        to write through a symlink planted at the marker path. Holds the
        per-vault lock so it serializes with any concurrent clone/fetch/cleanup.
        The bare dir itself is lstat-checked (a symlinked bare could redirect
        the write out of the storage root). The marker lives at the bare root, a
        location upstream repo content can never populate, so it stays
        unforgeable.
        """
        bare = self._bare_path(vault_name)
        with self._vault_write_lock(vault_name):
            # Absent bare → nothing to do; a bare that exists but is NOT a real
            # directory (a symlink, or a plain file) is fail-closed — a
            # symlinked bare could redirect the marker write out of storage.
            try:
                bare_st = os.lstat(bare)
            except FileNotFoundError:
                return False
            if stat.S_ISLNK(bare_st.st_mode) or not stat.S_ISDIR(bare_st.st_mode):
                raise MirrorMarkerError(
                    f"external-git mirror bare path for {vault_name!r} is not a "
                    "real directory"
                )
            # Containment backstop: confirm the bare resolves
            # INSIDE the storage root before we write the marker into it. The name
            # guard in ``_bare_path`` already blocks traversal via the NAME; this
            # resolve-under-root catches a parent-dir symlink that would redirect
            # the marker write out of storage on a restored/tampered layout.
            self._contained(bare)
            marker = bare / _MIRROR_MARKER
            if _marker_state(marker) == "valid":
                return False  # already a valid marker — idempotent no-op
            # marker is "absent": create atomically, no-clobber, no symlink-follow.
            try:
                fd = os.open(
                    marker,
                    os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW,
                    0o644,
                )
            except FileExistsError as e:
                # An entry appeared between the lstat and here. Re-classify: a
                # VALID regular-file marker is an idempotent no-op; anything else
                # is abnormal → fail-closed (never clobbered). _marker_state
                # itself raises for the abnormal shapes.
                if _marker_state(marker) == "valid":
                    return False
                raise MirrorMarkerError(
                    f"external-git mirror marker for {vault_name!r} raced an "
                    "abnormal entry"
                ) from e
            try:
                os.write(fd, b"akb external-git mirror\n")
            finally:
                os.close(fd)
            logger.info("Backfilled external-git mirror marker for vault %s", vault_name)
            return True

    @staticmethod
    def _git_author_env(author_name: str, author_email: str) -> dict[str, str]:
        return {
            "GIT_AUTHOR_NAME": author_name,
            "GIT_AUTHOR_EMAIL": author_email,
            "GIT_COMMITTER_NAME": author_name,
            "GIT_COMMITTER_EMAIL": author_email,
        }

    def _stage_and_commit(
        self,
        work_repo: Repo,
        message: str,
        author_name: str,
        author_email: str,
        *,
        parent_required: bool,
    ) -> str:
        """Commit the already-staged index without GitPython IndexFile ops.

        GitPython's IndexFile add/remove/commit path mutates process cwd.
        The `repo.git.*` command interface launches git with an explicit
        working directory instead, so writes remain safe across thread-pool
        workers and vaults.
        """
        tree_sha = work_repo.git.write_tree(**_write_kt())
        parent_args: list[str] = []
        expected_old = "0" * 40
        if parent_required:
            expected_old = work_repo.git.rev_parse(
                "--verify",
                "HEAD",
                **_write_kt(),
            ).strip()
            parent_tree = work_repo.git.rev_parse(
                f"{expected_old}^{{tree}}",
                **_write_kt(),
            ).strip()
            if tree_sha.strip() == parent_tree:
                return expected_old
            parent_args = ["-p", expected_old]

        with work_repo.git.custom_environment(**self._git_author_env(author_name, author_email)):
            commit_sha = work_repo.git.commit_tree(
                "--no-gpg-sign",
                tree_sha,
                *parent_args,
                "-m",
                message,
                **_write_kt(),
            ).strip()
        # Compare-and-swap the branch tip. A concurrent writer that advanced
        # HEAD after the parent snapshot makes this command fail loudly.
        work_repo.git.update_ref("HEAD", commit_sha, expected_old, **_write_kt())
        return commit_sha

    def _ensure_worktree(self, vault_name: str) -> Path | None:
        """Create a persistent worktree for this vault if one doesn't exist.
        Returns the worktree path, or None if the bare repo is empty (no
        HEAD yet — worktree add needs an existing branch).

        Callers must hold the vault lock.
        """
        bare = self._bare_path(vault_name)
        wt = self._worktree_path(vault_name)
        if (wt / ".git").exists():
            return wt
        with _managed_repo(Repo(str(bare))) as bare_repo:
            try:
                # Touch HEAD to see if there's at least one commit.
                _ = bare_repo.head.commit
                branch_name = bare_repo.head.ref.name
            except (ValueError, TypeError, GitError):
                return None  # empty repo; caller falls back to the clone path
            wt.parent.mkdir(parents=True, exist_ok=True)
            try:
                bare_repo.git.worktree("add", str(wt), branch_name, **_write_kt())
            except GitError as e:
                # A previous `worktree add` killed mid-write (SIGKILL, OOM,
                # container restart) can leave the bare's
                # `.git/worktrees/<name>/` metadata half-written. The next
                # call fails with "<name> is already registered" even though
                # the on-disk worktree dir is gone. `git worktree prune`
                # reaps those stale registrations; retry once after pruning.
                msg = str(e)
                if "already registered" not in msg:
                    raise
                logger.warning(
                    "worktree add for vault %s tripped stale registration; pruning and retrying: %s",
                    vault_name,
                    msg,
                )
                bare_repo.git.worktree("prune", **_write_kt())
                bare_repo.git.worktree("add", str(wt), branch_name, **_write_kt())
            logger.info(
                "Worktree created for vault %s at %s (branch=%s)",
                vault_name,
                wt,
                branch_name,
            )
            return wt

    # ── Vault lifecycle ──────────────────────────────────────

    def init_vault(self, vault_name: str) -> str:
        """Initialize a new bare repo for a vault. Returns the repo path."""
        with self._vault_write_lock(vault_name):
            bare_path = self._bare_path(vault_name)
            if bare_path.exists():
                raise FileExistsError(f"Vault already exists: {vault_name}")
            with _managed_repo(Repo.init(str(bare_path), bare=True)):
                pass
            return str(bare_path)

    def vault_exists(self, vault_name: str) -> bool:
        return self._bare_path(vault_name).exists()

    def is_healthy_repo(self, vault_name: str) -> bool:
        """Cheap structural soundness check on the bare repo: does it exist
        as a git repo whose HEAD resolves to a real commit AND whose root
        tree object is present?

        Catches the gross corruption a partial clone, partial fetch, disk
        error, or non-git leftover dir produces (no valid HEAD, missing
        root commit OR root tree object). Fine-grained missing-blob corruption
        is caught downstream by the reconciler's per-file error handling. Used to
        decide self-heal-by-reclone vs. a normal fetch — and it never
        false-positives a transient network fetch failure as corruption,
        because it only inspects local on-disk state.

        Resolved through :meth:`_mirror_bare` (not the unchecked ``_bare_path``)
        so a symlinked bare fails CLOSED with :class:`MirrorMarkerError` BEFORE
        the ``rev-parse`` reads git/config through it (every mirror read
        — poll-path and on-demand — shares the one symlink-checking resolver).
        The raise happens outside the ``try`` below, so it propagates rather than
        being swallowed into a benign "not sound" False.
        """
        bare = self._mirror_bare(vault_name)
        if not bare.exists():
            return False
        try:
            with self._vault_write_lock(vault_name):
                # Through the hermetic runner so GIT_NO_REPLACE_OBJECTS / sealed
                # env apply on the mirror path — never a raw
                # GitPython Repo on an external mirror. HEAD must resolve to a real
                # commit AND its ROOT TREE object must be present: a commit whose
                # tree is lost (partial fetch / disk error) passes the commit check
                # yet fails every blob read, so the self-heal re-clone would never
                # converge. The ``^{tree}`` peel forces the root tree to be read,
                # so its loss surfaces as unhealthy → re-clone.
                self._ext_runner.rev_parse(str(bare), "HEAD^{commit}")
                self._ext_runner.rev_parse(str(bare), "HEAD^{tree}")
            return True
        except Exception:  # noqa: BLE001 — any failure means "not sound"
            return False

    def cleanup_stale_locks(self, max_age_seconds: float = 60.0) -> int:
        """Remove `index.lock` files for every vault that are older than
        `max_age_seconds`.

        A crashed git process (OOM, SIGKILL, container restart mid-commit)
        leaves the index.lock behind; subsequent writes to that worktree
        fail with "Unable to create '.../index.lock': File exists" until
        the lock is cleared by hand. Running this at startup recovers
        every affected vault before any worker can run into the same wall.

        Vault enumeration source: every `<storage>/<name>.git` bare repo
        in `storage_path`. Iterating bare repos (rather than the linked
        worktree dir) means we still find locks for vaults whose
        `_worktrees/<name>` directory was wiped or never created — the
        admin path inside the bare can hold an `index.lock` independently.

        Lock locations checked per vault:
          1. `<bare>/worktrees/<name>/index.lock` — where git keeps the
             index for linked worktrees (the path the AKB write paths
             actually touch).
          2. `<worktree>/.git/index.lock` — fallback for non-linked
             setups (initial clone path) where `.git` is a real dir.

        Safe under concurrency: the only write paths that touch a
        worktree's index hold `_vault_lock(vault_name)` per-vault, so
        startup self-heal — which runs before workers — cannot remove
        a lock held by a live operation. The age threshold provides
        defense in depth in the unlikely case startup overlaps with an
        in-flight commit (lock would be < 1s old, well under 60s).

        Returns the number of locks removed.
        """
        cleared = 0
        if not self.storage_path.exists():
            return cleared
        for bare in self.storage_path.iterdir():
            # lstat (never is_dir(), which FOLLOWS a symlink): a symlinked
            # `<name>.git` planted in the storage root could point OUT of it, so
            # enumerating / unlinking "index.lock" under it would act outside the
            # storage root (partial containment). Only a REAL
            # directory is a vault bare; a symlink (or any non-dir) is skipped,
            # never followed.
            try:
                bare_st = os.lstat(bare)
            except OSError:
                continue
            if (
                stat.S_ISLNK(bare_st.st_mode)
                or not stat.S_ISDIR(bare_st.st_mode)
                or not bare.name.endswith(".git")
            ):
                continue
            vault_name = bare.name[: -len(".git")]
            if not vault_name:
                continue
            candidates = [
                bare / "worktrees" / vault_name / "index.lock",
                self._worktree_path(vault_name) / ".git" / "index.lock",
            ]
            for lock in candidates:
                # `.git` in a linked worktree is a file (gitdir pointer),
                # not a dir — its `index.lock` path is meaningless. Skip
                # quickly if the parent isn't a directory.
                if not lock.parent.is_dir():
                    continue
                # lstat (never stat): classify the lock WITHOUT following a
                # symlink (symlink-safe cleanup). A symlinked or
                # directory `index.lock` is not something git writes — never read
                # a symlink TARGET's mtime for the age gate (an age-check bypass)
                # nor act through it; skip it. A regular file's own mtime governs.
                try:
                    st = os.lstat(lock)
                except FileNotFoundError:
                    continue
                except OSError:
                    continue
                if stat.S_ISLNK(st.st_mode) or stat.S_ISDIR(st.st_mode):
                    continue
                age = time.time() - st.st_mtime
                if age < max_age_seconds:
                    continue
                try:
                    lock.unlink()
                except OSError as e:
                    logger.warning("failed to clear stale lock %s: %s", lock, e)
                    continue
                logger.warning(
                    "removed stale git index.lock (age=%.0fs) at %s",
                    age, lock,
                )
                cleared += 1
        return cleared

    def cleanup_vault_dirs(self, vault_name: str) -> None:
        """Idempotently remove every on-disk artefact a vault owns.

        Removes both the bare repo (`<storage>/{name}.git`) and the
        persistent linked worktree (`<storage>/_worktrees/{name}`).
        Safe to call when neither exists. Used by:

          - delete_vault — final on-disk cleanup after DB cascade.
          - create_vault rollback — undoes a half-finished init when
            the request fails between init_vault and the DB INSERT
            (without this, the bare directory persists and every
            subsequent create_vault for the same name trips
            init_vault's FileExistsError, requiring manual rm -rf).

        Errors during cleanup propagate — callers handle via their
        own try/except so a rollback failure doesn't hide the
        original exception.

        Held under `_vault_write_lock(vault_name)` so teardown serializes with
        any in-flight clone/fetch/commit on the same vault across processes. This is the
        only git-touching op that mutates the on-disk repo outside the
        lock, so without it a `delete_vault` rmtree can race a poller
        `clone_mirror` writing the same bare dir — leaving a partial /
        corrupt repo that a same-named recreate then adopts
        (`vault_exists()` is True → bootstrap clone skipped → fetch into a
        broken repo, failing every retry). No caller holds the lock when
        invoking this (delete_vault, create-vault rollback, rename
        rollback), so re-entry is safe.

        Containment backstop: this is a DIRECT DB / restore / admin
        entry to a destructive rmtree, so the vault name is re-validated for
        path-safety and each artefact is confirmed to resolve INSIDE the storage
        root before removal. Removal is symlink-safe — a symlink at the bare /
        worktree path is never rmtree'd THROUGH (which would delete its target,
        possibly outside the storage root); the link itself is removed instead.
        """
        self._require_safe_vault_name(vault_name)
        with self._vault_write_lock(vault_name):
            for path in (self._bare_path(vault_name), self._worktree_path(vault_name)):
                # lstat (never stat): classify the artefact WITHOUT following a
                # symlink at the final component.
                try:
                    st = os.lstat(path)
                except FileNotFoundError:
                    continue
                if stat.S_ISLNK(st.st_mode):
                    logger.warning(
                        "vault path %s is a symlink; removing the link, not its target",
                        path,
                    )
                    path.unlink()
                    continue
                # Only act on a real target that resolves inside the storage root.
                self._contained(path)
                if stat.S_ISDIR(st.st_mode):
                    shutil.rmtree(path)
                else:
                    # A stray non-dir artefact at the vault path — remove it too so
                    # cleanup stays idempotent (a same-named recreate is unblocked).
                    path.unlink()

    # ── External remote operations (all via ExternalGitRunner) ───────

    def clone_mirror(
        self,
        vault_name: str,
        remote_url: str,
        branch: str,
        auth_token: str | None = None,
        timeout: int | None = None,
    ) -> str:
        """Clone an external repo as the vault's bare repo via the hermetic
        runner and return the LOCAL materialized SHA of `branch`.

        The token is passed only as an `Authorization` header (never in the
        URL or the resulting `.git/config`), and the transport / DNS / scheme
        are locked down by the runner. The clone lands in a unique temp dir
        first and is renamed onto the final bare path atomically on success;
        any partial dir is removed on failure/timeout WITHOUT re-acquiring the
        (non-reentrant) vault lock.
        """
        bare_path = self._bare_path(vault_name)
        if bare_path.exists():
            raise FileExistsError(f"Vault already exists: {vault_name}")
        with self._vault_write_lock(vault_name):
            # Age-qualified sweep INSIDE the lock: only reap leftover
            # temp clone dirs older than the clone timeout, so a concurrent
            # same-vault clone's ACTIVE temp is never deleted. Same-vault clones
            # serialize on this lock; the per-vault prefix isolates other vaults.
            self._sweep_clone_tmp(vault_name)
            tmp = self.storage_path / f".extgit-clone-{vault_name}-{uuid.uuid4().hex}"
            try:
                sha = self._ext_runner.clone_bare(
                    remote_url, branch, auth_token, tmp, timeout=timeout
                )
                # Mark the fresh bare as a mirror BEFORE the atomic rename so the
                # marker appears together with the repo.
                (Path(tmp) / _MIRROR_MARKER).write_text(
                    "akb external-git mirror\n", encoding="utf-8"
                )
                # Containment backstop: confirm the rename
                # TARGET resolves inside the storage root before we materialize a
                # bare there. The name guard in ``_bare_path`` blocks a traversal
                # NAME; this catches a parent-dir symlink that would land the clone
                # outside storage. ``bare_path`` doesn't exist yet — ``_contained``
                # resolves its existing parents lexically, which is what we want.
                self._contained(bare_path)
                # Atomic within storage_path (same filesystem). bare_path was
                # asserted absent above / removed by a sterile re-clone caller.
                os.rename(tmp, bare_path)
            except BaseException:
                # Inline cleanup — MUST NOT call cleanup_vault_dirs() here, it
                # re-acquires _vault_lock (non-reentrant → deadlock).
                self._rmtree_quiet(tmp)
                raise
        logger.info("Mirror cloned: vault=%s branch=%s", vault_name, branch)
        return sha

    def fetch_remote(
        self,
        vault_name: str,
        remote_url: str,
        branch: str,
        auth_token: str | None = None,
        timeout: int | None = None,
    ) -> str:
        """Fetch `branch` from the remote into the bare repo and return the
        LOCAL materialized SHA of `refs/heads/<branch>` (force — mirrors track
        upstream literally).

        Lock discipline unchanged: the network fetch runs **outside** the
        per-vault lock (it can take minutes; objects land append-only in the
        shared object store), then a brief critical section promotes the tmp
        ref onto the branch ref and reads the materialized SHA.
        """
        bare_path = self._bare_path(vault_name)
        if not bare_path.exists():
            raise FileNotFoundError(f"Vault repo not found: {vault_name}")
        # Containment backstop: confirm the existing bare
        # resolves inside the storage root before a network fetch writes objects
        # into it — a bare that was replaced by a symlink out of root (restore /
        # tamper) is refused here, not fetched through.
        self._contained(bare_path)

        # `branch` is re-validated (with DNS) inside the runner; validate the
        # pure ref shape here too so the ref names we build below are safe.
        vbranch = validate_branch(branch)
        tmp_ref = f"refs/akb/fetch-tmp/{vbranch}"

        # Network I/O outside the lock, into a temporary ref.
        self._ext_runner.fetch_to_ref(
            bare_path, remote_url, branch, auth_token, tmp_ref, timeout=timeout
        )

        # Brief critical section: promote tmp ref → branch ref, read the sha.
        with self._vault_write_lock(vault_name):
            self._ext_runner.update_ref(bare_path, f"refs/heads/{vbranch}", tmp_ref)
            self._ext_runner.delete_ref_quiet(bare_path, tmp_ref)
            return self._ext_runner.rev_parse(bare_path, f"refs/heads/{vbranch}")

    def ls_remote_head(
        self,
        remote_url: str,
        branch: str,
        auth_token: str | None = None,
        timeout: int | None = None,
    ) -> str | None:
        """Return the SHA of the remote branch tip without fetching objects.

        A cheap change-detection HINT for the poller only — never used as a
        materialized cursor. Returns None if the branch is absent.
        The runner compares `refs/heads/<branch>` EXACTLY and requires a 40-hex
        OID before returning it.
        """
        return self._ext_runner.ls_remote_sha(
            remote_url, branch, auth_token, timeout=timeout
        )

    def materialized_sha(self, vault_name: str, branch: str) -> str:
        """Read the LOCAL `refs/heads/<branch>` SHA from the mirror bare repo.

        This is the authoritative materialized SHA the reconciler keys on:
        the ls-remote SHA is only a change hint, so tree / per-file
        attribution / cursor must all derive from what actually landed on disk.

        Through :meth:`_mirror_bare` so a symlinked bare fails CLOSED
        (:class:`MirrorMarkerError`) before the ``rev-parse`` reads through it
        (shared symlink-checking resolver).
        """
        bare_path = self._mirror_bare(vault_name)
        if not bare_path.exists():
            raise FileNotFoundError(f"Vault repo not found: {vault_name}")
        return self._ext_runner.rev_parse(
            bare_path, f"refs/heads/{validate_branch(branch)}"
        )

    def inspect_mirror_structure(
        self, vault_name: str, remote_url: str | None = None, branch: str | None = None
    ) -> list[str]:
        """Structural default-deny check on the mirror bare repo.

        Returns a list of value-less findings (empty = clean). Any finding makes
        the repo untrusted for a network fetch; the caller re-clones sterilely
        instead. ``branch`` lets the origin fetch refspec be validated exactly.

        Through :meth:`_mirror_bare` so a symlinked bare fails CLOSED
        (:class:`MirrorMarkerError`) before the inspector reads its config/refs
        through the link — an absent bare still returns ``[]`` (shared
        symlink-checking resolver).
        """
        bare_path = self._mirror_bare(vault_name)
        if not bare_path.exists():
            return []
        return self._ext_runner.inspect_structure(bare_path, remote_url, branch)

    def _sweep_clone_tmp(self, vault_name: str) -> None:
        """Reap temp clone dirs left by a hard-crashed prior clone of THIS vault
        (SIGKILL / OOM / container-restart; in-process failures are cleaned
        inline). Age-qualified so a concurrent clone's ACTIVE temp is never
        removed; callers hold the vault lock. Best-effort.
        """
        prefix = f".extgit-clone-{vault_name}-"
        try:
            entries = list(self.storage_path.iterdir())
        except OSError:
            return
        # Only sweep temps older than the clone timeout — anything younger could
        # be an in-flight clone (belt-and-suspenders atop the per-vault lock).
        cutoff = time.time() - max(60.0, float(settings.external_git_clone_timeout))
        for entry in entries:
            if not entry.name.startswith(prefix):
                continue
            try:
                if entry.stat().st_mtime > cutoff:
                    continue  # too young — could be an active clone
            except OSError:
                continue
            self._rmtree_quiet(entry)

    @staticmethod
    def _rmtree_quiet(path: Path) -> None:
        try:
            if path.is_dir():
                shutil.rmtree(path)
            elif path.exists():
                path.unlink()
        except OSError as e:
            logger.warning("failed to remove external-git temp path %s: %s", path, e)

    def ls_tree(self, vault_name: str, sha: str) -> dict[str, str]:
        """Return `{path: blob_sha}` for every blob reachable from `sha`.
        Used by the reconciler to compare upstream tree against local
        documents.external_blob without parsing diff status codes.

        Routed through the hermetic runner (`git ls-tree`), never a GitPython
        `commit.tree.traverse()` on a mirror — the sealed env +
        GIT_NO_REPLACE_OBJECTS + GIT_NO_LAZY_FETCH + GIT_LITERAL_PATHSPECS must
        apply. Resolved through :meth:`_mirror_bare` so a
        symlinked bare fails CLOSED (:class:`MirrorMarkerError`) before ls-tree
        reads through it (shared symlink-checking resolver).
        """
        bare_path = self._mirror_bare(vault_name)
        if not bare_path.exists():
            raise FileNotFoundError(f"Vault repo not found: {vault_name}")
        return self._ext_runner.ls_tree(bare_path, sha)

    def last_commit_for_path(
        self, vault_name: str, path: str, rev: str | None = None
    ) -> str | None:
        """Hex sha of the most recent commit that touched `path`. Used to
        stamp `documents.current_commit` per-file so mirror docs don't
        all share the reconcile-time HEAD sha. Returns None when the
        path has no commits.

        `rev` pins the walk to a specific tip (the materialized SHA), so
        attribution can't drift past the snapshot being written. Routed
        through the hermetic runner (`git log`), never GitPython
        `iter_commits` on a mirror. Resolved through :meth:`_mirror_bare` so a
        symlinked bare fails CLOSED (:class:`MirrorMarkerError`) before the log
        reads through it — an absent bare still returns None (shared
        symlink-checking resolver).
        """
        bare_path = self._mirror_bare(vault_name)
        if not bare_path.exists():
            return None
        return self._ext_runner.last_commit_for_path(bare_path, path, rev)

    def cat_blob(self, vault_name: str, blob_sha: str) -> bytes:
        """Read a blob's raw bytes from the object store by sha. Works
        regardless of whether the blob is currently reachable from HEAD.

        Routed through the hermetic runner (`git cat-file blob`), never
        GitPython on a mirror — so the sealed env + GIT_NO_REPLACE_OBJECTS
        apply and a replaced/grafted object can't substitute contents.

        Kill-switch choke-point: this is the lowest-level
        mirror-object read, funnelled through by the reconciler AND by
        metadata_worker (and any future indexer). Gating it here means a
        disabled deployment performs zero mirror I/O no matter which caller
        reaches it — the enabled-gated reconciler today, or a caller that
        forgets its own gate tomorrow. ``_use_mirror_reader`` raises a 503 for
        a disabled mirror and propagates :class:`MirrorMarkerError` for an
        abnormal marker (never a silent GitPython fallback); an enabled mirror
        or a manual (non-mirror) vault is unaffected.
        """
        self._use_mirror_reader(vault_name)
        bare_path = self._mirror_bare(vault_name)
        if not bare_path.exists():
            raise FileNotFoundError(f"Vault repo not found: {vault_name}")
        # Oversized gate: size the blob with the bounded
        # ``cat-file -s`` primitive and REFUSE it before materializing content, so
        # an over-cap blob never explodes memory on a read (the reconcile gate is
        # an ingestion gate only). Same cap choke-point as ``blob_exceeds_max``
        # (module ``settings``), so reconcile's own pre-check leaves the normal
        # path unaffected: a blob it already passed re-sizes IDENTICALLY here
        # (content-addressed, deterministic), so the guard never fires for it.
        # ``blob_sha`` is already an EXACT immutable OID (from ls_tree), so the
        # size check and the read bind to the same object — no size↔read TOCTOU;
        # the streamed ``max_output_bytes`` is a defence-in-depth backstop.
        cap = settings.external_git_blob_max_bytes
        size = self._ext_runner.blob_size(bare_path, blob_sha)
        if size > cap:
            raise ExternalGitOversizedError(size, cap)
        return self._ext_runner.cat_blob(
            bare_path, blob_sha, max_output_bytes=cap + _OUTPUT_MARGIN_BYTES
        )

    def blob_exceeds_max(self, vault_name: str, blob_sha: str) -> tuple[int, bool]:
        """``(size_bytes, oversized)`` for a mirror blob — its ``git cat-file -s``
        size (via the hermetic runner, WITHOUT reading the content) and whether
        that size exceeds ``settings.external_git_blob_max_bytes``.

        The reconciler's oversized-blob gate calls this BEFORE
        :meth:`cat_blob` so a blob over the cap is never materialized into memory:
        it uses ``oversized`` to skip / tombstone the path and ``size`` for the
        operational log line. Deciding the cap HERE (git_service already owns
        ``settings`` for storage config) keeps ``external_git_service`` free of a
        module-level ``settings`` dependency — the backfill-unconditional
        invariant (a kill-switched deployment must still stamp mirror markers).

        Shares :meth:`cat_blob`'s kill-switch choke-point: ``_use_mirror_reader``
        raises a 503 for a disabled mirror and propagates
        :class:`MirrorMarkerError` for an abnormal marker (never a silent
        fallback), so a disabled deployment performs zero mirror I/O here too."""
        self._use_mirror_reader(vault_name)
        bare_path = self._mirror_bare(vault_name)
        if not bare_path.exists():
            raise FileNotFoundError(f"Vault repo not found: {vault_name}")
        size = self._ext_runner.blob_size(bare_path, blob_sha)
        return size, size > settings.external_git_blob_max_bytes

    # ── Read operations ──────────────────────────────────────

    def read_file(self, vault_name: str, file_path: str, commit: str | None = None) -> str | None:
        """Read a file's content from the repo. Returns None if not found.

        Caller is expected to have validated ``commit`` against
        :func:`is_valid_commit_hash` before reaching here; we still catch
        BadName/BadObject defensively so an unexpected ref string surfaces
        as 404 rather than a 500.

        A mirror vault is read through the hermetic runner, never GitPython, so
        a planted promisor/rewrite config cannot trigger lazy-fetch on this
        public read.
        """
        if self._use_mirror_reader(vault_name):
            return self._read_file_mirror(vault_name, file_path, commit)
        with _managed_repo(self._get_repo(vault_name)) as repo:
            try:
                ref = repo.commit(commit) if commit else repo.head.commit
            except (ValueError, BadName, BadObject):
                # Empty repo, malformed hash, or hash unknown to this repo.
                return None
            try:
                blob = ref.tree / file_path
                return blob.data_stream.read().decode("utf-8")
            except (KeyError, TypeError):
                if commit is None:
                    return None
                historical_path = self.path_at_revision(vault_name, file_path, commit)
                if historical_path and historical_path != file_path:
                    try:
                        blob = ref.tree / historical_path
                        return blob.data_stream.read().decode("utf-8")
                    except (KeyError, TypeError):
                        pass
                return None

    @staticmethod
    def _parse_follow_path_log(output: str) -> list[dict]:
        """Parse ``git log --follow --name-status`` into revision/path pairs."""
        history: list[dict] = []
        active: dict | None = None

        def flush() -> None:
            if active is not None and active.get("path_at_revision"):
                history.append(active.copy())

        for line in str(output).splitlines():
            if "\x00" in line:
                flush()
                oid, epoch, *_ = line.split("\x00")
                if not re.fullmatch(r"[0-9a-f]{40}", oid):
                    active = None
                    continue
                try:
                    committed_at = datetime.fromtimestamp(int(epoch), tz=timezone.utc)
                except (TypeError, ValueError, OverflowError):
                    active = None
                    continue
                active = {
                    "legacy_git_oid": oid,
                    "committed_at": committed_at,
                }
                continue
            if active is None or not line:
                continue
            fields = line.split("\t")
            if len(fields) >= 2:
                active["path_at_revision"] = fields[-1]
        flush()
        return history

    def _follow_path_history(
        self,
        repo: Repo,
        fixed_ref: str,
        file_path: str,
        *,
        max_count: int | None = None,
        since_epoch: int | None = None,
    ) -> list[dict]:
        """Return newest-first rename-following revisions for ``file_path``."""
        log_args = ["log", "--follow", "-M", "--name-status"]
        if max_count is not None:
            log_args.append(f"--max-count={max_count}")
        if since_epoch is not None:
            log_args.append(f"--since=@{since_epoch}")
        log_args.extend(["--format=%H%x00%ct", fixed_ref, "--", file_path])
        return self._parse_follow_path_log(repo.git.log(*log_args[1:]))

    @staticmethod
    def _close_path_history_process(process, *, terminate: bool) -> None:
        """Close a streamed GitPython process, killing it when still active."""
        raw_process: Any = getattr(process, "proc", None) or process
        if terminate:
            try:
                if raw_process.poll() is None:
                    raw_process.kill()
            except (AttributeError, OSError):
                pass
            try:
                raw_process.wait(timeout=5)
            except (AttributeError, OSError, subprocess.TimeoutExpired):
                pass
        for stream_name in ("stdin", "stdout", "stderr"):
            stream = getattr(raw_process, stream_name, None)
            try:
                if stream is not None:
                    stream.close()
            except (OSError, ValueError):
                pass

    def _stream_path_at_revision(
        self,
        repo: Repo,
        fixed_ref: str,
        file_path: str,
        target: str,
    ) -> str | None:
        """Stream a rename-following log until ``target`` or a safe bound.

        ``GitPython`` normally buffers ``git log`` before returning.  The
        revision selector only needs the target commit's one status record, so
        this reader owns a process and consumes stdout incrementally.  It asks
        Git for one entry past the allowed prefix: that distinguishes an
        ordinary short history from a walk truncated by the entry cap without
        ever allowing an unbounded command or result.
        """
        max_entries = max(1, int(_PATH_AT_REVISION_MAX_ENTRIES))
        max_output_bytes = max(1, int(_PATH_AT_REVISION_MAX_OUTPUT_BYTES))
        try:
            timeout_secs = float(
                getattr(settings, "git_write_timeout_secs", _PATH_AT_REVISION_DEFAULT_TIMEOUT_SECS)
            )
        except (TypeError, ValueError):
            timeout_secs = _PATH_AT_REVISION_DEFAULT_TIMEOUT_SECS
        timeout_secs = max(0.001, timeout_secs)

        log_args = [
            "--follow",
            "-M",
            "--name-status",
            f"--max-count={max_entries + 1}",
            "--format=%H%x00%ct",
            fixed_ref,
            "--",
            file_path,
        ]
        try:
            process = repo.git.execute(
                [
                    repo.git.GIT_PYTHON_GIT_EXECUTABLE,
                    "-c",
                    "core.quotePath=false",
                    "log",
                    *log_args,
                ],
                as_process=True,
            )
        except (GitError, OSError) as exc:
            raise GitHistoryCommandError() from exc
        raw_process: Any = getattr(process, "proc", None) or process
        stdout = getattr(raw_process, "stdout", None)
        stderr = getattr(raw_process, "stderr", None)
        stdout_fd = stdout.fileno() if stdout is not None else -1
        selector = selectors.DefaultSelector()
        pending = bytearray()
        output_bytes = 0
        entry_count = 0
        active_oid: str | None = None
        finished = False
        deadline = time.monotonic() + timeout_secs

        def consume_line(line: bytes) -> str | None:
            nonlocal active_oid, entry_count
            text = line.decode("utf-8", "replace").rstrip("\r")
            if "\x00" in text:
                entry_count += 1
                if entry_count > max_entries:
                    raise GitHistoryBoundError("entry", max_entries)
                oid = text.split("\x00", 1)[0]
                active_oid = oid if re.fullmatch(r"[0-9a-f]{40}", oid) else None
                return None
            if active_oid is None or not text:
                return None
            fields = text.split("\t")
            if len(fields) >= 2 and active_oid == target:
                return fields[-1]
            return None

        try:
            for stream in (stdout, stderr):
                if stream is not None:
                    selector.register(stream, selectors.EVENT_READ)

            while selector.get_map():
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise GitHistoryBoundError("timeout", timeout_secs)
                events = selector.select(remaining)
                if not events:
                    continue
                for key, _mask in events:
                    try:
                        if key.fd == stdout_fd:
                            available = max_output_bytes - output_bytes - len(pending)
                            if available < 0:
                                raise GitHistoryBoundError("output", max_output_bytes)
                            read_size = min(
                                _PATH_AT_REVISION_READ_CHUNK_BYTES,
                                max(1, available + 1),
                            )
                        else:
                            read_size = _PATH_AT_REVISION_READ_CHUNK_BYTES
                        chunk = os.read(key.fd, read_size)
                    except (OSError, ValueError):
                        selector.unregister(key.fileobj)
                        continue
                    if not chunk:
                        selector.unregister(key.fileobj)
                        continue
                    if key.fd != stdout_fd:
                        continue

                    pending.extend(chunk)
                    while True:
                        newline = pending.find(b"\n")
                        if newline < 0:
                            break
                        line = bytes(pending[:newline])
                        del pending[: newline + 1]
                        output_bytes += newline + 1
                        if output_bytes > max_output_bytes:
                            raise GitHistoryBoundError("output", max_output_bytes)
                        path = consume_line(line)
                        if path is not None:
                            return path
                    if output_bytes + len(pending) > max_output_bytes:
                        raise GitHistoryBoundError("output", max_output_bytes)

            if pending:
                output_bytes += len(pending)
                if output_bytes > max_output_bytes:
                    raise GitHistoryBoundError("output", max_output_bytes)
                path = consume_line(bytes(pending))
                if path is not None:
                    return path

            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise GitHistoryBoundError("timeout", timeout_secs)
            try:
                status = raw_process.wait(timeout=remaining)
            except subprocess.TimeoutExpired:
                raise GitHistoryBoundError("timeout", timeout_secs) from None
            finished = True
            if status != 0:
                raise GitHistoryCommandError()
            return None
        finally:
            selector.close()
            self._close_path_history_process(process, terminate=not finished)

    def path_at_revision(
        self, vault_name: str, file_path: str, commit: str
    ) -> str | None:
        """Return the path carried by ``commit`` while following renames.

        ``file_path`` is the document's current path.  The walk starts at the
        current local tip, not at ``commit`` itself, so a pre-move selector can
        be mapped back to the old path.  Manual vaults use GitPython; mirror
        vaults remain on :class:`ExternalGitRunner`'s hermetic local boundary.
        """
        # External mirrors retain their existing fixed-path semantics. Their
        # separate source-corpus history contract is deliberately out of this
        # manual-vault logical revision change.
        if self._use_mirror_reader(vault_name):
            return None
        try:
            with _managed_repo(self._get_repo(vault_name)) as repo:
                target = repo.commit(commit).hexsha
                fixed_ref = repo.head.commit.hexsha
                return self._stream_path_at_revision(repo, fixed_ref, file_path, target)
        except (BadName, BadObject, FileNotFoundError, ValueError):
            return None

    def manual_fixed_ref_history(
        self,
        vault_name: str,
        fixed_ref: str,
        file_path: str,
        *,
        current_commit: str,
        since_epoch: int | None = None,
    ) -> dict:
        """Read one manual-vault document and its rename-following history.

        The caller supplies an exact, full commit OID for both the frozen tip
        and the document's recorded current commit.  This primitive never
        follows ``HEAD`` and deliberately refuses a mirror marker, so bridge
        code cannot accidentally use the external-git reader for a fixed-ref
        capture.  The returned body is the exact UTF-8 byte sequence at
        ``(current_commit, file_path)``; history entries use full commit OIDs
        and the path that commit carried after ``--follow`` rename tracking.
        """
        full_oid = re.compile(r"^[0-9a-f]{40}$")
        if not full_oid.fullmatch(fixed_ref):
            raise FixedRefHistoryError("fixed_ref must be a full lowercase 40-hex commit OID")
        if not full_oid.fullmatch(current_commit):
            raise FixedRefHistoryError(
                "current_commit must be a full lowercase 40-hex commit OID"
            )
        if self._is_mirror(vault_name):
            raise FixedRefHistoryError("fixed-ref history is limited to manual vaults")

        with _managed_repo(self._get_repo(vault_name)) as repo:
            return self._manual_fixed_ref_history_with_repo(
                repo,
                fixed_ref,
                file_path,
                current_commit=current_commit,
                since_epoch=since_epoch,
            )

    def _manual_fixed_ref_history_with_repo(
        self,
        repo: Repo,
        fixed_ref: str,
        file_path: str,
        *,
        current_commit: str,
        since_epoch: int | None,
    ) -> dict:
        """Implementation kept inside ``manual_fixed_ref_history``'s Repo scope."""
        try:
            repo.commit(fixed_ref)
            current = repo.commit(current_commit)
            repo.git.merge_base("--is-ancestor", current_commit, fixed_ref)
            blob = current.tree / file_path
            body = blob.data_stream.read()
        except (
            BadName,
            BadObject,
            FileNotFoundError,
            GitError,
            KeyError,
            TypeError,
            ValueError,
        ) as exc:
            raise FixedRefHistoryError(
                "fixed-ref history could not resolve the requested commit or body"
            ) from exc

        try:
            body.decode("utf-8", errors="strict")
        except UnicodeDecodeError as exc:
            raise FixedRefHistoryError("fixed-ref body is not valid UTF-8") from exc

        log_args = [
            "--follow",
            "-M",
            "--name-status",
        ]
        if since_epoch is not None:
            log_args.append(f"--since=@{since_epoch}")
        log_args.extend(["--format=%H%x00%ct", fixed_ref, "--", file_path])
        try:
            output = repo.git.log(*log_args)
        except (GitError, ValueError) as exc:
            raise FixedRefHistoryError("fixed-ref history could not be read") from exc

        activity = self._manual_fixed_ref_activity(repo, current, file_path)

        history: list[dict] = []
        active: dict | None = None

        def flush() -> None:
            if active is not None and active.get("path_at_revision"):
                history.append(active.copy())

        for line in str(output).splitlines():
            if "\x00" in line:
                flush()
                oid, epoch, *_ = line.split("\x00")
                active = {
                    "legacy_git_oid": oid,
                    "committed_at": datetime.fromtimestamp(
                        int(epoch), tz=timezone.utc
                    ),
                }
                continue
            if active is None or not line:
                continue
            fields = line.split("\t")
            status = fields[0]
            if status.startswith("R") and len(fields) >= 3:
                active["path_at_revision"] = fields[-1]
            elif len(fields) >= 2:
                active["path_at_revision"] = fields[-1]
        flush()

        return {
            "fixed_ref": fixed_ref,
            "current_commit": current_commit,
            "body": body,
            "history": history,
            "activity": activity,
        }

    def _manual_fixed_ref_activity(self, repo: Repo, commit, file_path: str) -> dict:
        """Freeze the legacy public activity projection for one file commit."""
        metadata = self._legacy_commit_metadata(commit)
        action = metadata["action"]
        if action not in {"create", "update", "move", "delete"}:
            raise FixedRefHistoryError(
                "fixed-ref current commit has no supported public activity action"
            )
        if not metadata["subject"] or not metadata["agent"]:
            raise FixedRefHistoryError(
                "fixed-ref current commit has incomplete activity identity"
            )

        try:
            output = repo.git.diff_tree(
                "--root", "-r", "--name-status", "-M", commit.hexsha,
            )
        except (GitError, ValueError) as exc:
            raise FixedRefHistoryError(
                "fixed-ref current commit activity could not be read"
            ) from exc

        changes: list[dict[str, str | None]] = []
        for line in str(output).splitlines():
            fields = line.split("\t")
            if not fields:
                continue
            status = fields[0]
            if status.startswith(("R", "C")) and len(fields) >= 3:
                path_from, path_to = fields[-2], fields[-1]
                if path_to == file_path:
                    changes.append(
                        {
                            "change": "move",
                            "path_from": path_from,
                            "path_to": path_to,
                        }
                    )
            elif len(fields) >= 2 and fields[-1] == file_path:
                change_kind = {
                    "A": "create",
                    "M": "update",
                    "D": "delete",
                }.get(status[:1])
                if change_kind is not None:
                    changes.append(
                        {
                            "change": change_kind,
                            "path_from": None,
                            "path_to": file_path,
                        }
                    )
        if len(changes) != 1 or changes[0]["change"] != action:
            raise FixedRefHistoryError(
                "fixed-ref current commit activity does not match the file action"
            )
        selected_change = changes[0]
        return {
            "legacy_git_oid": commit.hexsha,
            "committed_at": datetime.fromtimestamp(
                commit.committed_date, tz=timezone.utc
            ),
            "actor": metadata["agent"],
            "subject": metadata["subject"],
            "summary": metadata["summary"],
            "action": action,
            "path_from": selected_change["path_from"],
            "path_to": selected_change["path_to"],
            "changed_paths": changes,
        }

    # ── Mirror read variants (hermetic runner) ───────────
    def _read_file_mirror(
        self, vault_name: str, file_path: str, commit: str | None
    ) -> str | None:
        bare = self._mirror_bare(vault_name)
        if not bare.exists():
            return None
        rev = "HEAD"
        if commit:
            c = commit.strip().lower()
            # Resolve the caller's commit (short OR full 40-hex) to a full OID
            # hermetically. A genuinely unknown / malformed / corrupt commit is a
            # not-found → 404 (None) here, NOT the 502 that ``resolve_blob_oid``
            # now raises for an UNRESOLVABLE rev (MAJOR fail-closed, fix-4): a full
            # 40-hex that is absent used to bypass this resolve and reach
            # ``resolve_blob_oid`` directly, so the fail-closed contract would turn
            # a plain not-found into a 502. A present OID resolves to itself, so
            # the exact-OID binding below is unchanged; ``^{commit}`` +
            # ``--end-of-options`` keep the ref from being option/pathspec-parsed.
            try:
                rev = self._ext_runner.rev_parse(bare, f"{c}^{{commit}}")
            except Exception:  # noqa: BLE001 — unknown / malformed / corrupt commit
                return None
        # Exact-OID binding: resolve ``<rev>:<path>`` to
        # the immutable blob OID ONCE, then size AND read THAT oid. Sizing and
        # reading the same content-addressed object closes the "``HEAD`` promoted
        # by a concurrent fetch between the size check and the read" TOCTOU — a
        # small sized blob can no longer be swapped for a large materialized one.
        # A missing rev/path resolves to None → 404 (None), unchanged; an over-cap
        # blob is REFUSED before materializing, surfacing the same clean refusal
        # as ``cat_blob``; the streamed ``max_output_bytes`` is a backstop.
        oid = self._ext_runner.resolve_blob_oid(bare, rev, file_path)
        if oid is None:
            return None
        cap = settings.external_git_blob_max_bytes
        size = self._ext_runner.blob_size(bare, oid)
        if size > cap:
            raise ExternalGitOversizedError(size, cap)
        raw = self._ext_runner.cat_blob(
            bare, oid, max_output_bytes=cap + _OUTPUT_MARGIN_BYTES
        )
        try:
            return raw.decode("utf-8")
        except UnicodeDecodeError:
            return None

    def _file_log_mirror(
        self, vault_name: str, file_path: str, max_count: int, since_epoch: int | None
    ) -> list[dict]:
        bare = self._mirror_bare(vault_name)
        if not bare.exists():
            return []
        out: list[dict] = []
        for entry in self._ext_runner.log_for_path(bare, file_path, max_count):
            if since_epoch is not None and entry["committed_epoch"] < since_epoch:
                continue
            out.append(
                {
                    "hash": entry["hash"][:12],
                    "message": entry["message"].strip(),
                    "author": entry["author"],
                    "date": datetime.fromtimestamp(
                        entry["committed_epoch"], tz=timezone.utc
                    ).isoformat(),
                }
            )
        return out

    def _vault_log_mirror(
        self, vault_name: str, max_count: int, since: str | None, path: str | None
    ) -> list[dict]:
        bare = self._mirror_bare(vault_name)
        if not bare.exists():
            return []
        results: list[dict] = []
        for e in self._ext_runner.vault_log_entries(
            bare, max_count=max_count, since=since, path=path
        ):
            meta: dict[str, str] = {}
            for bl in (line.strip() for line in e["body"].split("\n")):
                if bl and ":" in bl:
                    k, val = bl.split(":", 1)
                    meta[k.strip().lower()] = val.strip()
            results.append(
                {
                    "hash": e["hash"][:12],
                    "subject": e["subject"],
                    "author": e["author"],
                    "date": datetime.fromtimestamp(
                        e["committed_epoch"], tz=timezone.utc
                    ).isoformat(),
                    "action": meta.get("action", ""),
                    "summary": meta.get("summary", ""),
                    "agent": meta.get("agent", e["author"]),
                    "files": e["files"],
                }
            )
        return results

    def _file_diff_mirror(
        self, vault_name: str, file_path: str, commit_hash: str
    ) -> dict:
        base = {"file": file_path, "commit": commit_hash}
        bare = self._mirror_bare(vault_name)
        if not bare.exists():
            return {**base, "type": "unknown", "diff": "", "error": "commit not found"}
        cap = settings.external_git_blob_max_bytes
        # Resolve the (possibly abbreviated) commit to ONE full OID up front. An
        # unknown/malformed commit yields the SAME "commit not found" result the
        # renderer would return — decided here so the size checks AND the read all
        # bind to this single resolved OID (exact-OID binding).
        try:
            full = self._ext_runner.rev_parse(bare, f"{commit_hash}^{{commit}}")
        except Exception:  # noqa: BLE001 — unknown/malformed commit
            full = None
        if full is None:
            return {**base, "type": "unknown", "diff": "", "error": "commit not found"}
        # Oversized gate: a diff materializes file
        # content — a root-commit / addition renders the FULL post-image via
        # ``cat_path``, and a ``diff-tree -p`` buffers BOTH the post-image AND the
        # PRE-image (so a commit that shrinks or DELETES a large file, whose
        # post-image is small/absent, would still buffer the large pre-image).
        # Size BOTH images against the RESOLVED full OID and refuse if EITHER is
        # over the cap, before any patch is materialized. ``path_size`` now
        # PROPAGATES a real sizing failure instead of masking it as "absent",
        # so a corrupt object surfaces here rather than silently
        # skipping the oversized gate; a genuinely absent path still sizes as None.
        post = self._ext_runner.path_size(bare, full, file_path)
        if post is not None and post > cap:
            raise ExternalGitOversizedError(post, cap)
        parent = self._ext_runner.first_parent_oid(bare, full)
        if parent is not None:
            pre = self._ext_runner.path_size(bare, parent, file_path)
            if pre is not None and pre > cap:
                raise ExternalGitOversizedError(pre, cap)
        # Render the diff bound to the SAME full OID the size checks used — pass
        # ``full``, never the abbreviated ``commit_hash`` the renderer would
        # re-resolve, so checked OID == read OID. The streaming cap is a
        # conservative backstop for a pathological many-hunk patch that renders
        # more than the two ≤cap images imply; the per-image size checks
        # above are the primary oversized gate.
        result = self._ext_runner.file_diff_entry(
            bare,
            full,
            file_path,
            max_output_bytes=_DIFF_OUTPUT_FACTOR * cap + _OUTPUT_MARGIN_BYTES,
        )
        if result["type"] == "unknown":
            return {**base, "type": "unknown", "diff": "", "error": "commit not found"}
        return {**base, "type": result["type"], "diff": result["diff"]}

    def list_files(self, vault_name: str, directory: str = "", extension: str = ".md") -> list[str]:
        """List files under a directory in HEAD.

        Mirror vaults are read through the hermetic runner so a
        planted promisor/rewrite config cannot re-open lazy-fetch on this
        read.
        """
        if self._use_mirror_reader(vault_name):
            return self._list_files_mirror(vault_name, directory, extension)
        with _managed_repo(self._get_repo(vault_name)) as repo:
            try:
                tree = repo.head.commit.tree
            except ValueError:
                return []

            if directory:
                try:
                    tree = tree / directory
                except KeyError:
                    return []

            results: list[str] = []
            self._walk_tree(tree, directory, extension, results)
            return results

    def _walk_tree(self, tree, prefix: str, extension: str, results: list[str]) -> None:
        for item in tree:
            rel_path = f"{prefix}/{item.name}" if prefix else item.name
            if item.type == "blob" and rel_path.endswith(extension):
                results.append(rel_path)
            elif item.type == "tree":
                self._walk_tree(item, rel_path, extension, results)

    def _list_files_mirror(
        self, vault_name: str, directory: str, extension: str
    ) -> list[str]:
        """Hermetic-runner equivalent of ``list_files`` for a mirror: derive the
        blob paths from a single recursive ``ls-tree`` at HEAD (sealed env +
        GIT_NO_LAZY_FETCH), filtering to those under ``directory`` ending in
        ``extension``. Paths carry the ``directory`` prefix, matching the
        GitPython walk (``_walk_tree``)."""
        bare = self._mirror_bare(vault_name)
        if not bare.exists():
            return []
        try:
            head = self._ext_runner.rev_parse(bare, "HEAD")
        except Exception:  # noqa: BLE001 — empty repo / no HEAD → nothing to list
            return []
        prefix = f"{directory}/" if directory else ""
        results: list[str] = []
        for path in self._ext_runner.ls_tree(bare, head):
            if prefix and not path.startswith(prefix):
                continue
            if path.endswith(extension):
                results.append(path)
        return results

    def list_directories(self, vault_name: str, parent: str = "") -> list[str]:
        """List immediate subdirectories under a path in HEAD.

        Mirror vaults are read through the hermetic runner.
        """
        if self._use_mirror_reader(vault_name):
            return self._list_directories_mirror(vault_name, parent)
        with _managed_repo(self._get_repo(vault_name)) as repo:
            try:
                tree = repo.head.commit.tree
            except ValueError:
                return []

            if parent:
                try:
                    tree = tree / parent
                except KeyError:
                    return []

            return [
                item.name
                for item in tree
                if item.type == "tree" and not item.name.startswith(".")
            ]

    def _list_directories_mirror(self, vault_name: str, parent: str) -> list[str]:
        """Hermetic-runner equivalent of ``list_directories`` for a mirror: the
        recursive ``ls-tree`` at HEAD carries no tree entries, so derive the
        IMMEDIATE subdirectory names under ``parent`` from the blob paths (a path
        with a further ``/`` after the ``parent`` prefix implies a subdirectory).
        Dot-directories are skipped, mirroring the GitPython path; results are
        sorted for a deterministic order."""
        bare = self._mirror_bare(vault_name)
        if not bare.exists():
            return []
        try:
            head = self._ext_runner.rev_parse(bare, "HEAD")
        except Exception:  # noqa: BLE001 — empty repo / no HEAD
            return []
        prefix = f"{parent}/" if parent else ""
        dirs: set[str] = set()
        for path in self._ext_runner.ls_tree(bare, head):
            if prefix and not path.startswith(prefix):
                continue
            rest = path[len(prefix):]
            seg, slash, _rest = rest.partition("/")
            if slash and seg and not seg.startswith("."):
                dirs.add(seg)
        return sorted(dirs)

    # ── Write operations ─────────────────────────────────────

    def commit_file(
        self,
        vault_name: str,
        file_path: str,
        content: str,
        message: str,
        author_name: str = "AKB System",
        author_email: str = "akb@system",
    ) -> str:
        """Write a file and commit. Returns the commit hash.

        Uses a persistent per-vault worktree linked to the bare repo;
        commits in the worktree update the bare's refs directly. Falls
        back to clone-and-push only when the bare is empty (no HEAD to
        attach the worktree to — happens once at vault creation).
        """
        with self._vault_write_lock(vault_name):
            wt = self._ensure_worktree(vault_name)
            if wt is None:
                return self._commit_via_clone(vault_name, file_path, content, message, author_name, author_email)

            with _managed_repo(Repo(str(wt))) as work_repo:
                # Defensive: if anything left the worktree dirty or behind the
                # bare ref (e.g., a previous crash mid-commit), sync to HEAD
                # before writing. With a single writer this is a no-op in the
                # steady state.
                work_repo.git.reset("--hard", "HEAD", **_write_kt())

                full_path = wt / file_path
                full_path.parent.mkdir(parents=True, exist_ok=True)
                full_path.write_text(content, encoding="utf-8")

                work_repo.git.add("--", file_path, **_write_kt())
                return self._stage_and_commit(
                    work_repo,
                    message,
                    author_name,
                    author_email,
                    parent_required=True,
                )

    def delete_file(
        self,
        vault_name: str,
        file_path: str,
        message: str,
        author_name: str = "AKB System",
        author_email: str = "akb@system",
    ) -> str:
        """Delete a file and commit. Returns the commit hash."""
        with self._vault_write_lock(vault_name):
            wt = self._ensure_worktree(vault_name)
            if wt is None:
                raise FileNotFoundError(f"File not found in vault: {file_path}")

            with _managed_repo(Repo(str(wt))) as work_repo:
                work_repo.git.reset("--hard", "HEAD", **_write_kt())

                full_path = wt / file_path
                if not full_path.exists():
                    raise FileNotFoundError(f"File not found in vault: {file_path}")

                work_repo.git.rm("--", file_path, **_write_kt())
                return self._stage_and_commit(
                    work_repo,
                    message,
                    author_name,
                    author_email,
                    parent_required=True,
                )

    def move_file(
        self,
        vault_name: str,
        old_path: str,
        new_path: str,
        message: str,
        author_name: str = "AKB System",
        author_email: str = "akb@system",
    ) -> str:
        """Rename/move a tracked file via ``git mv`` and commit. Returns the
        commit hash.

        ``git mv`` records the rename so ``git log --follow <new_path>`` traces
        the file's history across the move (blame/log continuity). Mirrors
        ``commit_file``/``delete_file``'s lock + worktree-prep + commit shape.
        """
        if old_path == new_path:
            raise ValueError("move_file: old_path and new_path are identical")
        with self._vault_write_lock(vault_name):
            wt = self._ensure_worktree(vault_name)
            if wt is None:
                raise FileNotFoundError(f"File not found in vault: {old_path}")

            with _managed_repo(Repo(str(wt))) as work_repo:
                work_repo.git.reset("--hard", "HEAD", **_write_kt())

                src = wt / old_path
                if not src.exists():
                    raise FileNotFoundError(f"File not found in vault: {old_path}")
                dst = wt / new_path
                # A case-only rename ("File.md" -> "file.md") on a case-INSENSITIVE
                # filesystem (macOS APFS/HFS+) makes dst.exists() report True even
                # though src and dst are the SAME inode. Allow that; only a genuine
                # different file at the destination is a conflict.
                if dst.exists() and not src.samefile(dst):
                    raise FileExistsError(f"Destination already exists in vault: {new_path}")
                dst.parent.mkdir(parents=True, exist_ok=True)

                work_repo.git.mv("--", old_path, new_path, **_write_kt())
                return self._stage_and_commit(
                    work_repo,
                    message,
                    author_name,
                    author_email,
                    parent_required=True,
                )

    def current_commit(self, vault_name: str) -> str | None:
        """Return the vault's current HEAD commit hash, or None if the bare repo
        has no commits yet. Used to reconcile DB state after a crash-recovery
        move where the git mv already committed.

        Mirror vaults resolve HEAD through the hermetic runner.
        """
        if self._use_mirror_reader(vault_name):
            bare = self._mirror_bare(vault_name)
            if not bare.exists():
                return None
            try:
                return self._ext_runner.rev_parse(bare, "HEAD")
            except Exception:  # noqa: BLE001 — no HEAD yet / unreadable
                return None
        try:
            with _managed_repo(self._get_repo(vault_name)) as repo:
                return repo.head.commit.hexsha
        except Exception:  # noqa: BLE001 — repo missing or no HEAD yet (empty repo)
            return None

    def delete_paths_bulk(
        self,
        *,
        vault_name: str,
        file_paths: list[str],
        message: str,
        author_name: str = "AKB System",
        author_email: str = "akb@system",
    ) -> str | None:
        """Remove many paths in one commit under a per-vault lock.

        Idempotent on missing paths: any entry in `file_paths` that does
        not exist in the worktree is skipped silently (no exception). If
        every requested path is already absent, no commit is made and
        this returns `None`. Duplicates in `file_paths` are deduplicated
        (order-preserving) before the presence check so a doubled path
        doesn't trip `git rm` on its second occurrence.

        Mirrors `delete_file`'s lock + worktree-prep + commit shape.
        Returns the new commit's hex SHA, or `None` when no commit was made.
        """
        with self._vault_write_lock(vault_name):
            wt = self._ensure_worktree(vault_name)
            if wt is None:
                # Empty bare repo or missing vault — nothing to delete.
                return None

            with _managed_repo(Repo(str(wt))) as work_repo:
                work_repo.git.reset("--hard", "HEAD", **_write_kt())

                # Dedupe while preserving caller order so log output is stable
                # and so a doubled path doesn't make `git rm` fail on
                # the second occurrence.
                unique_paths = list(dict.fromkeys(file_paths))
                present = [p for p in unique_paths if (wt / p).exists()]
                if not present:
                    logger.debug(
                        "delete_paths_bulk: all paths already absent for vault=%s (%d requested)",
                        vault_name, len(unique_paths),
                    )
                    return None

                work_repo.git.rm("--", *present, **_write_kt())
                return self._stage_and_commit(
                    work_repo,
                    message,
                    author_name,
                    author_email,
                    parent_required=True,
                )

    def _commit_via_clone(
        self,
        vault_name: str,
        file_path: str,
        content: str,
        message: str,
        author_name: str,
        author_email: str,
    ) -> str:
        """Legacy clone/push path, used only for the very first commit on
        an empty bare repo (before any branch exists — worktree add can't
        attach without a branch). One-shot cost at vault creation.

        GitPython's ``Repo.clone_from`` calls ``os.getcwd()`` internally
        to bootstrap the Git wrapper. If a previous call left the
        process working directory pointing at a now-deleted
        ``TemporaryDirectory``, that getcwd raises ``FileNotFoundError``
        and every subsequent vault-creation request 500s. Use plain
        ``subprocess`` with an explicit ``cwd`` so the call never reads
        the process cwd.

        Cancellation hazard: ``subprocess.run`` blocks the worker
        thread until the child exits. If the surrounding asyncio task
        is cancelled, the running ``git clone`` / ``git push`` keeps
        going on the local filesystem until it finishes or hits the
        timeout below. Acceptable because this path only runs on the
        very first commit of a fresh vault — but the timeouts are
        here as a hard upper bound so a wedged subprocess can't pin
        the vault lock forever.
        """
        import subprocess
        import tempfile
        bare_path = self._bare_path(vault_name)
        # Stable parent for the tmp dir so we don't depend on the
        # process cwd to resolve a relative name. ``storage_path``
        # always exists on a healthy deploy.
        clone_timeout = settings.git_write_timeout_secs
        with tempfile.TemporaryDirectory(dir=str(self.storage_path)) as tmp:
            try:
                subprocess.run(
                    ["git", "clone", "--quiet", str(bare_path), tmp],
                    check=True, cwd=tmp, timeout=clone_timeout,
                )
            except subprocess.TimeoutExpired as e:
                raise GitError(
                    f"git clone timed out after {clone_timeout:.0f}s for vault {vault_name}"
                ) from e
            with _managed_repo(Repo(tmp)) as work_repo:
                full_path = Path(tmp) / file_path
                full_path.parent.mkdir(parents=True, exist_ok=True)
                full_path.write_text(content, encoding="utf-8")
                work_repo.git.add("--", file_path, **_write_kt())
                commit_hash = self._stage_and_commit(
                    work_repo,
                    message,
                    author_name,
                    author_email,
                    parent_required=False,
                )
                # Push is local-to-local (bare repo on the same disk), so the
                # standard write timeout is generous; if it hangs the timeout
                # still releases the vault lock for the next caller.
                try:
                    work_repo.git.push("origin", **_write_kt())
                except GitError as e:
                    raise GitError(
                        f"git push failed for vault {vault_name}: {e}"
                    ) from e
        return commit_hash

    # ── History operations ───────────────────────────────────

    def file_log(
        self,
        vault_name: str,
        file_path: str,
        max_count: int = 20,
        since_epoch: int | None = None,
    ) -> list[dict]:
        """Get commit log for a specific file.

        ``since_epoch`` (Unix seconds) trims commits older than the boundary
        so that history at a path which was deleted and re-created starts
        clean from the current document's ``created_at`` — pre-fix, commits
        from a since-deleted prior document leaked into the new doc's
        history because git keys by path, not by document identity.

        Mirror vaults are read through the hermetic runner.
        """
        if self._use_mirror_reader(vault_name):
            return self._file_log_mirror(vault_name, file_path, max_count, since_epoch)
        with _managed_repo(self._get_repo(vault_name)) as repo:
            return self._file_log_with_repo(repo, file_path, max_count, since_epoch)

    def _file_log_with_repo(
        self,
        repo: Repo,
        file_path: str,
        max_count: int,
        since_epoch: int | None,
    ) -> list[dict]:
        try:
            entries = self._follow_path_history(
                repo,
                repo.head.commit.hexsha,
                file_path,
                max_count=max_count,
                since_epoch=since_epoch,
            )
        except (ValueError, GitError):
            return []

        results: list[dict] = []
        for entry in entries:
            committed_at = entry["committed_at"]
            if since_epoch is not None and int(committed_at.timestamp()) < since_epoch:
                continue
            try:
                commit = repo.commit(entry["legacy_git_oid"])
            except (BadName, BadObject, ValueError):
                continue
            results.append(
                {
                    "hash": commit.hexsha[:12],
                    "message": commit.message.strip(),
                    "author": str(commit.author),
                    "date": committed_at.isoformat(),
                }
            )
        return results

    @staticmethod
    def _legacy_commit_metadata(commit) -> dict[str, str]:
        """Parse the commit-message metadata used by the legacy activity feed."""
        message = str(commit.message)
        lines = message.strip().split("\n")
        metadata: dict[str, str] = {"subject": lines[0] if lines else ""}
        for body_line in (line.strip() for line in lines[1:] if line.strip()):
            if ":" in body_line:
                key, value = body_line.split(":", 1)
                metadata[key.strip().lower()] = value.strip()
        metadata.setdefault("action", "")
        metadata.setdefault("summary", "")
        metadata.setdefault("agent", str(commit.author))
        return metadata

    def vault_log(self, vault_name: str, max_count: int = 30, since: str | None = None, path: str | None = None) -> list[dict]:
        """Get commit log for the vault, optionally scoped to a path.

        Like `git log -- <path>`: Git natively filters to only commits
        that touched files under the given path. No post-filter limit issue.

        Mirror vaults are read through the hermetic runner.
        """
        if self._use_mirror_reader(vault_name):
            return self._vault_log_mirror(vault_name, max_count, since, path)
        with _managed_repo(self._get_repo(vault_name)) as repo:
            return self._vault_log_with_repo(repo, max_count, since, path)

    def _vault_log_with_repo(
        self,
        repo: Repo,
        max_count: int,
        since: str | None,
        path: str | None,
    ) -> list[dict]:
        try:
            # gitpython's iter_commits stub forbids **kwargs splatting
            # (each named param is typed individually). Two branches by
            # which optional flags are present — explicit, mypy-clean.
            if since and path:
                commits = list(repo.iter_commits(max_count=max_count, since=since, paths=path))
            elif since:
                commits = list(repo.iter_commits(max_count=max_count, since=since))
            elif path:
                commits = list(repo.iter_commits(max_count=max_count, paths=path))
            else:
                commits = list(repo.iter_commits(max_count=max_count))
        except (ValueError, GitError):
            return []

        results = []
        for c in commits:
            # Parse commit message for action/summary. `c.message` is
            # `str | bytes` per the stub; in practice gitpython always
            # decodes to str via its `default_encoding`. `str(...)` is
            # a cheap normalisation that also satisfies mypy.
            meta = self._legacy_commit_metadata(c)

            # Get changed files
            changed_files: list[dict] = []
            try:
                if c.parents:
                    diffs = c.parents[0].diff(c)
                    for d in diffs:
                        # `b_path`/`a_path` are typed as
                        # `str | PathLike[str] | None`; we want a plain
                        # str for the response payload either way.
                        diff_path_raw = d.b_path or d.a_path
                        if diff_path_raw is None:
                            continue
                        diff_path = str(diff_path_raw)
                        if diff_path.startswith("."):
                            continue
                        change_type = "added" if d.new_file else ("deleted" if d.deleted_file else "modified")
                        changed_files.append({"path": diff_path, "change": change_type})
                else:
                    # Initial commit — every blob in the tree counts as
                    # "added". `isinstance(item, Blob)` narrows the
                    # traverse() union AND skips submodules cleanly.
                    for item in c.tree.traverse():
                        if not isinstance(item, Blob):
                            continue
                        blob_path = str(item.path)
                        if not blob_path.startswith("."):
                            changed_files.append({"path": blob_path, "change": "added"})
            except (GitError, TypeError):
                pass

            results.append({
                "hash": c.hexsha[:12],
                "subject": meta["subject"],
                "author": str(c.author),
                "date": datetime.fromtimestamp(c.committed_date, tz=timezone.utc).isoformat(),
                "action": meta.get("action", ""),
                "summary": meta.get("summary", ""),
                "agent": meta.get("agent", str(c.author)),
                "files": changed_files,
            })

        return results

    def file_diff(self, vault_name: str, file_path: str, commit_hash: str) -> dict:
        """Get diff for a specific file at a specific commit.

        Returns the unified diff patch for the file.

        Mirrors read_file's defensive `BadName/BadObject` catch so an
        unknown / malformed commit hash surfaces as a clean
        ``{"type":"unknown"}`` response rather than propagating as an
        unhandled 500.

        Mirror vaults are read through the hermetic runner.
        """
        if self._use_mirror_reader(vault_name):
            return self._file_diff_mirror(vault_name, file_path, commit_hash)
        historical_path = self.path_at_revision(vault_name, file_path, commit_hash)
        lookup_path = historical_path or file_path
        with _managed_repo(self._get_repo(vault_name)) as repo:
            return self._file_diff_with_repo(
                repo,
                file_path,
                lookup_path,
                commit_hash,
            )

    def _file_diff_with_repo(
        self,
        repo: Repo,
        file_path: str,
        lookup_path: str,
        commit_hash: str,
    ) -> dict:
        try:
            commit = repo.commit(commit_hash)
        except (ValueError, BadName, BadObject):
            return {
                "file": file_path,
                "commit": commit_hash,
                "type": "unknown",
                "diff": "",
                "error": "commit not found",
            }

        if not commit.parents:
            # Initial commit — show full content as addition
            try:
                blob = commit.tree / lookup_path
                content = blob.data_stream.read().decode("utf-8")
                return {
                    "file": file_path,
                    "commit": commit_hash,
                    "type": "added",
                    "diff": "\n".join(f"+{line}" for line in content.split("\n")),
                }
            except (KeyError, TypeError):
                return {"file": file_path, "commit": commit_hash, "type": "unknown", "diff": ""}

        parent = commit.parents[0]
        diffs = parent.diff(commit, paths=[lookup_path], create_patch=True)

        for d in diffs:
            patch = d.diff
            if isinstance(patch, bytes):
                patch = patch.decode("utf-8", errors="replace")
            change_type = "added" if d.new_file else ("deleted" if d.deleted_file else "modified")
            return {
                "file": file_path,
                "commit": commit_hash,
                "type": change_type,
                "diff": patch,
            }

        return {"file": file_path, "commit": commit_hash, "type": "unchanged", "diff": ""}

    def diff(self, vault_name: str, from_commit: str, to_commit: str | None = None) -> str:
        """Get diff between two commits, or from a commit to HEAD."""
        if self._use_mirror_reader(vault_name):
            # A two-commit, whole-repo diff has no ExternalGitRunner primitive
            # yet (the runner exposes only the single-file ``file_diff_entry``).
            # Falling back to GitPython on a mirror would re-open the lazy-fetch /
            # replace-object surface this hardening closes, so refuse
            # the operation instead of failing open. No caller reaches this on a
            # mirror today (Finding #5); an unexpected call gets a clean 4xx (not
            # a 500). A hermetic ``diff_between`` runner method is the follow-up
            # (owned by external_git_runner.py).
            raise AKBError(
                "whole-repo diff is not supported on an external-git mirror vault",
                status_code=400,
                code="external_git_mirror_diff_unsupported",
            )
        with _managed_repo(self._get_repo(vault_name)) as repo:
            base = repo.commit(from_commit)
            head = repo.commit(to_commit) if to_commit else repo.head.commit
            return base.diff(head, create_patch=True).__str__()
