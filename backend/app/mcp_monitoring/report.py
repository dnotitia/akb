"""Read one installation's operator report from a single PostgreSQL snapshot."""

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID

from app.config import settings
from app.db.postgres import get_pool
from app.exceptions import NotFoundError
from app.mcp_monitoring.collector import STALE_SECONDS
from app.mcp_monitoring.errors import REASONS
from app.mcp_monitoring.schema import CallerKind

PAGE_SIZE = 50
_WHERE = """
    WHERE finished_at >= greatest($1,$7::timestamptz) AND finished_at < $2
      AND (account_kind = 'service' OR auth_kind = 'service') = ($8::text = 'service')
      AND ($3::uuid IS NULL OR actor_id=$3) AND ($4::text IS NULL OR auth_kind=$4)
      AND ($5::uuid IS NULL OR pat_id=$5) AND ($6::text IS NULL OR tool=$6)
"""
_FILTERED = """
WITH filtered AS (
    SELECT observation_id, finished_at, actor_id, auth_kind, pat_id, tool, outcome, duration_ms,
           samples, trace_id IS NOT NULL AS trace_available
    FROM mcp_monitoring.measured_calls
""" + _WHERE + ")"
# Use the same snapshot and validity rules for grouped credentials and individual calls.
_PAT_STATUS = """
, pat_status AS NOT MATERIALIZED (
    SELECT t.id, t.user_id, t.name, t.expires_at,
           CASE WHEN t.expires_at <= CURRENT_TIMESTAMP THEN 'expired'
                WHEN u.account_status = 'suspended' THEN 'account_suspended'
                ELSE 'active' END AS status
    FROM tokens t JOIN users u ON u.id = t.user_id WHERE t.key_class = 'pat'
)
"""
_REPORT = _FILTERED + _PAT_STATUS + Path(__file__).with_name("report.sql").read_text()
_RECENT = """
WITH recent AS (
    SELECT observation_id, finished_at, actor_id, auth_kind, pat_id, tool, outcome, duration_ms,
           error_code, error_diagnostic, trace_id IS NOT NULL AS trace_available
    FROM mcp_monitoring.tool_calls
""" + _WHERE + """
      AND (NOT $10::boolean OR outcome IN ('tool_error','rpc_error'))
    ORDER BY finished_at DESC, observation_id DESC LIMIT $9
)
""" + _PAT_STATUS + """
SELECT r.observation_id, r.finished_at, r.actor_id, r.auth_kind, r.pat_id, r.tool, r.outcome, r.duration_ms,
       r.error_code, r.error_diagnostic, r.trace_available, coalesce(nullif(u.display_name,''),u.username) AS user_label, t.name AS pat_label,
       t.status AS credential_status, t.expires_at AS pat_expires_at
FROM recent r
LEFT JOIN users u ON u.id=r.actor_id
LEFT JOIN pat_status t ON t.id=r.pat_id AND t.user_id=r.actor_id
ORDER BY r.finished_at DESC, r.observation_id DESC
"""

# Older rows retain sampled trace IDs and millisecond durations. A historical
# singleton has no evidence of a caller-supplied context and stays individual.
_OPERATION_CALLS = """
WITH operation_calls AS (
    SELECT observation_id, actor_id, tool, outcome, error_code, error_diagnostic, duration_ms, finished_at,
           coalesce(started_at, finished_at - duration_ms * interval '1 millisecond') AS call_start,
           coalesce(conversation_key, CASE WHEN started_at IS NULL THEN trace_id
                                          ELSE coalesce(request_trace_id, agent_turn_id) END) AS correlation_id,
           CASE WHEN conversation_key IS NOT NULL THEN 'conversation'
                WHEN started_at IS NOT NULL AND request_trace_id IS NULL AND agent_turn_id IS NOT NULL THEN 'turn'
                ELSE 'trace' END AS group_kind,
           conversation_agent,
           conversation_key IS NOT NULL OR request_trace_id IS NOT NULL OR agent_turn_id IS NOT NULL AS propagated,
           trace_id IS NOT NULL AS trace_available
    FROM mcp_monitoring.tool_calls
""" + _WHERE + ")"
_OPERATIONS = _OPERATION_CALLS + """
, grouped AS (
    SELECT actor_id, correlation_id, group_kind, CASE WHEN correlation_id IS NULL THEN observation_id END AS individual_id,
           min(conversation_agent) AS agent,
           count(*) FILTER (WHERE outcome IN ('tool_error','rpc_error')) AS errors,
           count(*) FILTER (WHERE outcome='ok') AS succeeded,
           count(*) FILTER (WHERE trace_available) AS trace_calls,
           count(*) FILTER (WHERE outcome='cancelled') AS cancelled,
           CASE WHEN count(*)=1 THEN min(error_code) END AS error_code,
           CASE WHEN count(*)=1 THEN min(error_diagnostic) END AS error_diagnostic,
           CASE WHEN count(*)=1 AND bool_or(trace_available) THEN min(observation_id::text)::uuid END AS trace_observation_id,
           count(*) AS calls, min(call_start) AS started_at, max(finished_at) AS finished_at,
           bool_or(propagated) OR count(*) > 1 AS correlated, CASE WHEN count(*)=1 THEN min(tool) END AS tool
    FROM operation_calls GROUP BY 1,2,3,4
), page AS (
    SELECT *, coalesce(correlation_id, individual_id) AS id FROM grouped WHERE NOT $11::boolean OR errors>0
    ORDER BY finished_at DESC, actor_id, correlation_id NULLS LAST, group_kind, individual_id LIMIT $9 OFFSET $10
)
SELECT p.id, p.actor_id, coalesce(nullif(u.display_name,''),u.username) AS user_label,
       p.calls, p.errors, p.succeeded, p.cancelled, p.trace_calls, p.error_code, p.error_diagnostic, p.trace_observation_id, p.started_at, p.finished_at, p.correlated, p.tool,
       CASE WHEN p.correlated THEN p.group_kind ELSE 'individual' END AS kind, p.agent,
       floor(extract(epoch FROM (p.finished_at - p.started_at))*1000)::bigint AS elapsed_ms
FROM page p LEFT JOIN users u ON u.id=p.actor_id
ORDER BY p.finished_at DESC, p.actor_id, p.correlation_id NULLS LAST, p.group_kind, p.individual_id
"""
_OPERATION_DETAIL = _OPERATION_CALLS + """
SELECT observation_id, tool, outcome, error_code, error_diagnostic, call_start AS started_at, finished_at, duration_ms, trace_available AND $13 AS trace_available,
       row_number() OVER (ORDER BY call_start, finished_at, observation_id) AS position
FROM operation_calls WHERE correlation_id=$9 AND group_kind=$12
ORDER BY call_start, finished_at, observation_id LIMIT $10 OFFSET $11
"""


async def read_operation_calls(start, end, actor_id, auth_kind, pat_id, tool, operation_id, page, kind="trace", caller_kind: CallerKind = "human"):
    retained = datetime.now(UTC) - timedelta(days=settings.mcp_monitoring.retention_days)
    pool = await get_pool()
    rows = await pool.fetch(_OPERATION_DETAIL, start, end, actor_id, auth_kind, pat_id, tool, retained, caller_kind,
                            operation_id, PAGE_SIZE + 1, page * PAGE_SIZE, kind,
                            bool(settings.mcp_monitoring.tempo_query_url))
    return {"calls": [_with_error_reason(row) for row in rows[:PAGE_SIZE]], "has_more": len(rows) > PAGE_SIZE}


_METRICS = _FILTERED + Path(__file__).with_name("metric_report.sql").read_text()


def _metric_panels(records, tool_filter, views):
    from mcp_server.tools import TOOL_METRICS

    panels = {}

    def panel(tool, key, descriptor, active):
        view = views.get(f"{tool}/{key}", {})
        group = view.get("group")
        if group not in {d["key"] for d in descriptor["dimensions"]}:
            group = None
        return dict(descriptor, tool=tool, key=key, active=active, group=group, page=view.get("page", 0),
                    calls=0, matched=0, rows=[], has_more=False)

    for tool, metrics in TOOL_METRICS.items():
        if tool_filter and tool != tool_filter:
            continue
        for metric in metrics:
            panels[tool, metric.key] = panel(tool, metric.key, metric.descriptor(), True)
    for record in records:
        identity = record["tool"], record["key"]
        if identity not in panels:
            panels[identity] = panel(*identity, json.loads(record["descriptor"]), False)
        current = panels[identity]
        current["calls"] = record["measured"]
        if current["kind"] == "zero_rate":
            current["matched"] = record["matched"]
        elif record["visible"]:
            current["rows"].append({"values": json.loads(record["dimensions"]), "calls": record["calls"],
                                    "p95_ms": record["p95_ms"]})
        current["has_more"] = record["total"] > record["skip"] + PAGE_SIZE
    return panels


# A healthy replica cannot fill a known gap on a different replica.
_COVERAGE = """
WITH recorded AS (
    SELECT range_agg(tstzrange(started_at, flushed_through, '[)')) AS periods
    FROM mcp_monitoring.writers WHERE collecting AND started_at < $2 AND flushed_through >= $1
), gaps AS (
    SELECT tstzrange(since,until_at,'[)') AS r FROM mcp_monitoring.gaps
    WHERE since < $2 AND until_at >= $1
    UNION ALL
    SELECT tstzrange(started_at, flushed_through,'[)') FROM mcp_monitoring.writers
    WHERE NOT collecting AND started_at < $2 AND flushed_through >= $1
    UNION ALL
    SELECT tstzrange(flushed_through, $2,'[)') FROM mcp_monitoring.writers
    WHERE NOT stopped AND flushed_through < $2
), verified AS (
    SELECT coalesce(periods, '{}'::tstzmultirange)
        - coalesce((SELECT range_agg(r) FROM gaps), '{}'::tstzmultirange) AS ranges FROM recorded
)
SELECT lower(r) AS lo, upper(r) AS hi FROM verified,
     unnest(ranges * tstzmultirange(tstzrange($1,$2,'[)'))) AS r ORDER BY lo
"""


def _with_error_reason(row):
    return dict(row, error_reason=REASONS.get(row["error_code"]))


def _credential_status(row):
    if row["auth_kind"] == "oauth":
        return "session_unverified"
    if row["pat_id"] is not None:
        return row["credential_status"] or "deleted"
    return None


async def read_report(
    start: datetime,
    end: datetime,
    user_id: UUID | None,
    auth_kind: str | None,
    pat_id: UUID | None,
    tool: str | None,
    user_page: int,
    credential_page: int,
    tool_page: int,
    browse_group: str = "conditions",
    browse_page: int = 0,
    metric_views: dict | None = None,
    operation_page: int = 0,
    caller_kind: CallerKind = "human",
    history_errors_only: bool = False,
    tool_sort: str = "calls",
):
    pool = await get_pool()
    step = timedelta(hours=1) if end - start <= timedelta(days=1) else timedelta(days=1)
    retained = datetime.now(UTC) - timedelta(days=settings.mcp_monitoring.retention_days)
    async with pool.acquire() as conn, conn.transaction(isolation="repeatable_read", readonly=True):
        if pat_id and not await conn.fetchval(
            """
            SELECT EXISTS(SELECT 1 FROM tokens WHERE id=$1 AND user_id=$2 AND key_class='pat')
                OR EXISTS(SELECT 1 FROM mcp_monitoring.tool_calls WHERE pat_id=$1 AND actor_id=$2
                          AND finished_at >= $3 AND auth_kind='pat' AND account_kind=$4)
        """,
            pat_id,
            user_id,
            retained,
            caller_kind,
        ):
            raise NotFoundError("PAT", str(pat_id))
        filters = (start, end, user_id, auth_kind, pat_id, tool, retained, caller_kind)
        views = {"akb_browse/latency": {"group": browse_group, "page": browse_page}, **(metric_views or {})}
        records = await conn.fetch(_METRICS, *filters, json.dumps(views), PAGE_SIZE)
        panels = _metric_panels(records, tool, views)
        recent = await conn.fetch(_RECENT, *filters, PAGE_SIZE, history_errors_only)
        operations = await conn.fetch(_OPERATIONS, *filters, PAGE_SIZE + 1, operation_page * PAGE_SIZE, history_errors_only)
        rows = await conn.fetch(
            _REPORT,
            *filters,
            step,
            PAGE_SIZE,
            user_page * PAGE_SIZE,
            credential_page * PAGE_SIZE,
            tool_page * PAGE_SIZE,
            tool_sort,
        )
        coverage = await conn.fetch(_COVERAGE, start, end)
        state = await conn.fetchrow(
            """
            SELECT CURRENT_TIMESTAMP AS status_as_of, min(started_at) FILTER (WHERE collecting) AS started_at,
                   (SELECT applied_at FROM schema_migrations WHERE filename = '138_mcp_call_error_code.py') AS error_codes_supported_from,
                   max(heartbeat_at) FILTER (WHERE collecting) AS last_flush,
                   count(*) FILTER (WHERE NOT stopped AND NOT collecting AND heartbeat_at >= now() - $1::interval) AS disabled_writers,
                   count(*) FILTER (WHERE NOT stopped AND heartbeat_at < now() - $1::interval) AS stale_writers,
                   coalesce(sum(dropped),0)::bigint AS dropped
            FROM mcp_monitoring.writers
        """,
            timedelta(seconds=STALE_SECONDS),
        )

    summary = {}
    series_by_time = {}
    groups: dict[str, list[dict]] = {"users": [], "credentials": [], "tools": []}
    more = dict.fromkeys(groups, False)
    for row in rows:
        kind = row["section"]
        metrics = {
            k: row[k] for k in ("calls", "active_users", "sso_calls", "pat_calls", "service_key_calls", "errors", "decided", "p95_ms")
        }
        if kind == "summary":
            summary = metrics
        elif kind == "series":
            series_by_time[row["bucket"]] = (row["calls"], row["decided"] - row["errors"], row["errors"])
        else:
            more[kind] = row["has_more"]
            groups[kind].append(
                dict(
                    metrics,
                    user_id=row["actor_id"],
                    pat_id=row["pat_id"],
                    auth_kind=row["auth_kind"],
                    tool=row["tool"],
                    label=row["label"],
                    credential_status=_credential_status(row),
                    pat_expires_at=row["pat_expires_at"],
                    last_observed_at=row["last_observed_at"] if kind == "credentials" else None,
                )
            )

    series = []
    bucket = start
    while bucket < end:
        upper = bucket + min(step, end - bucket)
        covered = sum(
            (max(timedelta(), min(r["hi"], upper) - max(r["lo"], bucket, retained)) for r in coverage), timedelta()
        )
        count, succeeded, failed = series_by_time.get(bucket, (0, 0, 0))
        status = "complete" if covered == upper - bucket else "partial" if covered or count else "unmeasured"
        measured = status != "unmeasured"
        series.append({"start": bucket, "coverage": status, "calls": count if measured else None,
                       "succeeded": succeeded if measured else None, "failed": failed if measured else None})
        bucket = upper
    # Compatibility projections for clients using the original report response.
    search = panels.get(("akb_search", "empty_results"), {})
    browse = panels.get(("akb_browse", "latency"), {})
    collection = dict(state)
    status_as_of = collection.pop("status_as_of")
    return {
        "credential_status_as_of": status_as_of,
        "window": {"from": start, "to": end},
        "collection": {
            **collection,
            "enabled": settings.mcp_monitoring.enabled,
            "retention_days": settings.mcp_monitoring.retention_days,
        },
        "summary": summary,
        "series": series,
        "metrics": sorted(panels.values(), key=lambda p: (p["tool"], p["key"])),
        "search_results": {"measured": search.get("calls", 0), "empty": search.get("matched", 0)},
        "browse_latency": [
            dict(page_size=r["values"].get("page_size"), depth=r["values"].get("depth"),
                 followup=r["values"].get("page"), calls=r["calls"], p95_ms=r["p95_ms"]) for r in browse.get("rows", [])
        ],
        "has_more_browse": browse.get("has_more", False),
        "operations": [
            dict(_with_error_reason(row),
                 trace_calls=row["trace_calls"] if settings.mcp_monitoring.tempo_query_url else 0,
                 trace_observation_id=row["trace_observation_id"] if settings.mcp_monitoring.tempo_query_url else None)
            for row in operations[:PAGE_SIZE]
        ],
        "has_more_operations": len(operations) > PAGE_SIZE,
        "recent_calls": [
            dict(_with_error_reason(call), credential_status=_credential_status(call), trace_available=call["trace_available"] and bool(settings.mcp_monitoring.tempo_query_url))
            for call in recent
        ],
        **groups,
        "has_more": more,
    }
