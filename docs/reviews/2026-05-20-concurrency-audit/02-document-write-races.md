# Document Write-Path Races — Findings

## Summary

The known PUT race (concurrent writers all pass the pre-check, all commit to git, only one DB row survives the unique constraint) is confirmed. Beyond that, nine additional defects were found across the update, edit, delete, re-create, and batch-replace paths. The dominant pattern is a read-then-write sequence with no locking between the read and the write: git is mutated first, then DB, and there is no per-(vault, path) reservation at any layer. The second dominant pattern is split transactions: git commit, then one standalone DB UPDATE, then a separate DB transaction for chunks/events, with multiple windows for partial failure or concurrent interference.

---

## Findings

### F1: PUT check-then-act race on (vault_id, path) — Severity: HIGH

**Operation**: PUT (create)

**File**: `backend/app/services/document_service.py:229–261`

**Race scenario**:

1. Requests A and B arrive for the same `(vault, collection, title)`, producing identical `file_path`.
2. Both call `doc_repo.find_by_path(vault_id, file_path)` at line 229 — both see NULL (no existing doc).
3. Both continue to `self.git.commit_file(...)` (line 240) — the vault-level `threading.Lock` serializes the two git commits, so A commits first, B commits second. HEAD now points at B's content.
4. A wins the DB `INSERT INTO documents` (the first to arrive), B's INSERT raises `UniqueViolationError` caught at `document_repo.py:49` and re-raised as `ConflictError`.
5. Caller B gets a 409. But git HEAD holds B's content while the DB row (which won) holds A's commit hash. Any subsequent `akb_get` reads git HEAD and returns B's body while the DB row says the doc was written by A.

The comment at line 224 in `document_service.py` explicitly acknowledges this: "Concurrent puts can still race past this gate; the unique index is the final backstop and the rare partial-write that results is rolled back by a future explicit-update workflow." The workaround is manual; the race is confirmed.

**Observable bad state**: DB row has A's `current_commit`; git HEAD has B's content. `akb_get` returns B's body. The DB/git split is permanent until an explicit update overwrites it.

**Fix direction**: Acquire a `SELECT ... FOR UPDATE` on a synthetic lock row (e.g., the `vaults` row itself or a dedicated `vault_path_locks` table) keyed to `(vault_id, file_path)` before the git commit. Only release it after both the git commit and DB INSERT succeed.

**Evidence**:

```python
# document_service.py:229-231  (pre-check, no lock)
if await doc_repo.find_by_path(vault_id, file_path):
    raise ConflictError(f"Document already exists at path: {file_path}")

# document_service.py:240-245  (git commit, OUTSIDE any DB tx)
commit_hash = await asyncio.to_thread(
    self.git.commit_file,
    vault_name=req.vault, file_path=file_path, ...
)

# document_repos.py:49-52  (UniqueViolation is the backstop)
except asyncpg.UniqueViolationError as e:
    raise ConflictError(f"Document already exists at path: {path}") from e
```

---

### F2: UPDATE has no optimistic concurrency check — Severity: HIGH

**Operation**: UPDATE

**File**: `backend/app/services/document_service.py:422–537`

**Race scenario**:

1. Agent A reads document at T0 (line 437: `self.git.read_file`), reads body V0.
2. Agent B updates the same document between T0 and T1, making HEAD = V1 and `current_commit = H1`.
3. Agent A finishes composing its update based on V0, calls `self.git.commit_file` (line 469) — git accepts it, HEAD = V2 (A's changes applied on top of what git currently has), `commit_hash = H2`.
4. `doc_repo.update(pg_doc_id, ..., commit_hash=H2, ...)` (line 477) writes H2 to DB.

B's changes are silently overwritten. There is no `expected_commit` or `If-Match` parameter on `DocumentUpdateRequest` (verified in `document.py:52-65`), no `SELECT ... FOR UPDATE` on the document row before the git read, and no check that HEAD at write time matches what was read.

**Observable bad state**: Lost update — B's content is wiped from git and DB without any conflict signal. Neither caller receives an error.

**Fix direction**: Add an optional `expected_commit` field to `DocumentUpdateRequest`. If provided, verify `row["current_commit"] == expected_commit` before the git commit; reject with 409 if it does not match.

**Evidence**:

```python
# document_service.py:437-481 — read → git write → db write, no version guard
row = await doc_repo.find_by_ref(vault_id, doc_ref)    # reads current_commit
current_content = await asyncio.to_thread(self.git.read_file, vault, file_path)
# ... compose new content ...
commit_hash = await asyncio.to_thread(self.git.commit_file, ...)  # no commit check
await doc_repo.update(pg_doc_id, ..., commit_hash=commit_hash, ...)
```

---

### F3: EDIT has no base-commit guard — Severity: HIGH

**Operation**: EDIT (akb_edit / `document_service.edit`)

**File**: `backend/app/services/document_service.py:541–661`, `backend/mcp_server/tools.py:143–171`

**Race scenario**:

1. Agent A calls `akb_edit` with `old_string="foo"`. The service reads git HEAD at line 575, finds "foo" in the body, and computes the replacement.
2. Agent B concurrently calls `akb_update` on the same document, replaces "foo" with "bar" and commits. HEAD now has "bar".
3. Agent A's `self.git.commit_file` (line 615) writes A's version, which was based on the stale read. A's `old_string` check at line 585-594 already passed — the check ran against the stale read, not the version about to be committed.

The tool schema (`tools.py:143–171`) has no `base_commit` parameter. The audit comment in the codebase (`CLAUDE.md`) says "usually has a `base_commit_hash` parameter for OCC" — that parameter does not exist in the current implementation.

**Observable bad state**: A's edit commits over B's update silently. B's changes are lost. A's edit is based on content that no longer existed at commit time.

**Fix direction**: Add `base_commit: str | None` to `akb_edit` schema and to `DocumentService.edit`. After reading git HEAD, compare `row["current_commit"]` against `base_commit` if provided; reject with 409 if they differ.

**Evidence**:

```python
# document_service.py:575-600 — read, check, then replace (no version guard)
current_content = await asyncio.to_thread(self.git.read_file, vault, file_path)
# ...
occurrences = current_body.count(old_string)    # checked against stale read
# ...
commit_hash = await asyncio.to_thread(self.git.commit_file, ...)  # committed unconditionally
```

```python
# mcp_server/tools.py:150-170 — schema has no base_commit field
"properties": {
    "uri": ..., "old_string": ..., "new_string": ...,
    "replace_all": ..., "message": ...
},
"required": ["uri", "old_string", "new_string"],
```

---

### F4: UPDATE — git commit and DB UPDATE are in separate transactions — Severity: MEDIUM

**Operation**: UPDATE

**File**: `backend/app/services/document_service.py:469–531`

**Race scenario**:

1. The git commit at line 469 succeeds. HEAD is now updated.
2. `doc_repo.update(pg_doc_id, ...)` at line 477 runs on its own acquired pool connection (see `document_repo.py:118-133` — it calls `self.pool.acquire()` independently). This is NOT inside the transaction that starts at line 491.
3. If the process crashes (OOM, SIGKILL, pod restart) between line 477's commit and the start of the `async with conn.transaction()` block at line 491, the DB row's `current_commit` is updated but the chunks are NOT replaced (they still hold the old content).
4. Even without a crash: a concurrent reader between line 477 and line 491 sees an updated `current_commit` but old chunks in the search index.

**Observable bad state**: DB `current_commit` points at the new git commit but the chunk index still contains the old body. Search returns stale content for an indeterminate window. In the crash scenario, this window is permanent until the next explicit update.

**Fix direction**: Move `doc_repo.update` inside the same transaction as `write_source_chunks` and `emit_event` (lines 491-531), passing `conn` to `doc_repo.update`.

**Evidence**:

```python
# document_service.py:477-491 — update is OUTSIDE the subsequent transaction
await doc_repo.update(pg_doc_id, ..., commit_hash=commit_hash, ...)  # own connection

pool = await get_pool()
async with pool.acquire() as conn:
    async with conn.transaction():
        # ...write_source_chunks, store_document_relations, emit_event...
```

The same split applies in `edit()` at lines 623-655: `doc_repo.update` (line 623) is outside the `write_source_chunks` call (lines 638-643), and `store_document_relations` (lines 650-655) is on a *third* separate connection acquired via a second `get_pool()` call at line 649.

---

### F5: DELETE + concurrent PUT to same path — zombie git tombstone — Severity: HIGH

**Operation**: DELETE followed immediately by PUT

**File**: `backend/app/services/document_service.py:665–728`

**Race scenario**:

1. Agent A deletes document at `vault/path/foo.md`: calls `self.git.delete_file(...)` at line 687 (commits git tombstone), then acquires a pool connection and starts a DB transaction at line 697.
2. Between the git delete commit (line 687) and the DB transaction commit (line 697-722), Agent B calls PUT for the same path. B passes the pre-check at line 229 (the DB row still exists — A's TX hasn't committed yet), so B considers the path available.

Wait — actually B would FAIL the pre-check because A's `documents` row is still visible to B's `find_by_path` (A's DELETE TX is still open). So B sees a row and gets a 409.

But consider: A's git delete commits first (line 687 is BEFORE the DB TX at line 697). Now B's PUT checks DB and sees the row still there — 409. But if A's TX then aborts (DB error, timeout) after the git commit, the git file is gone but the DB row remains. The document is now in an inconsistent state: DB row exists, git file absent. The comment at line 684 acknowledges this and makes git delete idempotent.

More critically: if A succeeds fully and then B immediately PUTs to the same path, B will commit a new git blob. The new git blob is written unconditionally by `git.commit_file` regardless of what HEAD currently is. If B races past the pre-check before A's DB transaction commits, B will try to INSERT a new row and fail on the unique constraint — but the git blob is already written. Now HEAD has B's content and the DB has only A's deleted-row-being-replaced scenario. The stale blob from B lives in git history forever.

**Observable bad state**: Orphan git blobs from B's failed PUT that raced DELETE. Git object store accumulates unreferenced blobs if the race triggers repeatedly.

**Fix direction**: Hold a `(vault_id, path)` DB-level advisory lock for the duration of both the git operation and the DB transaction in both PUT and DELETE so delete and re-create cannot interleave at the git layer.

**Evidence**:

```python
# document_service.py:686-697 — git delete outside DB transaction
await asyncio.to_thread(
    self.git.delete_file, vault_name=vault, file_path=file_path, message=commit_msg,
)
# --- window here: git file deleted, DB row still exists ---
pool = await get_pool()
async with pool.acquire() as conn:
    async with conn.transaction():
        await delete_document_chunks(conn, str(pg_doc_id))
        ...
        await doc_repo.delete(pg_doc_id, conn=conn)
```

---

### F6: EDIT — chunks and relations written in two separate transactions — Severity: MEDIUM

**Operation**: EDIT

**File**: `backend/app/services/document_service.py:637–655`

**Race scenario**: After the git commit and `doc_repo.update`, the edit path writes chunks in one transaction (lines 638-643) and then relations in a second, entirely separate transaction (lines 649-655 — note the second `pool = await get_pool()` call at line 649 acquires a fresh connection). If the process crashes between the chunk commit and the relation commit, chunks will have been replaced with the new content but the edge graph will still point at old dependencies/targets.

Furthermore, these chunk + relation writes are NOT in the same transaction as `doc_repo.update` (line 623 uses its own connection). So a crash between the DB update commit and either the chunk or relation commit leaves the system in a state where `current_commit` is updated but chunks or relations are stale.

**Observable bad state**: After a crash mid-edit, the knowledge graph edges reflect the pre-edit dependency list while the chunk index reflects the post-edit body. Or the inverse: new edges but old chunks. Both are invisible to the operator.

**Fix direction**: Consolidate git commit → `doc_repo.update` → `write_source_chunks` → `store_document_relations` → `emit_event` into one DB transaction (passing `conn` to each call), matching the pattern already used in `update()` at lines 491-531.

**Evidence**:

```python
# document_service.py:637-655 — three separate connection acquisitions
async with pool.acquire() as conn:
    chunks_indexed = await write_source_chunks(conn, ...)   # TX 1

pool = await get_pool()  # NEW pool acquire at line 649!
async with pool.acquire() as conn:
    await store_document_relations(conn, ...)               # TX 2 (separate)
```

---

### F7: CollectionRepository.get_or_create — read-then-insert race — Severity: MEDIUM

**Operation**: PUT (create) — collection setup

**File**: `backend/app/repositories/document_repo.py:312–330`

**Race scenario**:

1. Two concurrent PUTs for different documents both targeting the same collection path `"api-specs"`.
2. Both call `get_or_create(vault_id, "api-specs")`.
3. Both `SELECT id FROM collections WHERE vault_id=$1 AND path=$2` — both return no row.
4. Both try `INSERT INTO collections (id, vault_id, path, name) VALUES (...)`. This INSERT has NO `ON CONFLICT DO NOTHING` clause (unlike `create_empty` at line 385 which does use ON CONFLICT).
5. One INSERT wins; the other raises `asyncpg.UniqueViolationError` (the `UNIQUE(vault_id, path)` constraint). This exception is NOT caught in `get_or_create` — it propagates up and the entire PUT fails with a 500-level error.

The `create_empty` method (line 380) correctly uses `ON CONFLICT (vault_id, path) DO NOTHING`. But `get_or_create` (line 312) uses a plain `INSERT` with no conflict handling.

**Observable bad state**: A valid concurrent PUT fails with an uncaught `UniqueViolationError` (surfaces as 500 to the caller) when two writers simultaneously target a new collection. The git commit has already landed before this error is raised (line 253 for `get_or_create` comes AFTER the git commit at line 240).

**Fix direction**: Change `get_or_create`'s INSERT to `INSERT ... ON CONFLICT (vault_id, path) DO NOTHING`, then re-query for the existing id on the conflict path (mirroring `create_empty`).

**Evidence**:

```python
# document_repo.py:322-325 — plain INSERT, no ON CONFLICT
await c.execute(
    "INSERT INTO collections (id, vault_id, path, name) VALUES ($1, $2, $3, $4)",
    cid, vault_id, path, name,
)
```

---

### F8: Vector delete outbox — re-PUT to same path can have stale outbox rows delete new chunks — Severity: MEDIUM

**Operation**: DELETE followed by PUT to same path

**File**: `backend/app/services/index_service.py:418–435`, `backend/app/services/delete_worker.py:131–156`

(See report 03 for fuller treatment — chunk UUIDs are regenerated per PUT so the primary fear does not materialize on pgvector/qdrant. Seahorse driver has a separate issue, F3 of report 03.)

**Fix direction**: Document the timing dependency; consider safeguard that skips outbox entries where the chunk_id no longer exists in the chunks table.

---

### F9: akb_grep replace — batch update is non-atomic, partial failure undetected — Severity: MEDIUM

**Operation**: Batch update via `akb_grep` with `replace=`

**File**: `backend/app/services/search_service.py:611–651`

**Race scenario**:

The grep+replace path iterates `result_docs` and calls `doc_service.update(doc_vault, doc_path, req)` for each document sequentially (line 645). There is no enclosing transaction and no rollback on partial failure. The loop:

1. Replaces doc 1 successfully (new git commit + DB update).
2. Replaces doc 2: network error mid-git-commit → exception is NOT caught here (the `except Exception` is inside the individual `doc_service.get` call at line 621, but not around the `doc_service.update` at line 645). The exception propagates and the entire HTTP response fails.
3. Docs 3..N are never processed.

Result: doc 1 was mutated, docs 2..N were not. The caller receives a 500 and has no way to distinguish which documents were updated. Furthermore, doc 1's mutation is based on a snapshot read at line 621 (`doc_service.get`) — if concurrent writes modified doc 1 between the `get` and the `update`, doc 1 gets a silently lost-update (same race as F2).

**Observable bad state**: After a grep+replace with N matching documents, 1..K documents are updated and K+1..N are not. The caller gets a 500 with no partial-success list. Repeated retries will double-replace already-replaced docs.

**Fix direction**: Wrap each `doc_service.update` call in its own try/except and accumulate errors alongside successes. Return a partial-success response (HTTP 207 or 200 with an `errors` field).

**Evidence**:

```python
# search_service.py:615-645 — no try/except around doc_service.update
for doc_info in result_docs:
    doc = await doc_service.get(doc_vault, doc_path)
    body = doc.content or ""
    new_body = ...
    req = DocumentUpdateRequest(content=new_body, ...)
    result = await doc_service.update(doc_vault, doc_path, req, agent_id=agent_id)
    # ^^^ unguarded — any exception here aborts the entire loop
```

---

### F10: Path normalization is applied inconsistently across entry points — Severity: LOW

**Operation**: PUT / UPDATE / DELETE

**File**: `backend/app/api/routes/documents.py:44`, `backend/mcp_server/server.py:257`, `backend/app/util/text.py:76`

**Defect**: The MCP `akb_edit` handler (`server.py:257`) calls `split_uri(args["uri"], expected_type="doc")` and passes the raw vault name and doc path directly to `doc_service.edit`, bypassing NFC normalization. The `DocumentPutRequest` / `DocumentUpdateRequest` inherit from `NFCModel` so the PUT/UPDATE request body is NFC-normalized on construction. But paths extracted by `split_uri` are not normalized before being passed to `find_by_ref`. If a document was created with NFD-form path and another client looks it up with NFC, `find_by_ref` (exact path equality at `document_repo.py:63`) will fail to find it. Two writers with different normalization conventions can each see the path as absent and both INSERT, violating semantic uniqueness even though byte-level `UNIQUE(vault_id, path)` doesn't catch the case.

**Observable bad state**: Two DB rows for semantically identical paths if clients send different normalizations.

**Fix direction**: Apply `to_nfc` to the doc path extracted by `split_uri` in all MCP handlers before passing to service methods.

**Evidence**:

```python
# mcp_server/server.py:257 — path extracted from URI, no NFC normalization
vault, doc_path = split_uri(args["uri"], expected_type="doc")
# document_repo.py:63 — exact byte comparison
_MATCH_WHERE = "(d.id::text = $2 OR d.path = $2)"
```

---

## Essential Files

- `backend/app/services/document_service.py` — orchestration; PUT/UPDATE/EDIT/DELETE
- `backend/app/repositories/document_repo.py` — SQL operations
- `backend/app/services/git_service.py` — per-vault threading lock
- `backend/app/services/index_service.py` — `write_source_chunks`
- `backend/app/services/embed_worker.py`, `delete_worker.py`
- `backend/app/services/search_service.py` — grep batch-replace
- `backend/app/services/collection_service.py`
- `backend/mcp_server/server.py`, `tools.py`
- `backend/app/db/init.sql` — schema
- `backend/app/models/document.py` — request models
- `backend/app/util/text.py` — NFC normalization
