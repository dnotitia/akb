"""AKB — Agent Knowledgebase API Server."""

import asyncio
import logging
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass

import asyncpg
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, Response
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.requests import ClientDisconnect
from starlette.middleware.gzip import DEFAULT_EXCLUDED_CONTENT_TYPES, GZipMiddleware

from app.api.deps import get_current_user, get_optional_user
from app.api.routes import (
    access,
    admin_auth,
    admin_sso,
    activity,
    assets,
    user_avatar,
    app_inventory,
    app_installations,
    app_legacy_adoptions,
    app_rollouts,
    app_registry,
    agent_sessions,
    app_identity,
    auth,
    collections,
    documents,
    events,
    notifications,
    files,
    help as help_routes,
    knowledge,
    knowledge_io,
    oauth_metadata,
    public,
    search,
    tables,
)
from app.config import settings
from app.db.postgres import (
    forget_pool_progress_waiter,
    get_pool,
    pool_progress,
    pool_progress_waiter,
    readiness_acquires,
    readiness_ping,
    with_readiness_connection,
)
from app.db.transient import transient_error
from app.exceptions import AKBError, TransientDependencyError
from app.logging_redaction import install_secret_redaction
from app.openapi_contract import install_openapi_contract
from app.process_role import runtime_process_role
from app.http_middleware import CancelDisconnectedReadMiddleware, NoStorePublicSurfacesMiddleware
from app.services import (
    asset_gc_worker,
    audit_log,
    embed_worker,
    events_publisher,
    external_git_poller,
    external_git_service,
    metadata_worker,
    notification_worker,
    tool_usage,
)
from app.services.access_service import check_vault_access
from app.services.auth_service import AuthenticatedUser
from app.services.external_git_capability import check_external_git_capability
from app.services.health import vault_health
from app.services.lifecycle import (
    init_storage,
    shutdown_storage,
    start_workers,
    start_api_runtime,
    stop_api_runtime,
    stop_workers,
    worker_lifecycle_snapshot,
)
from app.services.revision_backend import selected_document_revision_backend
from app.services.vector_store import get_vector_store
from app.util.errors import (
    CONFLICT,
    GONE,
    INTERNAL,
    INVALID_ARGUMENT,
    METHOD_NOT_ALLOWED,
    NOT_FOUND,
    PERMISSION_DENIED,
    SERVICE_UNAVAILABLE,
)
from mcp_server.http_app import mcp_app

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
# Install the process-wide secret-aware log filter as early
# as possible (right after basicConfig configures the root handler), so no log
# record can carry a credential/URL-userinfo/token to a sink. Re-run in the
# lifespan too, once uvicorn has installed its own handlers.
install_secret_redaction()
logger = logging.getLogger("akb")


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("Starting AKB server")
    # Re-assert the secret-redaction filter now that uvicorn has installed its own
    # loggers/handlers (uvicorn.access/error don't propagate to the root handler).
    # Idempotent — already-covered handlers are skipped.
    install_secret_redaction()
    role = runtime_process_role()
    if role == "worker":
        raise RuntimeError(
            "AKB_PROCESS_ROLE=worker must use `python -m app.worker_main`, not uvicorn"
        )
    storage_initialized = False
    runtime_started = False
    try:
        await init_storage()
        storage_initialized = True
        bare_git_selected = selected_document_revision_backend() == "bare_git"
        if bare_git_selected:
            # Stamp the external-mirror marker on any mirror whose
            # bare repo predates it, so its reads route through the hermetic runner
            # (fail-closed) rather than GitPython. Runs AFTER the DB is up (authoritative
            # mirror list) and BEFORE workers/requests. This remains unconditional
            # within Bare-Git mode, even when external_git is disabled, because the
            # marker is the fail-closed safety net that makes the read paths correctly
            # REFUSE a disabled mirror (503) rather than serve it via GitPython. The
            # backfill is not composed for PostgreSQL Native, whose vault storage is
            # PostgreSQL-only and may have no Git write authority. FAIL-FAST: if a
            # marker cannot be written onto an existing mirror bare (disk/permission),
            # or the mirror list can't be read, this raises and startup ABORTS rather
            # than serving mirrors that would fall open. No serving has begun yet, so
            # the fail-open window is zero.
            marked = await external_git_service.backfill_mirror_markers()
            if marked:
                logger.info("Backfilled external-git mirror marker on %d vault(s)", marked)
            # When the mirror feature is enabled, prove at boot (in a
            # thread; it runs a git subprocess + loopback socket) that this git build
            # actually enforces the network controls the hermetic runner depends on
            # (http.curloptResolve DNS-pin, proxy-off, --config-env) and meets the git
            # >= 2.37 floor. Fast-fails the boot BEFORE workers/serving start if not; a
            # no-op when external_git is disabled. No real network (uses a *.invalid host).
            await asyncio.to_thread(check_external_git_capability, settings)
        # Mark this before synchronous composition so a partial start is still
        # unwound if a later component raises.
        runtime_started = True
        if role == "all":
            start_workers()
        else:
            start_api_runtime()
        async with mcp_app.run():
            yield
    finally:
        if runtime_started:
            if role == "all":
                await stop_workers()
            else:
                await stop_api_runtime()
        if storage_initialized:
            await shutdown_storage()
        # Drain the audit writer's queue so a rollout doesn't drop its tail. Off the
        # loop since shutdown() blocks on the queue join.
        await asyncio.to_thread(audit_log.shutdown)
        logger.info("Server shutdown (role=%s)", role)


app = FastAPI(
    title="AKB — Agent Knowledgebase",
    description="Organizational Memory for Agents. Git-backed, MCP-native knowledge base.",
    version="0.1.0",
    lifespan=lifespan,
)


# Global exception handler
@app.exception_handler(AKBError)
async def akb_error_handler(request: Request, exc: AKBError):
    detail: object = exc.message
    if exc.code or exc.hint or exc.details:
        detail_dict: dict[str, object] = {"message": exc.message}
        if exc.code:
            detail_dict["code"] = exc.code
        if exc.hint:
            detail_dict["hint"] = exc.hint
        if exc.details:
            detail_dict["details"] = exc.details
        detail = detail_dict
    # WriteBusyError (429) carries a retry hint; RFC 6585 wants it as a
    # Retry-After header so well-behaved clients back off without parsing
    # the body.
    headers = None
    retry_after = getattr(exc, "retry_after_secs", None)
    if retry_after is not None:
        headers = {"Retry-After": str(int(retry_after))}
    elif isinstance(exc, TransientDependencyError) and request.method in {"GET", "HEAD"}:
        headers = {"Retry-After": "1"}
    if request is not None and request.url.path == "/api/v1/auth/me/avatar":
        headers = {**(headers or {}), "Cache-Control": "private, no-store", "X-Content-Type-Options": "nosniff"}
    return JSONResponse(
        status_code=exc.status_code,
        content=_error_payload(exc.status_code, detail),
        headers=headers,
    )


@app.exception_handler(StarletteHTTPException)
async def http_error_handler(request: Request, exc: StarletteHTTPException):
    return JSONResponse(
        status_code=exc.status_code,
        content=_error_payload(exc.status_code, exc.detail),
        headers=getattr(exc, "headers", None),
    )


@app.exception_handler(RequestValidationError)
async def validation_error_handler(request: Request, exc: RequestValidationError):
    details = exc.errors()
    if request.url.path == "/api/v1/auth/tokens/issuance":
        fields = [{"field": ".".join(str(part) for part in error["loc"] if part != "body") or "form",
                   "code": error["type"], "message": error["msg"]} for error in details]
        return JSONResponse(status_code=422, content=_error_payload(422, {
            "code": "token_issuance_validation", "message": "Token issuance validation failed",
            "details": {"fields": fields},
        }))
    if request.url.path == "/api/v1/my/account/deletion" or request.url.path.startswith("/api/v1/documents/"):
        # Never echo passwords or a whole rejected document body in an input
        # error (a document model-level predicate error carries its raw body).
        details = [{k: e[k] for k in ("loc", "msg", "type") if k in e} for e in details]
    return JSONResponse(
        status_code=422,
        content=_error_payload(
            422,
            "Request validation failed",
            details=details,
            legacy_detail=details,
        ),
    )


# A peer that closes its connection while its request body is still being read
# makes `request.stream()` raise `ClientDisconnect`. The peer is gone, so no
# response reaches it (the server writes nothing to a disconnected peer) and
# this is not a server fault. Log one INFO line, with the method and path only.
# A handler must return a response; 499 (client closed request) names the event
# for anything in the process that reads the status. A handler for a
# specific exception class runs in Starlette's ExceptionMiddleware and is not
# re-raised, unlike the `Exception` handler below, which ServerErrorMiddleware
# re-raises to the server after responding (a second traceback).
# The path logged is the matched route's template where there is one: upload
# capability paths carry their credential as a path segment.
@app.exception_handler(ClientDisconnect)
async def client_disconnect_handler(request: Request, exc: ClientDisconnect):
    path = getattr(request.scope.get("route"), "path", None) or request.url.path
    logger.info("Client disconnected before its request body was read: %s %s", request.method, path)
    return Response(status_code=499)


@app.exception_handler(Exception)
async def unhandled_error_handler(request: Request, exc: Exception):
    logger.exception("Unhandled request error")
    return JSONResponse(
        status_code=500,
        content=_error_payload(500, "Internal server error"),
    )


@app.exception_handler(asyncpg.PostgresError)
@app.exception_handler(asyncpg.InterfaceError)
@app.exception_handler(TimeoutError)
@app.exception_handler(ConnectionError)
async def dependency_error_handler(request: Request, exc: Exception):
    mapped = transient_error(exc)
    if mapped is None:
        return await unhandled_error_handler(request, exc)
    logger.warning("Request dependency failure: code=%s", mapped.code)
    return await akb_error_handler(request, mapped)


def _error_payload(
    status_code: int,
    detail: object,
    *,
    details: object | None = None,
    legacy_detail: object | None = None,
) -> dict:
    hint = None
    code = _code_for_status(status_code)
    if isinstance(detail, dict):
        message = str(detail.get("message") or detail.get("error") or detail.get("detail") or status_code)
        if isinstance(detail.get("code"), str):
            code = detail["code"]
        if isinstance(detail.get("hint"), str):
            hint = detail["hint"]
        if details is None and "details" in detail:
            details = detail["details"]
        if details is None:
            details = {
                k: v for k, v in detail.items() if k not in {"message", "error", "detail", "code", "hint", "details"}
            } or None
        if legacy_detail is None:
            legacy_detail = detail
    else:
        message = str(detail)
        if legacy_detail is None:
            legacy_detail = detail

    payload = {
        "message": message,
        "error": message,
        "code": code,
        "detail": legacy_detail,
    }
    if details is not None:
        payload["details"] = details
    if hint is not None:
        payload["hint"] = hint
    if isinstance(detail, dict):
        for key in ("password_required", "slug"):
            if key in detail:
                payload[key] = detail[key]
    return payload


def _code_for_status(status_code: int) -> str:
    if status_code in {400, 413, 415, 422}:
        return INVALID_ARGUMENT
    if status_code == 405:
        return METHOD_NOT_ALLOWED
    if status_code in {401, 403}:
        return PERMISSION_DENIED
    if status_code == 404:
        return NOT_FOUND
    if status_code == 410:
        return GONE
    if status_code == 409:
        return CONFLICT
    if status_code == 503:
        return SERVICE_UNAVAILABLE
    return INTERNAL


app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)
# JSON listings such as a full-vault browse are multi-MB and compress well.
# Starlette already skips SSE and 206 range responses; binary downloads are
# excluded too so file streaming keeps its length and costs no CPU.
app.add_middleware(
    GZipMiddleware,
    minimum_size=1024,
    compresslevel=5,
    exclude_content_types=(
        *DEFAULT_EXCLUDED_CONTENT_TYPES, "application/octet-stream", "application/pdf", "image/*",
    ),
)


app.add_middleware(NoStorePublicSurfacesMiddleware)
# This is deliberately outside the header middleware: one task owns the
# handler and its transaction cleanup throughout disconnect cancellation.
app.add_middleware(CancelDisconnectedReadMiddleware)


app.include_router(auth.router, prefix="/api/v1", tags=["auth"])
app.include_router(user_avatar.router, prefix="/api/v1", tags=["auth"])
app.include_router(admin_auth.router, prefix="/api/v1", tags=["admin-auth"])
app.include_router(admin_sso.router, prefix="/api/v1", tags=["admin-sso"])
app.include_router(app_identity.router, prefix="/api/v1", tags=["app-identity"])
app.include_router(app_registry.router, prefix="/api/v1", tags=["app-registry"])
app.include_router(app_inventory.router, prefix="/api/v1", tags=["app-inventory"])
app.include_router(app_installations.router, prefix="/api/v1", tags=["app-installations"])
app.include_router(app_legacy_adoptions.router, prefix="/api/v1", tags=["app-legacy-adoptions"])
app.include_router(app_rollouts.router, prefix="/api/v1", tags=["app-rollouts"])
app.include_router(access.router, prefix="/api/v1", tags=["access"])
app.include_router(documents.router, prefix="/api/v1", tags=["documents"])
app.include_router(events.router, prefix="/api/v1", tags=["events"])
app.include_router(notifications.router, prefix="/api/v1", tags=["notifications"])
app.include_router(search.router, prefix="/api/v1", tags=["search"])
app.include_router(collections.router, prefix="/api/v1")
app.include_router(knowledge.router, prefix="/api/v1")
app.include_router(activity.router, prefix="/api/v1", tags=["activity"])
app.include_router(agent_sessions.router, prefix="/api/v1", tags=["agent-sessions"])
app.include_router(tables.router, prefix="/api/v1", tags=["tables"])
app.include_router(knowledge_io.router, prefix="/api/v1", tags=["export-import"])
app.include_router(files.router, prefix="/api/v1", tags=["files"])
# `/internal` is deliberately not `/api`: every ingress in the deployment maps
# `/api`, `/mcp`, `/.well-known` and the probes to this service and everything
# else to the frontend, so this prefix has no route from outside. The byte
# gateway reaches it over the cluster network with a shared key.
app.include_router(files.internal_router, prefix="/internal", include_in_schema=False)
app.include_router(assets.router, prefix="/api/v1", tags=["assets"])
app.include_router(assets.stable_router, prefix="/api", tags=["assets"])
app.include_router(public.router, prefix="/api/v1", tags=["public"])
app.include_router(help_routes.router, prefix="/api/v1/help", tags=["help"])
install_openapi_contract(app)

# RFC 9728 — well-known protected-resource metadata for /mcp. Mounted
# at the root (not under /api/v1) because the spec requires
# `/.well-known/...` literally on the resource's own origin.
app.include_router(oauth_metadata.router)

# Mount MCP Streamable HTTP at /mcp
app.mount("/mcp", mcp_app)


@app.get("/livez")
async def livez():
    return {"status": "alive"}


_READY_TTL_SECONDS = 30.0
# Every result, ready or not, answers every caller for at least this long.
# /readyz needs no authentication, and many of its results are not cached
# beyond this (failures, a saturated pool, a pool vouching for the database);
# the floor bounds the database work of any number of callers to about one
# evaluation per interval per process.
_READY_MIN_INTERVAL_SECONDS = 1.0
# The database step of one probe: pool initialization (only while there is no
# pool yet) plus connecting and pinging on the probe's dedicated connection.
# Nothing in it awaits a server on cancellation (connections are terminated,
# pending acquires abandoned, returns bounded), so it ends with its budget.
_READY_DB_TIMEOUT_SECONDS = 2.0
# How long a probe waits for the soft vector check. The check runs as a task
# the probe does not cancel-and-await: its cancellation cleanup can wait on a
# connection that never answers. With the database step, one evaluation, and
# every caller waiting for it, ends within about 7 seconds, inside kubelet's
# 8-second probe timeout.
_READY_VECTOR_TIMEOUT_SECONDS = 5.0


@dataclass
class _ReadyState:
    ts: float = 0.0
    ok: bool = False
    detail: dict | None = None
    # Monotonic time of the first probe in the current unbroken run of probes
    # that found the request pool saturated; None once a probe finds room.
    pool_saturated_since: float | None = None


_ready_state = _ReadyState()
_ready_lock = asyncio.Lock()


@dataclass
class _PoolObservation:
    """One reading of the request pool. The verdict uses these values as
    measured; only the public payload rounds them."""

    size: int
    max_size: int
    idle: int
    # None while the pool has room, which is not "saturated for 0 s".
    saturated_for: float | None
    # Whether the connections still change hands: a saturated pool that keeps
    # completing acquires is busy, one that has stopped is wedged.
    last_acquire_age: float | None

    def payload(self) -> dict:
        return {
            "size": self.size,
            "max": self.max_size,
            "idle": self.idle,
            # The earlier name of `idle`, kept for existing readers.
            "free": self.idle,
            "saturated_for_secs": _rounded(self.saturated_for),
            "last_acquire_age_secs": _rounded(self.last_acquire_age),
        }


def _rounded(seconds: float | None) -> float | None:
    return None if seconds is None else round(seconds, 3)


def _observe_pool(pool, state: _ReadyState) -> _PoolObservation:
    """Read the request pool's occupancy without waiting for a connection.

    Saturated means every connection the pool may open is open and lent out.
    The run of saturation is kept on `state` across probes; a probe that finds
    an idle connection, or room to open one, ends it.
    """
    size, max_size, idle = pool.get_size(), pool.get_max_size(), pool.get_idle_size()
    saturated_for: float | None = None
    if size >= max_size and idle == 0:
        now = time.monotonic()
        if state.pool_saturated_since is None:
            state.pool_saturated_since = now
        saturated_for = max(0.0, now - state.pool_saturated_since)
    else:
        state.pool_saturated_since = None
    # The acquire count itself stays internal: the payload is public.
    _, last_acquire_age = pool_progress()
    return _PoolObservation(size, max_size, idle, saturated_for, last_acquire_age)


def _pool_saturated_for(detail: dict | None) -> float | None:
    pool = detail.get("pool") if isinstance(detail, dict) else None
    return pool.get("saturated_for_secs") if isinstance(pool, dict) else None


def _pool_wedged(observed: _PoolObservation, grace: float) -> bool:
    """Saturated for the whole grace, with no acquire completed within it.

    A saturated pool whose connections keep changing hands stays ready however
    long it is saturated. With no acquire recorded at all (a pool built
    without the counting hook), the saturation run decides alone.
    """
    if observed.saturated_for is None or observed.saturated_for < grace:
        return False
    return observed.last_acquire_age is None or observed.last_acquire_age >= grace


# A server at max_connections refuses the probe's own connection while the
# pool's open connections keep working. Readiness then asks the pool instead,
# without queueing behind it, and labels a ready answer by how it was proven:
# a ping on an idle pool connection, or acquires completed within the grace.
_DB_OK_BY_POOL_IDLE = "ok:pool_idle"
_DB_OK_BY_POOL_PROGRESS = "ok:pool_progress"
_DB_READY = ("ok", _DB_OK_BY_POOL_IDLE, _DB_OK_BY_POOL_PROGRESS)


def _pool_unproven(pool: asyncpg.Pool) -> bool:
    """Nothing shows the pool could serve a request now: no idle connection,
    room to open one, and no acquire within the grace.

    Its connections may have expired since, and a server at max_connections
    refuses new ones even while the probe's own connection, opened earlier,
    still answers. Only an acquire through the pool settles that.
    """
    if pool.get_idle_size() > 0 or pool.get_size() >= pool.get_max_size():
        return False
    _, last_acquire_age = pool_progress()
    return last_acquire_age is None or last_acquire_age >= settings.readyz_pool_saturation_grace_secs


def _remaining(budget: asyncio.Timeout) -> float | None:
    deadline = budget.when()
    return None if deadline is None else max(0.0, deadline - asyncio.get_running_loop().time())


async def _ping_through_pool(pool: asyncpg.Pool, budget: asyncio.Timeout, progress_since: int) -> bool:
    """Run ``SELECT 1`` on a pool connection, borrowed and given back within
    the rest of the probe's budget. Raises when that does not succeed in time.

    Returns False without a ping when another caller's acquire has completed
    since `progress_since`, the count read before the probe decided to check
    the pool: the pool has just served someone. A probe queued behind a
    request whose connection is still opening must not report a working pool
    as broken.

    A connection that cannot finish in time is terminated rather than
    returned. Its query may never answer, nor the cancel request asyncpg
    sends for it, and returning it would wait for that cancel for as long
    again as the acquire was allowed. Terminating frees the pool slot at
    once and drops the pending cancel request.
    """
    conn = await _acquire_unless_another_caller_does(pool, budget, progress_since)
    if conn is None:
        return False
    try:
        await conn.fetchval("SELECT 1")
    except BaseException:
        _terminate_pool_connection(conn)
        raise
    left = _remaining(budget)
    if left is not None and left <= 0:
        _terminate_pool_connection(conn)
        raise TimeoutError
    await pool.release(conn, timeout=left)
    return True


async def _acquire_unless_another_caller_does(pool: asyncpg.Pool, budget: asyncio.Timeout, progress_since: int):
    """The probe's own acquire, or None once another caller's acquire has
    completed since `progress_since`.

    The progress waiter is registered before anything else can run, and the
    count is read again right after, so an acquire that completes in between
    is not missed. When another caller's acquire and the probe's own both
    end, the other caller's wins whatever happened to the probe's: the pool
    has served someone, even if the server refused the probe's extra
    connection.
    """
    progressed = pool_progress_waiter()
    try:
        if _progressed_since(progress_since):
            return None
        acquiring = asyncio.ensure_future(pool.acquire(timeout=_remaining(budget)))
        try:
            await asyncio.wait({acquiring, progressed}, return_when=asyncio.FIRST_COMPLETED)
        except BaseException:
            _abandon_acquire(acquiring)
            raise
        if progressed.done() or _progressed_since(progress_since):
            _stand_down(pool, acquiring)
            return None
        return acquiring.result()
    finally:
        forget_pool_progress_waiter(progressed)


# How long returning a connection the probe stood down from may take.
_READY_RETURN_TIMEOUT_SECONDS = 2.0
# Those returns run detached from the evaluation, held here until they end.
_standing_down: set[asyncio.Task] = set()


def _stand_down(pool: asyncpg.Pool, acquiring: asyncio.Future) -> None:
    """Another caller proved the pool, so the probe needs no connection.

    The verdict is settled; nothing here may wait. One the probe already holds
    goes back to the pool in a background task: terminating a working
    connection would cost the pool a server slot that the server, at its
    connection limit, may not give back, and waiting for the return could
    outlast the probe's budget and turn a proven pool into a failure. An
    acquire still pending is abandoned.
    """
    if not acquiring.done() or acquiring.cancelled() or acquiring.exception() is not None:
        _abandon_acquire(acquiring)
        return
    returning = asyncio.get_running_loop().create_task(
        _return_to_pool(pool, acquiring.result()), name="readyz-return-connection",
    )
    _standing_down.add(returning)
    returning.add_done_callback(_standing_down.discard)


async def _return_to_pool(pool: asyncpg.Pool, conn) -> None:
    """Return a connection within its own bound.

    asyncpg terminates a connection whose return fails or runs out of time;
    the terminate here only makes sure of it. A return still running when this
    task is cancelled is left to asyncpg, never terminated from outside.
    """
    try:
        await pool.release(conn, timeout=_READY_RETURN_TIMEOUT_SECONDS)
    except Exception:  # noqa: BLE001 — collected here; the verdict does not depend on it
        _terminate_pool_connection(conn)


def _progressed_since(count: int) -> bool:
    return pool_progress()[0] != count


def _abandon_acquire(acquiring: asyncio.Future) -> None:
    """Stop waiting for an acquire without awaiting its cleanup. A connection
    it still hands over is terminated; its error, if any, is consumed."""

    def drop(finished: asyncio.Future) -> None:
        if finished.cancelled():
            return
        if finished.exception() is None:
            _terminate_pool_connection(finished.result())

    if acquiring.done():
        drop(acquiring)
    else:
        acquiring.add_done_callback(drop)
        acquiring.cancel()


def _terminate_pool_connection(conn) -> None:
    try:
        conn.terminate()
    except Exception:  # noqa: BLE001 — the connection is given up either way
        logger.debug("readiness could not terminate a pool connection", exc_info=True)


async def _db_ok_despite_full_server(pool: asyncpg.Pool, budget: asyncio.Timeout) -> str | None:
    """How the pool vouches for the database after the server refused the
    probe's connection, or None when it cannot. A failing ping raises.

    An idle connection is borrowed for the rest of the probe's budget only.
    If another caller takes it first, that caller's completed acquire proves
    the pool instead, so the probe does not wait behind it. If the holder the
    probe takes has lost its connection, asyncpg reconnects, the full server
    refuses that too, and readiness fails.
    """
    if pool.is_closing():
        return None
    progress_since = pool_progress()[0]
    if pool.get_idle_size() > 0:
        if await _ping_through_pool(pool, budget, progress_since):
            return _DB_OK_BY_POOL_IDLE
        return _DB_OK_BY_POOL_PROGRESS
    _, last_acquire_age = pool_progress()
    if last_acquire_age is not None and last_acquire_age < settings.readyz_pool_saturation_grace_secs:
        return _DB_OK_BY_POOL_PROGRESS
    return None


async def _probe_ready() -> tuple[bool, dict]:
    """One readiness evaluation; see `_evaluate_readiness`."""
    # Every pool acquire this evaluation makes, directly or through the vector
    # check it starts, is readiness's own and does not count as pool progress.
    with readiness_acquires():
        return await _evaluate_readiness()


async def _evaluate_readiness() -> tuple[bool, dict]:
    """Readiness check. DB is the only hard dependency — failing DB takes
    every endpoint down. The vector store only powers search/publication
    views and has its own []-on-error fallback, so vector-store slowness
    is reported but does NOT fail readiness; otherwise a transient blip
    on the configured driver would pull the pod from the Service and
    break login/auth/CRUD for ~30s.

    The DB ping runs on a dedicated connection, and the request pool is only
    observed; it answers for the database only when the server refuses that
    connection (`_db_ok_despite_full_server`). A pool that clients have fully
    checked out is not a database failure, and a client burst saturates every
    replica's pool at the same moment: failing readiness on it would leave the
    Service with no endpoints while the database is fine. Saturation fails
    readiness only once it has lasted `readyz_pool_saturation_grace_secs` at
    every probe and no acquire has completed within that time: the pool is
    wedged, not busy.
    """
    detail: dict = {
        "document_revision_backend": selected_document_revision_backend(),
    }
    pool: asyncpg.Pool | None = None
    # Which check a failure belongs to: the database itself, or the pool's
    # ability to open a connection to it.
    stage = "db"
    try:
        # Initialization/reconnection is part of the same probe budget, and so
        # is every answer the pool gives.
        async with asyncio.timeout(_READY_DB_TIMEOUT_SECONDS) as budget:
            # An existing pool is returned at once; nothing is borrowed from it.
            pool = await get_pool()
            try:
                await readiness_ping(timeout=_READY_DB_TIMEOUT_SECONDS)
                detail["db"] = "ok"
            except asyncpg.TooManyConnectionsError:
                vouched = await _db_ok_despite_full_server(pool, budget)
                if vouched is None:
                    raise
                detail["db"] = vouched
            if detail["db"] == "ok":
                # Counted before deciding, so an acquire that completes from
                # here on proves the pool rather than slipping past the probe.
                progress_since = pool_progress()[0]
                if _pool_unproven(pool):
                    stage = "pool"
                    await _ping_through_pool(pool, budget, progress_since)
    except Exception as e:  # noqa: BLE001 — no connection strings in a public probe
        failure = "timeout" if isinstance(e, TimeoutError) else "unavailable"
        detail["db"] = f"error:{failure}" if stage == "db" else f"error:pool_{failure}"
    observed: _PoolObservation | None = None
    if pool is not None:
        observed = _observe_pool(pool, _ready_state)
        # Pool stats help diagnose leaks and bursts, on a failed probe too.
        detail["pool"] = observed.payload()
    if detail["db"] not in _DB_READY:
        return False, detail
    if observed is not None and observed.saturated_for is not None:
        if _pool_wedged(observed, settings.readyz_pool_saturation_grace_secs):
            return False, detail
        # A saturated result is shared for one second only, so a probe can run
        # every second while the pool stays saturated. The soft vector check
        # is skipped then: it cannot change the verdict, and it would add a
        # statement per second to a database already under that load.
        detail["vector_store"] = "skipped:pool_saturated"
        return True, detail
    detail["vector_store"] = await _vector_store_status()
    return True, detail


# The vector check in flight, if any. At most one is outstanding: a probe that
# finds the previous one still running waits for it, or, once a probe has given
# up on it, reports the timeout at once instead of starting another. A check
# given up on while returning its connection runs until that return ends.
_vector_check: asyncio.Task | None = None


async def _vector_store_status() -> str:
    global _vector_check
    task = _vector_check
    if task is None or task.done() or task.get_loop() is not asyncio.get_running_loop():
        task = asyncio.create_task(_vector_store_health(), name="readyz-vector-check")
        task.add_done_callback(_consume_outcome)
        _vector_check = task
    elif task.cancelling():
        return "degraded:timeout"
    done, _ = await asyncio.wait({task}, timeout=_READY_VECTOR_TIMEOUT_SECONDS)
    if not done:
        # Left to finish its cleanup in the background, however long that takes.
        task.cancel()
        return "degraded:timeout"
    if task.cancelled():
        return "degraded:timeout"
    error = task.exception()
    if error is not None:
        return "degraded:timeout" if isinstance(error, TimeoutError) else "degraded:unavailable"
    return "ok" if task.result() else "degraded:unreachable"


async def _vector_store_health() -> bool:
    # Imported here, as the factory does, so other drivers never load it.
    from app.services.vector_store.pgvector import PgvectorStore

    store = get_vector_store()
    if isinstance(store, PgvectorStore) and store.uses_main_pool:
        # The store reads the database the probe pings, through the request
        # pool. Its check runs on the probe's dedicated connection instead, so
        # the soft check never holds a request-pool connection that readiness
        # then judges as saturated or wedged.
        shared = store
        return await with_readiness_connection(
            lambda conn: shared.health(conn=conn), timeout=_READY_VECTOR_TIMEOUT_SECONDS,
        )
    return await store.health()


def _consume_outcome(task: asyncio.Task) -> None:
    # A check the probe gave up on still ends with an outcome; retrieve it so
    # it is not logged as never retrieved.
    if not task.cancelled():
        task.exception()


def _ready_response(state: _ReadyState, *, cached: bool):
    body = {
        "status": "ready" if state.ok else "not_ready",
        "cached": cached,
        "detail": state.detail,
    }
    if state.ok:
        return body
    raise HTTPException(status_code=503, detail=body)


def _cache_fresh(state: _ReadyState) -> bool:
    if state.detail is None:
        return False
    age = time.monotonic() - state.ts
    # Any result is shared for the floor. Callers queued on the lock while an
    # evaluation runs then reuse it instead of each starting their own.
    if age < _READY_MIN_INTERVAL_SECONDS:
        return True
    # Beyond the floor, only cache successes. If a previous probe failed, we
    # want to retry promptly so recovery is reflected in /readyz — a 30s
    # stale-failure cache pulls the pod from the Service for twice the actual
    # outage window.
    # Nor a success that saw a saturated pool: the grace is measured across
    # probes, so each probe during saturation has to look at the pool again.
    # Nor one the pool vouched for instead of the probe's own connection: an
    # idle connection or recent acquires prove the database only for now.
    return (
        state.ok
        and state.detail.get("db") == "ok"
        and _pool_saturated_for(state.detail) is None
        and age < _READY_TTL_SECONDS
    )


@app.get("/readyz")
async def readyz():
    if _cache_fresh(_ready_state):
        return _ready_response(_ready_state, cached=True)
    async with _ready_lock:
        if _cache_fresh(_ready_state):
            return _ready_response(_ready_state, cached=True)
        ok, detail = await _probe_ready()
        _ready_state.ts = time.monotonic()
        _ready_state.ok = ok
        _ready_state.detail = detail
    return _ready_response(_ready_state, cached=False)


def _terminal(section: dict | None) -> tuple[int, int]:
    """(exhausted, abandoned) of one queue section, tolerantly.

    Sections are not uniform — some expose `status`, some only counters,
    and a failed reporter is `{"error": ...}` — so read the counters, not
    the verdict. Unknown shapes contribute nothing rather than failing
    the whole response: a health endpoint must not 500 because one queue
    changed its stats dict.

    A section that distinguishes its live counts from its ledger reports
    both, suffixed `_at_head`, and that pair wins: an item given up on and
    later superseded by one that succeeded is history, not a fault, and a
    verdict that cannot tell the two apart says `degraded` forever after a
    single repaired loss. Sections without the suffix are unaffected.
    """
    if not isinstance(section, dict):
        return (0, 0)

    def _count(name: str) -> int:
        key = f"{name}_at_head" if f"{name}_at_head" in section else name
        try:
            return int(section.get(key) or 0)
        except (TypeError, ValueError):
            return 0

    return (_count("exhausted"), _count("abandoned"))


def _aggregate_status(sections: list[dict | None]) -> str:
    """Top-level verdict over the queue sections (#538).

    `degraded` when any section holds terminal work (exhausted or
    abandoned) — a queue that gave up on an item drains to `pending: 0`
    exactly like one that finished, so progress alone reads as complete
    while documents are missing. Otherwise `reconciling` while anything
    is still pending/retrying, else `ok`. Sections that only expose
    counters without terminal fields simply contribute zeros.

    "Terminal" is what `_terminal` says it is, which for a section that keeps
    a ledger means the part of it that is still true.
    """
    pending_work = False
    for section in sections:
        if not isinstance(section, dict) or section.get("error"):
            continue
        exhausted, abandoned = _terminal(section)
        if exhausted or abandoned:
            return "degraded"
        try:
            pending_work = pending_work or bool(
                int(section.get("pending") or 0) or int(section.get("retrying") or 0)
            )
        except (TypeError, ValueError):
            continue
    return "reconciling" if pending_work else "ok"


@app.get("/health")
async def health(user: AuthenticatedUser | None = Depends(get_optional_user)):
    """Detailed system health for dashboards.

    Auth posture: the operational stats (vector-store reachability, indexing /
    backfill counts, BM25, external-git / metadata / events backlogs) are
    monitoring data and stay readable by anyone — many e2e and uptime callers
    poll them unauthenticated. The sensitive operational INTERNALS, though —
    the RBAC hook-failure counters (which reveal role-sync security drift) and
    the audit-log stats — are returned ONLY to authenticated callers, so they
    are not world-readable. `/health/vault/{name}` remains the per-vault,
    reader-gated surface.

    Indexing is a single stage post-Phase-4 (embed + sparse + upsert
    in one atomic worker), so backfill stats live under
    `vector_store.backfill` and `embed_backfill` is gone — they were
    reporting the same `chunks.vector_indexed_at IS NULL` count.

    Every indexing queue reported here carries its own terminal-failure count
    beside its progress, and that adjacency is the point: a queue that gave up
    on an item drains to `pending: 0` exactly like one that finished, so
    progress alone reads as complete while documents are missing from ranked
    search. `native_derived` is the document/File-level queue — one intent per
    Resource revision, and `abandoned` there is a count of Resources that will
    never be chunked or embedded; `vector_store.backfill.upsert` is the
    chunk-level one below it; `native_file_projection` is the S3-to-Native
    admission ahead of both.

    `mcp_oauth` is not a queue: it reports whether the OIDC realm behind
    `mcp_oauth_enabled` advertises the DCR endpoint and vault scopes that
    path needs, and names the setup script when it does not. `unknown`
    means the IdP could not be read, which is not a verdict on the realm.
    """
    from app.services import (
        mcp_oauth_preconditions,
        native_derived_worker,
        native_file_projection,
        queue_rescuer,
        search_degradation_stats,
        sparse_encoder,
        vault_backfill,
    )

    store = get_vector_store()
    vs_info: dict = {"reachable": await store.health()}
    if settings.vector_store_driver == "pgvector":
        vs_info["sparse_shape"] = settings.sparse_shape_snapshot()
    try:
        vs_info["backfill"] = await embed_worker.pending_stats()
    except Exception as e:  # noqa: BLE001
        vs_info["backfill_error"] = str(e)
    # vault_id auto-backfill progress (issue #189 Phase 2). `ready` flips True
    # once every live-source point has its vault_id, which is when search
    # activates the vault-filter path; `null_remaining` includes orphans (which
    # never block readiness). pgvector same-instance only — else applicable=False.
    try:
        vs_info["vault_backfill"] = await vault_backfill.pending_stats()
    except Exception as e:  # noqa: BLE001
        vs_info["vault_backfill_error"] = str(e)
    try:
        vs_info["bm25"] = await sparse_encoder.stats_snapshot()
    except Exception as e:  # noqa: BLE001
        vs_info["bm25_error"] = str(e)

    async def _safe(fn):
        try:
            return await fn()
        except Exception as e:  # noqa: BLE001
            return {"error": str(e)}

    result: dict = {
        "service": "akb",
        "workers": worker_lifecycle_snapshot(),
        "queue_claims": queue_rescuer.snapshot(),
        "external_git": await _safe(external_git_poller.pending_stats),
        "asset_gc": await _safe(asset_gc_worker.pending_stats),
        "metadata_backfill": await _safe(metadata_worker.pending_stats),
        "events": await _safe(events_publisher.pending_stats),
        "notifications": await _safe(notification_worker.pending_stats),
        "native_file_projection": await _safe(native_file_projection.pending_stats),
        "native_derived": await _safe(native_derived_worker.pending_stats),
        "vector_store": vs_info,
        # Not a queue, and deliberately shaped like one anyway (akb#612): how
        # often a search response came back `degraded`, which cause, and how
        # many of those still carried results. Nothing counted this before —
        # `vector_store.reachable` answers a different question in the same
        # word, and a degraded search reaches `tool_usage_daily` under `ok`
        # beside every healthy one. The last number is what the consumer
        # decision turns on: whether a surface should show partial results
        # depends on how often they exist.
        "search": await _safe(search_degradation_stats.snapshot),
        # Not a queue either, and the only section here about a dependency
        # AKB does not own: whether the OIDC realm was ever given the client
        # scopes and DCR policy the MCP OAuth path needs (akb#635). An
        # unconfigured realm fails at the IdP with a bare 403 before any AKB
        # code runs, so this is the one place AKB can say which script fixes
        # it. Stays on the unauthenticated half because every input is a
        # public discovery document; see the module docstring for why it is
        # here rather than at startup, and for what it cannot see.
        "mcp_oauth": await _safe(mcp_oauth_preconditions.health_section),
    }

    # Top-level aggregate (#538): `degraded` when any queue section holds
    # terminal work, else `reconciling` while work is pending, else `ok`.
    # Computed AFTER the sections so the verdict always describes this
    # response, never a previous one. `vector_store.backfill.upsert` and
    # `.delete` are nested one level deeper than the flat sections, so they
    # join as their own entries; anything that failed to report (an
    # `{"error": ...}` dict, a missing key) contributes nothing.
    _backfill = vs_info.get("backfill") if isinstance(vs_info, dict) else None
    _upsert = _backfill.get("upsert") if isinstance(_backfill, dict) else None
    _delete = _backfill.get("delete") if isinstance(_backfill, dict) else None
    result["status"] = _aggregate_status([
        result["external_git"],
        result["metadata_backfill"],
        result["events"],
        result["native_file_projection"],
        result["native_derived"],
        _upsert,
        _delete,
    ])

    # Queue-head age, promoted to the top level because it is a named term in
    # the tenant-monitoring contract and that contract must not depend on the
    # internal shape of the operational blob below it — `vector_store.backfill`
    # is free to be reorganised, this key is not. It is also the one indexing
    # signal an external poller needs, so it stays on the unauthenticated half
    # of this response alongside the other backlog counters.
    #
    # Omitted when the queue is empty: there is no oldest item, and a 0 or an
    # epoch timestamp would render as an unboundedly stale queue.
    backfill = vs_info.get("backfill")
    if isinstance(backfill, dict):
        upsert = backfill.get("upsert")
        if isinstance(upsert, dict) and upsert.get("oldest_pending_enqueued_at") is not None:
            result["oldest_pending_enqueued_at"] = upsert["oldest_pending_enqueued_at"]

    # Sensitive operational internals — authenticated callers only:
    #  - PG-RBAC hook-failure counters + last reconcile outcome (silent
    #    role-sync drift); lets dashboards / oncall see it without grepping logs.
    #  - audit-log stats.
    #  - tool-usage queue depth + loss counters (an overflow or a systematic
    #    recording failure is otherwise only a rate-limited log line).
    if user is not None:
        try:
            from app.services.role_sync import get_role_sync

            result["rbac"] = get_role_sync().metrics_snapshot()
        except Exception as e:  # noqa: BLE001
            result["rbac"] = {"error": str(e)}
        from app.services.account_deletion_worker import pending_stats as account_cleanup_stats
        result["account_deletion_cleanup"] = await _safe(account_cleanup_stats)
        from app.services.sso_account_sync import pending_stats as sso_account_sync_stats
        result["sso_account_sync"] = await _safe(sso_account_sync_stats)
        result["audit"] = audit_log.stats()
        result["tool_usage"] = tool_usage.stats()

    return result


@app.get("/health/vault/{name}", summary="Per-vault indexing health (auth required)")
async def vault_health_route(
    name: str,
    user: AuthenticatedUser = Depends(get_current_user),
):
    """Vault-scoped pending-stats snapshot.

    Auth: vault reader role required. Unlike the global /health (which
    is unauthenticated for k8s probes and uptime monitors), this leaks
    vault existence — anonymous probing would tell an attacker which
    vault names exist. Consistent with the access model from issue #3.
    """
    access = await check_vault_access(user.user_id, name, required_role="reader")
    return {
        "vault": name,
        **(await vault_health(access["vault_id"])),
    }
