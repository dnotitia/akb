"""Shared startup/shutdown for the indexing/embedding background workers.

Both `app.main` (when AKB_DISABLE_WORKERS is unset) and `app.worker_main`
import these so the start/stop order stays consistent across entrypoints.
"""

from __future__ import annotations

import logging

from app.config import settings
from app.db.postgres import close_pool, get_pool, init_db
from app.services import audit_log, delete_worker, embed_worker, events_publisher, external_git_poller, http_pool, metadata_worker, s3_delete_worker, sparse_encoder
from app.services.git_service import GitService
from app.services.role_sync import RoleSync, get_role_sync, set_role_sync
from app.services.user_sql_executor import UserSqlExecutor, set_user_sql_executor
from app.services.vector_store import get_vector_store

logger = logging.getLogger("akb.lifecycle")


def _validate_required_settings() -> None:
    """Fail fast on missing required config so misconfigured deploys don't
    silently serve unsigned tokens or produce confusing downstream errors."""
    missing: list[str] = []
    if not settings.jwt_secret:
        missing.append("AKB_JWT_SECRET (signs auth tokens — use a strong random string)")
    if not settings.db_password:
        missing.append("AKB_DB_PASSWORD")
    if not settings.public_base_url:
        missing.append(
            "AKB_PUBLIC_BASE_URL (ingress origin — required so every "
            "publication response carries an absolute share_url; e.g. "
            "https://akb.example.com)"
        )
    if missing:
        raise RuntimeError(
            "Required configuration missing:\n  - " + "\n  - ".join(missing)
        )


async def init_storage() -> None:
    """Initialize DB schema/migrations and eagerly construct vector-store driver."""
    _validate_required_settings()
    await init_db()
    logger.info("Database initialized")
    # Self-heal: clear stale git index.lock files left behind by a
    # crashed prior process. Without this, the affected vault's writes
    # fail silently until an operator removes the lock by hand.
    try:
        cleared = GitService().cleanup_stale_locks()
        if cleared:
            logger.info("Cleared %d stale git lock(s) at startup", cleared)
    except Exception as e:  # noqa: BLE001 — never block startup on best-effort cleanup
        logger.warning("Stale-lock self-heal failed (continuing): %s", e)
    # Force-construct so a misconfigured vector-store URL/DSN fails at startup rather
    # than silently serving empty search results later.
    store = get_vector_store()
    # Eagerly run schema setup BEFORE workers start. Otherwise N concurrent
    # embed_workers all racing to be the first caller of ensure_collection can
    # exhaust the main PG pool when N approaches pool.max_size — the lock holder
    # waits for a second pooled conn while peers hold theirs waiting on the lock.
    # Doing it once here, single-threaded, sidesteps the cold-start contention
    # entirely; subsequent worker calls hit the _ensured_collection fast path.
    try:
        await store.ensure_collection()
        logger.info("Vector store schema ensured (eager init)")
    except Exception as e:  # noqa: BLE001 — fall through so degraded probes can surface it
        logger.warning("Vector store eager init failed (will retry per-worker): %s", e)
    # PG-native RBAC: reconcile role + GRANT state with the catalog
    # (users + vaults + vault_access + vault_tables). Idempotent —
    # creates missing roles, drops orphans, applies table-level GRANTs.
    # akb_sql relies on this state to enforce vault isolation via PG
    # ACL. Lifecycle hooks emit role DDL online for low-latency UX;
    # this reconciler is the convergence + drift-recovery mechanism.
    pool = await get_pool()
    role_sync = RoleSync(pool)
    set_role_sync(role_sync)
    set_user_sql_executor(UserSqlExecutor(pool))
    try:
        report = await role_sync.reconcile_from_catalog()
        logger.info("RoleSync reconcile at startup: %s", report)
    except Exception as e:  # noqa: BLE001
        logger.error("RoleSync reconcile failed at startup: %s", e)


def start_workers() -> None:
    embed_worker.start()
    delete_worker.start()
    external_git_poller.start()
    # BM25 corpus stats (total_docs, avgdl, per-term df) only become
    # non-degenerate after `recompute_stats()` runs. The refresher fires
    # once at startup and then on a configurable cadence so the sparse
    # leg of hybrid search isn't silently degraded on fresh installs or
    # after long periods without manual init.
    sparse_encoder.start_stats_refresher(settings.bm25_recompute_interval_secs)
    started = ["embed_worker", "delete_worker", "external_git_poller", "bm25_stats_refresher"]
    # s3_delete_worker drains s3_delete_outbox into S3 deletes. Only
    # makes sense when S3 is configured; otherwise file uploads are
    # disabled altogether and the outbox stays empty forever.
    if settings.s3_endpoint_url:
        s3_delete_worker.start()
        started.append("s3_delete_worker")
    else:
        logger.info("s3_delete_worker disabled (S3 not configured)")
    # metadata_worker is the only LLM consumer in the request-independent
    # path. Skip it when LLM isn't configured so OSS users running without
    # an LLM key don't get retry/abandon noise on every external_git import.
    if settings.llm_base_url and settings.llm_api_key:
        metadata_worker.start()
        started.append("metadata_worker")
    else:
        logger.info("metadata_worker disabled (LLM not configured)")
    # events_publisher fans `events` outbox rows out to Redis Streams.
    # Without redis_url we leave the rows accumulating in PG (still
    # useful for in-process LISTEN/NOTIFY consumers, sweeper just
    # never runs). No worker = no log noise / no abandoned-row stats.
    if settings.redis_url:
        events_publisher.start()
        started.append("events_publisher")
    else:
        logger.info("events_publisher disabled (redis_url not configured)")
    # Audit log — producer-only. `init` seeds the per-file hash chain;
    # the uploader (daily handoff to the WORM bucket) only runs when a
    # bucket is configured. File-only mode (no bucket) still writes the
    # JSON-lines stream for a co-located SIEM/Logstash to tail.
    if settings.audit.enabled:
        audit_log.init()
        if settings.audit.bucket:
            audit_log.start_uploader()
            started.append("audit_uploader")
        else:
            logger.info("audit enabled file-only (audit.bucket not set; no uploader)")
    else:
        logger.info("audit disabled (audit.enabled=false)")
    # PG-RBAC periodic reconcile — converges drift caused by silent
    # lifecycle-hook failures (counted in role_sync.metrics_snapshot).
    # Set role_sync_reconcile_interval_secs <= 0 in config to disable.
    if settings.role_sync_reconcile_interval_secs > 0:
        get_role_sync().start_reconcile_timer(
            settings.role_sync_reconcile_interval_secs,
        )
        started.append("role_sync_reconcile_loop")
    logger.info("Workers started: %s", ", ".join(started))


async def stop_workers() -> None:
    await get_role_sync().stop_reconcile_timer()
    await audit_log.stop_uploader()
    await events_publisher.stop()
    await metadata_worker.stop()
    await external_git_poller.stop()
    await s3_delete_worker.stop()
    await delete_worker.stop()
    await embed_worker.stop()
    await sparse_encoder.stop_stats_refresher()


async def shutdown_storage() -> None:
    await http_pool.close_client()
    await close_pool()
