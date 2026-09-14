-- Optional maintenance for EXISTING pgvector posting deployments.
-- Run explicitly with psql against the vector-store database, not necessarily
-- the main AKB database. Never run inside BEGIN or psql --single-transaction.
-- Example: psql "$VECTOR_DSN" -v schema=vector_index -f this-file.sql
-- Review free disk/WAL/replica capacity first. Each build scans a large table;
-- CONCURRENTLY permits writes but is not resource-free or instantaneous.
-- Fresh installs already include these columns in their original indexes.
-- Arrays-shaped stores do not have posting; this script is posting-only.
\set ON_ERROR_STOP on
\if :{?schema}
\else
  \set schema vector_index
\endif

CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_posting_search_covering
    ON :"schema".posting (term_id, chunk_id) INCLUDE (weight);
CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_vi_chunks_vault_covering
    ON :"schema".chunks (vault_id) INCLUDE (chunk_id);
CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_vi_chunks_source_covering
    ON :"schema".chunks (source_id) INCLUDE (chunk_id);

-- IF NOT EXISTS does not repair an INVALID index left by a cancelled build.
-- Inspect these rows; if invalid, drop ONLY that named index CONCURRENTLY
-- outside a transaction and retry in an approved maintenance window.
SELECT c.relname, i.indisvalid, i.indisready, pg_get_indexdef(c.oid)
FROM pg_index i JOIN pg_class c ON c.oid = i.indexrelid
JOIN pg_namespace n ON n.oid = c.relnamespace
WHERE n.nspname = :'schema'
  AND c.relname IN ('idx_posting_search_covering', 'idx_vi_chunks_vault_covering',
                   'idx_vi_chunks_source_covering');

-- Preserve the old indexes/primary key. Assess redundancy and write overhead
-- separately; this script intentionally does not drop existing structures.
-- Autovacuum/visibility-map coverage controls whether index-only scans can
-- avoid heap visits. Measure EXPLAIN (ANALYZE, BUFFERS) on an isolated replica
-- or approved workload; adding the index alone is not a latency guarantee.
