"""Product-admin monitoring response contract."""

from datetime import datetime
from typing import Literal, TypedDict
from uuid import UUID


CallerKind = Literal["human", "service"]
AuthKind = Literal["oauth", "pat", "service"]


class Metrics(TypedDict):
    calls: int
    active_users: int
    sso_calls: int
    pat_calls: int
    service_key_calls: int
    errors: int
    decided: int
    p95_ms: int | None


class CredentialInfo(TypedDict):
    credential_status: Literal["active", "expired", "deleted", "account_suspended", "session_unverified"] | None
    pat_expires_at: datetime | None


class BreakdownRow(Metrics, CredentialInfo):
    last_observed_at: datetime | None
    user_id: UUID | None
    pat_id: UUID | None
    auth_kind: AuthKind | None
    tool: str | None
    label: str | None


class FailureDetail(TypedDict):
    error_code: str | None
    error_reason: str | None
    error_diagnostic: str | None


class RecentCall(CredentialInfo, FailureDetail):
    observation_id: UUID
    finished_at: datetime
    actor_id: UUID
    user_label: str | None
    auth_kind: AuthKind
    pat_id: UUID | None
    pat_label: str | None
    tool: str
    outcome: Literal["ok", "tool_error", "rpc_error", "cancelled", "unknown"]
    duration_ms: int
    trace_available: bool


class Interval(TypedDict):
    succeeded: int | None
    failed: int | None
    start: datetime
    calls: int | None
    coverage: Literal["complete", "partial", "unmeasured"]


class Collection(TypedDict):
    error_codes_supported_from: datetime | None
    enabled: bool
    started_at: datetime | None
    last_flush: datetime | None
    dropped: int
    disabled_writers: int
    stale_writers: int
    retention_days: int


class MorePages(TypedDict):
    users: bool
    credentials: bool
    tools: bool


Window = TypedDict("Window", {"from": datetime, "to": datetime})


class SearchResults(TypedDict):
    measured: int
    empty: int


class BrowseLatency(TypedDict):
    page_size: int | None
    depth: int | None
    followup: bool | None
    calls: int
    p95_ms: int


class MetricDimension(TypedDict):
    key: str
    label: str
    labels: dict[str, str]


class MetricRow(TypedDict):
    values: dict[str, bool | int | None]
    calls: int
    p95_ms: int


class MetricPanel(TypedDict):
    tool: str
    key: str
    kind: Literal["latency", "zero_rate"]
    label: str
    dimensions: list[MetricDimension]
    active: bool
    group: str | None
    page: int
    calls: int
    matched: int
    rows: list[MetricRow]
    has_more: bool


class Operation(FailureDetail):
    errors: int
    succeeded: int
    cancelled: int
    trace_calls: int
    trace_observation_id: UUID | None
    kind: Literal["conversation", "trace", "turn", "individual"]
    agent: str | None
    id: UUID
    actor_id: UUID
    user_label: str | None
    calls: int
    started_at: datetime
    finished_at: datetime
    elapsed_ms: int
    correlated: bool
    tool: str | None


class OperationCall(FailureDetail):
    trace_available: bool
    position: int
    observation_id: UUID
    tool: str
    outcome: Literal["ok", "tool_error", "rpc_error", "cancelled", "unknown"]
    started_at: datetime
    finished_at: datetime
    duration_ms: int


class OperationDetails(TypedDict):
    calls: list[OperationCall]
    has_more: bool


class MonitoringReport(TypedDict):
    credential_status_as_of: datetime
    operations: list[Operation]
    has_more_operations: bool
    metrics: list[MetricPanel]
    search_results: SearchResults
    browse_latency: list[BrowseLatency]
    has_more_browse: bool
    window: Window
    collection: Collection
    summary: Metrics
    series: list[Interval]
    recent_calls: list[RecentCall]
    users: list[BreakdownRow]
    credentials: list[BreakdownRow]
    tools: list[BreakdownRow]
    has_more: MorePages
