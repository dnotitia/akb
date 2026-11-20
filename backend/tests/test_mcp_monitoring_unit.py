import asyncio
from types import SimpleNamespace
from uuid import uuid4

import pytest
from starlette.datastructures import Headers
from opentelemetry import trace
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import ReadableSpan
from opentelemetry.sdk.util.instrumentation import InstrumentationScope
from opentelemetry.trace import SpanContext, SpanKind, Status, StatusCode, TraceState

from app.mcp_monitoring import collector as module
from app.mcp_monitoring.tracing import exportable_span
from app.services.auth_service import AuthenticatedUser


@pytest.mark.parametrize("failure, outcome", [(ValueError, "rpc_error"), (asyncio.CancelledError, "cancelled")])
async def test_middleware_records_and_propagates_failures(monkeypatch, failure, outcome):
    recorder = module.Collector()
    monkeypatch.setattr(module, "collector", recorder)
    recorder.running = True
    user = AuthenticatedUser(str(uuid4()), "fixture", "", None, False, "oauth")
    ctx = SimpleNamespace(
        request_id=0,
        method="tools/call",
        params={"name": "private-input"},
        meta=None, request=SimpleNamespace(scope={"akb.mcp.user": user}, headers=Headers()),
    )

    async def failed(ctx):
        raise failure()

    with pytest.raises(failure):
        await module.observe_tool(ctx, failed)
    [event] = recorder.queue
    assert event.tool == "other" and event.outcome == outcome
    monkeypatch.setattr(module, "CAPACITY", 1)
    with pytest.raises(failure):
        await module.observe_tool(ctx, failed)
    assert recorder.dropped == 1 and list(recorder.queue) == [event]
    ctx.request_id = None
    with pytest.raises(failure):
        await module.observe_tool(ctx, failed)
    assert recorder.dropped == 1


def test_export_uses_only_catalog_values_and_trace_ids():
    ctx = SpanContext(1, 2, True, trace.TraceFlags(1), TraceState([("fixture", "private-input")]))
    span = ReadableSpan(
        "private-input",
        context=ctx,
        parent=ctx,
        kind=SpanKind.SERVER,
        resource=Resource({"host.name": "private-input"}),
        events=[object()],
        links=[object()],
        attributes={
            "mcp.method.name": "tools/call",
            "gen_ai.tool.name": "private-input",
            "akb.auth.method": "oauth",
            "jsonrpc.request.id": "private-input",
        },
        status=Status(StatusCode.ERROR, "private-input"),
        instrumentation_scope=InstrumentationScope("mcp-python-sdk"),
    )
    exported = exportable_span(span)
    assert exported and exported.name == "tools/call other" and not exported.events and not exported.links
    assert exported.attributes == {
        "mcp.method.name": "tools/call",
        "gen_ai.tool.name": "other",
        "akb.auth.method": "oauth",
    }
    assert exported.status.description is None and exported.status.status_code == StatusCode.ERROR
    assert exported.parent.span_id == 2 and exported.context.trace_id == 1
    assert not exported.parent.trace_state and not exported.context.trace_state
    assert dict(exported.resource.attributes) == {"service.name": "akb-mcp"}


def test_trace_detail_only_displays_this_observation_and_its_parent():
    from opentelemetry.proto.trace.v1.trace_pb2 import Span, TracesData

    from app.mcp_monitoring.trace_detail import project_spans

    data = TracesData()
    resource = data.resource_spans.add()
    resource.resource.attributes.add(key="service.name").value.string_value = "akb-mcp"
    parent = b"12345678"
    target = b"abcdefgh"
    trace_id = uuid4().bytes
    for scope, sid, pid in [
        ("akb.mcp.http", parent, b""), ("mcp-python-sdk", target, parent),
        ("mcp-python-sdk", b"87654321", parent),
    ]:
        group = resource.scope_spans.add()
        group.scope.name = scope
        group.spans.add(trace_id=trace_id, span_id=sid, parent_span_id=pid, kind=Span.SPAN_KIND_SERVER,
                        name="untrusted data", start_time_unix_nano=1_000_000, end_time_unix_nano=3_000_000)
    detail = project_spans(data, trace_id, target, "akb_help")
    assert [s["name"] for s in detail["spans"]] == ["/mcp", "tools/call akb_help"]
    assert all(s["duration_ms"] == 2 for s in detail["spans"])
    assert project_spans(data, trace_id, b"missing!", "akb_help") is None


@pytest.mark.parametrize("age_seconds,retention,status,expected,code", [
    (10, 48, 404, 404, "trace_export_pending"),
    (120, 48, 404, 404, "trace_not_found"),
    (48 * 3600, 48, 404, 410, "trace_expired"),
    (90 * 3600, None, 404, 404, "trace_not_found"),
    (10, 48, 200, 404, "trace_export_pending"),  # Partial trace, missing this span.
    (90 * 3600, 48, 503, 503, None),
    (90 * 3600, 48, 200, 200, None),  # Still readable during compaction.
])
async def test_trace_lookup_states(monkeypatch, age_seconds, retention, status, expected, code):
    from datetime import UTC, datetime, timedelta
    from unittest.mock import AsyncMock
    from fastapi import HTTPException
    import httpx
    from opentelemetry.proto.trace.v1.trace_pb2 import Span, TracesData
    from app.mcp_monitoring import trace_detail

    trace_id, span_id = uuid4(), b"abcdefgh"
    row = dict(trace_id=trace_id, span_id=span_id, tool="akb_put",
               finished_at=datetime.now(UTC) - timedelta(seconds=age_seconds))
    pool = SimpleNamespace(fetchrow=AsyncMock(return_value=row))
    monkeypatch.setattr(trace_detail, "get_pool", AsyncMock(return_value=pool))
    cfg = trace_detail.settings.mcp_monitoring
    monkeypatch.setattr(cfg, "tempo_query_url", "http://tempo.test")
    monkeypatch.setattr(cfg, "tempo_retention_hours", retention)
    data = TracesData()
    if expected == 200:
        resource = data.resource_spans.add()
        resource.resource.attributes.add(key="service.name").value.string_value = "akb-mcp"
        scope = resource.scope_spans.add()
        scope.scope.name = "mcp-python-sdk"
        scope.spans.add(trace_id=trace_id.bytes, span_id=span_id, kind=Span.SPAN_KIND_SERVER)
    transport = httpx.MockTransport(lambda _: httpx.Response(status, content=data.SerializeToString()))
    client = httpx.AsyncClient(transport=transport)
    monkeypatch.setattr(trace_detail.httpx, "AsyncClient", lambda **kw: client)
    if expected == 200:
        assert (await trace_detail.load_trace(uuid4())) == {
            "spans": [{"name": "tools/call akb_put", "start": datetime.fromtimestamp(0, UTC), "duration_ms": 0}],
        }
    else:
        with pytest.raises(HTTPException) as exc:
            await trace_detail.load_trace(uuid4())
        assert exc.value.status_code == expected
        if code:
            assert exc.value.detail["code"] == code


def test_declared_metrics_project_only_bounded_values():
    from app.mcp_monitoring.usage import CallMeasurements, Dimension, Latency, ZeroRate, current_usage, measure_result, measured_tool

    tool = measured_tool(
        ZeroRate("empty", "Misses", "returned"),
        Latency("cost", "Latency", (Dimension("size", "Size", lambda a, r: a["size"]),)),
        name="fixture", inputSchema={"type": "object"},
    )
    assert "metrics" not in str(tool.model_dump())
    for count, size, expected in [
        (0, 1, {"empty": True, "cost": {"size": 1}}), (2, None, {"empty": False, "cost": {"size": None}}),
        (True, "private", {}), (None, 2**60, {}),
    ]:
        usage = CallMeasurements(tool.metrics)
        with current_usage.set(usage):
            measure_result({"size": size, "query": "private"}, {"returned": count, "body": "private"})
        assert usage.values == expected


@pytest.mark.parametrize(("metric_key", "dimension_keys"), [
    ("empty/results", ()), ("latency", ("page/size",)), ("latency", ("page", "page")),
])
def test_catalog_rejects_unaddressable_or_ambiguous_metrics(metric_key, dimension_keys):
    from app.mcp_monitoring.usage import Dimension, Latency, measured_tool

    dimensions = tuple(Dimension(key, key, lambda args, result: 1) for key in dimension_keys)
    with pytest.raises(ValueError, match="monitoring key"):
        measured_tool(Latency(metric_key, "Latency", dimensions), name="fixture", input_schema={"type": "object"})


def test_operation_context_requires_valid_incoming_parent_and_ignores_sampling():
    from uuid import UUID

    trace_a, trace_b = uuid4().hex, uuid4().hex
    header = f"00-{trace_a}-1111111111111111-00"
    meta = f"00-{trace_b}-2222222222222222-01"

    def extract(headers=(), metadata=None):
        ctx = SimpleNamespace(meta=metadata, request=SimpleNamespace(headers=Headers(raw=[
            (b"traceparent", h.encode()) for h in headers
        ])))
        return module._request_trace_id(ctx)

    assert extract([header]) == UUID(trace_a)
    assert extract([header], {"traceparent": meta}) == UUID(trace_b)
    assert extract([header], {"traceparent": 5}) == UUID(trace_a)
    assert extract([], {"traceparent": meta}) == UUID(trace_b)
    assert extract([header, meta]) is None
    assert extract([f"00-{trace_a}-0000000000000000-01"]) is None
    assert extract() is None


def test_turn_key_requires_both_identifiers_and_is_namespaced():
    pair = {"thread_id": "session-1", "turn_id": "turn-9"}
    generic, codex = "com.dnotitia.akb/turn", "x-codex-turn-metadata"
    def group(key=generic, **ids):
        return module._agent_turn_id({key: ids})

    assert group(**pair) is not None
    assert len({group(**pair), group(codex, **pair), group(thread_id="other", turn_id="turn-9"),
                group(thread_id="session-1", turn_id="turn-10")}) == 4
    assert group(**pair) == group(**pair, unrelated="ignored")
    for invalid in (None, {}, "session-1", {"thread_id": "session-1"}, {**pair, "turn_id": ""},
                    {**pair, "thread_id": ["test"]}, {**pair, "turn_id": "x" * 129}):
        assert module._agent_turn_id({generic: invalid}) is None


def test_conversation_identity_is_scoped_to_actor_and_agent():
    actor, other = uuid4(), uuid4()

    def identify(meta, *, user=actor, client=None):
        params = SimpleNamespace(client_info=SimpleNamespace(name=client)) if client else None
        return module._conversation(SimpleNamespace(meta=meta, session=SimpleNamespace(client_params=params)), user)

    def codex(turn):
        return {"x-codex-turn-metadata": {"thread_id": "chat-1", "turn_id": turn}}

    key, agent = identify(codex("first"))
    assert key and agent == "Codex"
    assert identify(codex("second")) == identify({"threadId": "chat-1"}, client="codex-mcp-client") == (key, agent)
    assert identify(codex("first"), user=other)[0] != key
    assert identify({"x-codex-turn-metadata": {"thread_id": "other"}})[0] != key
    open_key, open_agent = identify({"ai.opencode/sessionID": "chat-1"})
    assert open_key and open_key != key and open_agent == "OpenCode"
    shared = {"com.dnotitia.akb/turn": {"thread_id": "chat-1"}}
    a = identify(shared, client="Agent A")
    assert a[0] and a[1] == "Agent A"
    assert a == identify({"gen_ai.conversation.id": "chat-1", "io.modelcontextprotocol/clientInfo": {"name": "Agent A"}})
    assert a[0] != identify(shared, client="Agent B")[0]
    assert identify(shared, client="akb-mcp-client") == (None, None)
    for meta in ({"threadId": "chat-1"}, {"session_id": "login"}, {"x-codex-turn-metadata": {"turn_id": "1"}},
                 {"ai.opencode/sessionID": "  "}, {"ai.opencode/sessionID": "x"*257}, {"ai.opencode/sessionID": {}}):
        assert identify(meta, client="Agent A") == (None, None)
