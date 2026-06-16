# Search ACL scale — bounded prefilter + limit clamp (issue #189)

Status: **Phase 1 accepted/implemented** · Phase 2 (vault_id pushdown) proposed,
needs migration + reindex approval.

## Problem (issue #189)

`SearchService.search` (behind both REST and MCP) has two spots that don't scale
with document count:

1. **Unbounded candidate prefilter.** Because the handlers always forward
   `user_id`, `has_filters` is effectively always true, so every search
   materializes *all* document/table/file ids the user can read (three SQL
   queries with **no LIMIT**, `search_service.py:225/251/277`) into
   `candidate_source_ids`, then passes that whole id list to the vector store as
   the only filter primitive (`source_ids`). It's O(accessible-corpus) per query:
   - pgvector → `= ANY($1::uuid[])` (planner cost grows with the array),
   - qdrant → `MatchAny(any=[...all ids...])` (filter-eval cost blows up),
   - seahorse_db / seahorse_cloud → a giant `source_id IN ('uuid', …)` **string
     over HTTP** that, past the query-size limit, raises → caught and **silently
     returns `[]`** (the user sees "no results" with no cause).
2. **No server-side `limit` clamp.** The MCP schema says `maximum: 50` but that's
   client-side only; a direct REST call or non-validating client can pass an
   arbitrary `limit` that propagates into the prefetch
   (`target_unique*3`, `prefetch_per_leg`).

## Analysis (4-angle, validated)

- **ACL is purely per-vault.** There is zero per-document/table/file ACL in AKB;
  read access is entirely vault membership (owner / `vault_access` grant /
  `public_access`). So a **vault-level** vector filter is ACL-equivalent to the
  current per-doc id enumeration. Cardinality: a user accesses ~tens of vaults
  vs thousands+ of docs.
- **Vector points carry no `vault_id`.** The chunks payload (all four drivers)
  stores only `source_id`/`source_type`/`section_path`/content/vectors — so the
  only filter primitive today is an id list. `vault_id` *is* available at index
  time (`embed_worker` / `reindex_all` already pass it).
- **Industry pattern (unanimous):** isolate tenants by **one small field**, never
  an id list — Qdrant payload index `is_tenant=true`, Pinecone namespaces,
  Weaviate per-tenant shard, Milvus partition-key, pgvector indexed `tenant_id` +
  `WHERE`. Passing thousands of ids in `MatchAny` / `IN (...)` is the documented
  anti-pattern AKB reproduces.
- **All four drivers can hold a `vault_id` payload + filter natively.** Effort:
  pgvector lowest (column + index, zero-downtime), qdrant very low (idempotent
  payload index), seahorse_cloud low (column + ALTER), seahorse_db/Coral moderate
  (immutable schema → table recreate). A single contract change
  (`hybrid_search(vault_ids=…)`) unifies all four. Backfill reuses the existing
  reindex path.

## Decision — phased

### Phase 1 (implemented here, no migration, ship immediately)

1. **Server-side limit clamp.** `clamp_search_limit(limit) = max(1, min(limit,
   settings.search_limit_max))` (config, default 50), applied at the
   `SearchService.search` and `.grep` entry points — the single chokepoint for
   MCP + REST + internal. Blocks the client-bypass blowup.
2. **Surface the silent failure.** `_run_vector_search` now returns
   `(hits, degradation_reason)` and distinguishes `VectorStoreUnavailable`
   (transient outage → warning, `"vector_store_unavailable"`) from any other
   exception (e.g. seahorse filter-size overflow → `logger.error`,
   `"vector_store_error"`). `SearchResponse` gains `degraded: bool` +
   `degradation_reason: str | None`, so an incomplete/empty result from a store
   failure is no longer indistinguishable from a genuine zero-match. Backward
   compatible (fields default to `False`/`None`).

Phase 1 does **not** remove the O(corpus) candidate materialization — it bounds
the limit and makes the seahorse overflow *visible*. The structural fix is Phase 2.

### Phase 2 (proposed — needs migration + reindex approval)

Push ACL down to **vault granularity** (the canonical multi-tenant pattern):

1. Add `vault_id` to the vector-store point payload (pgvector column + index;
   qdrant payload + idempotent index; seahorse_cloud/db column).
   **Backfill is metadata-only — NOT a re-embed** — because `vault_id` is
   derivable from the already-stored `source_id` and the embeddings don't change:
   - **pgvector** (the production driver): `ALTER TABLE vector_index.chunks ADD
     COLUMN vault_id` → one `UPDATE … SET vault_id = src.vault_id FROM (source
     JOIN) WHERE source_id = src.id` → `CREATE INDEX`. Seconds–minutes, zero
     embedding-API calls.
   - **qdrant**: batch `set_payload` from a `source_id → vault_id` map (vectors
     untouched). No re-embed.
   - **seahorse_cloud**: `ALTER` + `UPDATE` if the column can be added in place;
     otherwise re-upsert.
   - **seahorse_db / Coral** is the ONLY full-re-embed case (immutable schema →
     drop + recreate → `scripts/reindex_all.py`). Production does not use it.
   During the backfill window, gate the vault filter (fall back to the
   `source_ids` path) until `vault_id` is fully populated, so no points are
   missed.
2. Extend `VectorStore.hybrid_search` with `vault_ids: list[str] | None`
   (keep `source_ids` for the doc-filter / `source_uris` path).
3. Two codepaths in `SearchService.search`:
   - **no doc-level filter** (collection/doc_type/tags/source_uris absent) →
     resolve accessible `vault_ids` once (small), pass as the vector filter, skip
     the doc enumeration → O(vaults).
   - **doc-level filter present** → enumerate accessible `vault_ids`, then
     `doc_ids` *within those vaults* (bounded by the vault set, tractable) →
     pass `source_ids`.
4. (Optional) apply the same vault-filter optimization to `grep`'s ACL scan.

**Operational caveats:** a backfill window where un-reindexed points lack
`vault_id` (filter would miss them) → reindex before cutover or gate on a flag;
seahorse_db/Coral schema is immutable (drop+recreate). These are why Phase 2 is
gated behind explicit approval.

## Files (Phase 1)

- `backend/app/config.py` — `search_limit_max` setting.
- `backend/app/models/document.py` — `SearchResponse.degraded` + `.degradation_reason`.
- `backend/app/services/search_service.py` — `clamp_search_limit`, clamp at
  `search`/`grep` entry, `_run_vector_search` returns `(hits, reason)` with
  error-class discrimination, degraded threaded into both responses.
- `backend/tests/test_search_rerank_fusion.py` — clamp + degraded unit tests.
