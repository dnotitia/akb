# Indexing Pipeline — Findings

## Summary

The embed-worker / delete-worker / vector-store pipeline is largely well-structured: `SKIP LOCKED` prevents multi-pod races on the claim phase, the `next_attempt_at` lease prevents double-upsert within a batch window, and the pgvector driver correctly joins the caller's PG transaction so upsert and `vector_indexed_at` stamp commit atomically. However eight concrete defects were found ranging from a silent outbox bypass that can permanently orphan vector-store rows (HIGH), to a Seahorse-specific PK collision that corrupts multi-chunk documents (HIGH), to a hard shutdown that cancels in-flight upserts leaving chunks in a 10-minute claim limbo (MED).

---

## Findings

### F1: Silent outbox bypass on `enqueue_source_deletes` failure leaves orphan vector-store rows — Severity: HIGH

**Component**: `index_service._drop_source_chunks_with_outbox`
**File**: `backend/app/services/index_service.py:422-430`

**Scenario**: Any exception raised by `enqueue_source_deletes` (e.g. a transient PG error, a serialization failure, or the `uuid.UUID()` conversion throwing on a malformed `source_id`) is caught and swallowed. The code then proceeds to `DELETE FROM chunks`, which succeeds and commits. The chunks are gone from PG, but no outbox row was written. `delete_worker` never sees these chunk IDs. The vector-store rows are orphaned permanently.

**Observable bad state**: Vector store returns hits for documents that no longer exist in PG. `_hydrate_hits` silently drops them (the `meta.get(key)` miss at `search_service.py:318-320`), so search quietly mis-ranks or returns fewer results than expected. The operator's `/health` endpoint shows `delete.pending = 0` even though stale vectors exist.

**Fix direction**: Remove the `try/except` around `enqueue_source_deletes`. The outbox INSERT must either succeed or the entire enclosing transaction must abort. Swallowing the error breaks the atomicity guarantee the docstring claims to provide.

**Evidence**:
```python
# index_service.py:422-430
try:
    await delete_worker.enqueue_source_deletes(source_type, source_id, conn=conn)
except Exception as e:  # noqa: BLE001
    logger.warning("vector-store outbox enqueue failed: %s", e)
# DELETE still runs even when outbox INSERT failed
await conn.execute(
    "DELETE FROM chunks WHERE source_type = $1 AND source_id = $2",
    source_type, uuid.UUID(source_id),
)
```

The same pattern repeats in `delete_vault_chunks` at `index_service.py:455-469`.

---

### F2: `edit()` calls `write_source_chunks` outside a transaction — Severity: HIGH

**Component**: `document_service.DocumentService.edit`
**File**: `backend/app/services/document_service.py:637-643`

**Scenario**: `write_source_chunks` internally calls `_drop_source_chunks_with_outbox` (DELETE old chunks + INSERT outbox rows) then INSERTs new chunks — all on a bare `conn` with no surrounding `conn.transaction()`. If the process crashes or the connection is dropped after the old chunks are deleted but before the new chunks are inserted, the document has zero chunks in PG and no pending vector-store entries. It will never reappear in search results. Compare with `update()` at line 491-531 which wraps the identical operation in `async with conn.transaction()`.

**Observable bad state**: The document exists in `documents` table and git, but has no chunks. Search never returns it. The `pending` counter stays at 0 (no NULL-flag chunks). The document is silently invisible to hybrid search until a manual re-index.

**Fix direction**: Wrap the `write_source_chunks` call (and the subsequent `store_document_relations` call) in a single `async with conn.transaction()` block, mirroring the `update()` method's pattern.

**Evidence**:
```python
# document_service.py:637-655 — no transaction()
pool = await get_pool()
async with pool.acquire() as conn:
    chunks_indexed = await write_source_chunks(
        conn, "document", str(pg_doc_id), ...
    )
# entirely separate connection, no transaction linking these two
pool = await get_pool()
async with pool.acquire() as conn:
    await store_document_relations(conn, ...)
```

---

### F3: Seahorse PK collision corrupts multi-chunk documents — Severity: HIGH

**Component**: `vector_store.seahorse.SeahorseStore.upsert_one`
**File**: `backend/app/services/vector_store/seahorse.py:60-62, 281`

**Scenario**: The Seahorse primary key is computed as `_seahorse_pk(source_id, chunk_index)`. `chunk_index` is re-assigned sequentially from 0 by `chunk_markdown` after all sub-chunks are generated. When a document is updated and re-chunked with fewer chunks than before, the trailing old chunks' Seahorse rows are never deleted — the outbox DELETE uses `external_chunk_id` (the AKB UUID), but the new upsert at the same `(source_id, chunk_index)` overwrites `external_chunk_id` with the new UUID, so the filter for the old UUID matches nothing. Trailing old chunks remain in Seahorse.

**Observable bad state**: After a document is shortened, old content is still returned by Seahorse hybrid search. The vector store diverges silently from PG.

**Fix direction**: Either (a) delete all rows for `source_id` in Seahorse before re-upserting, or (b) use the AKB chunk UUID as the Seahorse PK.

**Evidence**:
```python
# seahorse.py:60-62
def _seahorse_pk(source_id: str, chunk_index: int) -> str:
    return f"{source_id}{_RS}{int(chunk_index)}"

# seahorse.py:281 — upsert uses this PK
COL_ID: _seahorse_pk(str(source_id), int(chunk_index)),
COL_EXTERNAL_CHUNK_ID: str(chunk_id),

# seahorse.py:315 — delete uses external_chunk_id (data column, not PK)
filter_sql = f"{COL_EXTERNAL_CHUNK_ID} = '{_sql_quote(str(chunk_id))}'"
```

---

### F4: Delete-during-index race produces permanent orphan vector points — Severity: HIGH

**Component**: `embed_worker._process_once`, `delete_worker._process_deletes_once`
**File**: `backend/app/services/embed_worker.py:152-199`, `backend/app/services/delete_worker.py:107-124`

**Scenario**:

1. Chunk C is inserted with `vector_indexed_at = NULL`.
2. `embed_worker` claims C (within batch).
3. User deletes the document. `delete_document_chunks` enqueues C's UUID into `vector_delete_outbox` and deletes C from `chunks`.
4. `delete_worker` picks up the outbox row, deletes C from the vector store.
5. `embed_worker` (still holding the in-memory claim from step 2) embeds C and upserts to the vector store. `_mark_success` UPDATEs a row that no longer exists (0 rows, silent).
6. C is now orphaned in the vector store with no outbox row to clean it up.

**Observable bad state**: Vector search returns hits for deleted documents.

**Fix direction**: Before `store.upsert_one`, re-check that the chunk row still exists in PG within the same transaction. If gone, skip upsert + mark.

**Evidence**:
```python
# embed_worker.py:170-185 — no existence check before upsert
async with pool.acquire() as conn:
    async with conn.transaction():
        await store.upsert_one(conn=conn, chunk_id=str(row["id"]), ...)
        await _mark_success(conn, row["id"])
```

---

### F5: Qdrant `upsert_one` is not transactional — Severity: MED

**Component**: `vector_store.qdrant.QdrantStore.upsert_one`, `embed_worker._process_once`
**File**: `backend/app/services/vector_store/qdrant.py:148-149`, `backend/app/services/embed_worker.py:170-185`

**Scenario**: For Qdrant, `upsert_one` writes to Qdrant (external HTTP) inside a PG transaction. If `_mark_success` fails after the Qdrant write succeeds, the PG TX rolls back, leaving the row unmarked. Re-upsert is idempotent so no data loss, but observability gap: `_mark_failure` runs in a separate transaction that itself can fail, leaving the row with `next_attempt_at` pushed but no error recorded.

**Fix direction**: No code change needed for steady-state idempotency. Log outbox-short-circuit alongside the count of unprocessed rows for observability.

---

### F6: `recompute_stats` races across multiple pods — Severity: MED

**Component**: `sparse_encoder.recompute_stats`
**File**: `backend/app/services/sparse_encoder.py:348-360`

**Scenario**: With multiple pods each running `start_stats_refresher`, two concurrent `recompute_stats` invocations both zero `bm25_vocab.df` and recompute. Their scans run against the live corpus without locking chunks; concurrent document inserts between the two scans produce inconsistent df counts.

**Observable bad state**: BM25 sparse search quality silently degrades during concurrent recompute_stats runs.

**Fix direction**: Add advisory lock or `SELECT ... FOR UPDATE` on `bm25_stats WHERE id = 1` at start of `recompute_stats`.

**Evidence**:
```python
# sparse_encoder.py:331-373 — all inside one transaction but no cross-pod lock
async with pool.acquire() as conn:
    async with conn.transaction():
        await conn.execute("UPDATE bm25_vocab SET df = 0, updated_at = NOW()")
        ...
        await conn.execute("UPDATE bm25_vocab v SET df = c.cnt ...")
        await conn.execute("UPDATE bm25_stats SET total_docs = $1 ...")
```

---

### F7: Graceful shutdown cancels in-flight upserts leaving chunks in 10-minute limbo — Severity: MED

**Component**: `_backfill.BackfillRunner.stop`
**File**: `backend/app/services/_backfill.py:64-73`

**Scenario**: `stop()` uses 5s timeout. If `_process_once` is mid-upsert (embed/vector-store HTTP can take up to 60s), cancellation triggers ROLLBACK. `vector_next_attempt_at` was already pushed out 10 min by claim phase. Kubernetes rolling restarts routinely hit this window.

**Observable bad state**: After rolling restart, freshly-written documents invisible to vector search for up to 10 minutes.

**Fix direction**: Increase stop timeout to cover longest expected upsert. Use `asyncio.shield` on per-chunk transaction so shutdown waits for current chunk to finish.

**Evidence**:
```python
# _backfill.py:68-71
try:
    await asyncio.wait_for(self._task, timeout=5.0)
except asyncio.TimeoutError:
    self._task.cancel()
```

---

### F8: `enqueue_source_deletes` without `conn` breaks outbox atomicity — Severity: MED

**Component**: `delete_worker.enqueue_source_deletes`
**File**: `backend/app/services/delete_worker.py:131-156`

**Scenario**: The `conn=None` fallback path acquires its own pool connection without opening a transaction. The SELECT and downstream caller's DELETE are on separate implicit transactions, reversing the intended outbox ordering. A future caller that forgets to pass `conn` silently breaks the atomicity contract.

**Fix direction**: Require `conn` to always be provided (raise on None), or wrap the standalone path in an explicit transaction.

**Evidence**:
```python
# delete_worker.py:152-156 — no transaction wrapper in the None-conn path
if conn is not None:
    return await _run(conn)
pool = await get_pool()
async with pool.acquire() as c:      # no async with c.transaction()
    return await _run(c)
```

---

### F9: Re-index interaction with Seahorse PK collision — Severity: LOW (amplifies F3)

**Component**: `delete_worker._claim_delete_batch`
**File**: `backend/app/services/delete_worker.py:139-141, 58-61`

On pgvector and Qdrant drivers (chunk UUID is the point id), re-index is safe. On Seahorse (F3), the outbox delete may fail to remove stale rows. This is informational; see F3 for the fix.

---

## Files audited

- `backend/app/services/embed_worker.py`
- `backend/app/services/delete_worker.py`
- `backend/app/services/index_service.py`
- `backend/app/services/sparse_encoder.py`
- `backend/app/services/_backfill.py`
- `backend/app/services/lifecycle.py`
- `backend/app/services/document_service.py`
- `backend/app/services/vector_store/{pgvector,qdrant,seahorse,base,factory}.py`
- `backend/app/services/search_service.py`
- `backend/app/services/health.py`
- `backend/app/services/s3_delete_worker.py`
