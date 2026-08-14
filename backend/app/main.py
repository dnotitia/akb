"""AKB — Agent Knowledgebase API Server."""

import asyncio
import logging
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.api.deps import get_current_user, get_optional_user
from app.api.routes import (
    access,
    admin_auth,
    admin_sso,
    activity,
    assets,
    app_inventory,
    app_installations,
    app_rollouts,
    app_registry,
    agent_sessions,
    app_identity,
    auth,
    collections,
    documents,
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
from app.db.postgres import get_pool
from app.exceptions import AKBError
from app.logging_redaction import install_secret_redaction
from app.openapi_contract import install_openapi_contract
from app.services import (
    asset_gc_worker,
    audit_log,
    embed_worker,
    events_publisher,
    external_git_poller,
    external_git_service,
    metadata_worker,
    tool_usage,
)
from app.services.access_service import check_vault_access
from app.services.auth_service import AuthenticatedUser
from app.services.external_git_capability import check_external_git_capability
from app.services.health import vault_health
from app.services.lifecycle import init_storage, shutdown_storage, start_workers, stop_workers
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
    await init_storage()
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
    start_workers()
    yield
    await stop_workers()
    await shutdown_storage()
    # Drain the audit writer's queue so a rollout doesn't drop its tail. Off the
    # loop since shutdown() blocks on the queue join.
    await asyncio.to_thread(audit_log.shutdown)
    logger.info("Server shutdown")


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
    return JSONResponse(
        status_code=422,
        content=_error_payload(
            422,
            "Request validation failed",
            details=details,
            legacy_detail=details,
        ),
    )


@app.exception_handler(Exception)
async def unhandled_error_handler(request: Request, exc: Exception):
    logger.exception("Unhandled request error")
    return JSONResponse(
        status_code=500,
        content=_error_payload(500, "Internal server error"),
    )


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
    return INTERNAL


app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.middleware("http")
async def _no_store_public_surfaces(request: Request, call_next):
    """Never let a browser, proxy, or CDN cache a publication surface
    (publish-hardening M2).

    These responses can carry password-gated content, the short-lived auth
    token, or presigned file URLs — anything cached here could be replayed to a
    later, unauthenticated request (e.g. via the back button or a shared cache).
    `no-store` is the safe default for the whole public + publications surface;
    the payloads are small and fetched by the SPA, so lost cacheability is a
    non-issue. `/api/v1/public` also matches `/api/v1/publications/*`.
    """
    response = await call_next(request)
    path = request.url.path
    # Match on segment boundaries so a hypothetical unrelated `/api/v1/publicX`
    # route can't accidentally inherit no-store (per Codex M2 review). The two
    # publication bases are listed explicitly — `/publications/*` is a sibling
    # of `/public/*`, not a child.
    for base in ("/api/v1/public", "/api/v1/publications", "/api/v1/oembed"):
        if path == base or path.startswith(base + "/"):
            response.headers["Cache-Control"] = "no-store"
            break
    if (
        path == "/api/v1/auth/config"
        or path.startswith("/api/v1/admin/")
        or path.startswith("/api/v1/app/installations/")
        or path.startswith("/api/v1/app/rollouts")
        or (path.startswith("/api/v1/apps/") and "/rollouts" in path)
        or (path.startswith("/api/v1/apps/") and "/installations/" in path)
    ):
        response.headers["Cache-Control"] = "no-store"
        response.headers["Pragma"] = "no-cache"
    return response


app.include_router(auth.router, prefix="/api/v1", tags=["auth"])
app.include_router(admin_auth.router, prefix="/api/v1", tags=["admin-auth"])
app.include_router(admin_sso.router, prefix="/api/v1", tags=["admin-sso"])
app.include_router(app_identity.router, prefix="/api/v1", tags=["app-identity"])
app.include_router(app_registry.router, prefix="/api/v1", tags=["app-registry"])
app.include_router(app_inventory.router, prefix="/api/v1", tags=["app-inventory"])
app.include_router(app_installations.router, prefix="/api/v1", tags=["app-installations"])
app.include_router(app_rollouts.router, prefix="/api/v1", tags=["app-rollouts"])
app.include_router(access.router, prefix="/api/v1", tags=["access"])
app.include_router(documents.router, prefix="/api/v1", tags=["documents"])
app.include_router(search.router, prefix="/api/v1", tags=["search"])
app.include_router(collections.router, prefix="/api/v1")
app.include_router(knowledge.router, prefix="/api/v1")
app.include_router(activity.router, prefix="/api/v1", tags=["activity"])
app.include_router(agent_sessions.router, prefix="/api/v1", tags=["agent-sessions"])
app.include_router(tables.router, prefix="/api/v1", tags=["tables"])
app.include_router(knowledge_io.router, prefix="/api/v1", tags=["export-import"])
app.include_router(files.router, prefix="/api/v1", tags=["files"])
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


@dataclass
class _ReadyState:
    ts: float = 0.0
    ok: bool = False
    detail: dict | None = None


_ready_state = _ReadyState()
_ready_lock = asyncio.Lock()


async def _probe_ready() -> tuple[bool, dict]:
    """Readiness check. DB is the only hard dependency — failing DB takes
    every endpoint down. The vector store only powers search/publication
    views and has its own []-on-error fallback, so vector-store slowness
    is reported but does NOT fail readiness; otherwise a transient blip
    on the configured driver would pull the pod from the Service and
    break login/auth/CRUD for ~30s.
    """
    detail: dict = {
        "document_revision_backend": selected_document_revision_backend(),
    }
    try:
        pool = await get_pool()
        await asyncio.wait_for(pool.fetchval("SELECT 1"), timeout=2.0)
        detail["db"] = "ok"
        # Pool stats help diagnose leaks: if free→0 while size→max we are
        # exhausting the pool and slow callers are holding connections.
        detail["pool"] = {
            "size": pool.get_size(),
            "free": pool.get_idle_size(),
            "max": pool.get_max_size(),
        }
    except Exception as e:  # noqa: BLE001 — repr() so TimeoutError() shows class
        detail["db"] = f"error: {e!r}"
        return False, detail
    try:
        vs_ok = await asyncio.wait_for(get_vector_store().health(), timeout=5.0)
        detail["vector_store"] = "ok" if vs_ok else "degraded:unreachable"
    except Exception as e:  # noqa: BLE001
        detail["vector_store"] = f"degraded:{e!r}"
    return True, detail


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
    # Only cache successes. If a previous probe failed, we want to retry
    # immediately so recovery is reflected in /readyz on the very next
    # call — a 30s stale-failure cache pulls the pod from the Service for
    # twice the actual outage window.
    return state.detail is not None and state.ok and (time.monotonic() - state.ts) < _READY_TTL_SECONDS


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
    """
    from app.services import sparse_encoder, vault_backfill

    store = get_vector_store()
    vs_info: dict = {"reachable": await store.health()}
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
        "status": "ok",
        "service": "akb",
        "external_git": await _safe(external_git_poller.pending_stats),
        "asset_gc": await _safe(asset_gc_worker.pending_stats),
        "metadata_backfill": await _safe(metadata_worker.pending_stats),
        "events": await _safe(events_publisher.pending_stats),
        "vector_store": vs_info,
    }

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
