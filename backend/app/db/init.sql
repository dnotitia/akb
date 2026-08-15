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
    is_recovery_admin BOOLEAN NOT NULL DEFAULT false,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    -- JWT revocation cutoff. A JWT is rejected if its `iat` claim is
    -- earlier than this timestamp. Default epoch keeps every JWT issued
    -- before the user explicitly revokes valid until natural expiry.
    -- Set to NOW() via POST /auth/revoke-all-sessions, admin force-logout,
    -- and automatically inside change_password.
    tokens_revoked_before TIMESTAMPTZ NOT NULL DEFAULT TIMESTAMPTZ '1970-01-01 00:00:00+00',
    -- How the account authenticates. 'local' = bcrypt password (the
    -- baseline). 'keycloak' = projected from a fully verified external
    -- principal; its password_hash is an unusable sentinel. Advisory only;
    -- route capabilities are selected by auth_mode. See migration 033.
    auth_provider TEXT NOT NULL DEFAULT 'local',
    -- Account-state and principal-kind guards are additive. Compatibility
    -- defaults preserve every pre-governance user as an active human.
    account_status TEXT NOT NULL DEFAULT 'active'
        CONSTRAINT users_account_status_check
        CHECK (account_status IN ('active', 'suspended')),
    account_kind TEXT NOT NULL DEFAULT 'human'
        CONSTRAINT users_account_kind_check
        CHECK (account_kind IN ('human', 'service')),
    CONSTRAINT users_recovery_admin_requires_admin
        CHECK (NOT is_recovery_admin OR is_admin),
    CONSTRAINT users_recovery_admin_provider_check
        CHECK (NOT is_recovery_admin OR auth_provider IN ('local', 'keycloak'))
);

CREATE UNIQUE INDEX IF NOT EXISTS users_one_recovery_admin_per_provider
    ON users (auth_provider)
    WHERE is_recovery_admin;

-- Stable external identity binding. Email is a mutable snapshot; verified
-- OIDC issuer + subject is the permanent key.
CREATE TABLE IF NOT EXISTS external_identities (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    issuer TEXT NOT NULL,
    subject TEXT NOT NULL,
    username_snapshot TEXT,
    email_snapshot TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    last_seen_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT external_identities_issuer_subject_key UNIQUE (issuer, subject)
);

CREATE INDEX IF NOT EXISTS idx_external_identities_user
    ON external_identities(user_id);
CREATE UNIQUE INDEX IF NOT EXISTS external_identities_id_user_key
    ON external_identities(id, user_id);

-- Singleton authority for the last accepted runtime auth boundary. The
-- installation generation is monotonic: an exact restart is accepted, a
-- greater generation performs one transition, and stale/conflicting starts
-- fail closed. Local mode never carries an SSO epoch.
CREATE TABLE IF NOT EXISTS auth_runtime_state (
    singleton BOOLEAN PRIMARY KEY DEFAULT TRUE
        CHECK (singleton),
    runtime_generation BIGINT NOT NULL
        CHECK (runtime_generation > 0),
    auth_mode TEXT NOT NULL
        CHECK (auth_mode IN ('local', 'sso')),
    sso_session_epoch UUID,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT auth_runtime_state_sso_session_epoch_key
        UNIQUE (sso_session_epoch),
    CONSTRAINT auth_runtime_state_epoch_shape
        CHECK (
            (auth_mode = 'local' AND sso_session_epoch IS NULL)
            OR
            (auth_mode = 'sso' AND sso_session_epoch IS NOT NULL)
        )
);

-- Machine-readable state for the one-time pre-epoch stop-the-world bridge.
-- `required` blocks normal startup until the explicit upgrade acknowledgement;
-- `enforced` makes legacy NULL writes fail; `rollback_ready` is written only
-- after every epoch-bound row and runtime authority row has been purged.
CREATE TABLE IF NOT EXISTS auth_runtime_epoch_upgrade (
    singleton BOOLEAN PRIMARY KEY DEFAULT TRUE
        CHECK (singleton),
    state TEXT NOT NULL
        CHECK (state IN ('ready', 'required', 'enforced', 'rollback_ready')),
    runtime_generation_floor BIGINT NOT NULL DEFAULT 0
        CHECK (runtime_generation_floor >= 0),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

INSERT INTO auth_runtime_epoch_upgrade (singleton, state)
VALUES (TRUE, 'ready')
ON CONFLICT (singleton) DO NOTHING;

-- Dedicated product-admin browser sessions. These rows contain only hashes
-- of opaque AKB session/CSRF values, an exact external-identity FK, and the
-- issuer/subject snapshot used to invalidate a changed binding. No Keycloak
-- access, refresh, or ID token is stored here.
CREATE TABLE IF NOT EXISTS admin_browser_sessions (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    -- Nullable only for pre-epoch image rollback compatibility. Migration 076
    -- installs a database guard that rejects NULL while current code is active.
    session_epoch UUID,
    token_hash TEXT NOT NULL UNIQUE
        CHECK (token_hash ~ '^[0-9a-f]{64}$'),
    csrf_token_hash TEXT NOT NULL
        CHECK (csrf_token_hash ~ '^[0-9a-f]{64}$'),
    user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    external_identity_id UUID NOT NULL,
    identity_issuer TEXT NOT NULL
        CHECK (char_length(identity_issuer) BETWEEN 1 AND 2048),
    identity_subject TEXT NOT NULL
        CHECK (char_length(identity_subject) BETWEEN 1 AND 1024),
    keycloak_sid TEXT NOT NULL
        CHECK (char_length(keycloak_sid) BETWEEN 1 AND 255),
    expires_at TIMESTAMPTZ NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    last_seen_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT admin_browser_session_external_user_fk
        FOREIGN KEY (external_identity_id, user_id)
        REFERENCES external_identities(id, user_id) ON DELETE CASCADE,
    CONSTRAINT admin_browser_session_epoch_fk
        FOREIGN KEY (session_epoch)
        REFERENCES auth_runtime_state(sso_session_epoch),
    CONSTRAINT admin_browser_session_positive_lifetime
        CHECK (expires_at > created_at)
);

CREATE INDEX IF NOT EXISTS idx_admin_browser_sessions_expiry
    ON admin_browser_sessions(expires_at);
CREATE INDEX IF NOT EXISTS idx_admin_browser_sessions_user
    ON admin_browser_sessions(user_id);

-- Ordinary-user SSO browser sessions. The browser receives only the opaque
-- token whose SHA-256 digest is stored here. Keycloak refresh and ID tokens
-- are held exclusively inside a versioned AES-256-GCM envelope bound to this
-- exact row and AKB user. Access tokens are verified and discarded.
CREATE TABLE IF NOT EXISTS sso_browser_sessions (
    id UUID PRIMARY KEY,
    session_epoch UUID,
    token_hash TEXT NOT NULL UNIQUE
        CHECK (token_hash ~ '^[0-9a-f]{64}$'),
    csrf_token_hash TEXT NOT NULL
        CHECK (csrf_token_hash ~ '^[0-9a-f]{64}$'),
    user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    external_identity_id UUID NOT NULL,
    identity_issuer TEXT NOT NULL
        CHECK (char_length(identity_issuer) BETWEEN 1 AND 2048),
    identity_subject TEXT NOT NULL
        CHECK (char_length(identity_subject) BETWEEN 1 AND 1024),
    keycloak_sid TEXT NOT NULL
        CHECK (char_length(keycloak_sid) BETWEEN 1 AND 255),
    token_envelope TEXT NOT NULL
        CHECK (char_length(token_envelope) BETWEEN 32 AND 65536),
    access_expires_at TIMESTAMPTZ NOT NULL,
    refresh_expires_at TIMESTAMPTZ NOT NULL,
    idle_expires_at TIMESTAMPTZ NOT NULL,
    absolute_expires_at TIMESTAMPTZ NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    last_seen_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    refreshed_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT sso_browser_session_external_user_fk
        FOREIGN KEY (external_identity_id, user_id)
        REFERENCES external_identities(id, user_id) ON DELETE CASCADE,
    CONSTRAINT sso_browser_session_epoch_fk
        FOREIGN KEY (session_epoch)
        REFERENCES auth_runtime_state(sso_session_epoch),
    CONSTRAINT sso_browser_session_positive_lifetime
        CHECK (
            access_expires_at > created_at
            AND refresh_expires_at > created_at
            AND idle_expires_at > created_at
            AND absolute_expires_at > created_at
            AND idle_expires_at <= absolute_expires_at
        )
);

CREATE INDEX IF NOT EXISTS idx_sso_browser_sessions_idle_expiry
    ON sso_browser_sessions(idle_expires_at);
CREATE INDEX IF NOT EXISTS idx_sso_browser_sessions_absolute_expiry
    ON sso_browser_sessions(absolute_expires_at);
CREATE INDEX IF NOT EXISTS idx_sso_browser_sessions_user
    ON sso_browser_sessions(user_id);
-- init.sql runs before migrations on every startup, including against tables
-- created by a pre-epoch image. Migration 076 replaces these two compatible
-- definitions with epoch-leading indexes after it adds session_epoch.
CREATE INDEX IF NOT EXISTS idx_sso_browser_sessions_sid
    ON sso_browser_sessions(identity_issuer, keycloak_sid);
CREATE INDEX IF NOT EXISTS idx_sso_browser_sessions_subject
    ON sso_browser_sessions(identity_issuer, identity_subject);

-- Durable, short-lived ordering fence for verified Keycloak back-channel
-- logout. Session creation and logout also share a transaction advisory lock;
-- this row rejects a callback that resumes only after logout committed.
CREATE TABLE IF NOT EXISTS sso_browser_logout_fences (
    session_epoch UUID,
    identity_issuer TEXT NOT NULL
        CHECK (char_length(identity_issuer) BETWEEN 1 AND 2048),
    keycloak_sid TEXT NOT NULL
        CHECK (char_length(keycloak_sid) BETWEEN 1 AND 255),
    identity_subject TEXT
        CHECK (
            identity_subject IS NULL OR
            char_length(identity_subject) BETWEEN 1 AND 1024
        ),
    logout_issued_at TIMESTAMPTZ NOT NULL,
    expires_at TIMESTAMPTZ NOT NULL,
    received_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    -- Retain the pre-epoch key so an explicitly prepared rollback image can
    -- still execute its original ON CONFLICT target.
    PRIMARY KEY (identity_issuer, keycloak_sid),
    CONSTRAINT sso_browser_logout_fence_epoch_fk
        FOREIGN KEY (session_epoch)
        REFERENCES auth_runtime_state(sso_session_epoch),
    CONSTRAINT sso_browser_logout_fence_positive_lifetime
        CHECK (expires_at > logout_issued_at)
);

CREATE INDEX IF NOT EXISTS idx_sso_browser_logout_fences_expiry
    ON sso_browser_logout_fences(expires_at);

-- A monotonic, non-secret receipt written only after the temporary bundled
-- Keycloak bootstrap client has been deleted and its credential rejected.
-- It lets later init-container runs prove that an absent one-time Secret is
-- expected, while a prematurely removed Secret fails closed.
CREATE TABLE IF NOT EXISTS standalone_sso_bootstrap_retirements (
    profile TEXT PRIMARY KEY
        CHECK (char_length(profile) BETWEEN 1 AND 128),
    issuer TEXT NOT NULL
        CHECK (char_length(issuer) BETWEEN 1 AND 2048),
    realm_id TEXT NOT NULL
        CHECK (char_length(realm_id) BETWEEN 1 AND 255),
    bootstrap_client_id TEXT NOT NULL
        CHECK (char_length(bootstrap_client_id) BETWEEN 1 AND 255),
    management_client_uuid TEXT NOT NULL
        CHECK (char_length(management_client_uuid) BETWEEN 1 AND 255),
    admin_client_uuid TEXT NOT NULL
        CHECK (char_length(admin_client_uuid) BETWEEN 1 AND 255),
    api_client_uuid TEXT NOT NULL
        CHECK (char_length(api_client_uuid) BETWEEN 1 AND 255),
    product_admin_subject TEXT NOT NULL
        CHECK (char_length(product_admin_subject) BETWEEN 1 AND 1024),
    akb_user_id UUID NOT NULL,
    backchannel_logout_uri TEXT
        CHECK (
            backchannel_logout_uri IS NULL OR
            char_length(backchannel_logout_uri) BETWEEN 1 AND 2048
        ),
    retired_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Durable post-commit cleanup ledger. Token rows are deleted in the same
-- transaction that suspends an account; PG token roles are DDL and are
-- cleaned afterward. Failed DDL remains retryable without restoring a token.
CREATE TABLE IF NOT EXISTS account_token_cleanup (
    token_id UUID PRIMARY KEY,
    user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    requested_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    completed_at TIMESTAMPTZ,
    attempts INTEGER NOT NULL DEFAULT 0,
    last_error TEXT
);

CREATE INDEX IF NOT EXISTS idx_account_token_cleanup_pending
    ON account_token_cleanup(user_id, requested_at)
    WHERE completed_at IS NULL;

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
    vault_scope JSONB,                     -- per-PAT vault scope {prefixes, extra_vaults}; NULL = unscoped (full user ACL). See migration 040.
    key_class TEXT NOT NULL DEFAULT 'pat' CHECK (key_class IN ('pat', 'service', 'publishable')),
    expires_at TIMESTAMPTZ,
    last_used_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_tokens_hash ON tokens(token_hash);
CREATE INDEX IF NOT EXISTS idx_tokens_user ON tokens(user_id);

-- ============================================================
-- OIDC transients — single-use, TTL-bounded state. The dedicated product-admin
-- and ordinary browser clients use distinct namespaced kinds. See migration 034.
-- ============================================================
CREATE TABLE IF NOT EXISTS oidc_transients (
    key         TEXT PRIMARY KEY,
    kind        TEXT NOT NULL,          -- namespaced browser-flow kind
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
    UNIQUE(vault_id, path),
    -- Redundant as a guarantee about documents (id is already the PK), and
    -- required all the same: publications reference (id, vault_id) as a pair
    -- so the vault match is structural, and PostgreSQL will not accept a
    -- composite FK without a unique constraint on exactly that column pair.
    -- Named explicitly because migration 058 looks it up by name.
    CONSTRAINT documents_id_vault_id_key UNIQUE (id, vault_id)
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

-- Guarded: `source_type` and `source_id` arrive together with migration 006;
-- see the note at the publications document_id guard below. 006 creates this
-- index itself, so the boot that skips it here does not go without it.
DO $$
BEGIN
    IF EXISTS (
        SELECT 1 FROM information_schema.columns
         WHERE table_schema = 'public'
           AND table_name = 'chunks'
           AND column_name = 'source_type'
    ) THEN
        CREATE INDEX IF NOT EXISTS idx_chunks_source ON chunks (source_type, source_id);
    END IF;
END $$;
-- Indexing-queue claim order (newest chunks first; see embed_worker._claim_batch).
-- Also covers the retry-eligibility WHERE filter — a single partial
-- index is enough; we used to keep idx_chunks_vector_pending alongside
-- this for vector_next_attempt_at, but the planner was selecting the
-- ORDER-BY-aligned index anyway, so the second one was dead weight.
-- Guarded: `vector_indexed_at` arrives with migration 009; see the note at the
-- publications document_id guard below.
DO $$
BEGIN
    IF EXISTS (
        SELECT 1 FROM information_schema.columns
         WHERE table_schema = 'public'
           AND table_name = 'chunks'
           AND column_name = 'vector_indexed_at'
    ) THEN
        CREATE INDEX IF NOT EXISTS idx_chunks_indexing_queue
            ON chunks (created_at DESC, id)
         WHERE vector_indexed_at IS NULL;
    END IF;
END $$;
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
    unique_keys JSONB NOT NULL DEFAULT '[]',
    indexes JSONB NOT NULL DEFAULT '[]',
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

CREATE TABLE IF NOT EXISTS vault_migrations (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    vault_id UUID NOT NULL REFERENCES vaults(id) ON DELETE CASCADE,
    name TEXT NOT NULL,
    applied_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    checksum TEXT NOT NULL,
    UNIQUE(vault_id, name)
);

CREATE INDEX IF NOT EXISTS idx_vault_tables_vault ON vault_tables(vault_id);
-- Guarded: `vault_tables.collection_id` arrives with migration 020; see the
-- note at the publications document_id guard below.
DO $$
BEGIN
    IF EXISTS (
        SELECT 1 FROM information_schema.columns
         WHERE table_schema = 'public'
           AND table_name = 'vault_tables'
           AND column_name = 'collection_id'
    ) THEN
        CREATE INDEX IF NOT EXISTS idx_vault_tables_collection ON vault_tables(collection_id);
    END IF;
END $$;
CREATE INDEX IF NOT EXISTS idx_vault_table_rows_table ON vault_table_rows(table_id);
CREATE INDEX IF NOT EXISTS idx_vault_table_rows_data ON vault_table_rows USING gin(data);
CREATE INDEX IF NOT EXISTS idx_vault_migrations_vault_applied
    ON vault_migrations(vault_id, applied_at DESC);

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
-- Composite (vault_id, endpoint): the common graph-read access pattern is
-- "scope by vault AND look up by endpoint URI" (BFS / overview induced edges /
-- degree rollups). See migration 039.
CREATE INDEX IF NOT EXISTS idx_edges_vault_source ON edges(vault_id, source_uri);
CREATE INDEX IF NOT EXISTS idx_edges_vault_target ON edges(vault_id, target_uri);

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
    kind TEXT NOT NULL DEFAULT 'file',
    upload_state TEXT NOT NULL DEFAULT 'pending',
    name TEXT NOT NULL,
    s3_key TEXT NOT NULL,
    mime_type TEXT,
    size_bytes BIGINT,
    content_hash TEXT,                 -- sha256 of file bytes
    hash_algorithm TEXT DEFAULT 'sha256',
    etag TEXT,                         -- object-store ETag/checksum hint
    storage_version TEXT,              -- object-store version id when available
    hash_verified_at TIMESTAMPTZ,      -- when AKB last verified content_hash from storage bytes
    attachment_claimed_at TIMESTAMPTZ, -- first document commit that referenced an editor attachment
    description TEXT,
    created_by TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE(vault_id, s3_key),
    CONSTRAINT vault_files_id_vault_id_key UNIQUE (id, vault_id)
);

CREATE INDEX IF NOT EXISTS idx_vault_files_vault ON vault_files(vault_id);
CREATE INDEX IF NOT EXISTS idx_vault_files_s3_key ON vault_files(s3_key);
-- Guarded: `vault_files.collection_id` arrives with migration 020; see the
-- note at the publications document_id guard below.
DO $$
BEGIN
    IF EXISTS (
        SELECT 1 FROM information_schema.columns
         WHERE table_schema = 'public'
           AND table_name = 'vault_files'
           AND column_name = 'collection_id'
    ) THEN
        CREATE INDEX IF NOT EXISTS idx_vault_files_collection ON vault_files(collection_id);
    END IF;
END $$;

-- Live document-image references are the private authorization set. They
-- disappear with the document; a separate bounded manifest keeps recent Git
-- revisions renderable without retaining unreferenced objects forever.
-- init.sql runs before pending migrations on an upgraded database. A database
-- that predates migration 058 does not yet have documents(id, vault_id) as a
-- unique key, so creating the composite FK here would abort startup before 058
-- can add it. Fresh/current databases take this fast path; older ones skip it
-- and migration 062 creates the table after 058 has completed.
DO $$
BEGIN
    IF EXISTS (
        SELECT 1 FROM pg_constraint
         WHERE conname = 'documents_id_vault_id_key'
           AND conrelid = 'documents'::regclass
    ) AND EXISTS (
        SELECT 1 FROM pg_constraint
         WHERE conname = 'vault_files_id_vault_id_key'
           AND conrelid = 'vault_files'::regclass
    ) THEN
        EXECUTE $ddl$
            CREATE TABLE IF NOT EXISTS document_asset_refs (
                document_id UUID NOT NULL,
                vault_id UUID NOT NULL,
                asset_id UUID NOT NULL,
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                PRIMARY KEY (document_id, asset_id),
                CONSTRAINT document_asset_refs_document_fk
                    FOREIGN KEY (document_id, vault_id)
                    REFERENCES documents(id, vault_id) ON DELETE CASCADE,
                CONSTRAINT document_asset_refs_asset_fk
                    FOREIGN KEY (asset_id, vault_id)
                    REFERENCES vault_files(id, vault_id) ON DELETE CASCADE
            )
        $ddl$;
        EXECUTE $ddl$
            CREATE INDEX IF NOT EXISTS idx_document_asset_refs_asset
                ON document_asset_refs(asset_id, vault_id)
        $ddl$;
        EXECUTE $ddl$
            CREATE TABLE IF NOT EXISTS document_asset_revision_refs (
                vault_id UUID NOT NULL,
                document_path TEXT NOT NULL,
                commit_hash TEXT NOT NULL,
                asset_id UUID NOT NULL,
                retain_until TIMESTAMPTZ NOT NULL,
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                PRIMARY KEY (vault_id, document_path, commit_hash, asset_id),
                CONSTRAINT document_asset_revision_refs_asset_fk
                    FOREIGN KEY (asset_id, vault_id)
                    REFERENCES vault_files(id, vault_id) ON DELETE CASCADE
            )
        $ddl$;
        EXECUTE $ddl$
            CREATE INDEX IF NOT EXISTS idx_document_asset_revision_refs_asset_retention
                ON document_asset_revision_refs(asset_id, retain_until)
        $ddl$;
        EXECUTE $ddl$
            CREATE INDEX IF NOT EXISTS idx_document_asset_revision_refs_expiry
                ON document_asset_revision_refs(retain_until)
        $ddl$;
    END IF;
END $$;

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
    -- rather than a single addressable resource. This is the handle every
    -- surface displays and the API returns; it is NOT the identity binding
    -- (see document_id below) because a path is reusable.
    resource_uri TEXT,

    -- The published document, when the publication has one.
    --
    -- NULLABLE, and that is not a placeholder for future tightening: rows
    -- that predate migration 058 are bound only if the binding is
    -- unambiguous, so "every document publication has a document_id" is a
    -- property of rows created after the publish path started recording it,
    -- not a schema invariant. table_query and file publications leave it
    -- NULL by definition — file cleanup is still app-level
    -- (delete_publications_for_file).
    --
    -- The FK is composite on purpose. Referencing documents(id) alone would
    -- promise only that the document exists; pairing vault_id in makes "the
    -- document is in the publication's own vault" structural instead of a
    -- rule each write has to remember. Match type is the default (MATCH
    -- SIMPLE) so a NULL document_id is exempt even though vault_id is NOT
    -- NULL — MATCH FULL would forbid the NULL and is wrong here.
    document_id UUID,
    CONSTRAINT publications_document_fk
        FOREIGN KEY (document_id, vault_id) REFERENCES documents(id, vault_id)
        ON DELETE CASCADE,

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
-- Guarded: `resource_uri` arrives with migration 022; see the note at the
-- document_id guard below.
DO $$
BEGIN
    IF EXISTS (
        SELECT 1 FROM information_schema.columns
         WHERE table_schema = 'public'
           AND table_name = 'publications'
           AND column_name = 'resource_uri'
    ) THEN
        CREATE INDEX IF NOT EXISTS idx_publications_resource_uri
            ON publications(resource_uri) WHERE resource_uri IS NOT NULL;
    END IF;
END $$;
-- Referencing side of publications_document_fk: without it, every document
-- delete seq-scans this table to find the rows to cascade. Partial because
-- most rows are NULL and `document_id = $1` implies NOT NULL, so the cascade
-- probe can still use it.
--
-- Guarded on the column, and that guard is load-bearing. This file is
-- re-executed IN FULL on every boot, BEFORE any migration runs. On a database
-- that has not reached migration 058 yet, the CREATE TABLE above is inert (the
-- table exists) so `document_id` is absent — and a bare CREATE INDEX on it
-- raises UndefinedColumn, which aborts init.sql and means the migration that
-- would have added the column never runs. Every boot, forever. Any future
-- index or ALTER here that names a column added by a migration needs the same
-- treatment.
DO $$
BEGIN
    IF EXISTS (
        SELECT 1 FROM information_schema.columns
         WHERE table_schema = 'public'
           AND table_name = 'publications'
           AND column_name = 'document_id'
    ) THEN
        CREATE INDEX IF NOT EXISTS idx_publications_document_id
            ON publications(document_id, vault_id) WHERE document_id IS NOT NULL;
    END IF;
END $$;
CREATE INDEX IF NOT EXISTS idx_publications_expires ON publications(expires_at) WHERE expires_at IS NOT NULL;

-- ============================================================
-- Todos table removed: the akb_todo/akb_todos/akb_todo_update MCP tools
-- that were its only entrypoint went in PR #43, leaving the table with no
-- reader on any surface (no REST router, no UI, no SDK). Migration 050
-- archives the historic rows to `todos_archive` and drops it. Do NOT
-- reintroduce a CREATE here: init.sql runs before migrations on every boot,
-- so it would resurrect an empty table that 050 will never drop again.

-- Agent memories + sessions tables removed in v0.4.0 alongside the
-- akb_remember/recall/forget MCP tool family. Agent dedicated memory
-- is now expressed as a vault (agent-memory-{username}) with per-session
-- collections, driven by the /api/v1/agent-sessions REST endpoints.
-- Existing rows are dropped by migration 031.

-- ============================================================
-- Shared trigger function: auto-bump `updated_at` on UPDATE.
-- ============================================================
-- PostgreSQL has no MySQL-style ON UPDATE CURRENT_TIMESTAMP, so the
-- dynamic vault data tables (`vt_*`) attach a BEFORE UPDATE trigger that
-- calls this function (see `table_data_repo.create_dynamic_table`).
-- Defined here so init.sql-only bootstraps (unit tests, bare CI DBs) have
-- the function BEFORE `create_dynamic_table` references it. Migration 038
-- also CREATE OR REPLACEs it (for DBs provisioned before this line existed)
-- and backfills the trigger onto pre-existing `vt_*` tables. SECURITY
-- INVOKER (the default) is correct: the assignment needs no privilege
-- beyond NOW(), so it runs unchanged under the per-user `akb_user_<uid>`
-- role that `akb_sql` switches into.
CREATE OR REPLACE FUNCTION akb_set_updated_at()
RETURNS TRIGGER AS $$
BEGIN
    NEW.updated_at = NOW();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;
