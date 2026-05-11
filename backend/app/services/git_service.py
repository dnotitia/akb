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
by a per-vault threading lock.

Remote ops (clone_mirror / fetch_remote / ls_remote_head) inject the auth
token into the URL only at command-invocation time and never persist it
to the bare repo's `.git/config`. That keeps the on-disk surface free of
secrets even when callers handed us a plaintext PAT.
"""

from __future__ import annotations

import logging
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit, quote

from git import Repo, Actor, cmd as git_cmd
from git.exc import GitError

from app.config import settings

logger = logging.getLogger("akb.git")


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


class GitService:
    def __init__(self, storage_path: str | None = None):
        self.storage_path = Path(storage_path or settings.git_storage_path)
        self.storage_path.mkdir(parents=True, exist_ok=True)
        self.worktrees_path = self.storage_path / "_worktrees"
        self.worktrees_path.mkdir(parents=True, exist_ok=True)

    def _bare_path(self, vault_name: str) -> Path:
        return self.storage_path / f"{vault_name}.git"

    def _worktree_path(self, vault_name: str) -> Path:
        return self.worktrees_path / vault_name

    def _get_repo(self, vault_name: str) -> Repo:
        bare_path = self._bare_path(vault_name)
        if not bare_path.exists():
            raise FileNotFoundError(f"Vault repo not found: {vault_name}")
        return Repo(str(bare_path))

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
        bare_repo = Repo(str(bare))
        try:
            # Touch HEAD to see if there's at least one commit.
            _ = bare_repo.head.commit
            branch_name = bare_repo.head.ref.name
        except (ValueError, TypeError, GitError):
            return None  # empty repo; caller falls back to the clone path
        wt.parent.mkdir(parents=True, exist_ok=True)
        bare_repo.git.worktree("add", str(wt), branch_name)
        logger.info("Worktree created for vault %s at %s (branch=%s)", vault_name, wt, branch_name)
        return wt

    # ── Vault lifecycle ──────────────────────────────────────

    def init_vault(self, vault_name: str) -> str:
        """Initialize a new bare repo for a vault. Returns the repo path."""
        bare_path = self._bare_path(vault_name)
        if bare_path.exists():
            raise FileExistsError(f"Vault already exists: {vault_name}")
        Repo.init(str(bare_path), bare=True)
        return str(bare_path)

    def vault_exists(self, vault_name: str) -> bool:
        return self._bare_path(vault_name).exists()

    def cleanup_stale_locks(self, max_age_seconds: float = 60.0) -> int:
        """Remove `index.lock` files for every vault that are older than
        `max_age_seconds`.

        A crashed git process (OOM, SIGKILL, container restart mid-commit)
        leaves the index.lock behind; subsequent writes to that worktree
        fail with "Unable to create '.../index.lock': File exists" until
        the lock is cleared by hand. Running this at startup recovers
        every affected vault before any worker can run into the same wall.

        Lock locations checked:
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
        if not self.worktrees_path.exists():
            return cleared
        for vault_dir in self.worktrees_path.iterdir():
            if not vault_dir.is_dir():
                continue
            vault_name = vault_dir.name
            candidates = [
                self._bare_path(vault_name) / "worktrees" / vault_name / "index.lock",
                vault_dir / ".git" / "index.lock",
            ]
            for lock in candidates:
                # `.git` in a linked worktree is a file (gitdir pointer),
                # not a dir — its `index.lock` path is meaningless. Skip
                # quickly if the parent isn't a directory.
                if not lock.parent.is_dir():
                    continue
                if not lock.exists() or lock.is_dir():
                    continue
                try:
                    age = time.time() - lock.stat().st_mtime
                except OSError:
                    continue
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
        """
        import shutil
        for path in (self._bare_path(vault_name), self._worktree_path(vault_name)):
            if path.exists():
                shutil.rmtree(path)

    # ── External remote operations ───────────────────────────

    @staticmethod
    def _with_auth(remote_url: str, auth_token: str | None) -> str:
        """Inject `x-access-token:<token>` into the URL's userinfo, only
        for the duration of one git command. Returns the URL unchanged
        when no token is supplied or when the URL is already authenticated.
        """
        if not auth_token:
            return remote_url
        parts = urlsplit(remote_url)
        if parts.scheme not in ("http", "https"):
            return remote_url
        if "@" in parts.netloc:
            return remote_url
        userinfo = f"x-access-token:{quote(auth_token, safe='')}"
        netloc = f"{userinfo}@{parts.netloc}"
        return urlunsplit((parts.scheme, netloc, parts.path, parts.query, parts.fragment))

    def clone_mirror(
        self,
        vault_name: str,
        remote_url: str,
        branch: str,
        auth_token: str | None = None,
        timeout: int | None = None,
    ) -> str:
        """Clone an external repo as the vault's bare repo. The on-disk
        remote URL is stored without auth so the token never touches
        `.git/config`. Subsequent fetches re-inject auth at invocation.
        `timeout` is in seconds (defaults to settings.external_git_clone_timeout);
        if the clone hasn't finished, git is killed so the worker can
        back off instead of hanging.
        """
        bare_path = self._bare_path(vault_name)
        if bare_path.exists():
            raise FileExistsError(f"Vault already exists: {vault_name}")
        timeout = timeout or settings.external_git_clone_timeout
        with _vault_lock(vault_name):
            authed = self._with_auth(remote_url, auth_token)
            git_cmd.Git().clone(
                "--bare", "--single-branch", "--branch", branch,
                authed, str(bare_path),
                kill_after_timeout=timeout,
            )
            # Strip auth from stored remote URL so the token isn't on disk.
            Repo(str(bare_path)).git.remote("set-url", "origin", remote_url)
        # Log hostname only; caller may have embedded a PAT in the URL.
        host = urlsplit(remote_url).hostname or "unknown"
        logger.info("Mirror cloned: vault=%s host=%s branch=%s", vault_name, host, branch)
        return str(bare_path)

    def fetch_remote(
        self,
        vault_name: str,
        remote_url: str,
        branch: str,
        auth_token: str | None = None,
        timeout: int | None = None,
    ) -> str:
        """Fetch the remote branch into the bare repo. Updates the local
        ref `refs/heads/<branch>` to whatever the remote currently is
        (force — mirrors track upstream literally). Returns the new SHA.
        """
        bare_path = self._bare_path(vault_name)
        if not bare_path.exists():
            raise FileNotFoundError(f"Vault repo not found: {vault_name}")
        timeout = timeout or settings.external_git_fetch_timeout
        with _vault_lock(vault_name):
            repo = Repo(str(bare_path))
            authed = self._with_auth(remote_url, auth_token)
            repo.git.fetch(
                authed, f"+refs/heads/{branch}:refs/heads/{branch}",
                kill_after_timeout=timeout,
            )
            return repo.git.rev_parse(f"refs/heads/{branch}")

    def ls_remote_head(
        self,
        remote_url: str,
        branch: str,
        auth_token: str | None = None,
        timeout: int | None = None,
    ) -> str | None:
        """Return the SHA of the remote branch HEAD without fetching
        objects. Cheap network round-trip used by the poller to decide
        whether a full fetch is worthwhile. Returns None if the branch
        doesn't exist on the remote.
        """
        authed = self._with_auth(remote_url, auth_token)
        timeout = timeout or settings.external_git_lsremote_timeout
        out = git_cmd.Git().ls_remote(authed, branch, kill_after_timeout=timeout)
        if not out:
            return None
        # Output: "<sha>\trefs/heads/<branch>" (possibly multiple lines).
        for line in out.splitlines():
            sha, _, ref = line.partition("\t")
            if ref.endswith(f"refs/heads/{branch}"):
                return sha.strip()
        return None

    def ls_tree(self, vault_name: str, sha: str) -> dict[str, str]:
        """Return `{path: blob_sha}` for every blob reachable from `sha`.
        Used by the reconciler to compare upstream tree against local
        documents.external_blob without parsing diff status codes.
        """
        repo = self._get_repo(vault_name)
        commit = repo.commit(sha)
        out: dict[str, str] = {}
        for item in commit.tree.traverse():
            if item.type == "blob":
                out[item.path] = item.hexsha
        return out

    def last_commit_for_path(self, vault_name: str, path: str) -> str | None:
        """Hex sha of the most recent commit that touched `path`. Used to
        stamp `documents.current_commit` per-file so mirror docs don't
        all share the reconcile-time HEAD sha. Returns None when the
        path has no commits (should not happen for a path we just
        read from the tree).
        """
        repo = self._get_repo(vault_name)
        try:
            commits = list(repo.iter_commits(paths=path, max_count=1))
        except (ValueError, GitError):
            return None
        return commits[0].hexsha if commits else None

    def cat_blob(self, vault_name: str, blob_sha: str) -> bytes:
        """Read a blob's raw bytes from the object store by sha. Works
        regardless of whether the blob is currently reachable from HEAD.
        `cat-file blob` (not `-p`) so the output is the literal blob
        contents, unaffected by git's pretty-printer for non-blob types.
        """
        repo = self._get_repo(vault_name)
        return repo.git.cat_file("blob", blob_sha, stdout_as_string=False)

    # ── Read operations ──────────────────────────────────────

    def read_file(self, vault_name: str, file_path: str, commit: str | None = None) -> str | None:
        """Read a file's content from the repo. Returns None if not found."""
        repo = self._get_repo(vault_name)
        try:
            ref = repo.commit(commit) if commit else repo.head.commit
        except ValueError:
            # Empty repo, no commits yet
            return None
        try:
            blob = ref.tree / file_path
            return blob.data_stream.read().decode("utf-8")
        except (KeyError, TypeError):
            return None

    def list_files(self, vault_name: str, directory: str = "", extension: str = ".md") -> list[str]:
        """List files under a directory in HEAD."""
        repo = self._get_repo(vault_name)
        try:
            tree = repo.head.commit.tree
        except ValueError:
            return []

        if directory:
            try:
                tree = tree / directory
            except KeyError:
                return []

        results = []
        self._walk_tree(tree, directory, extension, results)
        return results

    def _walk_tree(self, tree, prefix: str, extension: str, results: list[str]) -> None:
        for item in tree:
            rel_path = f"{prefix}/{item.name}" if prefix else item.name
            if item.type == "blob" and rel_path.endswith(extension):
                results.append(rel_path)
            elif item.type == "tree":
                self._walk_tree(item, rel_path, extension, results)

    def list_directories(self, vault_name: str, parent: str = "") -> list[str]:
        """List immediate subdirectories under a path in HEAD."""
        repo = self._get_repo(vault_name)
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
        with _vault_lock(vault_name):
            wt = self._ensure_worktree(vault_name)
            if wt is None:
                return self._commit_via_clone(vault_name, file_path, content, message, author_name, author_email)

            work_repo = Repo(str(wt))
            # Defensive: if anything left the worktree dirty or behind the
            # bare ref (e.g., a previous crash mid-commit), sync to HEAD
            # before writing. With a single writer this is a no-op in the
            # steady state.
            work_repo.git.reset("--hard", "HEAD")

            full_path = wt / file_path
            full_path.parent.mkdir(parents=True, exist_ok=True)
            full_path.write_text(content, encoding="utf-8")

            work_repo.index.add([file_path])
            author = Actor(author_name, author_email)
            commit = work_repo.index.commit(message, author=author, committer=author)
            return commit.hexsha

    def delete_file(
        self,
        vault_name: str,
        file_path: str,
        message: str,
        author_name: str = "AKB System",
        author_email: str = "akb@system",
    ) -> str:
        """Delete a file and commit. Returns the commit hash."""
        with _vault_lock(vault_name):
            wt = self._ensure_worktree(vault_name)
            if wt is None:
                raise FileNotFoundError(f"File not found in vault: {file_path}")

            work_repo = Repo(str(wt))
            work_repo.git.reset("--hard", "HEAD")

            full_path = wt / file_path
            if not full_path.exists():
                raise FileNotFoundError(f"File not found in vault: {file_path}")

            work_repo.index.remove([file_path], working_tree=True)
            author = Actor(author_name, author_email)
            commit = work_repo.index.commit(message, author=author, committer=author)
            return commit.hexsha

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
        """
        import tempfile
        bare_path = self._bare_path(vault_name)
        author = Actor(author_name, author_email)
        with tempfile.TemporaryDirectory() as tmp:
            work_repo = Repo.clone_from(str(bare_path), tmp)
            full_path = Path(tmp) / file_path
            full_path.parent.mkdir(parents=True, exist_ok=True)
            full_path.write_text(content, encoding="utf-8")
            work_repo.index.add([file_path])
            commit = work_repo.index.commit(message, author=author, committer=author)
            work_repo.remote("origin").push()
        return commit.hexsha

    # ── History operations ───────────────────────────────────

    def file_log(self, vault_name: str, file_path: str, max_count: int = 20) -> list[dict]:
        """Get commit log for a specific file."""
        repo = self._get_repo(vault_name)
        try:
            commits = list(repo.iter_commits(paths=file_path, max_count=max_count))
        except (ValueError, GitError):
            return []

        return [
            {
                "hash": c.hexsha[:12],
                "message": c.message.strip(),
                "author": str(c.author),
                "date": datetime.fromtimestamp(c.committed_date, tz=timezone.utc).isoformat(),
            }
            for c in commits
        ]

    def vault_log(self, vault_name: str, max_count: int = 30, since: str | None = None, path: str | None = None) -> list[dict]:
        """Get commit log for the vault, optionally scoped to a path.

        Like `git log -- <path>`: Git natively filters to only commits
        that touched files under the given path. No post-filter limit issue.
        """
        repo = self._get_repo(vault_name)
        try:
            kwargs = {"max_count": max_count}
            if since:
                kwargs["since"] = since
            if path:
                kwargs["paths"] = path
            commits = list(repo.iter_commits(**kwargs))
        except (ValueError, GitError):
            return []

        results = []
        for c in commits:
            # Parse commit message for action/summary
            lines = c.message.strip().split("\n")
            subject = lines[0] if lines else ""
            body_lines = [l.strip() for l in lines[1:] if l.strip()]

            meta = {}
            for bl in body_lines:
                if ":" in bl:
                    k, v = bl.split(":", 1)
                    meta[k.strip().lower()] = v.strip()

            # Get changed files
            changed_files = []
            try:
                if c.parents:
                    diffs = c.parents[0].diff(c)
                    for d in diffs:
                        path = d.b_path or d.a_path
                        if path and not path.startswith("."):
                            change_type = "added" if d.new_file else ("deleted" if d.deleted_file else "modified")
                            changed_files.append({"path": path, "change": change_type})
                else:
                    # Initial commit
                    for item in c.tree.traverse():
                        if item.type == "blob" and not item.path.startswith("."):
                            changed_files.append({"path": item.path, "change": "added"})
            except (GitError, TypeError):
                pass

            results.append({
                "hash": c.hexsha[:12],
                "subject": subject,
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
        """
        repo = self._get_repo(vault_name)
        commit = repo.commit(commit_hash)

        if not commit.parents:
            # Initial commit — show full content as addition
            try:
                blob = commit.tree / file_path
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
        diffs = parent.diff(commit, paths=[file_path], create_patch=True)

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
        repo = self._get_repo(vault_name)
        base = repo.commit(from_commit)
        head = repo.commit(to_commit) if to_commit else repo.head.commit
        return base.diff(head, create_patch=True).__str__()
