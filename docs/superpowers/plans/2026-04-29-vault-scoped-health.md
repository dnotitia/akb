# Vault-scoped Indexing Health Implementation Plan

> **For agentic workers:** REQUIRED: Use superpowers:subagent-driven-development (if subagents available) or superpowers:executing-plans to implement this plan. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Surface per-vault indexing pending counts (embed / qdrant / metadata) via a new authenticated `GET /health/vault/{name}` endpoint, plus UI on vault overview (badge) and vault settings (diagnostics section), while keeping the existing global `/health` endpoint intact.

**Architecture:** Denormalize `chunks.vault_id` (one-shot migration + writer signature update). Add a `vault_id` overload to each worker's `pending_stats()`. New REST route gated on reader role. Frontend `useVaultHealth` hook polls every 15s. Same `IndexingBadge` component reused with the vault-scoped data on vault overview metadata row.

**Tech Stack:** Python 3.11 / FastAPI / asyncpg, PostgreSQL 16 + pgvector, React 19 + TypeScript + Vite, Tailwind v4.

**Spec:** `docs/superpowers/specs/2026-04-29-vault-scoped-health-design.md`

---

## File Map

**Backend — schema and writes**
- Create `backend/app/db/migrations/014_chunks_vault_id.py` — adds nullable column, backfills from 3 parent tables, locks NOT NULL + FK + index, all in one transaction
- Modify `backend/app/db/init.sql` — add `vault_id UUID NOT NULL REFERENCES vaults(id) ON DELETE CASCADE` and matching index to chunks DDL so fresh DBs match the migrated state
- Modify `backend/app/services/index_service.py` — `write_source_chunks` gains required `vault_id` keyword arg, INSERT statement updated
- Modify `backend/app/services/document_service.py` — 3 callers (put / update / replace) pass `vault_id`
- Modify `backend/app/services/file_service.py` — `index_file_metadata` gains `vault_id` param + passes it; the upload-confirm caller (line 303) passes the vault's UUID
- Modify `backend/app/services/table_service.py` — `index_table_metadata` gains `vault_id` param + passes it; the create-table caller (line 183) passes the vault's UUID
- Modify `backend/app/services/external_git_service.py` — caller (`_sync_external_path`, around line 284) passes `vault_id`
- Modify `backend/app/db/postgres.py` — register the new migration in the explicit `_apply_migrations` tuple (lines 84-96)

**Backend — service layer (per-vault stats)**
- Modify `backend/app/services/embed_worker.py` — `pending_stats(vault_id=None)` overload
- Modify `backend/app/services/vector_indexer.py` — `pending_stats(vault_id=None)` overload (omits `delete` subset when vault-scoped — see Task 7 rationale)
- Modify `backend/app/services/metadata_worker.py` — `pending_stats(vault_id=None)` overload (uses `documents.vault_id` directly, no schema change needed)
- Create `backend/app/services/health.py` — `vault_health(vault_id)` helper that fans out to the three workers

**Backend — API**
- Modify `backend/app/main.py` — add `GET /health/vault/{name}` route next to existing `/health`

**Backend — tests**
- Modify `backend/tests/test_security_edge_e2e.sh` — add 4 ACL assertions

**Frontend — hook**
- Create `frontend/src/hooks/use-vault-health.ts` — 15s polling, auth-gated

**Frontend — UI surfaces**
- Modify `frontend/src/pages/vault.tsx` — add `IndexingBadge` to metadata row using `useVaultHealth(name)`
- Modify `frontend/src/pages/vault-settings.tsx` — add `§ DIAGNOSTICS` section with per-worker breakdown

---

## Task 1 — Migration scaffold

**Files:**
- Create: `backend/app/db/migrations/014_chunks_vault_id.py`
- Modify: `backend/app/db/postgres.py:84-96` (register the new migration in the explicit tuple)

**Context:** Migrations in this project use the `migrate(conn=None)` + `_run(conn)` wrapper pattern — see `013_nfc_normalize.py:55-64` for the canonical shape. The runner at `backend/app/db/postgres.py:81-101` calls `await module.migrate(conn=conn)` and iterates an **explicit hardcoded tuple of filenames** (no auto-discovery). The new file must (a) expose a `migrate()` callable and (b) be added to that tuple.

- [ ] **Step 1: Create the migration file**

```python
"""Migration 014: chunks.vault_id

Denormalize vault_id onto chunks so per-vault pending_stats can be a
single indexed COUNT instead of a polymorphic 3-branch JOIN.

The chunks table has source_type ∈ {document, table, file} and a
polymorphic source_id. Each parent table (documents / vault_tables /
vault_files) already carries vault_id, so we backfill from there.

Single transaction: ADD nullable column → backfill 3 parent types →
delete orphans → SET NOT NULL → FK + index. Concurrent INSERT during
the migration is safe because the column is nullable until step 4 and
the writer code (deployed before this migration) supplies vault_id.

Idempotent — running twice on already-migrated data is a no-op.
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent.parent))

from app.db.postgres import get_pool

logger = logging.getLogger("akb.migrations.014")


async def migrate(conn=None):
    """Entry point invoked by app.db.postgres._apply_migrations.

    Mirrors 013_nfc_normalize: if the runner did not pass us a conn,
    acquire our own from the pool. Inside `_run`, all DDL + backfill
    runs in a single transaction.
    """
    if conn is None:
        pool = await get_pool()
        async with pool.acquire() as new_conn:
            await _run(new_conn)
    else:
        await _run(conn)


async def _run(conn):
    async with conn.transaction():
        await conn.execute(
            "ALTER TABLE chunks ADD COLUMN IF NOT EXISTS vault_id UUID"
        )
        await conn.execute(
            """
            UPDATE chunks c
               SET vault_id = d.vault_id
              FROM documents d
             WHERE c.source_type = 'document'
               AND c.source_id = d.id
               AND c.vault_id IS NULL
            """
        )
        await conn.execute(
            """
            UPDATE chunks c
               SET vault_id = t.vault_id
              FROM vault_tables t
             WHERE c.source_type = 'table'
               AND c.source_id = t.id
               AND c.vault_id IS NULL
            """
        )
        await conn.execute(
            """
            UPDATE chunks c
               SET vault_id = f.vault_id
              FROM vault_files f
             WHERE c.source_type = 'file'
               AND c.source_id = f.id
               AND c.vault_id IS NULL
            """
        )
        orphans = await conn.fetchval(
            "SELECT COUNT(*) FROM chunks WHERE vault_id IS NULL"
        )
        if orphans:
            logger.warning(
                "Deleting %d orphan chunks with no parent row", orphans
            )
            await conn.execute(
                "DELETE FROM chunks WHERE vault_id IS NULL"
            )
        await conn.execute(
            "ALTER TABLE chunks ALTER COLUMN vault_id SET NOT NULL"
        )
        # FK is wrapped in DO block because PG raises if the constraint
        # already exists; idempotent migration must tolerate re-runs.
        await conn.execute(
            """
            DO $$
            BEGIN
                IF NOT EXISTS (
                    SELECT 1 FROM pg_constraint
                     WHERE conname = 'chunks_vault_id_fkey'
                ) THEN
                    ALTER TABLE chunks
                      ADD CONSTRAINT chunks_vault_id_fkey
                      FOREIGN KEY (vault_id)
                      REFERENCES vaults(id) ON DELETE CASCADE;
                END IF;
            END $$;
            """
        )
        await conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_chunks_vault_id
                ON chunks (vault_id)
            """
        )
```

- [ ] **Step 2: Register the migration in the runner**

Open `backend/app/db/postgres.py:84-96`. The function `_apply_migrations` iterates an explicit tuple of filenames. Append `"014_chunks_vault_id.py"` after the last existing entry (currently `"012_drop_llm_metadata_cache.py"`):

```python
for filename in (
    "002_public_shares.py",
    "003_rename_public_shares.py",
    "004_embed_retry_columns.py",
    "005_qdrant_index.py",
    "006_indexable_chunks.py",
    "007_outbox_sweep_index.py",
    "008_drop_legacy_document_id.py",
    "009_rename_qdrant_columns.py",
    "010_external_git_mirror.py",
    "011_external_doc_collections.py",
    "012_drop_llm_metadata_cache.py",
    "014_chunks_vault_id.py",   # ← NEW
):
```

(Note: `013_nfc_normalize.py` is intentionally absent from this list — that migration is invoked via a one-shot script, not the auto-runner. Don't add 013 here.)

- [ ] **Step 3: Verify the registration**

```bash
cd /Users/kwoo2/Desktop/storage/akb/backend
python -c "
import ast
src = open('app/db/postgres.py').read()
ast.parse(src)
print('postgres.py parses')
print('014 registered:', '014_chunks_vault_id.py' in src)
ast.parse(open('app/db/migrations/014_chunks_vault_id.py').read())
print('014 migration parses')
"
```

Expected: 3 `True`/`ok` lines.

- [ ] **Step 4: Commit**

```bash
git add backend/app/db/migrations/014_chunks_vault_id.py backend/app/db/postgres.py
git commit -m "feat(db): migration 014 — chunks.vault_id denormalization"
```

---

## Task 2 — init.sql parity

**Files:**
- Modify: `backend/app/db/init.sql:124-138` (the `chunks` block)

**Context:** Fresh DBs initialize from `init.sql` directly without running migrations. The schema there must match the post-migration state, otherwise newly-created DBs will be missing the column. Keep both definitions in lockstep — this is the project's convention.

- [ ] **Step 1: Update chunks DDL**

Replace the `chunks` `CREATE TABLE` block with:

```sql
CREATE TABLE IF NOT EXISTS chunks (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    source_type TEXT NOT NULL DEFAULT 'document'
        CHECK (source_type IN ('document','table','file')),
    source_id UUID NOT NULL,
    vault_id UUID NOT NULL REFERENCES vaults(id) ON DELETE CASCADE,
    section_path TEXT,
    content TEXT NOT NULL,
    embedding vector(4096),
    chunk_index INTEGER NOT NULL,
    char_start INTEGER,
    char_end INTEGER,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_chunks_source ON chunks (source_type, source_id);
CREATE INDEX IF NOT EXISTS idx_chunks_vault_id ON chunks (vault_id);
```

(Existing pgvector-related comments below the table stay unchanged.)

- [ ] **Step 2: Commit**

```bash
git add backend/app/db/init.sql
git commit -m "feat(db): chunks.vault_id in init.sql for fresh-DB parity"
```

---

## Task 3 — `write_source_chunks` signature

**Files:**
- Modify: `backend/app/services/index_service.py:368-402`

**Context:** This is the single function that all 5 chunk-writing paths call. Adding a required keyword-only `vault_id` parameter forces every caller to update — type checker / IDE flags missing args. The INSERT statement gains the new column.

- [ ] **Step 1: Update function signature and INSERT**

Replace `write_source_chunks` body with:

```python
async def write_source_chunks(
    conn,
    source_type: SourceType,
    source_id: str,
    vault_id: uuid.UUID,
    chunks: list[Chunk],
    embeddings: list[list[float]],
) -> int:
    """Replace chunks for any indexable source (document/table/file).

    Pass `embeddings=[]` (or shorter than `chunks`) to defer dense
    vector generation: the row lands with `embedding=NULL` and
    `embed_worker` backfills it via the embedding API. This keeps the
    request hot path off the upstream round-trip — the only sync write
    paths that pass real embeddings are reindex tools and tests.

    `vault_id` is denormalized onto every chunk so `pending_stats(vault_id)`
    can be a single indexed COUNT instead of a polymorphic JOIN through
    the parent table. Caller MUST pass the vault that owns `source_id`
    — there is no consistency check here.

    Qdrant indexing is delegated to `vector_indexer` which picks up
    rows where `vector_indexed_at IS NULL` (and only after embeddings
    exist). Crash-safe: rows go in with NULL flags; a crash anywhere
    after this call leaves them catchable by the workers.
    """
    await _drop_source_chunks_with_outbox(conn, source_type, source_id)
    if not chunks:
        return 0

    src_uuid = uuid.UUID(source_id)
    for i, chunk in enumerate(chunks):
        chunk_id = uuid.uuid4()
        vec = embeddings[i] if embeddings and i < len(embeddings) else None
        embedding_str = "[" + ",".join(str(v) for v in vec) + "]" if vec else None
        await conn.execute(
            """
            INSERT INTO chunks (id, source_type, source_id, vault_id,
                                section_path, content, embedding,
                                chunk_index, char_start, char_end)
            VALUES ($1, $2, $3, $4, $5, $6, $7::vector, $8, $9, $10)
            """,
            chunk_id, source_type, src_uuid, vault_id,
            chunk.section_path, chunk.content, embedding_str,
            chunk.chunk_index, chunk.char_start, chunk.char_end,
        )
    return len(chunks)
```

- [ ] **Step 2: Verify all callers will fail typecheck (sanity)**

Run: `cd /Users/kwoo2/Desktop/storage/akb/backend && grep -rn "write_source_chunks(" app/ --include="*.py" | grep -v "def write_source_chunks"`

Expected output: 6 caller lines (3 in document_service, 1 each in file_service, table_service, external_git_service). Each is missing the new `vault_id` arg — Tasks 4 will fix them.

- [ ] **Step 3: Commit**

```bash
git add backend/app/services/index_service.py
git commit -m "feat(index): write_source_chunks accepts vault_id; INSERT new column"
```

---

## Task 4 — Update all `write_source_chunks` callers

**Files:**
- Modify: `backend/app/services/document_service.py` — 3 sites (put, update, replace), each already has `vault_id` in scope
- Modify: `backend/app/services/file_service.py` — `index_file_metadata` (line 437) gains `vault_id` parameter; the upload-confirm caller (line 303) passes the vault's UUID
- Modify: `backend/app/services/table_service.py` — `index_table_metadata` (line 94) gains `vault_id` parameter; the create-table caller (line 183) passes the vault's UUID
- Modify: `backend/app/services/external_git_service.py` — `_sync_external_path` caller (around line 284) passes the in-scope `vault_id`

**Context:** Every call now needs `vault_id=` as a keyword arg. In document/external-git paths the variable is already in scope. The file/table indexer helpers currently receive `vault_name` only — both must be amended to also receive `vault_id`, and their callers must pass it (the caller in each case has the vault row in hand and can do `vault["id"]` or equivalent).

Function names referenced here are **`index_file_metadata`** (file_service.py:437) and **`index_table_metadata`** (table_service.py:94). Do not invent alternate names.

- [ ] **Step 1: Update `document_service.py:put` call site**

Find the existing call (around line 198, after the prior commit's "deferred embedding" refactor): `chunks_indexed = await write_source_chunks(conn, "document", str(pg_doc_id), chunks, [])`.

Replace with:
```python
chunks_indexed = await write_source_chunks(
    conn, "document", str(pg_doc_id),
    vault_id=vault_id,
    chunks=chunks, embeddings=[],
)
```

`vault_id` was already fetched as `vault_id = await vault_repo.get_id_by_name(req.vault)` near the top of `put()`. Same UUID, just thread it through.

- [ ] **Step 2: Update `document_service.py:update` call site**

Find the equivalent call inside `async def update(...)` (around line 340). `vault_id` is fetched at the start of the function. Apply the same shape:

```python
chunks_indexed = await write_source_chunks(
    conn, "document", str(pg_doc_id),
    vault_id=vault_id,
    chunks=chunks, embeddings=[],
)
```

- [ ] **Step 3: Update `document_service.py:replace` call site**

Same pattern inside `async def replace(...)` (around line 463). Verify `vault_id` is in scope (it is — `replace` fetches it the same way `put`/`update` do).

- [ ] **Step 4: Add `vault_id` to `index_file_metadata` signature + INSERT**

Open `backend/app/services/file_service.py:437`. The function currently is:
```python
async def index_file_metadata(
    file_id: str,
    vault_name: str,
    collection: str,
    name: str,
    mime_type: str | None,
    size_bytes: int | None,
    description: str | None,
) -> None:
    ...
    async with pool.acquire() as conn:
        await write_source_chunks(conn, "file", file_id, [chunk], [])
```

Change to:
```python
async def index_file_metadata(
    file_id: str,
    vault_id: uuid.UUID,
    vault_name: str,
    collection: str,
    name: str,
    mime_type: str | None,
    size_bytes: int | None,
    description: str | None,
) -> None:
    ...
    async with pool.acquire() as conn:
        await write_source_chunks(
            conn, "file", file_id,
            vault_id=vault_id,
            chunks=[chunk], embeddings=[],
        )
```

(`uuid` is already imported in this file. If not, add `import uuid` at the top.)

- [ ] **Step 5: Update `index_file_metadata` caller in `file_service.py:303`**

The upload-confirm flow already loads `vault_row` for the vault_name. Pass `vault_id` from the same query. Find the call at line 303:

```python
await index_file_metadata(
    file_id,
    vault_name=vault_row["name"] if vault_row else "",
    ...
)
```

The query just before (line 297) is `SELECT name FROM vaults WHERE id = $1`. Update it to also return `id`:
```python
vault_row = await conn.fetchrow(
    "SELECT id, name FROM vaults WHERE id = $1", vault_id,
)
```

…and update the call to pass both:
```python
await index_file_metadata(
    file_id,
    vault_id=vault_id,
    vault_name=vault_row["name"] if vault_row else "",
    collection=row["collection"] or "",
    name=row["name"],
    mime_type=row["mime_type"],
    size_bytes=size_bytes,
    description=row["description"],
)
```

(`vault_id` is already in scope on this caller — it's the parameter that drove the SELECT in the first place.)

- [ ] **Step 6: Add `vault_id` to `index_table_metadata` signature + INSERT**

Open `backend/app/services/table_service.py:94`. The function currently is:
```python
async def index_table_metadata(
    table_id: str,
    vault_name: str,
    name: str,
    description: str | None,
    columns: list[dict],
) -> None:
    ...
    async with pool.acquire() as conn:
        await write_source_chunks(conn, "table", table_id, [chunk], [])
```

Change to:
```python
async def index_table_metadata(
    table_id: str,
    vault_id: uuid.UUID,
    vault_name: str,
    name: str,
    description: str | None,
    columns: list[dict],
) -> None:
    ...
    async with pool.acquire() as conn:
        await write_source_chunks(
            conn, "table", table_id,
            vault_id=vault_id,
            chunks=[chunk], embeddings=[],
        )
```

- [ ] **Step 7: Update `index_table_metadata` caller in `table_service.py:183`**

The caller in `create_table` already has `vault["id"]` in scope (the vault row was fetched earlier). The current call is positional:

```python
await index_table_metadata(
    str(tid), vault["name"], name, description, columns,
)
```

Change to:
```python
await index_table_metadata(
    str(tid),
    vault_id=vault["id"],
    vault_name=vault["name"],
    name=name,
    description=description,
    columns=columns,
)
```

- [ ] **Step 8: Update `external_git_service.py:_sync_external_path`**

Find the call near line 284: `await write_source_chunks(conn, "document", str(pg_doc_id), chunks, [])`. `vault_id` is in scope at this site. Apply the keyword shape:
```python
await write_source_chunks(
    conn, "document", str(pg_doc_id),
    vault_id=vault_id,
    chunks=chunks, embeddings=[],
)
```

- [ ] **Step 9: Verify all sites parse and types are consistent**

```bash
cd /Users/kwoo2/Desktop/storage/akb/backend
python -c "
import ast
for f in ['app/services/document_service.py', 'app/services/file_service.py',
          'app/services/table_service.py', 'app/services/external_git_service.py',
          'app/services/index_service.py']:
    ast.parse(open(f).read())
    print('ok:', f)
"
```

Expected: 5 lines of `ok:` output. (`ast.parse` only catches syntax — type mismatches would manifest as runtime `TypeError` from unmatched keyword args. Visual review of the 6 call sites + the 2 helper signatures is the type check here. If `pyright` or `mypy` is configured locally, run that too.)

- [ ] **Step 10: Commit**

```bash
git add backend/app/services/document_service.py \
        backend/app/services/file_service.py \
        backend/app/services/table_service.py \
        backend/app/services/external_git_service.py
git commit -m "feat(index): pass vault_id through to write_source_chunks at all 6 sites"
```

---

## Task 5 — `embed_worker.pending_stats(vault_id)`

**Files:**
- Modify: `backend/app/services/embed_worker.py:128-148`

**Context:** Two explicit branches (with / without `vault_id`) instead of a conditional `WHERE vault_id = $X OR $X IS NULL` because the conditional form prevents PG from using the index correctly. The global call signature stays compatible (`vault_id=None` default).

- [ ] **Step 1: Replace the function body**

```python
async def pending_stats(vault_id: "uuid.UUID | None" = None) -> dict:
    """Snapshot of embedding backfill state.

    Without `vault_id`, returns the system-wide aggregate (used by
    the global /health endpoint). With `vault_id`, returns the count
    for chunks that belong to that vault only (used by /health/vault).
    Two explicit branches because PG can't use the partial index when
    the vault filter is gated on `$X IS NULL`.
    """
    pool = await get_pool()
    async with pool.acquire() as conn:
        if vault_id is None:
            row = await conn.fetchrow(
                """
                SELECT
                    COUNT(*) FILTER (WHERE embedding IS NULL)                                     AS pending,
                    COUNT(*) FILTER (WHERE embedding IS NULL AND embed_retry_count > 0
                                     AND embed_retry_count < $1)                                   AS retrying,
                    COUNT(*) FILTER (WHERE embedding IS NULL AND embed_retry_count >= $1)         AS abandoned
                  FROM chunks
                """,
                MAX_RETRIES,
            )
        else:
            row = await conn.fetchrow(
                """
                SELECT
                    COUNT(*) FILTER (WHERE embedding IS NULL)                                     AS pending,
                    COUNT(*) FILTER (WHERE embedding IS NULL AND embed_retry_count > 0
                                     AND embed_retry_count < $1)                                   AS retrying,
                    COUNT(*) FILTER (WHERE embedding IS NULL AND embed_retry_count >= $1)         AS abandoned
                  FROM chunks
                 WHERE vault_id = $2
                """,
                MAX_RETRIES, vault_id,
            )
    return {
        "pending":   int(row["pending"]),
        "retrying":  int(row["retrying"]),
        "abandoned": int(row["abandoned"]),
    }
```

- [ ] **Step 2: Confirm the existing global call site (`/health`) still typechecks**

Run: `grep -rn "embed_worker.pending_stats\b" /Users/kwoo2/Desktop/storage/akb/backend/app/`

Expected: at least the call in `main.py:190` (no arg, default `None` → global behavior preserved).

- [ ] **Step 3: Commit**

```bash
git add backend/app/services/embed_worker.py
git commit -m "feat(embed_worker): pending_stats(vault_id) overload"
```

---

## Task 6 — `vector_indexer.pending_stats(vault_id)`

**Files:**
- Modify: `backend/app/services/vector_indexer.py:321-358`

**Context:** Current `pending_stats()` returns both `upsert` (chunks-based) and `delete` (vector_delete_outbox-based). The outbox table has no `vault_id` column (chunks are already deleted by the time delete is enqueued, so vault_id is unrecoverable). Decision (matches spec): **omit the `delete` subset when vault-scoped**. The frontend `HealthSnapshot` interface declares `delete` as optional.

- [ ] **Step 1: Replace the function body**

```python
async def pending_stats(vault_id: "uuid.UUID | None" = None) -> dict:
    """Snapshot for /health.

    Without `vault_id`: system-wide stats including both upsert and
    delete queues. With `vault_id`: upsert stats only — the delete
    outbox doesn't carry vault_id (chunks are already deleted by the
    time delete is enqueued, so we can't recover vault membership
    without adding another column we don't actually need).
    """
    pool = await get_pool()
    async with pool.acquire() as conn:
        if vault_id is None:
            chunk_row = await conn.fetchrow(
                """
                SELECT
                    COUNT(*) FILTER (WHERE embedding IS NOT NULL AND vector_indexed_at IS NULL)                    AS pending,
                    COUNT(*) FILTER (WHERE embedding IS NOT NULL AND vector_indexed_at IS NULL
                                     AND vector_retry_count > 0 AND vector_retry_count < $1)                       AS retrying,
                    COUNT(*) FILTER (WHERE embedding IS NOT NULL AND vector_indexed_at IS NULL
                                     AND vector_retry_count >= $1)                                                  AS abandoned,
                    COUNT(*) FILTER (WHERE vector_indexed_at IS NOT NULL)                                           AS indexed
                  FROM chunks
                """,
                MAX_RETRIES,
            )
            del_row = await conn.fetchrow(
                """
                SELECT
                    COUNT(*) FILTER (WHERE processed_at IS NULL AND retry_count < $1)     AS pending,
                    COUNT(*) FILTER (WHERE processed_at IS NULL AND retry_count >= $1)    AS abandoned
                  FROM vector_delete_outbox
                """,
                MAX_RETRIES,
            )
            return {
                "upsert": {
                    "pending":   int(chunk_row["pending"]),
                    "retrying":  int(chunk_row["retrying"]),
                    "abandoned": int(chunk_row["abandoned"]),
                    "indexed":   int(chunk_row["indexed"]),
                },
                "delete": {
                    "pending":   int(del_row["pending"]),
                    "abandoned": int(del_row["abandoned"]),
                },
            }
        else:
            chunk_row = await conn.fetchrow(
                """
                SELECT
                    COUNT(*) FILTER (WHERE embedding IS NOT NULL AND vector_indexed_at IS NULL)                    AS pending,
                    COUNT(*) FILTER (WHERE embedding IS NOT NULL AND vector_indexed_at IS NULL
                                     AND vector_retry_count > 0 AND vector_retry_count < $1)                       AS retrying,
                    COUNT(*) FILTER (WHERE embedding IS NOT NULL AND vector_indexed_at IS NULL
                                     AND vector_retry_count >= $1)                                                  AS abandoned,
                    COUNT(*) FILTER (WHERE vector_indexed_at IS NOT NULL)                                           AS indexed
                  FROM chunks
                 WHERE vault_id = $2
                """,
                MAX_RETRIES, vault_id,
            )
            return {
                "upsert": {
                    "pending":   int(chunk_row["pending"]),
                    "retrying":  int(chunk_row["retrying"]),
                    "abandoned": int(chunk_row["abandoned"]),
                    "indexed":   int(chunk_row["indexed"]),
                },
            }
```

- [ ] **Step 2: Commit**

```bash
git add backend/app/services/vector_indexer.py
git commit -m "feat(vector_indexer): pending_stats(vault_id) overload (upsert only)"
```

---

## Task 7 — `metadata_worker.pending_stats(vault_id)`

**Files:**
- Modify: `backend/app/services/metadata_worker.py:201-222`

**Context:** This worker queries `documents` directly (not `chunks`). The `documents` table already has `vault_id`, so no schema work — just an extra `WHERE vault_id = $2` clause.

- [ ] **Step 1: Replace the function body**

```python
async def pending_stats(vault_id: "uuid.UUID | None" = None) -> dict:
    """Snapshot for /health.

    Operates on the documents table directly (one row per doc with
    llm_metadata_at + llm_retry_count) — vault_id has been on this
    table since day one, so the vault overload is a single clause.
    """
    pool = await get_pool()
    async with pool.acquire() as conn:
        if vault_id is None:
            row = await conn.fetchrow(
                """
                SELECT
                    COUNT(*) FILTER (WHERE source='external_git' AND llm_metadata_at IS NULL)
                                                                                      AS pending,
                    COUNT(*) FILTER (WHERE source='external_git' AND llm_metadata_at IS NULL
                                     AND llm_retry_count > 0 AND llm_retry_count < $1) AS retrying,
                    COUNT(*) FILTER (WHERE source='external_git' AND llm_metadata_at IS NULL
                                     AND llm_retry_count >= $1)                        AS abandoned
                  FROM documents
                """,
                MAX_RETRIES,
            )
        else:
            row = await conn.fetchrow(
                """
                SELECT
                    COUNT(*) FILTER (WHERE source='external_git' AND llm_metadata_at IS NULL)
                                                                                      AS pending,
                    COUNT(*) FILTER (WHERE source='external_git' AND llm_metadata_at IS NULL
                                     AND llm_retry_count > 0 AND llm_retry_count < $1) AS retrying,
                    COUNT(*) FILTER (WHERE source='external_git' AND llm_metadata_at IS NULL
                                     AND llm_retry_count >= $1)                        AS abandoned
                  FROM documents
                 WHERE vault_id = $2
                """,
                MAX_RETRIES, vault_id,
            )
    return {
        "pending":   int(row["pending"]),
        "retrying":  int(row["retrying"]),
        "abandoned": int(row["abandoned"]),
    }
```

- [ ] **Step 2: Commit**

```bash
git add backend/app/services/metadata_worker.py
git commit -m "feat(metadata_worker): pending_stats(vault_id) overload"
```

---

## Task 8 — `health.py` aggregator

**Files:**
- Create: `backend/app/services/health.py`

**Context:** Tiny module — one function. Could go in `main.py` but having a separate module makes the route handler in `main.py` a one-liner and keeps `main.py` from growing.

- [ ] **Step 1: Create the file**

```python
"""Per-vault health aggregator.

Composes the three worker pending_stats(vault_id) calls into a single
response shape that mirrors a subset of the global /health endpoint.
The auth check happens in the route handler; this module just composes.
"""

from __future__ import annotations

import uuid

from app.services import embed_worker, metadata_worker, vector_indexer


async def vault_health(vault_id: uuid.UUID) -> dict:
    """Return per-vault pending counts across embed / qdrant / metadata.

    Sequential awaits — the three queries are all sub-millisecond on
    indexed columns, gather() saves ~1-2ms but adds task-scheduling
    overhead. Revisit if PG round-trip latency becomes visible.
    """
    embed = await embed_worker.pending_stats(vault_id)
    qdrant = await vector_indexer.pending_stats(vault_id)
    metadata = await metadata_worker.pending_stats(vault_id)
    return {
        "embed_backfill":    embed,
        "metadata_backfill": metadata,
        "qdrant":            {"backfill": qdrant},
    }
```

- [ ] **Step 2: Commit**

```bash
git add backend/app/services/health.py
git commit -m "feat(health): vault_health(vault_id) aggregator"
```

---

## Task 9 — `GET /health/vault/{name}` route

**Files:**
- Modify: `backend/app/main.py` — add new route after the existing `@app.get("/health")` block

**Context:** Authenticated endpoint, reader role required. Mirrors the auth model used by every other vault-scoped route in this codebase (issue #3 KG fix uses the same pattern).

URL convention: the existing global `/health` route is registered directly on the FastAPI app instance (no `/api/v1` prefix) — see `main.py:166`. The new route follows the same convention so the two health endpoints sit at sibling paths (`/health` and `/health/vault/{name}`). All callers (frontend hook, e2e tests, smoke probes) MUST use the off-prefix URL accordingly.

- [ ] **Step 1: Add imports near the top of `main.py`**

Locate the existing imports for `get_current_user`, `AuthenticatedUser`, `check_vault_access`. If those aren't already in `main.py`, add:

```python
from app.api.deps import get_current_user
from app.services.access_service import check_vault_access
from app.services.auth_service import AuthenticatedUser
from app.services.health import vault_health
```

- [ ] **Step 2: Add the route handler**

Place after the existing `/health` route handler:

```python
@app.get("/health/vault/{name}", summary="Per-vault indexing health (auth required)")
async def vault_health_route(
    name: str,
    user: AuthenticatedUser = Depends(get_current_user),
):
    """Vault-scoped pending-stats snapshot.

    Auth: vault reader role required. Unlike the global /health (which
    is unauthenticated for k8s probes and uptime monitors), this leaks
    vault existence — anonymous probing would tell an attacker which
    vault names exist. Consistent with the access model from issue #3.
    """
    access = await check_vault_access(user.user_id, name, required_role="reader")
    return {
        "vault": name,
        **(await vault_health(access["vault_id"])),
    }
```

- [ ] **Step 3: Verify the route is registered**

```bash
cd /Users/kwoo2/Desktop/storage/akb/backend
python -c "
import ast
src = open('app/main.py').read()
ast.parse(src)
print('main.py parses')
print('vault_health import:', 'vault_health' in src)
print('route present:', '/health/vault/{name}' in src)
"
```

Expected: all three lines confirm True.

- [ ] **Step 4: Commit**

```bash
git add backend/app/main.py
git commit -m "feat(api): GET /health/vault/{name} (reader-gated)"
```

---

## Task 10 — E2E ACL tests

**Files:**
- Modify: `backend/tests/test_security_edge_e2e.sh`

**Context:** This file already follows a `pass` / `fail` pattern with `acurl_as` helper (PAT-aware curl) and `mr` (mcp result extractor). Add a new section near the existing "Knowledge graph access control" block (the issue #3 fix).

- [ ] **Step 1: Insert the test block**

Find the existing `# ── 1b. Knowledge graph access control (issue #3) ────────────` section (added in commit `116b91e`). Add this new section right after it, before the existing `# ── 2. Grep Regex Validation ─────────────────────────────────`:

```bash
# ── 1c. Vault-scoped health ACL ──────────────────────────────
echo ""
echo "▸ 1c. Vault-scoped health ACL"

# user1 sees own vault health (note: /health is off-prefix, no /api/v1)
R=$(acurl_as "$PAT1" "$BASE_URL/health/vault/$VAULT1")
HAS=$(echo "$R" | python3 -c "import sys,json; d=json.load(sys.stdin); print('embed_backfill' in d and 'qdrant' in d)" 2>/dev/null)
[ "$HAS" = "True" ] && pass "User1 sees own vault health" || fail "vault health self" "$R"

# user2 blocked from user1's private vault → 403
HTTP=$(curl -sS -o /dev/null -w "%{http_code}" -H "Authorization: Bearer $PAT2" "$BASE_URL/health/vault/$VAULT1")
[ "$HTTP" = "403" ] && pass "User2 blocked from vault health (403)" || fail "vault health ACL" "got HTTP $HTTP"

# unauthenticated → 401
HTTP=$(curl -sS -o /dev/null -w "%{http_code}" "$BASE_URL/health/vault/$VAULT1")
[ "$HTTP" = "401" ] && pass "Unauthenticated blocked from vault health (401)" || fail "vault health auth" "got HTTP $HTTP"

# unknown vault → 404
HTTP=$(curl -sS -o /dev/null -w "%{http_code}" -H "Authorization: Bearer $PAT1" "$BASE_URL/health/vault/nonexistent-vault-xyz")
[ "$HTTP" = "404" ] && pass "Unknown vault returns 404" || fail "vault health 404" "got HTTP $HTTP"
```

- [ ] **Step 2: Verify shell syntax**

```bash
bash -n /Users/kwoo2/Desktop/storage/akb/backend/tests/test_security_edge_e2e.sh && echo "shell syntax ok"
```

Expected: `shell syntax ok`

- [ ] **Step 3: Commit**

```bash
git add backend/tests/test_security_edge_e2e.sh
git commit -m "test(security): vault-scoped health ACL coverage"
```

---

## Task 11 — Frontend `useVaultHealth` hook

**Files:**
- Create: `frontend/src/hooks/use-vault-health.ts`

**Context:** Mirrors the existing `useHealth` hook (`frontend/src/hooks/use-health.ts`) — same polling cadence, same `HealthSnapshot` shape (with optional fields), but takes a `vaultName` argument and adds the auth header manually. The endpoint URL is `/health/vault/{name}` (no `/api/v1` prefix) — same convention as the existing global `/health`. Returns `null` until first response resolves; failures are silent (badge falls back to placeholder).

- [ ] **Step 1: Create the file**

```typescript
import { useEffect, useState } from "react";
import { getToken } from "@/lib/api";
import type { HealthSnapshot } from "./use-health";

const ENDPOINT_BASE = "/health/vault";
const DEFAULT_INTERVAL = 15000;

interface VaultHealthSnapshot extends HealthSnapshot {
  vault?: string;
}

/**
 * Polls /health/vault/{name} every 15s. Authenticated — bails out if no
 * token. Returns null until the first response resolves; subsequent
 * failures keep the last good snapshot rather than flickering through
 * null. Cleans up on unmount or vaultName change.
 *
 * Endpoint is off-prefix (sibling of /health) — do not prepend /api/v1.
 */
export function useVaultHealth(
  vaultName: string | undefined,
  intervalMs: number = DEFAULT_INTERVAL,
): VaultHealthSnapshot | null {
  const [data, setData] = useState<VaultHealthSnapshot | null>(null);

  useEffect(() => {
    if (!vaultName || !getToken()) {
      setData(null);
      return;
    }
    let cancelled = false;
    const tick = async () => {
      const token = getToken();
      if (!token) return;
      try {
        const r = await fetch(
          `${ENDPOINT_BASE}/${encodeURIComponent(vaultName)}`,
          { headers: { Authorization: `Bearer ${token}` } },
        );
        if (!r.ok) return;
        const json = (await r.json()) as VaultHealthSnapshot;
        if (!cancelled) setData(json);
      } catch {
        /* silent — IndexingBadge falls back to placeholder */
      }
    };
    tick();
    const id = setInterval(tick, intervalMs);
    return () => {
      cancelled = true;
      clearInterval(id);
    };
  }, [vaultName, intervalMs]);

  return data;
}
```

- [ ] **Step 2: Typecheck**

```bash
cd /Users/kwoo2/Desktop/storage/akb/frontend && npx tsc --noEmit; echo "exit=$?"
```

Expected: `exit=0`

- [ ] **Step 3: Commit**

```bash
git add frontend/src/hooks/use-vault-health.ts
git commit -m "feat(frontend): useVaultHealth hook polling /health/vault/{name}"
```

---

## Task 12 — Vault overview badge

**Files:**
- Modify: `frontend/src/pages/vault.tsx`

**Context:** Add `IndexingBadge` to the metadata badge row (between `VaultStateBadge` and the `MEMBERS / SETTINGS` link cluster). The component is already imported (used elsewhere) — check imports first. Polling is per-page, so this badge only updates while the user is on the overview page.

- [ ] **Step 1: Add hook import + invocation**

Near the top imports, add:
```typescript
import { useVaultHealth } from "@/hooks/use-vault-health";
import { IndexingBadge } from "@/components/status-badge";
```

(If `IndexingBadge` is already imported in this file, only add the hook import.)

Inside the component, after existing hooks:
```typescript
const vaultHealth = useVaultHealth(name);
const vaultPending: number | null = vaultHealth
  ? (vaultHealth.embed_backfill?.pending || 0) +
    (vaultHealth.qdrant?.backfill?.upsert?.pending || 0) +
    (vaultHealth.metadata_backfill?.pending || 0)
  : null;
```

- [ ] **Step 2: Place the badge in the metadata row**

Find the existing metadata badge row (`<div className="flex flex-wrap items-center gap-2 mt-4">`). Insert the badge between `VaultStateBadge` and the right-aligned member/settings links:

```tsx
<div className="flex flex-wrap items-center gap-2 mt-4">
  {info?.role && <RoleBadge role={info.role} />}
  <VaultStateBadge
    archived={info?.is_archived}
    externalGit={info?.is_external_git}
    publicAccess={info?.public_access}
  />
  <IndexingBadge pending={vaultPending} />
  <div className="ml-auto flex items-baseline gap-4">
    {/* existing MEMBERS / SETTINGS links unchanged */}
    ...
  </div>
</div>
```

- [ ] **Step 3: Typecheck + visually verify**

```bash
cd /Users/kwoo2/Desktop/storage/akb/frontend && npx tsc --noEmit; echo "exit=$?"
```

Expected: `exit=0`

(Live verification on the dev server happens after Task 13 once both UI surfaces are in place.)

- [ ] **Step 4: Commit**

```bash
git add frontend/src/pages/vault.tsx
git commit -m "feat(vault): IndexingBadge on overview metadata row (vault-scoped)"
```

---

## Task 13 — Vault settings diagnostics section

**Files:**
- Modify: `frontend/src/pages/vault-settings.tsx`

**Context:** A new section below `§ LIFECYCLE` showing per-worker pending/retrying/abandoned counts. Owner-only page (already gated upstream), so the diagnostics audience is exactly the people with authority to act on stuck workers.

- [ ] **Step 1: Add hook import**

Near the existing imports:
```typescript
import { useVaultHealth } from "@/hooks/use-vault-health";
```

- [ ] **Step 2: Invoke hook in component body**

After existing hooks:
```typescript
const vaultHealth = useVaultHealth(name);
```

- [ ] **Step 3: Add the DIAGNOSTICS section**

Append after the `§ LIFECYCLE` section, before the closing `</div>` of the page:

```tsx
{vaultHealth && (
  <section aria-labelledby="diag-h" className="mt-12">
    <header className="flex items-baseline gap-3 pb-3 border-b border-border mb-4">
      <span id="diag-h" className="coord-ink">§ DIAGNOSTICS</span>
      <span className="coord">indexing pipeline</span>
    </header>
    <div className="grid grid-cols-3 gap-px border border-border bg-border">
      <DiagCell title="EMBED" stats={vaultHealth.embed_backfill} />
      <DiagCell title="QDRANT" stats={vaultHealth.qdrant?.backfill?.upsert} />
      <DiagCell title="METADATA" stats={vaultHealth.metadata_backfill} />
    </div>
    <p className="text-xs text-foreground-muted mt-2 leading-relaxed max-w-prose">
      Backfill workers process new chunks asynchronously after a write.
      Numbers reset to zero when caught up. Persistent non-zero values
      across multiple refreshes signal a stuck worker — check the
      embedding API or Qdrant health.
    </p>
  </section>
)}
```

- [ ] **Step 4: Add the `DiagCell` helper at the bottom of the file**

Outside the page component, before the default export (or after, depending on existing order):

```tsx
interface DiagStats {
  pending?: number;
  retrying?: number;
  abandoned?: number;
}

function DiagCell({ title, stats }: { title: string; stats?: DiagStats }) {
  return (
    <div className="bg-surface p-3">
      <div className="coord-ink mb-2">{title}</div>
      <dl className="text-xs space-y-1 font-mono tabular-nums">
        <div className="flex justify-between">
          <dt>pending</dt>
          <dd>{stats?.pending ?? "—"}</dd>
        </div>
        <div className="flex justify-between">
          <dt>retrying</dt>
          <dd>{stats?.retrying ?? "—"}</dd>
        </div>
        <div className="flex justify-between text-destructive">
          <dt>abandoned</dt>
          <dd>{stats?.abandoned ?? "—"}</dd>
        </div>
      </dl>
    </div>
  );
}
```

- [ ] **Step 5: Typecheck**

```bash
cd /Users/kwoo2/Desktop/storage/akb/frontend && npx tsc --noEmit; echo "exit=$?"
```

Expected: `exit=0`

- [ ] **Step 6: Commit**

```bash
git add frontend/src/pages/vault-settings.tsx
git commit -m "feat(settings): § DIAGNOSTICS section with per-worker breakdown"
```

---

## Task 14 — Final verification (build + e2e)

**Files:** none (verification only)

- [ ] **Step 1: Backend syntax + frontend build**

```bash
cd /Users/kwoo2/Desktop/storage/akb/backend && python -c "
import ast
import os
for root, _, files in os.walk('app'):
    for f in files:
        if f.endswith('.py') and not f.startswith('__'):
            ast.parse(open(os.path.join(root, f)).read())
print('backend ast: ok')
"

cd /Users/kwoo2/Desktop/storage/akb/frontend && npx tsc --noEmit && pnpm build 2>&1 | tail -3
```

Expected: backend ast OK, frontend tsc clean, build succeeds.

- [ ] **Step 2: Run frontend tests**

```bash
cd /Users/kwoo2/Desktop/storage/akb/frontend && npx vitest run 2>&1 | tail -6
```

Expected: all existing tests still pass (no test added for new hook — see spec rationale).

- [ ] **Step 3: Confirm migration runs cleanly on dev DB**

(Local dev DB or test DB — depends on project setup. The deploy script triggers migrations on K8s during rollout.)

```bash
# Manual sanity if you have a local PG with chunks:
psql "$AKB_DATABASE_URL" -c "
SELECT COUNT(*) FILTER (WHERE vault_id IS NULL) AS null_count,
       COUNT(*) AS total
  FROM chunks
"
```

Expected: `null_count = 0` after migration.

---

## Task 15 — Deploy + live verification

**Files:** none

**Context:** Use the standard deploy script. Sequence per spec: code deploys carry `vault_id` in the writer signature, then migration backfills, then API/frontend go live.

The migration's first step adds a nullable column, so even during the rolling-deploy overlap window (old pods still writing without `vault_id`, new pods writing with it) inserts succeed. The migration's `SET NOT NULL` step (inside the same transaction) only runs after the backfill, so by then every concurrently-inserted row has `vault_id` set — provided the new code shipped first. The deploy script's standard flow (image build → push → kubectl apply → readyz wait → migration on lifespan startup of new pods) preserves this ordering: new pods become Ready before they handle traffic, and the migration runs at their startup. If you ever split this across releases, the rule is **code first, migration second** — never the other way.

- [ ] **Step 1: Run deploy script**

```bash
bash /Users/kwoo2/Desktop/storage/akb/deploy/k8s/deploy.sh 2>&1 | tail -20
```

Expected: backend + frontend pods rotate, new pods reach 1/1 Running, prior pods Terminating.

- [ ] **Step 2: Run E2E**

```bash
bash /Users/kwoo2/Desktop/storage/akb/backend/tests/test_security_edge_e2e.sh
```

Expected: all `pass` lines, no `fail`. New section "1c. Vault-scoped health ACL" shows 4 passes.

- [ ] **Step 3: Smoke check live endpoint**

```bash
# Without auth header — expect 401 (note: off-prefix, no /api/v1)
curl -sS -o /dev/null -w "%{http_code}\n" http://localhost:8000/health/vault/gnu
```

Expected: `401`

```bash
# With reader PAT — expect JSON with embed_backfill / qdrant
curl -sS -H "Authorization: Bearer $YOUR_PAT" http://localhost:8000/health/vault/gnu | python3 -m json.tool
```

Expected: pretty JSON with `vault`, `embed_backfill`, `metadata_backfill`, `qdrant.backfill.upsert` keys.

- [ ] **Step 4: Browser visual check**

Open vault overview → see `IndexingBadge` in metadata row alongside role / state badges. Open vault settings → see `§ DIAGNOSTICS` section with three cells (EMBED / QDRANT / METADATA), each showing 0/0/0 if no pending work, otherwise actual numbers.

Write a large doc via `akb_put` to your vault. Within ~20s, watch the badge tick up briefly then disappear once embed_worker + vector_indexer catch up.

---

## Reference

- Spec: `docs/superpowers/specs/2026-04-29-vault-scoped-health-design.md`
- Issue #3 precedent (auth pattern): commit `116b91e`
- Embedding async refactor (worker pattern): commit `862fe6d`
- Global IndexingBadge placement decision: commit `131f445`
