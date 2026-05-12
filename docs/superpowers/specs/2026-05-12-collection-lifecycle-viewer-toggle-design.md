# Collection lifecycle, refresh, and viewer toggle — design

**Date**: 2026-05-12
**Status**: Approved (brainstorming)
**Owner**: kwoo24
**Related**: prior collections cache (`init.sql:77`), `CollectionRepository.get_or_create` (`document_repo.py:290`)

## Problem

Three UX/tool gaps surfaced in review:

1. **No way to manage collections as a unit.** The `collections` table already exists as an L1 metadata cache (`init.sql:77`) and is auto-populated by `akb_put` via `CollectionRepository.get_or_create()` (`document_repo.py:290`). But there is no `akb_create_collection`, no `akb_delete_collection`, and no UI surface for either. Users can only create a collection as a side effect of putting a document, and can only "delete" a collection by deleting its members one at a time. The DB exposes `doc_count`, `summary`, `last_updated` columns on the row, treating it as a first-class object — but the lifecycle tools to match that design were never built.

2. **No manual refresh.** The sidebar vault list and per-vault tree fetch on mount only. After a mutation (create/delete collection, document, vault, file) the cache stays stale until the user navigates away and back. There is no `⟳` button and no automatic invalidation on mutations.

3. **No raw text view for documents.** `frontend/src/pages/document.tsx:228` renders `doc.content` through `react-markdown` only. The raw markdown is already present in the response payload, but there is no toggle. Files have their own viewer; this gap is specific to documents.

## Non-goals

- **Collection rename / move.** Out of scope. Tree restructuring is a separate, larger problem (path rewrites cascade through git history, chunks, relations).
- **Real-time tree updates from other clients.** Manual refresh + post-mutation invalidate is sufficient; SSE/WebSocket is a separate feature with different infrastructure costs.
- **Side-by-side rendered+raw view.** Single-toggle only. The edit dialog already provides a writing surface; the viewer is for reading.
- **Raw view for file pages or table pages.** Scope is documents only.
- **Re-litigating `documents.collection_id ON DELETE SET NULL`.** The cascade is handled at the application layer (single transaction, explicit document deletes); no schema change.
- **Keyboard shortcut for view toggle.** Explicitly declined by user.
- **Garbage collection of empty collections.** Under the new model empty is a valid state, not a zombie. Cleanup is explicit only.

## Conceptual model (the resolved inconsistency)

The brainstorming process surfaced a tension: if collections only exist as side effects of documents (purely implicit), then "create empty collection" and "delete empty collection" are both nonsensical. If collections are first-class, then both make sense — but `akb_delete` should not auto-remove the row when the last doc goes (that would make "create empty collection" useless because deleting the only doc you ever put there would unmake your container).

**Resolved model: collections are first-class. Empty is a valid state.**

| Operation | Effect |
|---|---|
| `akb_put(collection="x")` | If `x` does not exist, create it. `doc_count++`. (Unchanged.) |
| `akb_delete(doc)` | Delete doc. `doc_count--`. **Row stays even at `doc_count == 0`.** (Unchanged — existing code already only decrements.) |
| `akb_create_collection(vault, path, summary?)` | Idempotent. Creates a row at `doc_count = 0` if absent; returns `{created: false}` if already present. (NEW.) |
| `akb_delete_collection(vault, path)` | If `doc_count == 0`: delete row. If `doc_count > 0`: reject with count. (NEW.) |
| `akb_delete_collection(vault, path, recursive=true)` | Cascade — delete all docs and files under prefix, then delete row. (NEW.) |

The symmetry: explicit create ↔ explicit delete. Implicit create (via `akb_put`) still works; there is no "implicit delete." A user who creates an empty container can refill it without surprise.

Note: `CollectionRepository.decrement_count` (`document_repo.py:326`) is already being called from `DocumentService.delete` (`document_service.py:582`). No pre-existing bug to fix in the count path — earlier brainstorming dialogue speculated otherwise; this spec corrects that.

## Architecture

```
                ┌──────────────────────────────────┐
                │  akb_create_collection           │  MCP tool (NEW)
                │  akb_delete_collection           │  MCP tool (NEW)
                └──────────────────────────────────┘
                              │
                              ▼
                ┌──────────────────────────────────┐
                │  POST   /collections/{vault}     │  REST (NEW, MCP mirror)
                │  DELETE /collections/{v}/{path}  │
                └──────────────────────────────────┘
                              │
                              ▼
                ┌──────────────────────────────────┐
                │  CollectionService               │  NEW
                │   .create(vault, path, summary)  │
                │   .delete(vault, path, recursive)│
                └──────────────────────────────────┘
                       │            │
        ┌──────────────┘            └──────────────┐
        ▼                                          ▼
┌──────────────────────┐              ┌──────────────────────────┐
│ CollectionRepository │              │ DocumentService (reused) │
│  .create_empty()     │              │  bulk-delete primitives  │
│  .delete_by_path()   │              │  • git.delete_paths_bulk │
│  .list_docs_under()  │              │  • delete_chunks (outbox)│
└──────────────────────┘              │  • file row + S3 outbox  │
        │                             └──────────────────────────┘
        └─── ON DELETE SET NULL on documents.collection_id
             (existing; harmless because cascade handles docs explicitly)
```

The frontend talks REST. MCP and REST share the same `CollectionService` to keep semantics identical across agent and UI.

## Backend — Collection service

### New file: `backend/app/services/collection_service.py`

```python
class CollectionService:
    async def create(self, *, vault: str, path: str,
                     summary: str | None, agent_id: str | None) -> dict:
        # 1. resolve vault, check writer+ permission
        # 2. normalize path (strip leading/trailing '/', reject '', '..', absolute)
        # 3. INSERT ... ON CONFLICT DO NOTHING RETURNING id
        #    if not returned -> SELECT existing, return {created: false, ...}
        # 4. emit 'collection.create' event
        # 5. no git side effect (empty collection = no commit;
        #    git has no concept of empty dirs anyway)

    async def delete(self, *, vault: str, path: str, recursive: bool,
                     agent_id: str | None) -> dict:
        # 1. resolve vault, check writer+ permission
        # 2. SELECT collection FOR UPDATE  (lock against concurrent put)
        # 3. enumerate docs (path LIKE 'path/%' OR path = 'path/...')
        #    and files (collection = path or collection LIKE 'path/%')
        # 4. if non-empty and not recursive: raise 409 with counts
        # 5. cascade path:
        #    a. git.delete_paths_bulk(paths=[...docs, ...files],
        #                              message="[delete-collection] <path>\n\n<N> docs, <M> files")
        #    b. PG txn:
        #         delete chunks for each doc (writes vector_delete_outbox)
        #         delete relations for each doc
        #         delete documents rows
        #         delete files rows  (writes s3_delete_outbox)
        #         delete collections row
        #         emit 'collection.delete' event with counts
        # 6. empty path: skip 5a/5b's doc and file deletes, just delete row + emit
```

### Path normalization rules

- Trim leading/trailing `/`.
- Reject empty string after trim.
- Reject components equal to `.` or `..`.
- Reject paths starting with `/` (absolute).
- Reject paths with NUL or control characters.
- Reject paths > 1024 bytes (sanity).
- Permit nested paths (`api/v1/specs`) — the row is the leaf; intermediate components are not separately registered.

### Repository additions: `backend/app/repositories/document_repo.py`

```python
class CollectionRepository:
    async def create_empty(self, vault_id, path, summary=None, conn=None) -> tuple[uuid.UUID, bool]:
        """Insert with ON CONFLICT DO NOTHING. Returns (id, created)."""

    async def delete_by_id(self, collection_id, conn=None) -> None: ...

    async def list_docs_under(self, vault_id, path, conn=None) -> list[dict]:
        """Returns docs where path = 'path/X' or path LIKE 'path/%/X'.
        Used by cascade to enumerate before deleting."""
```

### Bulk delete in `GitService`

New method on `GitService`:

```python
def delete_paths_bulk(self, *, vault_name: str, file_paths: list[str], message: str) -> None:
    """git rm <each path>, then a single commit. Idempotent on missing files.
    Reuses the persistent worktree + per-vault lock (existing pattern)."""
```

This matches the project's stated convention (CLAUDE.md): commits go through the persistent linked worktree, serialized by per-vault `threading.Lock`. We dispatch from async via `asyncio.to_thread`.

### Race safety

`SELECT ... FOR UPDATE` on the collection row serializes cascade against concurrent `akb_put`. A put racing a cascade either:
- Wins the lock first → its document is part of the snapshot, gets deleted by cascade. User sees doc-not-found on read-after. Acceptable.
- Loses the lock → blocks until cascade commits, then `get_or_create` re-inserts the collection row (post-cascade it's gone) → new empty collection, then doc added. User's doc survives in a fresh collection by the same name. Acceptable.

Either outcome is internally consistent. The window is narrow (held only during cascade) and the API caller sees a deterministic response per attempt.

### Partial failure

- **Git commit fails mid-cascade**: nothing committed (git has not yet seen the staged changes — `git rm` runs sequentially, then one commit). PG transaction is rolled back. No partial state.
- **PG txn fails after git commit**: caller sees error; vector outbox / s3 outbox writes are rolled back. Git is ahead of PG. Self-heal: existing reaper logic for orphan docs is per-doc; for cascade we accept that on retry the user will see the docs gone (git already removed) and the DB will be cleaned up by an idempotent retry. We document this in the tool docstring; in practice PG commit after a successful git commit is the unlikely failure.
- **Vector outbox drain fails**: existing behavior — `vector_indexer` retries from the outbox. No change.

## Backend — MCP tools

### `backend/mcp_server/tools.py`

Add two tool schemas:

```python
{
    "name": "akb_create_collection",
    "description": "Create an empty collection (folder) in a vault. Idempotent — returns existing if already present.",
    "inputSchema": {
        "type": "object",
        "properties": {
            "vault": {"type": "string"},
            "path":  {"type": "string", "description": "Collection path, e.g. 'api-specs' or 'docs/guides'"},
            "summary": {"type": "string"},
        },
        "required": ["vault", "path"],
    },
}
{
    "name": "akb_delete_collection",
    "description": "Delete a collection. If non-empty, requires recursive=true to cascade delete all docs and files inside.",
    "inputSchema": {
        "type": "object",
        "properties": {
            "vault": {"type": "string"},
            "path":  {"type": "string"},
            "recursive": {"type": "boolean", "default": False},
        },
        "required": ["vault", "path"],
    },
}
```

### `backend/mcp_server/server.py`

Add handlers `_handle_create_collection` and `_handle_delete_collection`, each registered via `@_h(...)` decorator. Both delegate to `CollectionService` and return envelopes consistent with neighbouring tools (e.g., `_handle_archive_vault`).

### `backend/mcp_server/help.py`

Add entries for the two new tools to the `_TOOL_DOCS` dict and the verb table near `akb_delete`.

## Backend — REST routes

New file: `backend/app/api/routes/collections.py`:

```
POST   /api/v1/collections/{vault}
  body: {path: str, summary?: str}
  201 Created     -> {created: true,  collection: {...}}
  200 OK          -> {created: false, collection: {...}}
  400 invalid path
  403 not writer
  404 vault not found

DELETE /api/v1/collections/{vault}/{path:path}?recursive=bool
  200 OK     -> {deleted_docs: int, deleted_files: int, collection: path}
  409 Conflict if non-empty without recursive
                 body: {doc_count: int, file_count: int}
  403 not writer
  404 vault or collection not found
```

`GET /api/v1/collections/{vault}` is already implicitly served by `akb_browse`'s top-level listing; no new endpoint needed.

## Frontend — Collection UX

### New components

- `frontend/src/components/create-collection-dialog.tsx`
  - Single input: collection path.
  - Optional summary textarea.
  - Validates path client-side (mirror server rules) before submit.
  - On 200 created=false: show "Collection already exists" inline, no error toast.

- `frontend/src/components/delete-collection-dialog.tsx`
  - Two visual modes, decided from the explorer's `countDocs(node)`:
    - **Empty mode**: "Delete empty collection 'x'?" with simple Cancel/Delete buttons.
    - **Cascade mode**: "This will permanently delete N documents and M files in 'x'. Type 'x' to confirm." Delete button disabled until input matches.
  - Cascade mode sends `recursive=true`. Empty mode omits the parameter (server treats absent and `false` identically).

### Changed components

- `frontend/src/components/vault-explorer.tsx`
  - Add `+ Collection` button to the sidebar header (top of the tree).
  - Add a small action overlay (visible on row hover or keyboard focus) on each `kind === "collection"` row with a trash icon → opens the delete dialog.
  - Action visibility gated on `vaultRole in ('writer', 'admin', 'owner')` (consistent with `document.tsx:241`).

- `frontend/src/lib/api.ts`
  - `createCollection(vault, path, summary?)` → POST
  - `deleteCollection(vault, path, recursive?)` → DELETE
  - Both surface 409 as a typed error so the dialog can route to cascade mode if the user accidentally hits empty-delete on a non-empty collection (defensive — the dialog already chooses upfront, this is just a fallback).

## Frontend — Refresh

### Hook changes

- `frontend/src/hooks/use-vault-tree.ts` — currently fetches on mount. Refactor to expose `{tree, isLoading, error, refetch}`. Implementation: extract the fetch into a `useCallback`, call from a `useEffect`, return the callback.

- Identify the vault-list hook (likely `frontend/src/hooks/use-vaults.ts` or inline in the sidebar layout). Apply the same pattern.

### UI

- Sidebar headers (vault list section + per-vault tree section) get a small `⟳` icon button. Click → `refetch()`. Spinning animation while in flight (`isLoading` from the hook).
- Keyboard: button is normal focusable; no chord shortcut.

### Auto-invalidate on mutations

Mutation call sites (create/delete vault, create/delete collection, create/update/delete document, upload/delete file) call the relevant `refetch()` on success. Plumbing:

- A lightweight `useVaultMutations()` hook that wraps the existing api functions and accepts a `{ onSuccess?: () => void }` per call site. Each caller passes `refetch` from the hook context.
- Alternatively (preferred for minimum diff): expose `refetch` from `use-vault-tree` and `use-vaults` via a small context (`VaultRefreshContext`) so mutations anywhere in the tree can `useContext(VaultRefreshContext).refetchTree()` without prop drilling.

The context approach is the simpler choice; document the contract that mutation success handlers call the appropriate `refetch`.

## Frontend — Document viewer toggle

### Single file change: `frontend/src/pages/document.tsx`

- Read view mode from `useSearchParams()` — `?view=raw` → raw, else rendered.
- Toggle button placed at the top of the article header (above the title's coord strip, inline with existing right-rail actions visually).
- Rendered branch: existing `<Markdown remarkPlugins={[remarkGfm]} components={markdownComponents}>` block unchanged.
- Raw branch:
  ```tsx
  <div className="relative">
    <button onClick={copyRaw} aria-label="Copy markdown"
            className="absolute top-2 right-2 ...">
      {copiedRaw ? <Check/> : <Copy/>} {copiedRaw ? "Copied" : "Copy"}
    </button>
    <pre className="font-mono text-[13px] leading-[1.55] whitespace-pre-wrap
                    overflow-x-auto bg-surface-muted p-4">
      {doc.content || ""}
    </pre>
  </div>
  ```
- `copyRaw` follows the existing `copyPublicLink` pattern (`document.tsx`): `navigator.clipboard.writeText(doc.content)`, then a 1.5s timer flips `copiedRaw` back to false.
- View mode persists across navigation by living in URL query, not local state.

## Error handling

| Failure | Backend response | UI behavior |
|---|---|---|
| Invalid path in create | 400 with message | inline error in dialog |
| Collection exists | 200, `{created: false}` | inline "already exists", non-blocking |
| Delete on missing collection | 404 | error toast |
| Delete non-empty without recursive | 409 with counts | dialog auto-switches to cascade mode |
| Permission denied | 403 | error toast |
| Git commit fails mid-cascade | 500, txn rolled back | error toast, tree refetched (still shows pre-cascade state) |
| Clipboard API unavailable | (n/a) | "Copy" button disabled with tooltip "Clipboard unavailable" |

## Testing

### New backend E2E: `backend/tests/test_collection_lifecycle_e2e.sh`

Coverage:
- Create empty → tree shows it via `akb_browse`.
- Create same path twice → second call returns `created: false`.
- Create with invalid path (`""`, `"../x"`, `"/abs"`) → 400.
- Delete empty → row gone, tree no longer lists.
- Put doc into existing empty collection → `doc_count = 1`.
- Delete non-empty without recursive → 409, doc still present.
- Delete with recursive → all docs + files gone, git log shows single `[delete-collection]` commit, vector outbox populated, collection row gone.
- Concurrent put + recursive delete → final state is deterministic (one of the two safe outcomes from "Race safety" above).
- Permission denied (reader role) → 403 on both create and delete.

### New backend E2E: extend `backend/tests/test_mcp_e2e.sh`

- After bulk-creating docs in a collection then deleting them via `akb_delete`, assert the collection row still exists with `doc_count = 0` and `akb_browse` shows it at top level (empty-is-valid invariant).

### Frontend tests

- `frontend/src/components/__tests__/create-collection-dialog.test.tsx` — path validation, idempotent message on `created: false`.
- `frontend/src/components/__tests__/delete-collection-dialog.test.tsx` — empty vs cascade branch rendering, type-to-confirm gate.
- `frontend/src/pages/__tests__/document-view-toggle.test.tsx` — toggle flips between `<Markdown>` and `<pre>`, copy button writes to clipboard, URL query syncs.
- `frontend/src/hooks/__tests__/use-vault-tree.test.ts` — `refetch` causes a new fetch and updates state.

## Files touched (summary)

**New (backend)**:
- `backend/app/services/collection_service.py`
- `backend/app/api/routes/collections.py`
- `backend/tests/test_collection_lifecycle_e2e.sh`

**Changed (backend)**:
- `backend/mcp_server/tools.py` — add two tool schemas
- `backend/mcp_server/server.py` — add two handlers
- `backend/mcp_server/help.py` — add two help entries
- `backend/app/repositories/document_repo.py` — `CollectionRepository.create_empty`, `delete_by_id`, `list_docs_under`
- `backend/app/services/git_service.py` — `delete_paths_bulk`
- `backend/app/main.py` (or wherever routers are included) — wire `collections.py`
- `backend/tests/test_mcp_e2e.sh` — empty-is-valid invariant

**New (frontend)**:
- `frontend/src/components/create-collection-dialog.tsx`
- `frontend/src/components/delete-collection-dialog.tsx`
- `frontend/src/contexts/vault-refresh-context.tsx` (or similar) — exposes `refetchTree()`, `refetchVaults()`
- Component + hook + page tests listed above

**Changed (frontend)**:
- `frontend/src/components/vault-explorer.tsx` — header `+ Collection` button, row-hover delete, ⟳ buttons
- `frontend/src/hooks/use-vault-tree.ts` — expose `refetch`
- `frontend/src/hooks/use-vaults.ts` (or equivalent) — expose `refetch`
- `frontend/src/lib/api.ts` — `createCollection`, `deleteCollection`
- `frontend/src/pages/document.tsx` — view toggle, raw pane, copy button
- Existing mutation call sites — invoke `refetch` on success
