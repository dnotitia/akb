"""Shared startup/shutdown for the indexing/embedding background workers.

Both `app.main` (when AKB_DISABLE_WORKERS is unset) and `app.worker_main`
import these so the start/stop order stays consistent across entrypoints.
"""

from __future__ import annotations

import asyncio
import logging

from app.config import settings
from app.db.postgres import close_pool, get_pool, init_db
from app.services import audit_log, app_rollout_worker, delete_worker, embed_worker, events_publisher, external_git_poller, http_pool, m1_file_transfer_reaper, metadata_worker, s3_delete_worker, sparse_encoder, tool_usage, vault_backfill, write_lane
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
    # Keycloak is OPTIONAL — only validate its config when it's turned on.
    # When enabled, an incomplete config would 500 mid-login instead of
    # failing the deploy, so fail fast here.
    if settings.keycloak_enabled:
        if not settings.keycloak_server_url:
            missing.append("keycloak_server_url (keycloak_enabled is true)")
        if not settings.keycloak_redirect_uri:
            missing.append(
                "keycloak_redirect_uri (keycloak_enabled is true — the "
                "backend callback URL registered on the Keycloak client)"
            )
        if not settings.keycloak_public_client and not settings.keycloak_client_secret:
            missing.append(
                "keycloak_client_secret (confidential client — set it in "
                "secret.yaml, or set keycloak_public_client: true for PKCE)"
            )
    # MCP-OAuth (the Resource Server path) reuses the Keycloak JWKS,
    # issuer, and audience-mapped scopes — so it's only meaningful when
    # `keycloak_enabled` is also true. A deployment with mcp_oauth on +
    # keycloak off would 503 mid-discovery and silently reject every
    # RS256 token. Fail the boot instead so the misconfig surfaces at
    # deploy time.
    if settings.mcp_oauth_enabled and not settings.keycloak_enabled:
        missing.append(
            "keycloak_enabled (mcp_oauth_enabled is true — the OAuth "
            "Resource Server path keys on the realm's JWKS + issuer; "
            "either turn keycloak on, or turn mcp_oauth off)"
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
    if settings.native_revision_m1_file_driver != "s3_current":
        # Text Files share the native ledger and PostgreSQL BodyStore with the
        # guarded M1 grep arm. Install only after 048 and 053-057 are durable; normal
        # deployments never import or compose this measurement path.
        from app.services.m1_native_text_file_bridge import (
            install_m1_native_text_file_bridge,
        )

        install_m1_native_text_file_bridge()
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
    # ``start_workers`` is normally called from the FastAPI lifespan loop.
    # Keep direct, loop-free lifecycle probes (and import-time diagnostics)
    # side-effect free; the rollout runner owns asyncio tasks and cannot be
    # started without a running loop.
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        logger.debug("app_rollout_worker deferred: no running event loop")
    else:
        app_rollout_worker.start()
    # external-git kill-switch: the mirror poller must not run when the feature is
    # off (no claim, no outbound). Gated so a disabled deployment
    # starts zero mirror I/O; the read paths refuse mirror reads and the marker
    # backfill (fail-closed safety net) still runs unconditionally at startup.
    if settings.external_git_enabled:
        external_git_poller.start()
    # Auto-backfill vault_id onto pre-upgrade pgvector points (issue #189
    # Phase 2). Non-blocking; search self-activates the vault path once it
    # reports ready (until then the source-id path runs unchanged). No-op for
    # other drivers / separate-instance / fully-backfilled corpora.
    vault_backfill.start()
    # BM25 corpus stats (total_docs, avgdl, per-term df) only become
    # non-degenerate after `recompute_stats()` runs. The refresher fires
    # once at startup and then on a configurable cadence so the sparse
    # leg of hybrid search isn't silently degraded on fresh installs or
    # after long periods without manual init.
    # Dedicated Kiwi tokenizer process pool. Kiwi's native tokenize() holds the
    # GIL, so asyncio.to_thread can't parallelize it and concurrent tokenization
    # (search + this corpus-wide stats refresher + embed_worker) starves the
    # event loop → probe timeouts → 503. Run it off-process. Start it BEFORE the
    # stats refresher, which tokenizes the whole corpus on first tick. (Serving
    # also depends on it — move to the always-run path if API/worker tiers split.)
    sparse_encoder.start_tokenizer_pool()
    sparse_encoder.start_stats_refresher(settings.bm25_recompute_interval_secs)
    # Dedicated git-commit executor (write-lane, command-lane round-05).
    # Git mutations run here so blocked/slow commits can never crowd git
    # READS out of asyncio.to_thread's shared default executor.
    write_lane.start_commit_pool()
    started = ["tokenizer_pool", "git_commit_pool", "embed_worker", "delete_worker", "app_rollout_worker", "bm25_stats_refresher", "vault_backfill"]
    if settings.external_git_enabled:
        started.append("external_git_poller")
    if m1_file_transfer_reaper.enabled():
        m1_file_transfer_reaper.start()
        started.append("m1_file_transfer_reaper")
    # s3_delete_worker drains s3_delete_outbox into S3 deletes. Only
    # makes sense when S3 is configured; otherwise file uploads are
    # disabled altogether and the outbox stays empty forever.
    if settings.s3_endpoint_url:
        s3_delete_worker.start()
        started.append("s3_delete_worker")
    else:
        logger.info("s3_delete_worker disabled (S3 not configured)")
    # metadata_worker fills LLM-derived metadata on external_git imports only
    # (source='external_git'), so it is meaningless — and, per the external-git
    # kill-switch, forbidden — when external-git is off: no mirror
    # can produce work and it must issue zero LLM outbound. It is also the only
    # LLM consumer in the request-independent path, so it still stays off when
    # LLM isn't configured (no retry/abandon noise for OSS users without a key).
    if not settings.external_git_enabled:
        logger.info(
            "metadata_worker disabled (external_git_enabled=false; it only "
            "fills metadata on external_git mirror imports)"
        )
    elif settings.llm_base_url and settings.llm_api_key:
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
    # MCP tool-usage analytics — a separate sink from audit (queryable PG rows
    # rather than a hash-chained ledger) with its own flag. The maintenance
    # runner starts either way so that disabling collection still rolls up and
    # prunes what was already gathered.
    tool_usage.start()
    started.append("tool_usage_maintenance")
    if settings.tool_usage.enabled:
        started.append("tool_usage_flusher")
    else:
        logger.info("tool_usage collection disabled (tool_usage.enabled=false)")
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
    await m1_file_transfer_reaper.stop()
    await audit_log.stop_uploader()
    await tool_usage.stop()
    await events_publisher.stop()
    await metadata_worker.stop()
    await external_git_poller.stop()
    await s3_delete_worker.stop()
    await app_rollout_worker.stop()
    await delete_worker.stop()
    await embed_worker.stop()
    await vault_backfill.stop()
    await sparse_encoder.stop_stats_refresher()
    sparse_encoder.stop_tokenizer_pool()
    write_lane.stop_commit_pool()


async def shutdown_storage() -> None:
    await http_pool.close_client()
    # Close the optional Keycloak OIDC client if it was ever constructed.
    if settings.keycloak_enabled:
        from app.services.keycloak_oidc import get_keycloak_oidc
        await get_keycloak_oidc().aclose()
    await close_pool()
