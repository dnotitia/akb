"""Opt-in OTLP/HTTP export of the MCP SDK and HTTP server spans."""

from collections.abc import Sequence

from mcp_types.version import KNOWN_PROTOCOL_VERSIONS
from opentelemetry import trace
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.instrumentation.asgi import OpenTelemetryMiddleware
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import ReadableSpan, TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor, SpanExportResult
from opentelemetry.sdk.util.instrumentation import InstrumentationScope
from opentelemetry.trace import SpanContext, SpanKind, Status
from starlette.types import ASGIApp

_RESOURCE = Resource({"service.name": "akb-mcp"})
_HTTP_SCOPE = "akb.mcp.http"
_SDK_SCOPE = "mcp-python-sdk"
_METHODS = {"initialize", "ping", "server/discover", "tools/call", "tools/list", "notifications/initialized"}
_AUTH = ("oauth", "pat", "service")
_OUTCOMES = ("ok", "tool_error", "rpc_error", "cancelled", "unknown")


def _ids(context: SpanContext | None) -> SpanContext | None:
    if context is None:
        return None
    return SpanContext(context.trace_id, context.span_id, context.is_remote, context.trace_flags)


def exportable_span(span: ReadableSpan) -> ReadableSpan | None:
    """Project to the export contract. SDK names, events and status descriptions can contain client input."""
    scope = span.instrumentation_scope
    if span.kind != SpanKind.SERVER or scope is None or scope.name not in (_HTTP_SCOPE, _SDK_SCOPE):
        return None
    source = span.attributes or {}
    attributes: dict[str, str | int] = {}
    if scope.name == _HTTP_SCOPE:
        name = "/mcp"
        status = source.get("http.response.status_code", source.get("http.status_code"))
        if isinstance(status, int) and 100 <= status <= 599:
            attributes["http.response.status_code"] = status
    else:
        from mcp_server.tools import catalog_tool_name

        method = source.get("mcp.method.name")
        name = method if isinstance(method, str) and method in _METHODS else "other"
        attributes["mcp.method.name"] = name
        if name == "tools/call":
            tool = catalog_tool_name(source.get("gen_ai.tool.name"))
            attributes["gen_ai.tool.name"] = tool
            name = f"tools/call {tool}"
        for key, allowed in (
            ("mcp.protocol.version", KNOWN_PROTOCOL_VERSIONS),
            ("akb.auth.method", _AUTH),
            ("akb.tool.outcome", _OUTCOMES),
        ):
            value = source.get(key)
            if isinstance(value, str) and value in allowed:
                attributes[key] = value
    return ReadableSpan(
        name,
        context=_ids(span.context),
        parent=_ids(span.parent),
        resource=_RESOURCE,
        instrumentation_scope=InstrumentationScope(scope.name),
        attributes=attributes,
        kind=span.kind,
        status=Status(span.status.status_code),
        start_time=span.start_time,
        end_time=span.end_time,
    )


class McpSpanExporter(OTLPSpanExporter):
    def export(self, spans: Sequence[ReadableSpan]) -> SpanExportResult:
        safe = [item for span in spans if (item := exportable_span(span)) is not None]
        return super().export(safe) if safe else SpanExportResult.SUCCESS


def instrument_mcp(app: ASGIApp) -> ASGIApp:
    # The pinned MCP SDK uses the global provider. Install once per process.
    provider = trace.get_tracer_provider()
    if isinstance(provider, trace.ProxyTracerProvider):
        provider = TracerProvider(resource=_RESOURCE)
        trace.set_tracer_provider(provider)
    if not isinstance(provider, TracerProvider):
        raise RuntimeError("MCP trace export requires an OpenTelemetry SDK tracer provider")
    provider.add_span_processor(BatchSpanProcessor(McpSpanExporter()))
    return OpenTelemetryMiddleware(app, tracer=provider.get_tracer(_HTTP_SCOPE), exclude_spans=["send", "receive"])
