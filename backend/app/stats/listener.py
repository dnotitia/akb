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
import socket
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
# Held so `stop()` can release it deterministically. Both existing paths do
# already close it — uvicorn's graceful shutdown closes the sockets it was
# handed, and on the cancel path the `asyncio.Server` that `loop.create_server`
# built around this fd closes it when collected — but the second of those is a
# release timed by the garbage collector, and a listening socket held past the
# process's own shutdown is what stops the next pod from binding the port.
_socket: socket.socket | None = None


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


def _report_serve_exit(task: asyncio.Task) -> None:
    """Make a serving task that died visible somewhere.

    The socket is bound before the task exists, so the realistic failures are
    behind it; but a task that raises after that — uvicorn's startup, a
    protocol-level error — would simply finish. `is_running()` would turn
    False, `stop()` reads a finished task as nothing to drain, and no line
    anywhere would say the port stopped answering.
    """
    if task.cancelled():
        return
    exc = task.exception()
    if exc is not None:
        logger.error("stats listener stopped serving", exc_info=exc)


def start() -> bool:
    """Bind the stats socket. Returns False when the feature is not configured.

    Raises when a configured port cannot be bound. The socket is opened HERE,
    synchronously, and not inside the serving task: a bind that fails inside
    the task would leave `start()` returning True and the boot continuing, so a
    port misrendered onto one already in use would produce a healthy pod with
    no stats socket — the exact failure `configured_port` refuses to allow for
    a malformed value, and one the platform would only notice as connection
    errors on its poller.

    Idempotent: a second call while the listener is up is a no-op.
    """
    global _server, _task, _socket

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
        # Logging is left exactly as the API server configured it. `Config` is
        # not a per-server object where logging is concerned: its constructor
        # runs `configure_logging()`, which with the default `log_config`
        # re-runs `dictConfig` over the process-wide `uvicorn*` loggers —
        # replacing the handler the secret-redaction filter was installed on —
        # and `access_log=False` / `log_level=` strip and re-level the very
        # loggers the API server shares with this one. The price of this line
        # is one access-log entry per poll on the API's log; the price of
        # anything else here is the API's own logs.
        log_config=None,
    )
    try:
        sock = config.bind_socket()
    except SystemExit as exc:
        # uvicorn logs the OSError and calls sys.exit(1) rather than raising it.
        # SystemExit derives from BaseException, so it would sail past every
        # `except Exception` between here and the top of the boot and terminate
        # the process with no context but a stray log line. Convert it to an
        # ordinary error that names what could not be bound.
        raise RuntimeError(
            f"stats listener could not bind {settings.stats.host}:{port} "
            f"(see the preceding uvicorn error)"
        ) from exc

    server = _NoSignalServer(config)
    try:
        task = asyncio.create_task(server.serve(sockets=[sock]), name="stats_listener")
    except BaseException:
        # The socket is bound but nothing will ever serve it — most plausibly
        # there is no running loop. Release the port and leave the module
        # state untouched, so this reads as "never started" rather than as a
        # listener that exists and answers nothing.
        sock.close()
        raise
    task.add_done_callback(_report_serve_exit)
    # Published only once there is something to stop.
    _server, _socket, _task = server, sock, task
    logger.info("stats listener bound on %s:%d", settings.stats.host, port)
    return True


async def stop(timeout: float = 5.0) -> None:
    """Ask the listener to drain, then stop waiting once the budget is spent."""
    global _server, _task, _socket

    server, task, sock = _server, _task, _socket
    _server, _task, _socket = None, None, None
    if server is not None:
        server.should_exit = True
    try:
        if task is None or task.done():
            return

        _, pending = await asyncio.wait([task], timeout=timeout)
        if not pending:
            return
        logger.warning("stats listener did not drain in %.1fs; cancelling", timeout)
        task.cancel()
        await asyncio.wait([task], timeout=1.0)
    finally:
        # Usually redundant, deliberately kept: `close()` on an already-closed
        # socket is a no-op, and this makes the release happen HERE rather than
        # whenever the last reference to uvicorn's server object goes away. On
        # the cancel path nothing runs uvicorn's shutdown, so without this the
        # fd survives until the collector reaches the `asyncio.Server` holding
        # it — an ordering no part of this file controls.
        if sock is not None:
            sock.close()
