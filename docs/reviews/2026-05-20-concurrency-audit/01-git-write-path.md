# Git Write Path & Commit Atomicity — Findings

## Summary

The AKB git write path serializes all worktree mutations within a vault behind a single per-vault `threading.Lock`, which correctly prevents concurrent commits on the same worktree from corrupting the index. However, the lock covers only the git operations; the DB writes that must be causally coupled to each commit (`documents.current_commit`, `doc_repo.create`, `doc_repo.update`) happen outside any git-level critical section and in many cases outside any DB transaction that includes the commit hash. This produces multiple concrete windows where git HEAD and the DB row permanently diverge. Additionally, the lock granularity creates liveness problems (a slow network fetch for mirror vaults starves all local writers on the same vault), and the concurrent-put race described in the bug report is structurally unresolved for the update/edit paths.

---

## Findings

### F1: git commit precedes DB write with no shared transaction — permanent HEAD/DB divergence on crash

**File**: `backend/app/services/document_service.py:469–481` (update), `615–627` (edit), `240–262` (put)

**Race scenario**:

For `update()`:
1. `asyncio.to_thread(self.git.commit_file, ...)` returns `commit_hash` at line 469–474. Git HEAD now points at the new commit.
2. The thread pool releases its slot. The event loop resumes.
3. Before `doc_repo.update(...)` at line 477 executes, the process is killed (OOM, SIGKILL, pod eviction).
4. On restart: git HEAD = new commit, `documents.current_commit` = old commit. Every subsequent `get()` reads the new content from git (via `HEAD`) but the DB row reports the old commit hash. `get_at_commit(version=old_hash)` returns stale content.

The same window exists identically in `edit()` (lines 615–627) and in `put()` (lines 240–262: git commit at 240, DB INSERT at 257).

**Observable bad state**: `documents.current_commit` permanently trails the actual git HEAD for this path. The field is used by the history endpoint and `get_at_commit`; clients see wrong commit attribution. On re-update: `read_file` reads the NEW content (which HEAD points at), merges it as the "current" content, and the user loses visibility that the previous update was recorded.

**Fix direction**: Wrap `doc_repo.update`/`doc_repo.create` and the git commit hash together so that a failure before the DB write causes the git commit to be logically orphaned (and recovered by a startup job) rather than leaving a permanently desynchronized DB row. The simplest structural fix is moving `doc_repo.update` inside the same DB transaction that writes chunks and events (which already exists at lines 490–531), updating `current_commit` atomically with the rest of the metadata in one `COMMIT`.

**Evidence**:
```python
# document_service.py:469-481 — git commit then DB update, NOT in same TX
commit_hash = await asyncio.to_thread(
    self.git.commit_file,          # line 469: git HEAD advances
    vault_name=vault, ...
)
# <-- process can die here -->
await doc_repo.update(             # line 477: separate autocommit UPDATE
    pg_doc_id, ..., commit_hash=commit_hash, ...
)
```

---

### F2: Concurrent put() past the pre-check gate — git commit with no DB row

**File**: `backend/app/services/document_service.py:229–262`

**Race scenario**:

1. Callers A and B concurrently call `put()` for the same vault + same title (same derived `file_path`).
2. Both read `doc_repo.find_by_path(vault_id, file_path)` at line 229 — the path does not exist yet — so both pass the pre-check.
3. Both call `asyncio.to_thread(self.git.commit_file, ...)`. The per-vault lock serializes them: A commits first (new commit C1), then B commits (new commit C2). HEAD is now C2.
4. A's `doc_repo.create(...)` at line 257 inserts successfully.
5. B's `doc_repo.create(...)` at line 257 hits the `UNIQUE(vault_id, path)` constraint and raises `ConflictError`.
6. B returns a 409 to its caller. But git now has two commits for the same path (C1 and C2). C2 is HEAD. The DB row records C1 (A's commit hash).

**Observable bad state**: git HEAD has B's content; the DB row's `current_commit` has A's hash. Any call to `get()` reads A's commit hash from DB but actually reads B's content from git HEAD (via `read_file` with no `commit` argument). This is the exact divergence described in the bug report. The comment at line 226–228 explicitly acknowledges this and calls it "a future explicit-update workflow" — but no such workflow exists.

**Fix direction**: Reserve the `(vault_id, path)` slot in the DB (e.g., via `INSERT ... ON CONFLICT DO NOTHING` with a status field, or a `FOR UPDATE` advisory lock keyed on `(vault_id, path)`) before the git commit, so that losing the DB race aborts the git write, not vice versa.

**Evidence**:
```python
# document_service.py:229-245 — check is not atomic with git commit
if await doc_repo.find_by_path(vault_id, file_path):      # line 229 pre-check
    raise ConflictError(...)

# <-- concurrent B passes here simultaneously -->

commit_hash = await asyncio.to_thread(                     # line 240
    self.git.commit_file, ...                              # lock acquired, A commits C1, B commits C2
)
pg_doc_id = await doc_repo.create(...)                     # line 257: B gets 409, A gets row with C1
```

---

### F3: update() and edit() have no path-level reservation — last-writer-wins on DB, not on git

**File**: `backend/app/services/document_service.py:422–537` (update), `541–661` (edit)

**Race scenario**:

1. Callers A and B concurrently call `update()` on the same `(vault, doc_ref)`.
2. Both read `doc_repo.find_by_ref` (line 429) — both see the same DB row.
3. Both read `git.read_file` (line 437) — both see the same current content (or A's version if A already committed).
4. Both call `asyncio.to_thread(self.git.commit_file, ...)`. The vault lock serializes: A commits C1, then B commits C2 (which is a commit on top of A's content because `reset --hard HEAD` re-reads C1 first). HEAD is now C2.
5. A calls `doc_repo.update(..., commit_hash=C1)`. Succeeds, row now says C1.
6. B calls `doc_repo.update(..., commit_hash=C2)`. Succeeds, row now says C2.

Final state: `current_commit = C2`, git HEAD = C2 — these agree. However, A's update is silently swallowed into C2's content only if B happened to read before A committed. If B read A's content and only changed a different field, both updates survive in the content. But if B read before A committed (both start nearly simultaneously), B's commit C2 contains only B's changes; A's content change (which C1 captured correctly) is overwritten. The DB never records C1 as "the current commit" after B completes. **The net effect is a lost-update for A's content changes while the history log shows two commits.**

**Observable bad state**: A's content change is in git history (reachable via `file_log`) but is no longer in HEAD, and the DB row was transiently set to C1 and then to C2. No error is surfaced to A. `get()` shows only B's content. A's changes are permanently lost without any indication.

**Fix direction**: Acquire an advisory or row-level DB lock on the document row before reading git content, so concurrent updates are serialized at the DB layer before any git I/O occurs, making the read-modify-write atomic.

**Evidence**:
```python
# document_service.py:437-474 — no lock between read and commit
current_content = await asyncio.to_thread(        # line 437: both A and B read same content
    self.git.read_file, vault, file_path)
...
commit_hash = await asyncio.to_thread(            # line 469: vault lock serializes git,
    self.git.commit_file, ...)                    # but DB row read happened BEFORE this
```

---

### F4: `fetch_remote` holds the vault lock during network I/O — starves local writers

**File**: `backend/app/services/git_service.py:273–280`

**Race scenario**:

1. The external git poller calls `asyncio.to_thread(self.git.fetch_remote, vault_name, ...)`.
2. `fetch_remote` acquires `_vault_lock(vault_name)` at line 273.
3. `repo.git.fetch(authed, ..., kill_after_timeout=timeout)` at line 276 performs a network fetch. Default `external_git_fetch_timeout` can be minutes on large repos.
4. Meanwhile, a user `put()` or `update()` call on the same vault calls `asyncio.to_thread(self.git.commit_file, ...)`. The commit thread blocks waiting for `_vault_lock(vault_name)`, which is held by the fetch thread.
5. The `asyncio.to_thread` thread pool slot is occupied. Every user request to this vault blocks its thread for the full fetch duration.

**Observable bad state**: User-facing PUT/UPDATE/DELETE operations on a mirror vault (which should be read-only, but any vault that happens to also be a local-write vault) block for the full fetch duration. The event loop is not directly blocked (the lock contention is in the thread pool), but the thread pool can be exhausted if enough vaults are concurrently fetching.

Note: External git vaults are read-only by access control, so for mirror vaults specifically the local commit functions are never called. However, the shared `_VAULT_LOCKS` dictionary and thread pool are process-wide. If a future code path calls `commit_file` on a mirror vault (e.g., during a write inadvertently bypassing access control), or if a non-mirror vault's name collides with a mirror vault name (impossible today but the lock map has no vault-type awareness), the starvation would be silent.

The more immediate issue: `fetch_remote` holds the lock across network I/O with no asynchronous handoff. If the network hangs (kill_after_timeout fires), the subprocess is killed but git may leave the index or packed-refs in an intermediate state before the lock releases.

**Fix direction**: Move the bulk of `fetch_remote` outside the lock; hold the lock only for the final ref-update step (which is internal and fast). Alternatively, separate the fetch lock from the worktree commit lock.

**Evidence**:
```python
# git_service.py:273-280
with _vault_lock(vault_name):                          # line 273: lock held
    repo = Repo(str(bare_path))
    authed = self._with_auth(remote_url, auth_token)
    repo.git.fetch(                                    # line 276: network I/O under lock
        authed, ..., kill_after_timeout=timeout,       # may block for minutes
    )
    return repo.git.rev_parse(...)                     # line 280
```

---

### F5: `delete()` in document_service commits git outside the DB transaction that deletes the DB row

**File**: `backend/app/services/document_service.py:680–722`

**Race scenario**:

1. `delete()` calls `asyncio.to_thread(self.git.delete_file, ...)` at line 686. Git commits a delete, returning a commit hash. HEAD now lacks the file.
2. The process dies before the DB transaction at line 696 (`pool.acquire() / conn.transaction()`) executes.
3. On restart: git HEAD has no file for this path. The `documents` row still exists, with `current_commit` pointing at the last-before-delete commit. Any call to `get()` calls `git.read_file(vault, row["path"])` which now returns `None` (line 324), resulting in an empty body returned to the caller rather than the document content. The document appears to exist in the DB but returns empty content forever.

**Observable bad state**: The document row persists with a stale commit hash, but the file is gone from git. `get()` returns a document with empty `content` field. The document cannot be deleted again via `delete()` because the FileNotFoundError handler at line 689 logs a warning and proceeds to the DB cleanup — so recovery is actually possible on the next delete attempt. However, a caller who doesn't retry sees a zombie document.

**Fix direction**: Same structural fix as F1 — the git delete and the DB row delete need to be crash-paired, ideally via an outbox pattern (record "pending git delete" in DB first, apply git deletion after DB commit, with a startup job to drain pending deletes).

**Evidence**:
```python
# document_service.py:680-697
try:
    await asyncio.to_thread(                           # line 686: git delete commits
        self.git.delete_file, vault_name=vault, ...)
except FileNotFoundError:
    logger.warning(...)

# <-- crash here leaves git HEAD with file deleted, DB row intact -->

async with pool.acquire() as conn:                     # line 696: DB TX begins AFTER git
    async with conn.transaction():
        ...
        await doc_repo.delete(pg_doc_id, conn=conn)   # line 722
```

---

### F6: `collection_service.delete()` calls `delete_paths_bulk` inside a live DB transaction — deadlock risk and git-outside-TX

**File**: `backend/app/services/collection_service.py:223–283`

**Race scenario (deadlock)**:

1. The collection delete opens a DB transaction at line 223 and acquires `FOR UPDATE` locks on collection rows (lines 228–244).
2. At line 278, still inside the DB transaction, it calls `asyncio.to_thread(self.git.delete_paths_bulk, ...)`. This dispatches to a thread-pool thread that acquires the vault's `threading.Lock`.
3. Concurrently, a user `put()` on the same vault has already acquired the vault's `threading.Lock` in a thread (its `commit_file` is in progress) and is now waiting to acquire a DB connection from the pool to call `doc_repo.create`.
4. If the DB pool is exhausted (all connections are either held by the collection delete TX or waiting for a connection), the `put()` thread is stuck holding the vault git lock while waiting for a DB connection. The collection delete TX is stuck holding DB row locks while waiting for the git lock (via its to_thread call).

This is a classic lock-ordering deadlock between the DB transaction (holding row locks) and the vault threading.Lock (held by the git commit thread). The to_thread dispatch means these two mutexes are acquired in opposite orders by different execution paths.

**Race scenario (git-outside-TX)**:

Even without deadlock, `delete_paths_bulk` is called at line 278 inside the DB transaction. If the git delete succeeds but the DB transaction subsequently rolls back (e.g., an exception in the document chunk-delete loop at line 296), the files are deleted from git but the `documents` rows persist. Git HEAD now lacks the files; the DB rows point at a commit where the files existed.

**Fix direction**: Move the `delete_paths_bulk` call outside the DB transaction (call it before opening the TX, recording the affected paths first, or after committing, with an outbox for recovery). This also eliminates the lock-ordering inversion.

**Evidence**:
```python
# collection_service.py:222-283
async with conn.transaction():                         # line 223: DB TX open, row locks held
    target_row = await conn.fetchrow(
        "... FOR UPDATE", ...)                         # row lock acquired
    ...
    await asyncio.to_thread(                           # line 278: acquires vault threading.Lock
        self.git.delete_paths_bulk, ...)               # while DB TX + row locks are HELD
```

---

### F7: `_ensure_worktree` is not idempotent under concurrent vault creation

**File**: `backend/app/services/git_service.py:75–96`

**Race scenario**:

1. A fresh vault is created. The first `commit_file` call goes through `_commit_via_clone` (the bare is empty, so `_ensure_worktree` returns None at line 92). This creates the initial branch via clone-and-push.
2. A second concurrent call to `commit_file` on the same vault (e.g., the vault-skill seed in `document_service.create_vault` at line 1033 calling `put()`, which calls `commit_file` again) acquires the vault lock at line 422.
3. `_ensure_worktree` at line 423 checks `(wt / ".git").exists()` at line 84. The worktree directory does not exist yet.
4. It successfully reads `bare_repo.head.commit` (because the first commit just landed) and calls `bare_repo.git.worktree("add", str(wt), branch_name)` at line 94 to create the worktree.
5. This is fine — but if two threads simultaneously pass the line-84 check (both see `.git` absent) and both attempt `worktree add`, the second `worktree add` will fail with "already exists" because git itself prevents duplicate worktrees. The exception bubbles as a `GitError` and kills the second commit operation.

In practice this window is very narrow (only during vault initialization, while the vault lock is held), but the vault lock is acquired *before* calling `_ensure_worktree`, so two threads trying to call `commit_file` simultaneously will serialize. The real risk is that `_ensure_worktree` is called again after the first successful worktree add if the process restarts with the worktree directory present but the in-memory `_VAULT_LOCKS` map empty — but in that case line 84 returns `True` and the method returns early. This is safe.

The actual defect is narrower: if `worktree add` crashes (SIGKILL mid-execution), the bare's `.git/worktrees/<name>` metadata directory is left partially written. The next `commit_file` call sees `(wt / ".git").exists()` returns False (the file pointer wasn't written), calls `worktree add` again, and git rejects it with "already registered" because the metadata in the bare is intact. Subsequent writes to this vault fail permanently until `git worktree prune` is run manually.

**Observable bad state**: After a SIGKILL during `worktree add`, all subsequent `commit_file` calls on the vault raise `GitError: already registered` and return 500. The vault is write-dead until manual repair.

**Fix direction**: Catch the "already registered" error from `worktree add` and fall back to verifying the worktree is usable (run `git worktree list` to confirm, then repair or remove the stale metadata and retry).

**Evidence**:
```python
# git_service.py:84-94
if (wt / ".git").exists():                             # line 84: check for wt pointer file
    return wt                                          # fast path
bare_repo = Repo(str(bare))
...
bare_repo.git.worktree("add", str(wt), branch_name)   # line 94: no crash recovery
```

---

### F8: `doc_repo.update` uses `COALESCE($7, current_commit)` — a concurrent update with NULL commit_hash silently preserves stale hash

**File**: `backend/app/repositories/document_repo.py:119–133`

**Race scenario**:

1. Caller A calls `update()` and commits a new git content. `doc_repo.update(..., commit_hash="abc123", ...)` at line 477 of `document_service.py`. The SQL at line 128 of `document_repo.py` runs `current_commit = COALESCE('abc123', current_commit)` → writes "abc123".
2. Concurrently, caller B calls a metadata-only update path that ends up calling `doc_repo.update(..., commit_hash=None, ...)`. The SQL runs `current_commit = COALESCE(NULL, current_commit)` → leaves the old value in place.

This is by design — but creates a silent ordering hazard: if A and B race, and B's `doc_repo.update` executes after A's git commit but before A's `doc_repo.update`, B's write completes first (setting `current_commit` to the pre-A hash), then A's write overwrites it with "abc123". That is the correct final state.

But if the ordering is A-git → B-db-update(NULL) → A-db-update(abc123), A wins and state is correct. If A-git → A-db-update(abc123) → B-db-update(NULL), B's COALESCE(NULL, "abc123") = "abc123" — still correct.

The dangerous case is: A-git → B-git → B-db-update(B_hash) → A-db-update(A_hash). Now `current_commit = A_hash` but git HEAD = B's commit. This is the same as F3 but expressed from the DB side. The COALESCE pattern itself is not the bug — the missing `SELECT ... FOR UPDATE` on the document row before the read-modify-write cycle is.

**Observable bad state**: `current_commit` in the DB points at A's commit, but git HEAD is B's commit. This is the exact scenario described in the bug report (A/B/C/D concurrent PUTs).

**Fix direction**: This is the same root cause as F2/F3. The COALESCE is not the issue; the issue is that the document row is not locked between the pre-check read and the git commit + DB write.

**Evidence**:
```python
# document_repo.py:128 — COALESCE is safe alone, but doesn't protect against
# concurrent row mutation between the git commit and this UPDATE
current_commit = COALESCE($7, current_commit),
```

---

### F9: `_commit_via_clone` holds the vault lock across a subprocess `git clone` + `git push`

**File**: `backend/app/services/git_service.py:422–425` and `516–556`

**Race scenario**:

`commit_file` acquires `_vault_lock(vault_name)` at line 422, then at line 425 delegates to `_commit_via_clone` when the worktree is not yet available (fresh vault). `_commit_via_clone` runs:
- `subprocess.run(["git", "clone", ...])` — spawns a subprocess that copies the entire bare repo into a temp dir (line 545)
- `work_repo.index.commit(...)` (line 554)
- `work_repo.remote("origin").push()` (line 555) — a second subprocess that pushes back to the bare

All of this happens while `_vault_lock` is held. This is correct for preventing concurrent worktree access, but it means the vault lock is held for the duration of two subprocess calls. If the vault is large (or the filesystem is slow), all other operations on this vault block for the duration.

More critically: `subprocess.run` does not propagate Python's `asyncio` cancellation. If the calling asyncio task is cancelled (HTTP timeout, client disconnect), the subprocess continues running, but the `asyncio.to_thread` future returns immediately. The lock is released by the destructor of the `with` block when the thread eventually returns — but if the subprocess is still running in the background (orphaned), a second `commit_file` call can acquire the lock, run its own commit, and then the orphaned subprocess finishes its `push()` on top of the second commit, corrupting the branch.

**Observable bad state**: Two commits for different content land on the branch in undefined order, with the DB recording whichever one its caller happened to write to `documents.current_commit`.

**Fix direction**: Replace `subprocess.run` (blocking, not cancellation-safe) in `_commit_via_clone` with a timeout-aware call, and ensure the subprocess is waited on before releasing the vault lock. Since this path is only used for the very first commit, prefer migrating to a direct `git hash-object` + `git update-ref` plumbing approach that never needs a clone.

**Evidence**:
```python
# git_service.py:544-556 — two uninterruptible subprocess calls under vault lock
with tempfile.TemporaryDirectory(...) as tmp:
    subprocess.run(["git", "clone", ...], check=True, ...)   # line 545: blocks
    work_repo = Repo(tmp)
    ...
    work_repo.remote("origin").push()                         # line 555: blocks again
```

---

## Essential Files

- `backend/app/services/git_service.py` — vault lock, worktree management, all git write primitives
- `backend/app/services/document_service.py` — all git-then-DB sequences (put/update/edit/delete, create_vault)
- `backend/app/repositories/document_repo.py` — `create`, `update`, `upsert_external` — the DB side of every commit-hash write
- `backend/app/services/collection_service.py` — `delete()` method: git call inside live DB transaction (F6)
- `backend/app/services/external_git_service.py` — `reconcile` / `fetch_remote` interaction with the vault lock
