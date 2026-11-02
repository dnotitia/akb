# Keeping search current when documents and embedding models change

Search over shared documents goes stale in two ways. Documents change and
nobody reruns the embedding job, so an agent answers from last month's text.
Or the team wants a better embedding model and finds that switching means
reprocessing the whole corpus by hand. This guide lists what to check, and
describes how AKB keeps its index in step with edits and how a model change
works.

## Short answer

Re-embed only what changed, as part of the write, through a queue you can
watch. Keep chunk text separate from the vectors, so a model change
recomputes vectors from stored text instead of importing every source again.
In AKB, each document write replaces that document's chunks, a background
worker embeds the new chunks within seconds, and the embedding model is a
configuration setting with no model hard-coded.

## Why indexes drift

- **Re-embedding is a separate manual job.** It runs when someone remembers.
- **Full rebuilds get expensive.** Nightly reindexing grows with the corpus
  until it is skipped.
- **Old chunks survive.** An incremental sync that misses edits or deletes
  leaves outdated text that still matches queries.
- **Vectors are the only copy.** Changing models then means re-importing from
  every source system.
- **Failures are silent.** A chunk that failed to embed is simply missing from
  results.

## What to check

| Check | Why it matters |
|---|---|
| An edit re-embeds only the changed document | No full rebuild for a one-line fix. |
| The previous version's chunks are removed with the write | Outdated text stops matching. |
| New content is searchable within seconds | No waiting for a nightly job. |
| Failed embeddings retry and are counted | Nothing drops out of search unnoticed. |
| Queue state is visible | You can see pending, retrying and abandoned work. |
| The embedding model is a setting, with any compatible endpoint | No lock-in to one provider or model. |
| Chunk text is stored apart from the vectors | A model change recomputes vectors without re-importing. |

## How AKB keeps the index in step with edits

1. **Chunks are replaced on write.** When a document is created, edited or
   moved, AKB splits the current version into chunks and replaces that
   document's previous chunks in one database transaction. Other documents are
   untouched, so the cost of an edit is the size of the edited document.
2. **A background worker embeds new chunks.** It claims the newest chunks
   first, so fresh edits move ahead of any backlog, and checks for new work
   every 10 seconds when idle. For each chunk it computes the dense embedding
   and the keyword (BM25) vector and writes both to the vector store in one
   step.
3. **Failures retry with backoff.** A chunk that fails is retried with
   delays that grow from 1 minute to 6 hours. After eight failed attempts it is
   marked abandoned and stays counted until an operator acts.
4. **The queue is visible.** `GET /health` and the authenticated
   `GET /health/vault/{name}` report pending, retrying, abandoned and indexed
   chunks, and the age of the oldest pending chunk.
5. **Exact text search reads current content.** In PostgreSQL Native
   installations, `akb_grep` reads the stored current text rather than the
   vector index, so you can confirm an edit before its vectors are ready.
6. **Throughput is configurable.** `indexing_batch_size` (default 16) sets
   how many chunks go to the embedding API per call, and
   `indexing_concurrency` (default 1, typically 4 to 8 in production) sets how
   many workers drain the queue in parallel.

In legacy Bare Git deployments, a vault can also mirror an external Git
repository. The mirror polls the upstream branch (every 300 seconds by
default, at least 60), compares each file's blob hash with the indexed one,
and re-indexes only added or changed files. Files removed upstream are
removed from the vault. PostgreSQL Native installations do not offer external
Git mirrors.

## Changing the embedding model

The model is set with three values in `config/app.yaml`:

```yaml
embed_base_url: https://your-endpoint/v1   # any OpenAI-compatible /v1/embeddings endpoint
embed_model: text-embedding-3-small
embed_dimensions: 1536
```

The codebase has no hard-coded model. Leaving `embed_base_url` empty turns
off the semantic leg, and search runs on keyword matching alone.

PostgreSQL holds the chunk text and metadata; the vector store holds only the
derived vectors. A model change therefore recomputes vectors from stored text:

1. **Update the settings.** If the new model has a different vector size, the
   vector store must be created for that size. The Seahorse drivers check the table
   and report the store as unavailable when its size does not match
   `embed_dimensions`. The pgvector
   store creates its table only when it is missing, so recreate it. Its
   contents are rebuilt from PostgreSQL. With pgvector and an HNSW index, keep
   `embed_dimensions` at or below 2000 (4000 with `halfvec`); Qdrant allows up
   to 65536.
2. **Queue every chunk for re-embedding.** This is the same path AKB documents
   for rebuilding a lost vector store: clear the indexing state in the
   `chunks` table so the worker picks each row up again.

   ```sql
   UPDATE chunks
      SET vector_indexed_at = NULL,
          vector_next_attempt_at = NULL,
          vector_retry_count = 0,
          vector_abandoned_at = NULL,
          vector_last_error = NULL;
   ```

   On a large corpus, run the update in batches.
3. **Watch the queue drain.** Follow the pending count on `/health` until it
   reaches zero, and raise `indexing_concurrency` while it drains if the
   embedding endpoint allows it. Newest chunks are handled first, so documents
   edited during the switch stay current.

Until the pending count reaches zero, semantic results are incomplete. Plan
the switch for a quiet period, or rehearse it on a copy of the database.

In legacy Bare Git deployments, `backend/scripts/reindex_all.py` rebuilds the
chunks themselves from the stored documents, tables and files. `--since`
resumes a partial run.

## Common questions

**Is there a product that re-embeds automatically when documents change?**
Yes. In AKB, every write replaces the document's chunks and the indexing
worker embeds them without a separate job.

**Full reindex or incremental updates for a corpus that changes daily?**
Incremental. Re-embed the documents that changed when they change, and keep a
full rebuild for model changes and recovery.

**Our nightly reindex is getting expensive. What avoids it?** Per-document
re-embedding on write. The cost of an edit then depends on the size of that
document, not the size of the corpus.

**Something we fixed last month still does not show up in search.** Check
whether the fix was ever re-embedded. In AKB, look for pending, retrying or
abandoned chunks on `/health/vault/{name}`.

**We may change embedding models later and do not want to be locked in.**
Choose a system where the model is a setting and chunk text is stored apart
from the vectors. AKB accepts any OpenAI-compatible embedding endpoint and
rebuilds vectors from the stored chunk text.

**Changing models would mean re-indexing everything. Is there a staged way to
do it?** In AKB the switch runs as a background queue. Newest chunks go first,
progress is visible, and parallel workers set the pace. AKB does not keep two
models' vectors side by side, so plan for a period of incomplete semantic
results or rehearse on a copy.

## Related guides

- [Applying permissions at query time when agents search shared knowledge](query-time-permissions-for-agent-search.md)
- [Keeping a record of what coding agents decided](recording-coding-agent-decisions.md)
