"""Shared process composition for serving support and durable workers.

``AKB_PROCESS_ROLE=all`` preserves the local all-in-one runtime. Kubernetes
uses ``api`` for FastAPI and ``worker`` for :mod:`app.worker_main`; both
entrypoints import this module so pool and worker ordering cannot drift.
"""

from __future__ import annotations

import asyncio
import logging
import os
from urllib.parse import urlsplit

from app.db.postgres import close_pool, get_pool, init_db
from app.config import settings
from app.process_role import runtime_process_role
from app.services._backfill import request_stop_all, runner_snapshots
from app.services import (
    asset_gc_worker,
    audit_log,
    app_rollout_worker,
    delete_worker,
    embed_worker,
    events_publisher,
    external_git_poller,
    http_pool,
    m1_file_transfer_reaper,
    metadata_worker,
    queue_rescuer,
    s3_delete_worker,
    sparse_encoder,
    tool_usage,
    vault_backfill,
    write_lane,
)
from app.services.git_service import GitService
from app.services.native_revision_authority import (
    pre_migration_revision_authority_guard,
    startup_revision_authority_preflight,
)
from app.services.revision_backend import (
    canonical_document_revision_backend,
    selected_document_revision_backend,
)
from app.services.sso_callback_urls import is_backchannel_logout_uri
from app.services.role_sync import RoleSync, get_role_sync, set_role_sync
from app.services.user_sql_executor import UserSqlExecutor, set_user_sql_executor
from app.services.vector_store import get_vector_store
from app.stats import listener as stats_listener, sampler as stats_sampler

logger = logging.getLogger("akb.lifecycle")


def _is_secure_browser_url(value: str) -> bool:
    """Require HTTPS for browser authorities, with a narrow loopback exception."""
    try:
        parsed = urlsplit(value)
    except ValueError:
        return False
    if (
        parsed.hostname is None
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
    ):
        return False
    if parsed.scheme == "https":
        return True
    return parsed.scheme == "http" and parsed.hostname in {
        "localhost",
        "127.0.0.1",
        "::1",
    }


def _validate_required_settings() -> None:
    """Fail fast on missing required config so misconfigured deploys don't
    silently serve unsigned tokens or produce confusing downstream errors."""
    auth_mode = settings.require_auth_mode()
    if auth_mode == "local" and settings.jwt_algorithm != "RS256":
        raise RuntimeError(
            "jwt_algorithm must be RS256 for the local-session-rs256-v2 profile; "
            "HS256 sessions require forced re-login during upgrade"
        )
    if settings.mcp_oauth_enabled and settings.api_oauth_audience_effective == settings.mcp_oauth_audience_effective:
        raise RuntimeError("API and MCP OAuth audiences must be distinct resource identifiers")
    missing: list[str] = []
    if not settings.system_hmac_secret_effective:
        missing.append("system_hmac_secret (internal non-session HMAC material — use a strong random string)")
    if auth_mode == "local":
        if not settings.local_session_private_key_path.strip():
            missing.append("local_session_private_key_path (auth_mode is local)")
        if not settings.local_session_jwks_path.strip():
            missing.append("local_session_jwks_path (auth_mode is local)")
        if not settings.local_session_issuer_effective:
            missing.append("local_session_issuer or public_base_url (auth_mode is local)")
        if not settings.local_session_audience_effective:
            missing.append("local_session_audience or public_base_url (auth_mode is local)")
    if not settings.db_password:
        missing.append("AKB_DB_PASSWORD")
    if not settings.public_base_url:
        missing.append(
            "AKB_PUBLIC_BASE_URL (ingress origin — required so every "
            "publication response carries an absolute share_url; e.g. "
            "https://akb.example.com)"
        )
    # A configured Keycloak authority always needs its pinned issuer inputs.
    # Ordinary browser custody is activated separately by its encryption key;
    # the dedicated product-admin client remains confidential and independently
    # validated here.
    if settings.keycloak_enabled:
        if not settings.keycloak_server_url.strip():
            missing.append("keycloak_server_url (keycloak_enabled is true)")
        if not settings.keycloak_realm.strip():
            missing.append("keycloak_realm (keycloak_enabled is true)")
        if settings.sso_human_auth_enabled:
            if settings.sso_session_epoch is None:
                missing.append(
                    "sso_session_epoch (auth_mode is sso — generate and persist one UUID per SSO authority epoch)"
                )
            if not settings.keycloak_human_client_ids:
                missing.append(
                    "keycloak_client_id or companion client ID (auth_mode is sso — human API client allowlist is empty)"
                )
            if not settings.api_oauth_audience_effective.strip():
                missing.append("api_oauth_audience (auth_mode is sso — human API resource audience is empty)")
            if not settings.keycloak_admin_client_id.strip():
                missing.append("keycloak_admin_client_id (auth_mode is sso — product-admin client is empty)")
            if not settings.keycloak_admin_client_secret:
                missing.append(
                    "keycloak_admin_client_secret (auth_mode is sso — confidential product-admin client is required)"
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
        raise RuntimeError("Required configuration missing:\n  - " + "\n  - ".join(missing))
    if auth_mode == "sso":
        if settings.sso_browser_session_encryption_key:
            from app.services.sso_browser_session_crypto import (
                BrowserSessionCipher,
                BrowserSessionKeyError,
            )

            try:
                BrowserSessionCipher.from_encoded_key(settings.sso_browser_session_encryption_key)
            except BrowserSessionKeyError:
                raise RuntimeError(
                    "sso_browser_session_encryption_key must be base64url-encoded 256-bit key material"
                ) from None
        if settings.keycloak_admin_client_id in settings.keycloak_human_client_ids:
            raise RuntimeError("keycloak_admin_client_id must be dedicated to the product-admin surface")
        if not _is_secure_browser_url(settings.public_base_url):
            raise RuntimeError(
                "auth_mode=sso requires an HTTPS public_base_url; plain HTTP is allowed only on loopback"
            )
        if not _is_secure_browser_url(settings.keycloak_server_url):
            raise RuntimeError(
                "auth_mode=sso requires an HTTPS public Keycloak URL; plain HTTP is allowed only on loopback"
            )
        if not is_backchannel_logout_uri(
            settings.keycloak_backchannel_logout_uri_effective
        ):
            raise RuntimeError(
                "auth_mode=sso requires an exact HTTP(S) AKB back-channel logout URI"
            )
    if auth_mode == "local":
        from app.services.local_session_keys import get_local_session_keyset

        get_local_session_keyset()


async def init_storage() -> None:
    """Initialize DB schema/migrations and eagerly construct vector-store driver."""
    _validate_required_settings()
    await pre_migration_revision_authority_guard()
    await init_db()
    logger.info("Database initialized")
    from app.services.sso_session_epoch import reconcile_sso_session_epoch

    epoch_result = await reconcile_sso_session_epoch()
    if epoch_result.changed:
        logger.warning(
            "Authentication runtime boundary changed; revoked ordinary=%d admin=%d logout_fences=%d",
            epoch_result.ordinary_sessions_revoked,
            epoch_result.admin_sessions_revoked,
            epoch_result.logout_fences_revoked,
        )
    authority_status = await startup_revision_authority_preflight()
    logger.info(
        "Document revision authority ready: backend=%s status=%s",
        canonical_document_revision_backend(settings.document_revision_backend),
        authority_status,
    )
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
    if selected_document_revision_backend() == "bare_git":
        try:
            cleared = await asyncio.to_thread(GitService().cleanup_stale_locks)
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


def start_runtime_pools() -> None:
    """Start process-local CPU/Git executors needed by API and workers."""
    raw_processes = os.getenv("AKB_TOKENIZER_PROCESSES", "").strip()
    processes = settings.tokenizer_processes
    if raw_processes:
        try:
            processes = int(raw_processes)
        except ValueError:
            raise RuntimeError("AKB_TOKENIZER_PROCESSES must be an integer") from None
        if not 1 <= processes <= 4:
            raise RuntimeError("AKB_TOKENIZER_PROCESSES must be between 1 and 4")
    sparse_encoder.start_tokenizer_pool(processes)
    write_lane.start_commit_pool()


def stop_runtime_pools() -> None:
    sparse_encoder.stop_tokenizer_pool()
    write_lane.stop_commit_pool()


def _start_api_local(started: list[str]) -> None:
    """Start sinks whose queues live in the serving process's memory."""
    if settings.audit.enabled:
        audit_log.init()
        if settings.audit.bucket:
            audit_log.start_uploader()
            started.append("audit_uploader")
        else:
            logger.info("audit enabled file-only (audit.bucket not set; no uploader)")
    else:
        logger.info("audit disabled (audit.enabled=false)")

    tool_usage.start()
    started.append("tool_usage_maintenance")
    if settings.tool_usage.enabled:
        started.append("tool_usage_flusher")
    else:
        logger.info("tool_usage collection disabled (tool_usage.enabled=false)")

    # `/stats` lives on its own socket in this same process, so it is composed
    # with the serving process and never with the worker one. The sampler is
    # started only alongside a bound listener — nothing else reads its cache,
    # and sampling into a snapshot nobody can fetch is pure database load.
    if stats_listener.start():
        stats_sampler.start()
        started.append("stats_listener")
        started.append("stats_sampler")
    else:
        logger.info(
            "stats listener disabled (neither stats.port nor %s is set)",
            stats_listener.PORT_ENV_VAR,
        )


def start_api_runtime() -> None:
    """Start only process-local serving support, never durable queue workers."""
    start_runtime_pools()
    started = ["tokenizer_pool", "git_commit_pool"]
    _start_api_local(started)
    logger.info("API runtime started: %s", ", ".join(started))


def start_workers(*, include_api_local: bool = True) -> None:
    start_runtime_pools()
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
    # External-Git mirrors are a Bare-Git subsystem. The feature kill-switch
    # still gates it within that mode, while PostgreSQL Native composes no
    # mirror poller because its vault storage has no Git write authority.
    bare_git_selected = selected_document_revision_backend() == "bare_git"
    if bare_git_selected and settings.external_git_enabled:
        external_git_poller.start()
    # Auto-backfill vault_id onto pre-upgrade pgvector points (issue #189
    # Phase 2). Non-blocking; search self-activates the vault path once it
    # reports ready (until then the source-id path runs unchanged). No-op for
    # other drivers / separate-instance / fully-backfilled corpora.
    vault_backfill.start()
    queue_rescuer.start()
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
    sparse_encoder.start_stats_refresher(settings.bm25_recompute_interval_secs)
    # Dedicated git-commit executor (write-lane, command-lane round-05).
    # Git mutations run here so blocked/slow commits can never crowd git
    # READS out of asyncio.to_thread's shared default executor.
    started = [
        "tokenizer_pool",
        "git_commit_pool",
        "embed_worker",
        "delete_worker",
        "app_rollout_worker",
        "bm25_stats_refresher",
        "vault_backfill",
        "queue_rescuer",
    ]
    if bare_git_selected and settings.external_git_enabled:
        started.append("external_git_poller")
    if m1_file_transfer_reaper.enabled():
        m1_file_transfer_reaper.start()
        started.append("m1_file_transfer_reaper")
    # s3_delete_worker drains s3_delete_outbox into S3 deletes. Only
    # makes sense when S3 is configured; otherwise file uploads are
    # disabled altogether and the outbox stays empty forever.
    if settings.s3_endpoint_url:
        asset_gc_worker.start()
        s3_delete_worker.start()
        started.append("asset_gc_worker")
        started.append("s3_delete_worker")
    else:
        logger.info("s3_delete_worker disabled (S3 not configured)")
    # metadata_worker fills LLM-derived metadata on external_git imports only
    # (source='external_git'), so it is meaningless — and, per the external-git
    # kill-switch, forbidden — when external-git is off: no mirror
    # can produce work and it must issue zero LLM outbound. It is also the only
    # LLM consumer in the request-independent path, so it still stays off when
    # LLM isn't configured (no retry/abandon noise for OSS users without a key).
    if not bare_git_selected:
        logger.info("metadata_worker disabled (document revision backend is not Bare Git)")
    elif not settings.external_git_enabled:
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
    if include_api_local:
        _start_api_local(started)
    # PG-RBAC periodic reconcile — converges drift caused by silent
    # lifecycle-hook failures (counted in role_sync.metrics_snapshot).
    # Set role_sync_reconcile_interval_secs <= 0 in config to disable.
    if settings.role_sync_reconcile_interval_secs > 0:
        get_role_sync().start_reconcile_timer(
            settings.role_sync_reconcile_interval_secs,
        )
        started.append("role_sync_reconcile_loop")
    logger.info("Workers started: %s", ", ".join(started))


async def _stop_component(name: str, stop) -> None:
    """Stop one component without letting it skip the rest of shutdown."""
    try:
        await stop()
    except asyncio.CancelledError:
        # A component may use cancellation as its normal stop mechanism. The
        # lifecycle already broadcast the global stop request, so one such
        # outcome must not prevent siblings from joining.
        logger.warning("worker shutdown cancelled for %s", name)
    except Exception as exc:  # noqa: BLE001
        logger.warning("worker shutdown failed for %s: %s", name, exc)


async def stop_workers(*, include_api_local: bool = True) -> None:
    """Signal every worker first, then join them under one absolute budget."""
    # BackfillRunner.stop used to be called sequentially. A slow first worker
    # therefore left every later worker claiming new rows until Kubernetes sent
    # SIGKILL. Broadcast first so all queue consumers become quiescent together.
    request_stop_all()
    components = [
        ("role_sync", lambda: get_role_sync().stop_reconcile_timer()),
        ("m1_file_transfer_reaper", m1_file_transfer_reaper.stop),
        ("events_publisher", events_publisher.stop),
        ("metadata_worker", metadata_worker.stop),
        ("external_git_poller", external_git_poller.stop),
        ("asset_gc_worker", asset_gc_worker.stop),
        ("s3_delete_worker", s3_delete_worker.stop),
        ("app_rollout_worker", app_rollout_worker.stop),
        ("delete_worker", delete_worker.stop),
        ("embed_worker", embed_worker.stop),
        ("vault_backfill", vault_backfill.stop),
        ("queue_rescuer", queue_rescuer.stop),
        ("bm25_stats_refresher", sparse_encoder.stop_stats_refresher),
    ]
    if include_api_local:
        components.extend([
            ("audit_uploader", audit_log.stop_uploader),
            ("tool_usage", lambda: tool_usage.stop()),
            ("stats_listener", stats_listener.stop),
            ("stats_sampler", stats_sampler.stop),
        ])
    tasks = [
        asyncio.create_task(_stop_component(name, stop), name=f"stop:{name}")
        for name, stop in components
    ]
    try:
        done, pending = await asyncio.wait(
            tasks,
            timeout=settings.worker_shutdown_timeout_secs,
        )
        for task in done:
            # Retrieve exceptions even though _stop_component should contain
            # ordinary failures. Cancellation remains visible to this caller.
            task.result()
        if pending:
            logger.error(
                "worker shutdown deadline exceeded; %d component(s) still running: %s",
                len(pending),
                ", ".join(sorted(task.get_name() for task in pending)),
            )
            for task in pending:
                task.cancel()
            # Do not wait beyond the shared deadline. Consume eventual task
            # outcomes so an uncooperative callback cannot emit warnings later.
            for task in pending:
                task.add_done_callback(
                    lambda finished: None
                    if finished.cancelled()
                    else finished.exception()
                )
    finally:
        stop_runtime_pools()


async def stop_api_runtime() -> None:
    """Stop only serving-process sinks and process-local executors."""
    request_stop_all()
    tasks = [
        asyncio.create_task(
            _stop_component("audit_uploader", audit_log.stop_uploader),
            name="stop:audit_uploader",
        ),
        asyncio.create_task(
            _stop_component("tool_usage", lambda: tool_usage.stop()),
            name="stop:tool_usage",
        ),
        asyncio.create_task(
            _stop_component("stats_listener", stats_listener.stop),
            name="stop:stats_listener",
        ),
        asyncio.create_task(
            _stop_component("stats_sampler", stats_sampler.stop),
            name="stop:stats_sampler",
        ),
    ]
    try:
        done, pending = await asyncio.wait(
            tasks,
            timeout=settings.worker_shutdown_timeout_secs,
        )
        for task in done:
            task.result()
        for task in pending:
            task.cancel()
            task.add_done_callback(
                lambda finished: None
                if finished.cancelled()
                else finished.exception()
            )
    finally:
        stop_runtime_pools()


def worker_lifecycle_snapshot() -> dict:
    runners = runner_snapshots()
    return {
        "process_role": runtime_process_role(),
        "shutdown_timeout_secs": settings.worker_shutdown_timeout_secs,
        "runners": runners,
        "abandoned_total": sum(item["abandoned"] for item in runners),
    }


async def shutdown_storage() -> None:
    await http_pool.close_client()
    # Close the optional Keycloak OIDC client if it was ever constructed.
    if settings.keycloak_enabled:
        from app.services.keycloak_oidc import get_keycloak_oidc

        await get_keycloak_oidc().aclose()
    await close_pool()
