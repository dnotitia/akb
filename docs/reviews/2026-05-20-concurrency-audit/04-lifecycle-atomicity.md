# Vault / Collection / File Lifecycle — Findings

## Summary

Nine concrete defects were found spanning vault creation, vault deletion, file upload atomicity, publication snapshot integrity, and collection delete ordering. The most severe issues are: the vault create race where the TOCTOU existence check runs outside any transaction before the git bare repo is initialized; the vault delete not being transactional (S3 deletes run inside the same DB connection that also holds the cascade DELETEs, with no outbox); the publication view-count check running without a row lock; and the `get_or_create` collection helper missing a `ON CONFLICT` clause making it susceptible to lost-insert under concurrent puts.

---

## Findings

### F1: Vault Create — TOCTOU Race on Name Check — Severity: HIGH

**Surface**: `akb_create_vault`
**File**: `backend/app/services/document_service.py:946-993`

**Scenario**: Two concurrent requests arrive for `create_vault("my-vault")`. Both execute `vault_repo.get_by_name(name)` (line 946) and both see `None`. Both proceed past the `ConflictError` guard. The first runs `git.init_vault(name)` (line 993), then `vault_repo.create(...)` (line 1004). The second then runs `git.init_vault(name)` and crashes with `FileExistsError`. Control jumps to the `except BaseException` block, which calls `cleanup_vault_dirs(name)` — this deletes the bare repo and worktree that the first request just successfully created.

**Observable bad state**: The vault exists in the DB (first request committed) but its bare repo and worktree have been deleted. Subsequent write operations fail with `FileNotFoundError`. The vault is permanently corrupted.

**Fix direction**: Use DB-level advisory lock keyed on the vault name hash, or `INSERT INTO vaults ... ON CONFLICT (name) DO NOTHING RETURNING id` and treat NULL return as duplicate. Move `init_vault` to after the DB row is confirmed.

**Evidence**: `vault_repo.get_by_name` (line 946) is a plain SELECT, not in a transaction with no `FOR UPDATE`. The `cleanup_vault_dirs` rollback (line 1036) does not check whether another process already owns the directory.

---

### F2: Vault Delete — S3 Deletions Run Inside DB Connection Without Outbox — Severity: HIGH

**Surface**: `akb_delete_vault`
**File**: `backend/app/services/access_service.py:608-649`

**Scenario**: `delete_vault` issues `s3_adapter.delete()` calls (line 622) and DB DELETE statements on the same connection but NOT inside a `conn.transaction()` block. If S3 deletes succeed but a DB DELETE later fails, S3 objects are gone but DB rows remain (downloads → 404). Conversely a crash after DB deletes but before S3 deletes orphans S3 objects with no cleanup path.

**Observable bad state**: (a) S3 deleted but DB rows remain → presigned URLs 404. (b) DB rows deleted but S3 remains → orphan S3 billing.

**Fix direction**: Wrap entire cascade in `async with conn.transaction()`. Use the `s3_delete_outbox` pattern (already used by `file_service.delete()`) — enqueue keys inside the TX, let `s3_delete_worker` drain them after commit.

**Evidence**: Lines 608–649 in `access_service.py`. Compare with `file_service.delete()` which uses `_enqueue_s3_delete(conn, ...)` inside a transaction.

---

### F3: Vault Delete — Git Cleanup Races With In-Flight Writes — Severity: HIGH

**Surface**: `akb_delete_vault` concurrent with `akb_put`
**File**: `backend/app/services/access_service.py:649`, `backend/app/services/git_service.py:422`

**Scenario**: After DB cascade, `delete_vault` calls `cleanup_vault_dirs(vault_name)` (line 649) which `shutil.rmtree`s the bare repo and worktree. If a concurrent `akb_put` holds the per-vault `threading.Lock` mid-`commit_file`, the worktree is deleted out from under it. The lock object in `_VAULT_LOCKS` is never evicted, so a recreated vault inherits the stale lock.

**Observable bad state**: In-flight writes crash with `FileNotFoundError`. Stale lock objects pollute the in-memory dict.

**Fix direction**: Acquire the vault's `_vault_lock` before calling `cleanup_vault_dirs`. After cleanup, evict the lock from `_VAULT_LOCKS` under `_VAULT_LOCKS_GUARD`.

---

### F4: Collection Delete — Git Commit Inside Open PG Transaction — Severity: MED

**Surface**: `akb_delete_collection` with `recursive=True`
**File**: `backend/app/services/collection_service.py:222-283`

**Scenario**: Inside `async with conn.transaction()` holding `FOR UPDATE` row locks, line 278 calls `await asyncio.to_thread(self.git.delete_paths_bulk, ...)`. Git operation can take seconds on large collections; throughout, PG row locks held. Concurrent `akb_put` calls that touch `get_or_create` for the same prefix block on the PG row lock for the full duration of git I/O. Under load this can exhaust the connection pool.

**Observable bad state**: Connection pool starvation under concurrent write load.

**Fix direction**: Dispatch git operations outside the PG transaction. Snapshot doc paths inside the TX, release, do git commit, re-acquire transaction for the DB deletes.

---

### F5: `CollectionRepository.get_or_create` — SELECT-then-INSERT Race — Severity: MED

**Surface**: Any `akb_put` / `akb_put_file` specifying a new collection
**File**: `backend/app/repositories/document_repo.py:312-330`

**Scenario**: Two concurrent puts target the same new collection. Both SELECT, see nothing, both INSERT. One raises `UniqueViolationError` which propagates as 500. The git commit has already landed before the PG insert in the document put flow.

**Observable bad state**: Concurrent puts to a new collection fail with 500 even though the operation is logically valid.

**Fix direction**: Replace SELECT + INSERT with `INSERT ... ON CONFLICT (vault_id, path) DO NOTHING RETURNING id` followed by SELECT to recover the id on conflict. Mirror `create_empty` (line 385–388).

**Evidence**:
```python
# document_repo.py:322-325 — plain INSERT, no ON CONFLICT
await c.execute(
    "INSERT INTO collections (id, vault_id, path, name) VALUES ($1, $2, $3, $4)",
    cid, vault_id, path, name,
)
```

---

### F6: File Upload — Orphan DB Row if Presigned PUT Succeeds but Confirm Never Called — Severity: MED

**Surface**: `akb_put_file` proxy flow
**File**: `backend/app/services/file_service.py:137-196`, `packages/akb-mcp-client/lib/proxy.mjs:346-390`

**Scenario**: Proxy calls `/upload` (DB row inserted with `size_bytes=0`), uploads to S3 directly, then calls `/confirm`. If the proxy is killed after the S3 upload but before `/confirm`, a 0-byte phantom row persists. `confirm_upload` self-heals only when called explicitly; there is no sweeper.

**Observable bad state**: 0-byte file entries visible in `akb_browse` / `list_files` that cannot be downloaded.

**Fix direction**: Background sweeper deletes `vault_files` rows where `size_bytes = 0 AND created_at < NOW() - INTERVAL '1 hour'`. Or defer DB insert to confirm-time after verifying S3.

---

### F7: Publication View-Count Limit — TOCTOU Race — Severity: MED

**Surface**: `akb_publish` / public viewer under concurrent access
**File**: `backend/app/services/publication_service.py:570-604`

**Scenario**: With `max_views=1`, two concurrent reads both see `view_count=0`, both pass the limit check, both serve content, both increment. Final `view_count=2`, exceeding `max_views=1`.

**Observable bad state**: `max_views=1` publications can serve content to multiple viewers.

**Fix direction**: Single atomic `UPDATE publications SET view_count = view_count + 1 WHERE id = $1 AND (max_views IS NULL OR view_count < max_views) RETURNING ...`. If 0 rows updated, treat as limit exceeded.

**Evidence**: Lines 570–604: separate `fetchrow` and `execute` with no row lock between.

---

### F8: Publication Snapshot — S3 Write and DB Mode Flip Not Atomic — Severity: LOW

**Surface**: `akb_publication_snapshot`
**File**: `backend/app/services/publication_service.py:977-1032`

**Scenario**: S3 PUT (line 1009) then DB UPDATE (line 1019). A crash between leaves an orphan S3 object (keyed by `snapshots/{publication_id}.json`) with no outbox cleanup.

**Observable bad state**: Orphan snapshot objects in S3 accumulate after crashes.

**Fix direction**: Track old snapshot keys in `s3_delete_outbox` before overwriting; or DB-update-first with a tentative key promoted after S3 confirms.

---

### F9: Vault Archive — Archived Check Doesn't Block Admin Role Mutations — Severity: LOW

**Surface**: `akb_archive_vault` followed by `akb_grant`/`akb_revoke`
**File**: `backend/app/services/access_service.py:59-61`

**Scenario**: `check_vault_access` only raises `ForbiddenError` for `required_role == "writer"` when vault is archived. `grant_access`/`revoke_access` use `required_role="admin"` and bypass the archived guard. An admin can still mutate ACLs (including removing other admins/owner) on an archived vault.

**Fix direction**: Extend archived check to also block `admin` role mutations (grant/revoke), while still permitting `owner`-level operations (delete, transfer) that must work on archived vaults.

**Evidence**:
```python
# access_service.py:60
if vault["status"] == "archived" and required_role in ("writer",):
```

---

## File Reference Summary

- `backend/app/services/document_service.py` — `create_vault` (F1)
- `backend/app/services/access_service.py` — `delete_vault`, `check_vault_access` (F2, F3, F9)
- `backend/app/services/git_service.py` — `_vault_lock`, `cleanup_vault_dirs` (F3)
- `backend/app/services/collection_service.py` — `delete` (F4)
- `backend/app/repositories/document_repo.py` — `CollectionRepository.get_or_create` (F5)
- `backend/app/services/file_service.py`, `packages/akb-mcp-client/lib/proxy.mjs` — file upload (F6)
- `backend/app/services/publication_service.py` — `resolve_publication`, `create_snapshot` (F7, F8)
- `backend/app/services/s3_delete_worker.py` — outbox pattern reference
