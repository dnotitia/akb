# AKB Concurrency & Atomicity Audit — Executive Summary

**Date**: 2026-05-20
**Scope**: Backend write paths, indexing pipeline, vault/file lifecycle, history reads, access control
**Method**: 6 parallel domain-specialist agents reading source code and surfacing line-level evidence

---

## Headline

The codebase has a consistent structural defect: **git mutations execute outside the DB transaction that records them, and many read-then-write sequences have no row-level locking**. The user-reported A/B/C/D PUT race is one instance of this family; we identified **20+ additional sibling defects** across 6 domains. The common root cause appears in ~6 distinct patterns, each fixable with a small number of focused changes.

---

## Theme map (root causes → findings)

### Theme A — "Git first, DB second, no shared transaction"
A git commit lands before the DB row is written/updated. Crash window leaves git HEAD ahead of DB; in race conditions DB and HEAD attribute to different writers.

- **01-git-write-path** F1 (put/update/edit), F5 (delete)
- **02-document-write-races** F1 (PUT pre-check), F4 (split TX), F5 (delete-recreate), F6 (edit 3 TX)
- **04-lifecycle-atomicity** F1 (vault create), F2 (vault delete)
- **05-history-consistency** F7 (event vs git ordering)

### Theme B — "Check-then-act with no row lock" (TOCTOU)
A SELECT (or pre-check) followed by an INSERT/UPDATE with no `FOR UPDATE` and no advisory lock. Includes the headline PUT race plus ACL and publication races.

- **01-git-write-path** F2 (PUT), F3 (UPDATE), F8 (COALESCE OCC gap)
- **02-document-write-races** F1, F2 (no OCC on update), F3 (no base_commit on edit), F7 (collection get_or_create)
- **04-lifecycle-atomicity** F1 (vault create), F5 (collection get_or_create), F7 (max_views)
- **06-access-control** F2 (TOCTOU on permission), F7 (PAT last_used vs revoke), F8 (view_count)

### Theme C — "Outbox / atomic-coupling bypassed"
A DELETE on PG happens but the matching outbox INSERT either fails silently or runs on a different connection.

- **03-indexing-pipeline** F1 (silent enqueue_source_deletes swallow), F8 (conn=None bypass)
- **04-lifecycle-atomicity** F2 (vault delete no outbox), F8 (snapshot S3 orphan)

### Theme D — "Long-held lock starves liveness"
A coarse-grained lock (or a TX holding row locks) is kept across slow I/O.

- **01-git-write-path** F4 (fetch_remote under vault lock), F6 (collection delete deadlock risk), F9 (subprocess clone+push under lock)
- **04-lifecycle-atomicity** F4 (collection delete TX over git I/O)

### Theme E — "Path-based git history ≠ document identity"
Git is path-keyed; AKB has stable document UUIDs. After delete-recreate at the same path, history/diff/activity mix lineages.

- **05-history-consistency** F1 (metadata/body mismatch), F3 (mixed-lineage history), F4 (activity link rot), F8 (no BadName handling)

### Theme F — "Worker / shutdown / crash recovery gaps"
The pipeline assumes graceful shutdown and no crashes between cooperative steps.

- **01-git-write-path** F7 (worktree add SIGKILL → write-dead vault), F9 (orphaned subprocess)
- **03-indexing-pipeline** F2 (edit without TX), F4 (delete-during-index orphan), F7 (5s shutdown timeout), F6 (recompute_stats no cross-pod lock)

### Theme G — "ACL / audit gaps"
Inputs not validated against authorization context; mutations leave no trail.

- **05-history-consistency** F2 (unvalidated ?version=), F5 (no snapshot_commit)
- **06-access-control** F1 (transfer 3 writes no TX), F3 (no JWT revocation), F4 (session start), F5 (recent vs public_access), F6 (grep ACL bypass on None), F9 (no audit events)

---

## Top 10 to fix first (severity × likelihood × blast radius)

| # | Finding | Report | Severity | Why first |
|---|---------|--------|----------|-----------|
| 1 | **PUT/UPDATE/EDIT path-level lock** — concurrent writes diverge git↔DB | 01-F1/F2/F3, 02-F1/F2/F3 | HIGH | The user's reported bug + dominant root cause for Theme A & B |
| 2 | **Silent outbox enqueue swallow** — orphan vector rows forever | 03-F1 | HIGH | `try/except` hides the failure; permanent search-index drift |
| 3 | **Vault delete no transaction + cleanup races writers** | 04-F2, 04-F3, 01-F7 | HIGH | Can leave vault write-dead; S3 + DB diverge |
| 4 | **edit() writes chunks without transaction** | 02-F6, 03-F2 | HIGH | Crash mid-edit → zero-chunk document, silently invisible to search |
| 5 | **Delete-during-index orphan in vector store** | 03-F4 | HIGH | Stale hits in search after delete |
| 6 | **transfer_ownership no transaction** | 06-F1 | HIGH | Partial transfer locks legitimate owner out |
| 7 | **TOCTOU on every ACL-gated mutation** | 06-F2 | HIGH | Revoked admins can still mutate; needs `FOR UPDATE` pattern |
| 8 | **History at commit mixes metadata + body** | 05-F1 | HIGH | UI shows internally contradictory data; user-facing |
| 9 | **`?version=` accepts any git ref** | 05-F2 | HIGH | Trust-boundary input; users can read refs/stash, HEAD~N |
| 10 | **Path-based history after delete-recreate leaks** | 05-F3 | HIGH | Different document's body shown as another doc's history |

---

## Recommended sequencing

### Phase 1 — Foundation locking primitive (1 PR, foundational)
Add a `(vault_id, path)` advisory lock helper: a transaction-scoped `SELECT pg_advisory_xact_lock(hashtext(vault_id::text || '::' || path))` wrapped in a context manager. Acquire it at the very top of put/update/edit/delete, BEFORE any git read. Release implicitly via TX commit/rollback. This fixes Theme A+B for the document layer in one stroke.

### Phase 2 — Outbox & transaction discipline (2-3 PRs)
- Remove the silent `try/except` around `enqueue_source_deletes` (03-F1)
- Wrap `edit()` chunk/relation writes in one transaction matching `update()` (02-F6, 03-F2)
- Wrap `transfer_ownership` and `delete_vault` in transactions (06-F1, 04-F2)
- Apply `s3_delete_outbox` pattern to `delete_vault` (04-F2)

### Phase 3 — OCC for update/edit (1 PR)
- Add optional `expected_commit` to `DocumentUpdateRequest`
- Add required-or-optional `base_commit` to `akb_edit` schema
- Compare in service layer before commit; return 409 on mismatch (02-F2, 02-F3)

### Phase 4 — History/diff correctness (2 PRs)
- Validate `?version=` / `commit=` against `^[0-9a-f]{7,64}$`; reject symbolic refs; verify reachability (05-F2)
- Filter `file_log` by `created_at` boundary to fix delete-recreate lineage mixing (05-F3)
- Add `snapshot_commit` column to `publications` for document mode (05-F5)
- Parse historical frontmatter for `get_at_commit` metadata, OR add `metadata_is_current` warning (05-F1)

### Phase 5 — Access control hardening (1 PR)
- Add `check_vault_access(reader)` to `POST /sessions/start` (06-F4)
- Add `OR v.public_access IN (...)` to `/recent` (06-F5)
- Validate `user_id` not None in `grep` (06-F6)
- Combine PAT fetch+UPDATE into single statement (06-F7)
- Extend `archived` block to admin role mutations (04-F9)
- Emit audit events for grant/revoke/transfer/archive (06-F9)

### Phase 6 — Worker / shutdown robustness (1 PR)
- Increase `_backfill.stop` timeout; shield in-flight chunk transactions (03-F7)
- Re-check chunk exists before upsert in embed_worker (03-F4)
- Add advisory lock to `recompute_stats` (03-F6)
- Recover from `git worktree add` partial-write state (01-F7)

### Phase 7 — Long-held lock cleanup (1 PR)
- Move `fetch_remote` network I/O outside vault lock (01-F4)
- Move `delete_paths_bulk` call out of collection delete TX (04-F4)
- Replace `_commit_via_clone` subprocess with cancellation-safe plumbing (01-F9)

### Phase 8 — Driver-specific (1 PR)
- Fix Seahorse PK to use chunk UUID, OR delete-then-upsert on re-index (03-F3)

---

## Reproducibility / verification harness

The audit produced 50+ specific race scenarios with file:line evidence. We recommend a new test suite `backend/tests/test_concurrency_e2e.sh` (or pytest) that drives each scenario via two concurrent HTTP clients and asserts the post-state. Suggested scenarios to seed it:

1. Two PUTs to same `(vault, path)` → assert git HEAD matches `documents.current_commit`
2. PUT then immediate DELETE same path × N → assert no orphan blobs reachable from any ref
3. UPDATE with stale `expected_commit` → assert 409
4. `akb_edit` while concurrent `akb_update` → assert one of them fails, no lost update
5. `delete_vault` while a writer is mid-PUT → assert vault is either fully present or fully removed (no zombie)
6. `transfer_ownership` interrupted (use a fault-injection hook) → assert recovery is well-defined
7. JWT issued, user demoted → assert next admin call denied (requires F3 fix first)
8. `?version=HEAD~5` against a vault → assert 400 (after F2 fix)
9. Delete-then-recreate at same path → `akb_history` shows only post-recreate commits (after F3 fix)
10. Publication `max_views=1` with 10 concurrent reads → assert exactly 1 succeeds (after F8 fix)

---

## Files audited (essential)

### Services
- `backend/app/services/git_service.py`
- `backend/app/services/document_service.py`
- `backend/app/services/access_service.py`
- `backend/app/services/auth_service.py`
- `backend/app/services/collection_service.py`
- `backend/app/services/file_service.py`
- `backend/app/services/publication_service.py`
- `backend/app/services/search_service.py`
- `backend/app/services/index_service.py`
- `backend/app/services/embed_worker.py`
- `backend/app/services/delete_worker.py`
- `backend/app/services/_backfill.py`
- `backend/app/services/sparse_encoder.py`
- `backend/app/services/s3_delete_worker.py`
- `backend/app/services/vector_store/{pgvector,qdrant,seahorse,base,factory}.py`
- `backend/app/services/external_git_service.py`

### Routes & deps
- `backend/app/api/routes/{documents,access,auth,collections,files,sessions,search,public}.py`
- `backend/app/api/deps.py`
- `backend/mcp_server/server.py`, `tools.py`

### Repos & schema
- `backend/app/repositories/{document_repo,vault_repo,vault_files_repo,events_repo}.py`
- `backend/app/db/init.sql`

### Frontend (history surfaces only)
- `frontend/src/pages/vault-activity.tsx`
- `frontend/src/pages/document.tsx`
- `frontend/src/components/history-list.tsx`

### Reports (this audit)
- `01-git-write-path.md` — 9 findings
- `02-document-write-races.md` — 10 findings
- `03-indexing-pipeline.md` — 9 findings
- `04-lifecycle-atomicity.md` — 9 findings
- `05-history-consistency.md` — 8 findings
- `06-access-control.md` — 9 findings
- **Total: 54 findings** (de-duped: ~45 unique defects across themes)
