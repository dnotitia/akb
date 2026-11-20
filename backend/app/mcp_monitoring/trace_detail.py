"""Read only the exported server spans belonging to this stored observation."""

from datetime import UTC, datetime, timedelta
from uuid import UUID

from fastapi import HTTPException
from google.protobuf.message import DecodeError
import httpx
from opentelemetry.proto.trace.v1.trace_pb2 import TracesData

from app.config import settings
from app.db.postgres import get_pool


EXPORT_GRACE = timedelta(minutes=2)


def _missing_span(age: timedelta, retention_hours: float | None) -> HTTPException:
    if retention_hours is not None and age.total_seconds() >= retention_hours * 3600:
        return HTTPException(410, detail={
            "code": "trace_expired",
            "message": f"This call is outside the {retention_hours:g}-hour Tempo retention period.",
        }, headers={"Cache-Control": "no-store"})
    if age < EXPORT_GRACE:
        return HTTPException(404, detail={
            "code": "trace_export_pending",
            "message": "This recent call may still be exporting. Try again shortly.",
        }, headers={"Cache-Control": "no-store"})
    return HTTPException(404, detail={
        "code": "trace_not_found", "message": "Tempo did not return the recorded span for this call.",
    }, headers={"Cache-Control": "no-store"})


def project_spans(data: TracesData, trace_id: bytes, span_id: bytes, tool: str):
    spans = {}
    for resource in data.resource_spans:
        if not any(a.key == "service.name" and a.value.string_value == "akb-mcp" for a in resource.resource.attributes):
            continue
        for scope in resource.scope_spans:
            for span in scope.spans:
                if span.trace_id == trace_id and span.kind == span.SPAN_KIND_SERVER:
                    spans[(scope.scope.name, span.span_id)] = span
    call = spans.get(("mcp-python-sdk", span_id))
    if call is None:
        return None
    parent = spans.get(("akb.mcp.http", call.parent_span_id))
    result = []
    for name, item in (("/mcp", parent), (f"tools/call {tool}", call)):
        if item is not None:
            result.append({
                "name": name,
                "start": datetime.fromtimestamp(item.start_time_unix_nano / 1e9, UTC),
                "duration_ms": max(0, item.end_time_unix_nano - item.start_time_unix_nano) / 1e6,
            })
    return {"spans": result}


async def load_trace(observation_id: UUID):
    cfg = settings.mcp_monitoring
    pool = await get_pool()
    row = await pool.fetchrow(
        """SELECT trace_id, span_id, tool, finished_at FROM mcp_monitoring.tool_calls
           WHERE observation_id=$1 AND finished_at >= $2 AND trace_id IS NOT NULL""",
        observation_id, datetime.now(UTC) - timedelta(days=cfg.retention_days),
    )
    if not row or not cfg.tempo_query_url:
        raise HTTPException(404, "No trace is available for this call")
    trace_id = row["trace_id"]
    url = f"{cfg.tempo_query_url.rstrip('/')}/api/traces/{trace_id.hex}"
    try:
        async with httpx.AsyncClient(timeout=5, trust_env=False) as client:
            async with client.stream("GET", url, headers={"Accept": "application/protobuf"}) as response:
                if response.status_code == 404:
                    raise _missing_span(datetime.now(UTC) - row["finished_at"], cfg.tempo_retention_hours)
                response.raise_for_status()
                body = bytearray()
                async for chunk in response.aiter_bytes():
                    if len(body) + len(chunk) > 4 * 1024 * 1024:
                        raise HTTPException(502, "Trace exceeds the supported size")
                    body.extend(chunk)
        data = TracesData.FromString(bytes(body))
    except (httpx.HTTPError, DecodeError) as exc:
        raise HTTPException(503, "Trace store is unavailable") from exc
    detail = project_spans(data, trace_id.bytes, row["span_id"], row["tool"])
    if detail is None:
        raise _missing_span(datetime.now(UTC) - row["finished_at"], cfg.tempo_retention_hours)
    return detail
