"""The second HTTP socket that serves `/stats`.

**Why a separate port at all.** The control plane must be able to read a
tenant's inventory without being able to reach that tenant's data. The only
enforcement layer available for that is a Kubernetes NetworkPolicy, which
selects on port — so the surface the monitor may reach and the surface it may
not have to be different ports. A path on the API port could only ever be
protected by application-level logic, which is the wrong side of the boundary.

**Why it carries no authentication.** Reachability is the authorization. The
socket is not bound unless an operator sets a port, and once bound the
NetworkPolicy decides who may connect; there is no credential to distribute,
rotate, or leak into a monitor's config. This is only sound because the payload
is a handful of aggregate counters — no vault names, no document titles, no
actor identities — and because the platform side treats a missing policy as a
deployment blocker rather than a warning.

**Why in-process rather than a sidecar.** The snapshot is computed by a task in
this process against the serving pool. A sidecar would need its own database
credentials and its own view of the same tables — a second thing to keep in
sync with the schema, to grant, and to get wrong.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
from typing import Iterator

import uvicorn
from fastapi import FastAPI
from fastapi.responses import JSONResponse

from app.config import settings
from app.stats import sampler

logger = logging.getLogger("akb.stats_listener")

# The one environment override, and the same deliberate exception the pool
# sizing knobs are (see `app/config.py`'s module docstring): the port is a
# deployment-layer fact. The control plane provisions this port per tenant and
# renders the matching NetworkPolicy from the same value, so it must be able to
# set it without re-rendering the tenant's YAML config. Everything else about
# the surface stays in `stats:` in app.yaml.
PORT_ENV_VAR = "AKB_STATS_PORT"

_server: uvicorn.Server | None = None
_task: asyncio.Task | None = None


def configured_port() -> int | None:
    """The port to bind, or None to compose nothing at all.

    Fails loudly on a malformed value. A monitoring port that silently falls
    back to "off" because someone typed `AKB_STATS_PORT=909O` produces a
    deployment that looks healthy and is invisible to the control plane; the
    boot should stop instead.
    """
    raw = os.getenv(PORT_ENV_VAR, "").strip()
    if not raw:
        return settings.stats.port
    try:
        port = int(raw)
    except ValueError:
        raise RuntimeError(f"{PORT_ENV_VAR} must be an integer; got {raw!r}") from None
    if not 1 <= port <= 65535:
        raise RuntimeError(f"{PORT_ENV_VAR} must be between 1 and 65535; got {port}")
    return port


stats_app = FastAPI(
    title="AKB tenant stats",
    # No interactive docs and no schema route: this listener answers exactly
    # one path, and the schema this surface is contracted by is the versioned
    # JSON Schema in this package, not a generated OpenAPI document.
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
)


@stats_app.get("/stats")
async def stats() -> JSONResponse:
    """The cached snapshot, or 503 until the first sample completes.

    503 rather than an empty or zero-filled body: a consumer that receives a
    payload has to treat it as measured, and there is nothing to measure yet.
    An explicit "not ready" is retried; a zero is charted.

    Once a snapshot exists it keeps being served even while sampling fails —
    the payload's own `computed_at` is how a consumer notices staleness.
    """
    payload = sampler.snapshot()
    if payload is None:
        return JSONResponse(
            status_code=503,
            content={
                "status": "unavailable",
                "reason": "no stats snapshot has been computed yet",
            },
        )
    return JSONResponse(content=payload)


class _NoSignalServer(uvicorn.Server):
    """A uvicorn server that does not touch process signal handlers.

    `Server.serve()` normally installs its own SIGINT/SIGTERM handlers for the
    lifetime of the call. Running a second server inside the first one's
    lifespan would therefore replace the main server's handlers with this
    one's, and a SIGTERM from Kubernetes would ask the stats listener to shut
    down while the API server never learned it was being terminated — the pod
    would be SIGKILLed at the end of its grace period with in-flight requests
    and an undrained audit queue.

    This listener is shut down explicitly by `stop()` from the API's own
    lifespan, so it needs no signal handling of its own.
    """

    @contextlib.contextmanager
    def capture_signals(self) -> Iterator[None]:
        yield


def is_running() -> bool:
    return _task is not None and not _task.done()


def start() -> bool:
    """Bind the stats socket. Returns False when the feature is not configured.

    Idempotent: a second call while the listener is up is a no-op.
    """
    global _server, _task

    port = configured_port()
    if port is None:
        return False
    if is_running():
        return True

    config = uvicorn.Config(
        app=stats_app,
        host=settings.stats.host,
        port=port,
        # The app has no lifespan of its own, and running one would emit a
        # second set of startup/shutdown events inside the main app's lifespan.
        lifespan="off",
        # One line per poll, every couple of minutes, forever, saying 200 —
        # that is the whole traffic profile of this port.
        access_log=False,
        log_level="warning",
    )
    _server = _NoSignalServer(config)
    _task = asyncio.create_task(_server.serve(), name="stats_listener")
    logger.info("stats listener bound on %s:%d", settings.stats.host, port)
    return True


async def stop(timeout: float = 5.0) -> None:
    """Ask the listener to drain, then stop waiting once the budget is spent."""
    global _server, _task

    server, task = _server, _task
    _server, _task = None, None
    if server is not None:
        server.should_exit = True
    if task is None or task.done():
        return

    _, pending = await asyncio.wait([task], timeout=timeout)
    if not pending:
        return
    logger.warning("stats listener did not drain in %.1fs; cancelling", timeout)
    task.cancel()
    await asyncio.wait([task], timeout=1.0)
