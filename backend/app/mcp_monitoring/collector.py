"""Non-blocking MCP completion recording in the API process."""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from collections import deque
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from itertools import islice
from typing import Any, NamedTuple
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

from mcp.server.context import CallNext, HandlerResult, ServerRequestContext
from mcp.types import CallToolResult
from opentelemetry import trace
from opentelemetry.trace.propagation.tracecontext import TraceContextTextMapPropagator

from app.config import settings
from app.db.postgres import get_pool
from app.mcp_monitoring.usage import CallMeasurements, current_usage
from app.services._backfill import BackfillRunner
from app.services.auth_service import AuthenticatedUser

logger = logging.getLogger(__name__)
CAPACITY = 10_000
BATCH_SIZE = 250
HEARTBEAT_SECONDS = 15
STALE_SECONDS = 45
_TRACE_CONTEXT = TraceContextTextMapPropagator()
_TURN_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:@/-]{0,127}")


def _request_trace_id(ctx: ServerRequestContext) -> UUID | None:
    # The MCP SDK gives valid message metadata precedence over the HTTP parent.
    # Extract into a fresh context so a locally generated span never groups calls.
    headers = ctx.request.headers.getlist("traceparent") if ctx.request is not None else []
    for value in ((ctx.meta or {}).get("traceparent"), headers[0] if len(headers) == 1 else None):
        if isinstance(value, str) and len(value) <= 512:
            parent = trace.get_current_span(_TRACE_CONTEXT.extract({"traceparent": value})).get_span_context()
            if parent.is_valid:
                return UUID(int=parent.trace_id)
    return None


def _agent_turn_id(meta: Mapping[str, object]) -> UUID | None:
    for key in ("com.dnotitia.akb/turn", "x-codex-turn-metadata"):
        turn = meta.get(key)
        if isinstance(turn, dict):
            thread_id, turn_id = turn.get("thread_id"), turn.get("turn_id")
            if (isinstance(thread_id, str) and _TURN_ID.fullmatch(thread_id)
                    and isinstance(turn_id, str) and _TURN_ID.fullmatch(turn_id)):
                # Store only a stable grouping key. Source, thread and turn
                # boundaries are unambiguous even for non-UUID client IDs.
                return uuid5(NAMESPACE_URL, f"akb:mcp:turn\n{key}\n{thread_id}\n{turn_id}")
    return None


def _conversation(ctx: ServerRequestContext, actor: UUID) -> tuple[UUID | None, str | None]:
    meta = ctx.meta or {}
    if not any(key in meta for key in ("x-codex-turn-metadata", "ai.opencode/sessionID", "threadId",
                                       "com.dnotitia.akb/turn", "gen_ai.conversation.id")):
        return None, None
    # Prefer the per-request client identity. Legacy transports may provide
    # the negotiated identity on the SDK session instead.
    info = meta.get("io.modelcontextprotocol/clientInfo")
    if isinstance(info, dict):
        name = info.get("name")
    else:
        params = ctx.session.client_params
        name = params.client_info.name if params and params.client_info else None
    if not isinstance(name, str) or not 0 < len(name) <= 128 or not name.isprintable() or not name.strip():
        name = None
    if name in ("akb-mcp-client", "akb-mcp"):
        name = None  # A proxy fallback identifies no originating agent.
    if name in ("codex", "codex-mcp-client", "codex_cli_rs"):
        name = "Codex"
    if name == "opencode":
        name = "OpenCode"

    codex = meta.get("x-codex-turn-metadata")
    shared = meta.get("com.dnotitia.akb/turn")
    candidates = (
        ("Codex", codex.get("thread_id") if isinstance(codex, dict) else None),
        ("OpenCode", meta.get("ai.opencode/sessionID")),
        (name, meta.get("threadId") if name == "Codex" else None),
        (name, shared.get("thread_id") if isinstance(shared, dict) else None),
        (name, meta.get("gen_ai.conversation.id")),
    )
    for agent, identifier in candidates:
        if (agent and isinstance(identifier, str) and 0 < len(identifier) <= 256
                and identifier.isprintable() and identifier.strip()):
            return uuid5(NAMESPACE_URL, f"akb:mcp:conversation\n{actor}\n{agent}\n{identifier}"), agent
    return None, None


class Observation(NamedTuple):
    observation_id: UUID
    finished_at: datetime
    actor_id: UUID
    account_kind: str
    auth_kind: str
    pat_id: UUID | None
    tool: str
    outcome: str
    duration_ms: int
    trace_id: UUID | None = None
    span_id: bytes | None = None
    usage: str | None = None
    started_at: datetime | None = None
    request_trace_id: UUID | None = None
    agent_turn_id: UUID | None = None
    conversation_key: UUID | None = None
    conversation_agent: str | None = None


class Collector:
    def __init__(self) -> None:
        self.queue: deque[Observation] = deque()
        self.running = False
        self.writer_id = uuid4()
        self.started = self.through = self.heartbeat = datetime.now(UTC)
        self.dropped = self.reported_drops = 0
        self.next_purge = 0.0
        self.runner = BackfillRunner("mcp_monitoring", self.tick, idle_secs=1, log_progress=False)

    def start(self) -> None:
        if self.runner.is_running() or self.runner.abandoned():
            return
        self.writer_id = uuid4()
        self.started = self.through = datetime.now(UTC)
        self.heartbeat = self.started - timedelta(seconds=HEARTBEAT_SECONDS)
        self.dropped = self.reported_drops = 0
        self.running = True
        self.next_purge = 0
        self.runner.start()

    def record(self, user: AuthenticatedUser, tool: str, outcome: str, elapsed: float,
               started_at: datetime, ctx: ServerRequestContext) -> None:
        if not self.running or not settings.mcp_monitoring.enabled:
            return
        if len(self.queue) >= CAPACITY:
            self.dropped += 1
            return
        auth = "service" if user.key_class == "service" else user.auth_method
        if auth not in ("pat", "service", "oauth") or (auth == "pat" and not user.token_id):
            return
        span = trace.get_current_span()
        context = span.get_span_context()
        sampled = settings.mcp_monitoring.export_traces and span.is_recording() and context.trace_flags.sampled
        usage = current_usage.get() if outcome == "ok" else None
        parent = _request_trace_id(ctx)
        actor = UUID(user.user_id)
        conversation, agent = _conversation(ctx, actor)
        self.queue.append(
            Observation(
                uuid4(),
                started_at + timedelta(seconds=elapsed),
                actor,
                user.account_kind,
                auth,
                UUID(user.token_id) if auth == "pat" and user.token_id else None,
                tool,
                outcome,
                int(elapsed * 1000),
                UUID(int=context.trace_id) if sampled else None,
                context.span_id.to_bytes(8, "big") if sampled else None,
                json.dumps(usage.values) if usage and usage.values else None,
                started_at,
                parent,
                _agent_turn_id(ctx.meta or {}) if parent is None else None,
                conversation,
                agent,
            )
        )

    async def flush(self, *, stopping: bool = False) -> int:
        now = datetime.now(UTC)
        batch = list(islice(self.queue, BATCH_SIZE))
        if not batch and not stopping and (now - self.heartbeat).total_seconds() < HEARTBEAT_SECONDS:
            return 0
        # An enqueue while the transaction is in flight belongs to the next
        # batch. Keep this prefix until commit confirmation, including on retry.
        through = batch[-1].finished_at if len(self.queue) > len(batch) else now
        drops = self.dropped
        pool = await get_pool()
        async with pool.acquire() as conn, conn.transaction():
            if batch:
                from mcp_server.tools import TOOL_METRICS

                definitions = [
                    (tool, metric.key, json.dumps(metric.descriptor()))
                    for tool in {event.tool for event in batch if event.usage}
                    for metric in TOOL_METRICS.get(tool, ())
                ]
                if definitions:
                    await conn.executemany(
                        "INSERT INTO mcp_monitoring.metric_definitions (tool, key, descriptor) VALUES ($1,$2,$3) "
                        "ON CONFLICT (tool,key) DO NOTHING", definitions,
                    )
                await conn.executemany(
                    """
                    INSERT INTO mcp_monitoring.tool_calls
                        (observation_id,finished_at,actor_id,account_kind,auth_kind,pat_id,tool,outcome,duration_ms,trace_id,span_id,
                         usage,started_at,request_trace_id,agent_turn_id,conversation_key,conversation_agent)
                    VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15,$16,$17) ON CONFLICT (observation_id) DO NOTHING
                """,
                    batch,
                )
            await conn.execute(
                """
                INSERT INTO mcp_monitoring.writers (id,started_at,flushed_through,heartbeat_at,stopped,dropped,collecting)
                VALUES ($1,$2,$3,$4,$5,$6,$7) ON CONFLICT (id) DO UPDATE SET
                    flushed_through=EXCLUDED.flushed_through, heartbeat_at=EXCLUDED.heartbeat_at,
                    stopped=EXCLUDED.stopped, dropped=EXCLUDED.dropped
            """,
                self.writer_id,
                self.started,
                through,
                now,
                stopping and len(batch) == len(self.queue),
                drops,
                settings.mcp_monitoring.enabled,
            )
            if drops > self.reported_drops or (now - self.heartbeat).total_seconds() > STALE_SECONDS:
                await conn.execute(
                    """
                    INSERT INTO mcp_monitoring.gaps VALUES ($1,$2,$3)
                    ON CONFLICT (writer_id,since) DO UPDATE SET until_at=greatest(gaps.until_at,EXCLUDED.until_at)
                """,
                    self.writer_id,
                    self.through,
                    now,
                )
        for _ in batch:
            self.queue.popleft()
        self.through, self.heartbeat, self.reported_drops = through, now, drops
        return len(batch)

    async def tick(self) -> int:
        n = await self.flush() if self.running else 0
        if time.monotonic() >= self.next_purge:
            cutoff = datetime.now(UTC) - timedelta(days=settings.mcp_monitoring.retention_days)
            pool = await get_pool()
            result = await pool.execute(
                """
                DELETE FROM mcp_monitoring.tool_calls WHERE observation_id IN (
                    SELECT observation_id FROM mcp_monitoring.tool_calls WHERE finished_at < $1
                    ORDER BY finished_at LIMIT 2000 FOR UPDATE SKIP LOCKED)
            """,
                cutoff,
            )
            await pool.execute("DELETE FROM mcp_monitoring.gaps WHERE until_at < $1", cutoff)
            await pool.execute("DELETE FROM mcp_monitoring.writers WHERE heartbeat_at < $1", cutoff)
            self.next_purge = time.monotonic() + (1 if result == "DELETE 2000" else 300)
        return n

    async def stop(self) -> None:
        was_running = self.running
        self.running = False
        if not await self.runner.stop(timeout=3) or not was_running:
            return
        try:
            async with asyncio.timeout(5):
                while await self.flush(stopping=True):
                    if not self.queue:
                        break
        finally:
            if self.queue:
                logger.warning("MCP monitoring stopped with %d observations pending", len(self.queue))
                self.queue.clear()


collector = Collector()


async def observe_tool(ctx: ServerRequestContext[Any, Any], call_next: CallNext) -> HandlerResult:
    if (
        ((not collector.running or not settings.mcp_monitoring.enabled) and not settings.mcp_monitoring.export_traces)
        or ctx.method != "tools/call"
        or ctx.request_id is None
        or ctx.request is None
    ):
        return await call_next(ctx)
    user = ctx.request.scope.get("akb.mcp.user")
    if not isinstance(user, AuthenticatedUser):
        return await call_next(ctx)
    from mcp_server.tools import TOOL_METRICS, catalog_tool_name

    tool = catalog_tool_name((ctx.params or {}).get("name"))
    if collector.running and settings.mcp_monitoring.enabled and (metrics := TOOL_METRICS.get(tool)):
        with current_usage.set(CallMeasurements(metrics)):
            return await _observe_completion(ctx, call_next, user, tool)
    return await _observe_completion(ctx, call_next, user, tool)


async def _observe_completion(
    ctx: ServerRequestContext[Any, Any], call_next: CallNext, user: AuthenticatedUser, tool: str,
) -> HandlerResult:
    started_at = datetime.now(UTC)
    started = time.monotonic()
    outcome = "unknown"
    try:
        result = await call_next(ctx)
        match result:
            case CallToolResult(is_error=True) | {"isError": True}:
                outcome = "tool_error"
            case CallToolResult() | dict():
                outcome = "ok"
        return result
    except asyncio.CancelledError:
        outcome = "cancelled"
        raise
    except Exception:
        outcome = "rpc_error"
        raise
    finally:
        try:
            collector.record(user, tool, outcome, time.monotonic() - started, started_at, ctx)
            span = trace.get_current_span()
            if span.is_recording():
                span.set_attribute("akb.auth.method", "service" if user.key_class == "service" else user.auth_method)
                span.set_attribute("akb.tool.outcome", outcome)
        except Exception:
            logger.exception("Could not record MCP monitoring observation")
