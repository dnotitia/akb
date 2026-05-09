"""Shared startup/shutdown for the indexing/embedding background workers.

Both `app.main` (when AKB_DISABLE_WORKERS is unset) and `app.worker_main`
import these so the start/stop order stays consistent across entrypoints.
"""

from __future__ import annotations

import logging

from app.config import settings
from app.db.postgres import close_pool, init_db
from app.services import delete_worker, embed_worker, events_publisher, external_git_poller, http_pool, metadata_worker, s3_delete_worker
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
    if missing:
        raise RuntimeError(
            "Required configuration missing:\n  - " + "\n  - ".join(missing)
        )


async def init_storage() -> None:
    """Initialize DB schema/migrations and eagerly construct vector-store driver."""
    _validate_required_settings()
    await init_db()
    logger.info("Database initialized")
    # Force-construct so a misconfigured vector-store URL/DSN fails at startup rather
    # than silently serving empty search results later.
    get_vector_store()


def start_workers() -> None:
    embed_worker.start()
    delete_worker.start()
    external_git_poller.start()
    started = ["embed_worker", "delete_worker", "external_git_poller"]
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
    logger.info("Workers started: %s", ", ".join(started))


async def stop_workers() -> None:
    await events_publisher.stop()
    await metadata_worker.stop()
    await external_git_poller.stop()
    await s3_delete_worker.stop()
    await delete_worker.stop()
    await embed_worker.stop()


async def shutdown_storage() -> None:
    await http_pool.close_client()
    await close_pool()
