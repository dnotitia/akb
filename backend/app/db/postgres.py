import asyncio
import logging
import asyncpg
from pathlib import Path

from app.config import settings

logger = logging.getLogger(__name__)

_pool: asyncpg.Pool | None = None


async def get_pool() -> asyncpg.Pool:
    global _pool
    if _pool is None:
        for attempt in range(10):
            try:
                _pool = await asyncpg.create_pool(
                    dsn=settings.asyncpg_dsn,
                    min_size=2,
                    max_size=20,
                    # Per-statement timeout. Without this a hung query holds
                    # its pool slot forever; an upstream service freeze can
                    # drain the pool in minutes (observed 2026-04-16).
                    command_timeout=30.0,
                    # Server-side guards so a stuck transaction can't pin a
                    # connection indefinitely from the database side.
                    server_settings={
                        "statement_timeout": "30000",
                        "idle_in_transaction_session_timeout": "60000",
                    },
                )
                break
            except (ConnectionRefusedError, OSError):
                if attempt < 9:
                    await asyncio.sleep(2)
                else:
                    raise
    return _pool


async def close_pool() -> None:
    global _pool
    if _pool is not None:
        await _pool.close()
        _pool = None


async def init_db(max_retries: int = 10, delay: float = 2.0) -> None:
    """Run init.sql to create tables, then apply pending migrations.
    Retries on connection failure.

    Main PG no longer holds embedding vectors (moved to the vector store
    in Phase 4 of the driver abstraction), so init.sql is plain
    PostgreSQL — no extension prerequisites, no dimension placeholder
    substitution.
    """
    for attempt in range(max_retries):
        try:
            pool = await get_pool()
            init_sql = Path(__file__).parent / "init.sql"
            sql = init_sql.read_text()
            async with pool.acquire() as conn:
                await conn.execute(sql)
            await _apply_migrations()
            return
        except (ConnectionRefusedError, asyncpg.CannotConnectNowError, OSError):
            if attempt < max_retries - 1:
                logger.warning(
                    "DB not ready (attempt %d/%d), retrying in %.1fs...",
                    attempt + 1, max_retries, delay,
                )
                await asyncio.sleep(delay)
            else:
                raise


def _load_migration(filename: str):
    """Load a migration module by filename (handles digit-prefixed names)."""
    import importlib.util as _ilu
    mig_path = Path(__file__).parent / "migrations" / filename
    if not mig_path.exists():
        return None
    spec = _ilu.spec_from_file_location(f"akb_mig_{filename}", str(mig_path))
    if spec is None or spec.loader is None:
        return None
    module = _ilu.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


async def _run_one_migration(pool, filename: str, module, *, retries: int = 10, backoff: float = 4.0) -> None:
    """Apply one migration under a bounded lock_timeout, retrying on a lock
    conflict, then record it in the ledger.

    Migrations that ALTER `chunks` need an ACCESS EXCLUSIVE lock. During a
    rolling deploy the outgoing pod's workers may still hold an open
    transaction on that table; without a bound the ALTER would block until
    the connection's 30s statement_timeout cancels it (QueryCanceledError),
    crashing startup. A short lock_timeout makes us fail fast and retry
    until the lock clears (the server also kills idle-in-transaction holders
    at 60s). The pooled connection's state is reset on release, so the
    per-migration `SET lock_timeout` does not leak to other callers.
    """
    import logging
    log = logging.getLogger("akb.migrations")
    for attempt in range(retries):
        try:
            async with pool.acquire() as conn:
                await conn.execute("SET lock_timeout = '5s'")
                await module.migrate(conn=conn)
                await conn.execute(
                    "INSERT INTO schema_migrations (filename) VALUES ($1) "
                    "ON CONFLICT (filename) DO NOTHING",
                    filename,
                )
            return
        except (asyncpg.LockNotAvailableError, asyncpg.QueryCanceledError) as e:
            if attempt < retries - 1:
                log.warning(
                    "Migration %s blocked on a lock (attempt %d/%d): %s — retrying in %.0fs",
                    filename, attempt + 1, retries, e, backoff,
                )
                await asyncio.sleep(backoff)
            else:
                raise


async def _apply_migrations() -> None:
    """Apply migration scripts once each, in order. Safe to call repeatedly.

    A `schema_migrations` ledger records applied files so steady-state boots
    run ZERO migration DDL — see the ledger note in init.sql for why that
    matters (the `chunks` ALTERs take an ACCESS EXCLUSIVE lock that races
    live workers during a rolling deploy). Unrecorded migrations run via
    :func:`_run_one_migration` (bounded lock_timeout + retry).
    """
    pool = await get_pool()
    async with pool.acquire() as conn:
        applied = {
            r["filename"] for r in await conn.fetch("SELECT filename FROM schema_migrations")
        }
    for filename in (
        "002_public_shares.py",     # legacy is_public/public_slug → public_shares
        "003_rename_public_shares.py",  # public_shares → publications
        "004_embed_retry_columns.py",   # chunks: embed_retry_count/last_error/next_attempt_at
        "005_qdrant_index.py",          # chunks.qdrant_*, qdrant_delete_outbox, bm25_vocab/stats
        "006_indexable_chunks.py",      # chunks.source_type/source_id (docs/tables/files)
        "007_outbox_sweep_index.py",    # partial index for outbox sweep worker
        "008_drop_legacy_document_id.py",  # chunks/outbox document_id column removed
        "009_rename_qdrant_columns.py",    # chunks.qdrant_* → vector_*, outbox table rename
        "010_external_git_mirror.py",      # vault_external_git, documents.source/external_*/llm_metadata_at
        "011_external_doc_collections.py", # backfill collection_id on external_git docs missing it
        "012_drop_llm_metadata_cache.py",  # cache was low-yield, removed
        "014_chunks_vault_id.py",          # denormalize vault_id onto chunks
        "015_events_outbox.py",            # transactional outbox + NOTIFY trigger
        "016_chunks_drop_embedding.py",    # drop embedding + embed_* cols (vector store owns the dense vec now)
        "017_chunks_indexing_queue_index.py",  # partial index for new-first claim order
        "018_drop_redundant_pending_index.py",  # idx_chunks_vector_pending is dead weight after 017
        "019_s3_delete_outbox.py",              # s3_delete_outbox + indices for atomic file deletion
        "020_unify_collection_membership.py",   # vault_tables/vault_files collection_id FK; drop legacy vault_files.collection TEXT
        "021_events_resource_uri.py",           # collapse events (ref_type, ref_id) → events.resource_uri (URI canonical)
        "022_publications_resource_uri.py",     # collapse publications (document_id, file_id) → publications.resource_uri (URI canonical)
        "023_drop_metadata_id.py",              # strip legacy d-prefix `metadata.id` from documents (cosmetic; SQL lookup arm already removed)
        "024_tokens_revoked_before.py",         # users.tokens_revoked_before for JWT revocation (default epoch — pre-existing JWTs unaffected)
        "025_drop_phantom_root_collection.py",  # drop path='' collection rows that legacy put() created when collection was omitted (issues #81/#82)
        "026_uri_collection_prefix.py",         # rewrite edges/publications/events URIs to 0.3.0 canonical (akb://V[/coll/<path>]/<type>/<id>)
        "027_collection_path_reserved_segments.py",  # WARN about pre-existing collection paths whose segments collide with URI structural markers (coll/doc/table/file)
        "028_edges_kind.py",                    # edges.kind ('implicit' rewriteable | 'explicit' akb_link-created) so akb_link edges survive akb_update rewrites
        "029_outbox_chunk_id_index.py",         # partial index on vector_delete_outbox(chunk_id) WHERE processed_at IS NULL for reaper dedup lookups
        "030_resource_content_hash.py",         # documents/vault_files content_hash projection for manifests and preconditions
        "031_drop_memories_sessions.py",        # drop legacy memories+sessions tables; agent memory is now vault-shaped
        "032_drop_supersedes.py",               # drop never-used documents.supersedes column (status leaned to draft/active/archived)
        "033_users_auth_provider.py",           # users.auth_provider ('local' default | 'keycloak' for JIT-provisioned SSO accounts)
        "034_oidc_transients.py",               # oidc_transients: short-lived OIDC state + one-time exchange codes (HA-safe; empty when Keycloak off)
        "035_fix_wikilink_alias_edges.py",      # repair edges whose target_uri carries a wikilink alias ([[…|label]] → …|label); strip alias, re-validate existence, drop orphans
        "036_resource_aliases.py",              # rename/move redirect table (old path/name → current resource id); old akb:// URIs keep resolving after a move
        "037_table_unique_keys_indexes.py",     # vault_tables.unique_keys + .indexes JSONB (declarative DDL metadata; AKB #215)
        "038_dynamic_table_updated_at_trigger.py",  # akb_set_updated_at() + BEFORE UPDATE trigger on vt_* tables (PG has no ON UPDATE CURRENT_TIMESTAMP)
        "039_edges_vault_endpoint_indexes.py",  # composite (vault_id, source_uri)/(vault_id, target_uri) indexes for graph reads (overview/BFS/degree; AKB graph viewer Phase 2)
        "040_tokens_vault_scope.py",            # tokens.vault_scope JSONB (per-PAT vault scope; NULL = unscoped)
    ):
        if filename in applied:
            continue
        module = _load_migration(filename)
        if module is None:
            continue
        await _run_one_migration(pool, filename, module)
