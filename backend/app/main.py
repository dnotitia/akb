"""AKB — Agent Knowledgebase API Server."""

import asyncio
import logging
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app.api.deps import get_current_user
from app.db.postgres import get_pool
from app.exceptions import AKBError
from app.api.routes import access, auth, documents, files, memory, public, search, collections, knowledge, sessions, tables
from app.services import embed_worker, events_publisher, external_git_poller, metadata_worker
from app.services.access_service import check_vault_access
from app.services.auth_service import AuthenticatedUser
from app.services.health import vault_health
from app.services.lifecycle import init_storage, shutdown_storage, start_workers, stop_workers
from app.services.vector_store import get_vector_store
from mcp_server.http_app import mcp_app

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("akb")


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("Starting AKB server")
    await init_storage()
    start_workers()
    yield
    await stop_workers()
    await shutdown_storage()
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
    return JSONResponse(
        status_code=exc.status_code,
        content={"error": exc.message},
    )


app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(auth.router, prefix="/api/v1", tags=["auth"])
app.include_router(access.router, prefix="/api/v1", tags=["access"])
app.include_router(documents.router, prefix="/api/v1", tags=["documents"])
app.include_router(search.router, prefix="/api/v1", tags=["search"])
app.include_router(collections.router, prefix="/api/v1", tags=["collections"])
app.include_router(knowledge.router, prefix="/api/v1", tags=["knowledge"])
app.include_router(sessions.router, prefix="/api/v1", tags=["sessions"])
app.include_router(memory.router, prefix="/api/v1", tags=["memory"])
app.include_router(tables.router, prefix="/api/v1", tags=["tables"])
app.include_router(files.router, prefix="/api/v1", tags=["files"])
app.include_router(public.router, prefix="/api/v1", tags=["public"])

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
    detail: dict = {}
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
    return (
        state.detail is not None
        and state.ok
        and (time.monotonic() - state.ts) < _READY_TTL_SECONDS
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


@app.get("/health")
async def health():
    """Detailed system health for dashboards.

    Indexing is a single stage post-Phase-4 (embed + sparse + upsert
    in one atomic worker), so backfill stats live under
    `vector_store.backfill` and `embed_backfill` is gone — they were
    reporting the same `chunks.vector_indexed_at IS NULL` count.
    """
    from app.services import sparse_encoder
    vs_info: dict = {"reachable": await get_vector_store().health()}
    try:
        vs_info["backfill"] = await embed_worker.pending_stats()
    except Exception as e:  # noqa: BLE001
        vs_info["backfill_error"] = str(e)
    try:
        vs_info["bm25"] = await sparse_encoder.stats_snapshot()
    except Exception as e:  # noqa: BLE001
        vs_info["bm25_error"] = str(e)

    async def _safe(fn):
        try:
            return await fn()
        except Exception as e:  # noqa: BLE001
            return {"error": str(e)}

    return {
        "status": "ok",
        "service": "akb",
        "external_git": await _safe(external_git_poller.pending_stats),
        "metadata_backfill": await _safe(metadata_worker.pending_stats),
        "events": await _safe(events_publisher.pending_stats),
        "vector_store": vs_info,
    }


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
