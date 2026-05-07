import asyncio
import asyncpg
from pathlib import Path

from app.config import settings

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
                print(f"DB not ready (attempt {attempt + 1}/{max_retries}), retrying in {delay}s...")
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


async def _apply_migrations() -> None:
    """Run all idempotent migration scripts in order. Safe to call repeatedly."""
    pool = await get_pool()
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
    ):
        module = _load_migration(filename)
        if module is None:
            continue
        async with pool.acquire() as conn:
            await module.migrate(conn=conn)
