"""Dedicated background-worker process entrypoint."""

from __future__ import annotations

import asyncio
import logging
import os
import signal
from contextlib import suppress
from pathlib import Path

from app.config import settings
from app.logging_redaction import install_secret_redaction
from app.process_role import runtime_process_role
from app.services import external_git_service
from app.services.external_git_capability import check_external_git_capability
from app.services.lifecycle import (
    init_storage,
    shutdown_storage,
    start_workers,
    stop_workers,
)
from app.services.revision_backend import (
    get_revision_backend,
    selected_document_revision_backend,
)

logger = logging.getLogger("akb.worker_main")
_HEARTBEAT_PATH = Path(os.getenv("AKB_WORKER_HEARTBEAT_PATH", "/tmp/akb-worker-heartbeat"))


async def _heartbeat(stop_event: asyncio.Event) -> None:
    while not stop_event.is_set():
        # This file is intentionally touched on the worker event loop. It lives
        # on container-local /tmp, so the operation is tiny, and a fresh mtime
        # proves the loop itself is scheduling rather than merely proving that
        # a shared/default executor thread is still alive.
        _HEARTBEAT_PATH.touch(mode=0o600, exist_ok=True)
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=5.0)
        except TimeoutError:
            pass


def _select_revision_backend() -> str:
    """Compose the process-scoped revision backend before worker startup.

    API imports select the backend through their route composition, but the
    standalone worker entrypoint does not import those routes.  Without this
    explicit composition step ``selected_document_revision_backend()`` remains
    ``None`` and Bare-Git-only workers are incorrectly omitted.
    """
    get_revision_backend()
    selected = selected_document_revision_backend()
    if selected is None:
        raise RuntimeError("worker revision backend selection did not complete")
    return selected


async def run() -> None:
    if runtime_process_role() != "worker":
        raise RuntimeError(
            "app.worker_main requires AKB_PROCESS_ROLE=worker (fail-closed against duplicate workers)"
        )

    install_secret_redaction()
    # A container restart preserves /tmp. Never let a heartbeat written by the
    # previous process make startup/readiness pass before this process is ready.
    _HEARTBEAT_PATH.unlink(missing_ok=True)
    stop_event = asyncio.Event()
    heartbeat_task: asyncio.Task | None = None
    storage_initialized = False
    workers_started = False
    revision_backend = _select_revision_backend()
    loop = asyncio.get_running_loop()
    for signum in (signal.SIGTERM, signal.SIGINT):
        with suppress(NotImplementedError):
            loop.add_signal_handler(signum, stop_event.set)

    try:
        await init_storage()
        storage_initialized = True
        if revision_backend == "bare_git":
            marked = await external_git_service.backfill_mirror_markers()
            if marked:
                logger.info(
                    "Backfilled external-git mirror marker on %d vault(s)", marked,
                )
            await asyncio.to_thread(check_external_git_capability, settings)

        # Mark this before the synchronous call so a partial start is still
        # unwound if a later component raises during composition.
        workers_started = True
        start_workers(include_api_local=False)
        heartbeat_task = asyncio.create_task(
            _heartbeat(stop_event), name="worker-heartbeat",
        )
        logger.info("AKB worker process ready")
        await stop_event.wait()
    finally:
        stop_event.set()
        if heartbeat_task is not None:
            heartbeat_task.cancel()
            with suppress(asyncio.CancelledError):
                await heartbeat_task
        _HEARTBEAT_PATH.unlink(missing_ok=True)
        if workers_started:
            await stop_workers(include_api_local=False)
        if storage_initialized:
            await shutdown_storage()
        logger.info("AKB worker process stopped")


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    asyncio.run(run())


if __name__ == "__main__":
    main()
