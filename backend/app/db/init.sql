-- AKB Database Schema
-- PostgreSQL only — main DB has no vector dependency. The pgvector
-- extension lives in the vector store (which may be the same PG
-- instance under a separate schema, but that's a deploy choice, not
-- a hard requirement of this DB).

CREATE EXTENSION IF NOT EXISTS "uuid-ossp";

CREATE EXTENSION IF NOT EXISTS pgcrypto;

-- ============================================================
-- Migration ledger — records which migration scripts have been applied
-- so they are NOT re-run on every boot. Several migrations ALTER the hot
-- `chunks` table (ACCESS EXCLUSIVE lock); re-running them each startup
-- races live workers' open transactions and can stall a rolling deploy.
-- With the ledger, a steady-state boot runs zero migration DDL.
-- (init.sql itself is still re-run every boot — it is pure CREATE … IF
-- NOT EXISTS and takes no conflicting locks.)
-- ============================================================
CREATE TABLE IF NOT EXISTS schema_migrations (
    filename   TEXT PRIMARY KEY,
    applied_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- ============================================================
-- Users
-- ============================================================
CREATE TABLE IF NOT EXISTS users (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    username TEXT NOT NULL UNIQUE,
    email TEXT NOT NULL UNIQUE,
    password_hash TEXT NOT NULL,
    display_name TEXT,
    is_admin BOOLEAN NOT NULL DEFAULT false,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    -- JWT revocation cutoff. A JWT is rejected if its `iat` claim is
    -- earlier than this timestamp. Default epoch keeps every JWT issued
    -- before the user explicitly revokes valid until natural expiry.
    -- Set to NOW() via POST /auth/revoke-all-sessions, admin force-logout,
    -- and automatically inside change_password.
    tokens_revoked_before TIMESTAMPTZ NOT NULL DEFAULT TIMESTAMPTZ '1970-01-01 00:00:00+00',
    -- How the account authenticates. 'local' = bcrypt password (the
    -- baseline). 'keycloak' = JIT-provisioned on first OIDC login; its
    -- password_hash is an unusable sentinel. Advisory only — Keycloak
    -- itself is gated by keycloak_enabled in config; when SSO is off this
    -- column is never read. See migration 033.
    auth_provider TEXT NOT NULL DEFAULT 'local'
);

-- ============================================================
-- Personal Access Tokens (PAT)
-- ============================================================
CREATE TABLE IF NOT EXISTS tokens (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    name TEXT NOT NULL,                -- e.g. "claude-code-macbook"
    token_hash TEXT NOT NULL UNIQUE,   -- sha256 of the token
    token_prefix TEXT NOT NULL,        -- first 8 chars for identification (akb_xxxx)
    scopes TEXT[] DEFAULT '{read,write}',  -- read, write, admin
    expires_at TIMESTAMPTZ,
    last_used_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_tokens_hash ON tokens(token_hash);
CREATE INDEX IF NOT EXISTS idx_tokens_user ON tokens(user_id);

-- ============================================================
-- OIDC transients — short-lived state for the OPTIONAL Keycloak login
-- flow (CSRF state + PKCE verifier, and one-time exchange codes mapping
-- to a freshly minted AKB JWT). HA-safe (shared across replicas),
-- single-use, TTL-bounded. Stays empty when Keycloak is disabled.
-- See migration 034.
-- ============================================================
CREATE TABLE IF NOT EXISTS oidc_transients (
    key         TEXT PRIMARY KEY,
    kind        TEXT NOT NULL,          -- 'state' | 'exchange'
    payload     JSONB NOT NULL DEFAULT '{}'::jsonb,
    expires_at  TIMESTAMPTZ NOT NULL,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_oidc_transients_expiry ON oidc_transients(expires_at);

-- ============================================================
-- Vaults (each maps to a Git bare repo)
-- ============================================================
CREATE TABLE IF NOT EXISTS vaults (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    name TEXT NOT NULL UNIQUE,
    description TEXT,
    git_path TEXT NOT NULL,           -- path to bare repo on disk
    owner_id UUID REFERENCES users(id),
    public_access TEXT NOT NULL DEFAULT 'none' CHECK (public_access IN ('none','reader','writer')),  -- none, reader, writer
    status TEXT NOT NULL DEFAULT 'active',  -- active, archived
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- ============================================================
-- Vault access (user-level roles)
-- ============================================================
CREATE TABLE IF NOT EXISTS vault_access (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    vault_id UUID NOT NULL REFERENCES vaults(id) ON DELETE CASCADE,
    user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    role TEXT NOT NULL DEFAULT 'reader',  -- owner, admin, writer, reader
    granted_by UUID REFERENCES users(id),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE(vault_id, user_id)
);

CREATE INDEX IF NOT EXISTS idx_vault_access_user ON vault_access(user_id);
CREATE INDEX IF NOT EXISTS idx_vault_access_vault ON vault_access(vault_id);

-- ============================================================
-- Collections (L1 - directory-level metadata cache)
-- ============================================================
CREATE TABLE IF NOT EXISTS collections (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    vault_id UUID NOT NULL REFERENCES vaults(id) ON DELETE CASCADE,
    path TEXT NOT NULL,                -- relative path within vault
    name TEXT NOT NULL,
    summary TEXT,                      -- L1 summary (auto-generated)
    doc_count INTEGER NOT NULL DEFAULT 0,
    last_updated TIMESTAMPTZ,
    UNIQUE(vault_id, path)
);

-- ============================================================
-- Documents (L2 - index of Git-stored markdown files)
-- ============================================================
CREATE TABLE IF NOT EXISTS documents (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    vault_id UUID NOT NULL REFERENCES vaults(id) ON DELETE CASCADE,
    collection_id UUID REFERENCES collections(id) ON DELETE SET NULL,
    path TEXT NOT NULL,                -- relative path within vault (e.g. "api-specs/payment-v2.md")
    title TEXT NOT NULL,
    doc_type TEXT,                     -- note, report, decision, spec, plan, session, task, reference
    status TEXT NOT NULL DEFAULT 'draft',  -- draft, active, archived
    summary TEXT,                      -- L2 summary (auto-generated or author-provided)
    domain TEXT,                       -- engineering, product, ops, legal, ...
    created_by TEXT,                   -- principal who created
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    current_commit TEXT,               -- Git commit hash
    content_hash TEXT,                 -- sha256 of canonical document body returned by AKB
    hash_algorithm TEXT DEFAULT 'sha256',
    content_hash_commit TEXT,          -- commit the content_hash projection was computed from
    tags TEXT[] DEFAULT '{}',
    metadata JSONB DEFAULT '{}',       -- extended metadata from frontmatter
    UNIQUE(vault_id, path)
);

CREATE INDEX IF NOT EXISTS idx_documents_vault ON documents(vault_id);
CREATE INDEX IF NOT EXISTS idx_documents_collection ON documents(collection_id);
CREATE INDEX IF NOT EXISTS idx_documents_type ON documents(doc_type);
CREATE INDEX IF NOT EXISTS idx_documents_status ON documents(status);
CREATE INDEX IF NOT EXISTS idx_documents_tags ON documents USING gin(tags);
CREATE INDEX IF NOT EXISTS idx_documents_created_by ON documents(created_by);

-- ============================================================
-- Chunks (L3 - section-level content; SoT for re-indexing)
-- ============================================================
-- Chunks are indexable units from any of documents, tables, or files
-- (discriminator = source_type). FK CASCADE is NOT used because the
-- source can live in three different tables; document_service /
-- table_service / file_service drop their own chunks explicitly on
-- delete.
--
-- The dense embedding and BM25 sparse vector are NOT stored here —
-- they live in the configured vector store (driver-pluggable). Re-
-- indexing from text is always cheap because vocab + tokenizer +
-- embedding model are deterministic functions of (content, model).
-- The vector_*_at columns track per-chunk indexing state so the
-- worker can resume after crashes.
CREATE TABLE IF NOT EXISTS chunks (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    source_type TEXT NOT NULL DEFAULT 'document'
        CHECK (source_type IN ('document','table','file')),
    source_id UUID NOT NULL,
    vault_id UUID NOT NULL REFERENCES vaults(id) ON DELETE CASCADE,
    section_path TEXT,
    content TEXT NOT NULL,
    chunk_index INTEGER NOT NULL,
    char_start INTEGER,
    char_end INTEGER,
    -- Indexing state (single stage: embed → sparse → vector-store upsert).
    vector_indexed_at TIMESTAMPTZ,
    vector_next_attempt_at TIMESTAMPTZ,
    vector_retry_count INTEGER NOT NULL DEFAULT 0,
    vector_last_error TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_chunks_source ON chunks (source_type, source_id);
-- Indexing-queue claim order (newest chunks first; see embed_worker._claim_batch).
-- Also covers the retry-eligibility WHERE filter — a single partial
-- index is enough; we used to keep idx_chunks_vector_pending alongside
-- this for vector_next_attempt_at, but the planner was selecting the
-- ORDER-BY-aligned index anyway, so the second one was dead weight.
CREATE INDEX IF NOT EXISTS idx_chunks_indexing_queue
    ON chunks (created_at DESC, id)
 WHERE vector_indexed_at IS NULL;
-- idx_chunks_vault_id is created by migration 014 because on a pre-existing
-- DB the chunks table is older than the vault_id column. Putting the index
-- here would fail on the very first init.sql pass after upgrade (column
-- doesn't exist yet), preventing migrations from ever running. Migration
-- 014 adds the column AND the index in one transaction; init.sql stays
-- minimal so init_db() doesn't get blocked on a forward-looking index.

-- ============================================================
-- Vault Tables (structured data alongside documents)
-- ============================================================
CREATE TABLE IF NOT EXISTS vault_tables (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    vault_id UUID NOT NULL REFERENCES vaults(id) ON DELETE CASCADE,
    collection_id UUID REFERENCES collections(id) ON DELETE SET NULL,
    name TEXT NOT NULL,
    description TEXT,
    columns JSONB NOT NULL DEFAULT '[]',
    created_by TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE(vault_id, name)
);

CREATE TABLE IF NOT EXISTS vault_table_rows (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    table_id UUID NOT NULL REFERENCES vault_tables(id) ON DELETE CASCADE,
    data JSONB NOT NULL DEFAULT '{}',
    created_by TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_vault_tables_vault ON vault_tables(vault_id);
CREATE INDEX IF NOT EXISTS idx_vault_tables_collection ON vault_tables(collection_id);
CREATE INDEX IF NOT EXISTS idx_vault_table_rows_table ON vault_table_rows(table_id);
CREATE INDEX IF NOT EXISTS idx_vault_table_rows_data ON vault_table_rows USING gin(data);

-- ============================================================
-- Edges (unified cross-type relation graph via URI scheme)
-- Replaces document-only 'relations' for cross-type connections.
-- URI format: akb://{vault}/doc/{path}
--             akb://{vault}/table/{name}
--             akb://{vault}/file/{id}
-- ============================================================
CREATE TABLE IF NOT EXISTS edges (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    vault_id UUID NOT NULL REFERENCES vaults(id) ON DELETE CASCADE,
    source_uri TEXT NOT NULL,           -- akb://vault/doc/path or table/name or file/id
    target_uri TEXT NOT NULL,
    relation_type TEXT NOT NULL,        -- depends_on, related_to, implements, links_to, references, attached_to, derived_from
    source_type TEXT NOT NULL CHECK(source_type IN ('doc', 'table', 'file')),
    target_type TEXT NOT NULL CHECK(target_type IN ('doc', 'table', 'file')),
    metadata JSONB DEFAULT '{}',
    created_by TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    -- 'implicit' = derived from frontmatter / body markdown links (rewritten on
    -- every document write). 'explicit' = created via akb_link and durable
    -- across document writes. Without this discriminator,
    -- store_document_relations' DELETE-then-reinsert pattern silently destroys
    -- every akb_link-created edge on the next akb_update of the source doc.
    kind TEXT NOT NULL DEFAULT 'implicit' CHECK(kind IN ('implicit', 'explicit')),
    UNIQUE(source_uri, target_uri, relation_type)
);

CREATE INDEX IF NOT EXISTS idx_edges_vault ON edges(vault_id);
CREATE INDEX IF NOT EXISTS idx_edges_source ON edges(source_uri);
CREATE INDEX IF NOT EXISTS idx_edges_target ON edges(target_uri);
CREATE INDEX IF NOT EXISTS idx_edges_type ON edges(relation_type);
CREATE INDEX IF NOT EXISTS idx_edges_source_type ON edges(source_type);
CREATE INDEX IF NOT EXISTS idx_edges_target_type ON edges(target_type);

-- ============================================================
-- Resource aliases (rename/move redirects)
-- A former reference (old path/name) → the CURRENT resource id. Keying on the
-- durable id (never on a new path) means N renames collapse to one hop — no
-- redirect chains (cf. MediaWiki). find_by_ref consults this so old akb:// URIs
-- keep resolving after a move. See docs/designs/doc-identity-slug/00-overview.md.
-- ============================================================
CREATE TABLE IF NOT EXISTS resource_aliases (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    vault_id UUID NOT NULL REFERENCES vaults(id) ON DELETE CASCADE,
    resource_type TEXT NOT NULL CHECK(resource_type IN ('document', 'table', 'file')),
    old_ref TEXT NOT NULL,              -- former path (doc/file) or name (table)
    resource_id UUID NOT NULL,          -- current resource id — NEVER a path (no chains)
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    -- One alias per (vault, type, old_ref). If the old_ref is later reused by a
    -- NEW resource, the writer drops the stale alias first (a real resource at a
    -- path always wins over a redirect).
    UNIQUE(vault_id, resource_type, old_ref)
);

CREATE INDEX IF NOT EXISTS idx_resource_aliases_lookup
    ON resource_aliases(vault_id, resource_type, old_ref);
CREATE INDEX IF NOT EXISTS idx_resource_aliases_resource
    ON resource_aliases(resource_id);

-- ============================================================
-- Vault Files (S3-backed binary/large file storage)
-- ============================================================
CREATE TABLE IF NOT EXISTS vault_files (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    vault_id UUID NOT NULL REFERENCES vaults(id) ON DELETE CASCADE,
    collection_id UUID REFERENCES collections(id) ON DELETE SET NULL,
    name TEXT NOT NULL,
    s3_key TEXT NOT NULL,
    mime_type TEXT,
    size_bytes BIGINT,
    content_hash TEXT,                 -- sha256 of file bytes
    hash_algorithm TEXT DEFAULT 'sha256',
    etag TEXT,                         -- object-store ETag/checksum hint
    storage_version TEXT,              -- object-store version id when available
    hash_verified_at TIMESTAMPTZ,      -- when AKB last verified content_hash from storage bytes
    description TEXT,
    created_by TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE(vault_id, s3_key)
);

CREATE INDEX IF NOT EXISTS idx_vault_files_vault ON vault_files(vault_id);
CREATE INDEX IF NOT EXISTS idx_vault_files_collection ON vault_files(collection_id);

-- ============================================================
-- Publications (unified public-link feature for documents, tables, files)
-- A publication makes a resource accessible via /p/{slug} without auth.
-- ============================================================
CREATE TABLE IF NOT EXISTS publications (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    slug TEXT NOT NULL UNIQUE,
    vault_id UUID NOT NULL REFERENCES vaults(id) ON DELETE CASCADE,
    resource_type TEXT NOT NULL CHECK(resource_type IN ('document', 'table_query', 'file')),

    -- Canonical resource handle — `akb://{vault}/{type}/{identifier}`.
    -- NULL only for table_query publications, which surface a SQL query
    -- rather than a single addressable resource. The cascade on
    -- vault/document/file delete still works because vault_id has
    -- ON DELETE CASCADE; document/file deletion bumps publications
    -- via app-level cleanup (delete_publications_for_document /
    -- delete_publications_for_file).
    resource_uri TEXT,

    -- For table_query type: stored canned SQL with :param placeholders
    query_sql TEXT,
    query_vault_names TEXT[],
    query_params JSONB DEFAULT '{}',  -- {param_name: {type, default, required}}

    -- Access control
    password_hash TEXT,                -- bcrypt hash, NULL = no password
    max_views INTEGER,                 -- NULL = unlimited
    view_count INTEGER NOT NULL DEFAULT 0,
    expires_at TIMESTAMPTZ,            -- NULL = never expires

    -- Snapshot mode (P4)
    mode TEXT NOT NULL DEFAULT 'live' CHECK(mode IN ('live', 'snapshot')),
    snapshot_s3_key TEXT,
    snapshot_at TIMESTAMPTZ,

    -- Embed / section filter (P5)
    section_filter TEXT,
    allow_embed BOOLEAN NOT NULL DEFAULT true,

    -- Metadata
    title TEXT,
    created_by UUID REFERENCES users(id),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_publications_slug ON publications(slug);
CREATE INDEX IF NOT EXISTS idx_publications_vault ON publications(vault_id);
CREATE INDEX IF NOT EXISTS idx_publications_resource_uri ON publications(resource_uri) WHERE resource_uri IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_publications_expires ON publications(expires_at) WHERE expires_at IS NOT NULL;

-- ============================================================
-- Todos (per-user task assignments)
-- ============================================================
CREATE TABLE IF NOT EXISTS todos (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    assignee_id UUID NOT NULL REFERENCES users(id),
    created_by UUID NOT NULL REFERENCES users(id),
    vault_id UUID REFERENCES vaults(id),
    title TEXT NOT NULL,
    note TEXT,
    ref_doc_id UUID REFERENCES documents(id) ON DELETE SET NULL,
    priority TEXT DEFAULT 'normal',
    status TEXT DEFAULT 'open',
    due_date DATE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    completed_at TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS idx_todos_assignee ON todos(assignee_id, status);
CREATE INDEX IF NOT EXISTS idx_todos_created_by ON todos(created_by);

-- Agent memories + sessions tables removed in v0.4.0 alongside the
-- akb_remember/recall/forget MCP tool family. Agent dedicated memory
-- is now expressed as a vault (agent-memory-{username}) with per-session
-- collections, driven by the /api/v1/agent-sessions REST endpoints.
-- Existing rows are dropped by migration 031.
