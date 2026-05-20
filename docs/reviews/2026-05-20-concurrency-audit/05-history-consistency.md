# History / Diff / Snapshot Read Consistency — Findings

## Summary

Eight concrete defects were found across the `?commit=` historical read path, `akb_history`/`akb_diff`/`akb_activity`, and the publication snapshot surface. The most severe issues are: (1) `get_at_commit` mixing current-DB metadata with historical-Git body, producing internally inconsistent responses with no indication to the caller; (2) unvalidated `commit` / `version` parameters that accept any git object reference, enabling cross-vault object reads; (3) path-based Git history producing mixed-lineage results after delete-and-recreate; (4) publication `resolve_document_publication` reading HEAD without pinning a commit while the publication row carries no snapshot commit field.

---

## Findings

### F1: `get_at_commit` mixes historical body with current-DB metadata — Severity: HIGH

**Surface**: `GET /documents/{vault}/{id}?version=<commit>` and `akb_get` with `version=` param

**File**: `backend/app/services/document_service.py:347-402`

**Scenario**: A user opens a document at commit `A` where title was "Design v1" and type was "spec". Between commit `A` and now, the document was updated: title became "Design v2", type changed to "decision". The caller requests `?version=A`.

**Observable bad state**: The response returns `content` from git at commit `A` (correct old body) but `title="Design v2"`, `type="decision"`, summary/tags/status/domain, `created_at`, `updated_at` from the current PG row. `current_commit` is set to `version`, falsely suggesting all fields reflect that commit. The "HISTORICAL VIEW" banner says "Viewing version X" while the title block shows present-day metadata.

**Fix direction**: Either (a) parse frontmatter from the historical blob and return those fields instead of the PG row's fields, or (b) add a `metadata_is_current=True` warning field in the response.

**Evidence**:
- `document_service.py:355-363` fetches `row` from PG (current state)
- `document_service.py:365-369` fetches body from git at historical `version`
- `document_service.py:391-401` builds `DocumentResponse` mixing PG fields with historical content
- Comment at line 352-354: "The metadata ... is read from the current PG row — historical metadata is not tracked here."

---

### F2: Unvalidated `?version=`/`commit=` accepts any git ref — Severity: HIGH

**Surface**: `GET /documents/{vault}/{id}?version=<hash>` and `akb_diff` `commit=` param

**File**: `backend/app/services/git_service.py:344-356`, `639-676`

**Scenario**: `repo.commit(commit)` in GitPython accepts ANY string git can resolve — symbolic refs like `HEAD~5`, `HEAD^`, `refs/heads/main`, `FETCH_HEAD`, `refs/stash`. There is no format validation.

**Observable bad state**: A user passes `?version=HEAD~5` and reads file content from five commits ago. `?version=refs/stash` reads a stashed work-in-progress.

**Fix direction**: Validate `version`/`commit` against `^[0-9a-f]{7,64}$` before passing to `repo.commit()`. Reject symbolic refs. Also validate the resolved commit is reachable from the vault's own HEAD.

**Evidence**:
- `git_service.py:348`: `ref = repo.commit(commit) if commit else repo.head.commit` — no validation
- `git_service.py:645`: `commit = repo.commit(commit_hash)` — no validation in `file_diff`
- `git_service.py:681`: `base = repo.commit(from_commit)` — same in `diff()`
- `documents.py:90-91`: `version: str | None = None` passed directly to `get_at_commit`

---

### F3: Path-based Git history returns mixed-lineage content after delete-and-recreate — Severity: HIGH

**Surface**: `akb_history`, `akb_diff`, History tab in document UI

**File**: `backend/app/services/git_service.py:560-576` (`file_log`), `backend/mcp_server/server.py:777-787` (`_handle_history`)

**Scenario**: Document "Alpha" is created at `specs/alpha.md` (commits C1, C2, C3). It is deleted (C4). A new unrelated document "Beta" is later created at the same path (C5, C6). User opens "Beta" → History tab.

**Observable bad state**: `file_log` calls `repo.iter_commits(paths="specs/alpha.md")` which returns C1, C2, C3, C5, C6. Clicking C1 reads "Alpha"'s body and displays it under "Beta"'s title (F1 interaction).

**Fix direction**: Filter `file_log` results to commits that occurred after the current DB row's `created_at`. Compare each commit's date in `_handle_history`.

**Evidence**:
- `git_service.py:563-564`: `repo.iter_commits(paths=file_path, max_count=max_count)` — no date boundary
- `server.py:786`: `history = git.file_log(vault, doc["path"], ...)` — no created_at filter

---

### F4: `akb_activity` serves mixed-path commits as document `?commit=` links — Severity: MED

**Surface**: `vault-activity.tsx` link construction; `GET /activity/{vault}`

**File**: `frontend/src/pages/vault-activity.tsx:138-140`, `backend/app/api/routes/sessions.py:40`

**Scenario**: Activity page lists all vault commits. Each commit's link is built from the first changed file path with `?commit=<hash>`. `vault_log` returns commits for ALL files, including `.vault.yaml`, `_tables/`, and since-deleted documents.

**Observable bad state**: Clicking a commit that touched a deleted doc's path sends `GET /documents/{vault}/{deleted}?version=...` → 404. Commits touching `.vault.yaml` route to non-document URLs.

**Fix direction**: Filter `changed_files` to only paths matching known documents (join against PG `documents`), or mark non-doc links as non-navigable.

---

### F5: `resolve_document_publication` reads HEAD, no pinned commit — Severity: MED

**Surface**: `GET /api/v1/public/p/<slug>` for `resource_type=document`, `mode=live`

**File**: `backend/app/services/publication_service.py:654-657`

**Scenario**: User publishes document "Policy v1" at slug `abc123`. Document is updated three times. Reader visits `/p/abc123`.

**Observable bad state**: `resolve_document_publication` reads HEAD (no `commit=`). No `snapshot_commit` column exists in `publications` for documents — only `table_query` publications can be pinned via `snapshot_s3_key`.

**Fix direction**: Add `snapshot_commit` column to `publications`. Populate from `documents.current_commit` at publish time. Pass `commit=publication["snapshot_commit"]` when `mode=="snapshot"`.

**Evidence**:
- `publication_service.py:654-657`: HEAD read with no commit pin
- `publication_service.py:904-905`: snapshot guard checks `snapshot_s3_key` only

---

### F6: `akb_diff` `unchanged` vs `not_present` ambiguity — Severity: MED

**Surface**: `akb_diff` MCP tool
**File**: `backend/mcp_server/server.py:367-376`, `backend/app/services/git_service.py:674-676`

**Scenario**: Caller passes a commit hash for a commit where the file was never touched. `file_diff` returns `{"type": "unchanged", "diff": ""}` with no distinction between "file existed but unchanged" and "file did not exist at this commit".

**Fix direction**: After `diffs` is exhausted without a match, check `file_path` existence in `commit.tree`. If absent, return `{"type": "not_present"}` instead of `"unchanged"`.

---

### F7: `emit_event` inside chunk/relation TX but outside git commit — Severity: MED

**Surface**: `document.put`, `document.update` events; activity log consistency
**File**: `backend/app/services/document_service.py:276-301` (put), `489-531` (update)

**Scenario**: Git commit lands before any DB transaction. If `doc_repo.create` fails (e.g., race past the pre-check causing unique violation), the git commit has already landed. Activity log shows the commit, file exists in git, but no PG row. Activity link → 404.

**Fix direction**: Reorder so git write is last durable operation. Write to git AFTER DB INSERT succeeds, accepting a narrow window where PG has the row but git doesn't (easier to reconcile via background job).

---

### F8: History `?commit=` 12-char short hash no error handling on bad ref — Severity: LOW

**Surface**: HistoryList clickable rows → document `?commit=` URL → `GET /documents/{vault}/{id}?version=<hash>`
**File**: `backend/app/services/git_service.py:344-356`, `568-575`

**Scenario**: `file_log` returns `c.hexsha[:12]`. Front-end displays 7 chars but stores 12. No catch for `git.exc.BadName` or `BadObject` in `read_file`'s `repo.commit(commit)` call — invalid hash propagates as 500 instead of 404.

**Fix direction**: Wrap `repo.commit(commit)` in `read_file` and `file_diff` with `try/except (git.exc.BadName, git.exc.BadObject, ValueError)` returning `None` / raising `NotFoundError`.

---

## Files

- `backend/app/services/document_service.py` — `get_at_commit` (F1, F7)
- `backend/app/services/git_service.py` — `read_file` (F2, F8), `file_log` (F3, F8), `vault_log` (F4), `file_diff` (F6)
- `backend/app/api/routes/documents.py` — `version` param (F2)
- `backend/app/api/routes/sessions.py` — activity (F4)
- `backend/mcp_server/server.py` — `_handle_history` (F3), `_handle_diff` (F6), `_handle_activity` (F4)
- `backend/app/services/publication_service.py` — `resolve_document_publication` (F5)
- `frontend/src/pages/vault-activity.tsx` — commit link (F4)
- `frontend/src/pages/document.tsx`, `frontend/src/components/history-list.tsx`
