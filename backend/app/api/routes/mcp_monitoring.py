"""Installation-local, product-admin MCP monitoring."""

from datetime import UTC, datetime, timedelta
from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import AwareDatetime, StringConstraints

from app.api.routes.admin_auth import get_current_product_admin
from app.mcp_monitoring.report import read_operation_calls, read_report
from app.mcp_monitoring.trace_detail import load_trace
from app.mcp_monitoring.usage import METRIC_KEY_PATTERN, TOOL_NAME_PATTERN
from app.mcp_monitoring.schema import AuthKind, CallerKind, MonitoringReport, OperationDetails

router = APIRouter(dependencies=[Depends(get_current_product_admin)])


MetricView = Annotated[str, StringConstraints(pattern=rf"^{TOOL_NAME_PATTERN}/{METRIC_KEY_PATTERN}/(?:{METRIC_KEY_PATTERN}|\*)/[0-9]{{1,6}}$")]


def report_window(
    since: Annotated[AwareDatetime | None, Query(alias="from")] = None,
    until: Annotated[AwareDatetime | None, Query(alias="to")] = None,
) -> tuple[datetime, datetime]:
    try:
        end = until.astimezone(UTC) if until else datetime.now(UTC)
        start = since.astimezone(UTC) if since else end - timedelta(days=7)
    except OverflowError as exc:
        raise HTTPException(422, "Time window is outside the supported UTC range") from exc
    if not timedelta() < end - start <= timedelta(days=30):
        raise HTTPException(422, "Choose a positive time window of up to 30 days")
    return start, end


Window = Annotated[tuple[datetime, datetime], Depends(report_window)]


@router.get("/admin/mcp-monitoring", response_model=MonitoringReport, summary="Read the MCP monitoring dashboard")
async def mcp_monitoring(
    window: Window,
    history_errors_only: bool = False,
    tool_sort: Literal["calls", "errors", "latency"] = "calls",
    operation_page: Annotated[int, Query(ge=0, le=1_000_000)] = 0,
    user_id: UUID | None = None,
    caller_kind: CallerKind = "human",
    auth_kind: AuthKind | None = None,
    pat_id: UUID | None = None,
    tool: Annotated[str | None, Query(min_length=1, max_length=128)] = None,
    user_page: Annotated[int, Query(ge=0, le=1_000_000)] = 0,
    credential_page: Annotated[int, Query(ge=0, le=1_000_000)] = 0,
    tool_page: Annotated[int, Query(ge=0, le=1_000_000)] = 0,
    metric_view: Annotated[list[MetricView] | None, Query(max_length=128)] = None,
    browse_group: Literal["conditions", "page_size", "depth", "page"] = "conditions",
    browse_page: Annotated[int, Query(ge=0, le=1_000_000)] = 0,
):
    if pat_id and (not user_id or auth_kind != "pat"):
        raise HTTPException(422, "A PAT filter requires its user and PAT authentication")
    views = {}
    for selection in metric_view or []:
        name, key, group, page = selection.split("/")
        views[f"{name}/{key}"] = {"group": group, "page": int(page)}
    return await read_report(
        *window, user_id, auth_kind, pat_id, tool, user_page, credential_page, tool_page, browse_group, browse_page, views, operation_page, caller_kind,
        history_errors_only, tool_sort,
    )


@router.get("/admin/mcp-monitoring/operations/{actor_id}/{operation_id}/calls", response_model=OperationDetails)
async def operation_calls(
    actor_id: UUID, operation_id: UUID, window: Window,
    caller_kind: CallerKind = "human",
    auth_kind: AuthKind | None = None,
    pat_id: UUID | None = None,
    tool: Annotated[str | None, Query(min_length=1, max_length=128)] = None,
    page: Annotated[int, Query(ge=0, le=1_000_000)] = 0,
    kind: Literal["conversation", "trace", "turn"] = "trace",
):
    if pat_id and auth_kind != "pat":
        raise HTTPException(422, "A PAT filter requires PAT authentication")
    return await read_operation_calls(*window, actor_id, auth_kind, pat_id, tool, operation_id, page, kind, caller_kind)


@router.get("/admin/mcp-monitoring/calls/{observation_id}/trace")
async def observation_trace(observation_id: UUID):
    return await load_trace(observation_id)
