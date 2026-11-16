export type Breakdown = "users" | "credentials" | "tools";
export type AuthKind = "oauth" | "pat" | "service";
export type CallerKind = "human" | "service";

export interface MonitoringMetrics {
  calls: number;
  active_users: number;
  sso_calls: number;
  pat_calls: number;
  service_key_calls: number;
  errors: number;
  decided: number;
  p95_ms: number | null;
}

export interface MonitoringRow extends MonitoringMetrics {
  user_id: string | null;
  pat_id: string | null;
  tool: string | null;
  label: string | null;
  auth_kind: AuthKind | null;
}

export interface RecentCall {
  observation_id: string;
  finished_at: string;
  actor_id: string;
  user_label: string | null;
  auth_kind: AuthKind;
  pat_id: string | null;
  pat_label: string | null;
  tool: string;
  outcome: "ok" | "tool_error" | "rpc_error" | "cancelled" | "unknown";
  duration_ms: number;
  trace_available: boolean;
}

export interface McpOperation {
  kind: "conversation" | "trace" | "turn" | "individual";
  agent: string | null;
  id: string;
  actor_id: string;
  user_label: string | null;
  calls: number;
  started_at: string;
  finished_at: string;
  elapsed_ms: number;
  correlated: boolean;
  tool: string | null;
}

export interface OperationDetails {
  calls: (Pick<RecentCall, "observation_id" | "tool" | "outcome" | "finished_at" | "duration_ms" | "trace_available"> & {
    position: number;
    started_at: string;
  })[];
  has_more: boolean;
}

export interface ToolMetric {
  tool: string;
  key: string;
  kind: "zero_rate" | "latency";
  label: string;
  active: boolean;
  group: string | null;
  page: number;
  has_more: boolean;
  calls: number;
  matched: number;
  dimensions: { key: string; label: string; labels: Record<string, string> }[];
  rows: { values: Record<string, number | boolean | null>; calls: number; p95_ms: number }[];
}

export interface MonitoringReport extends Record<Breakdown, MonitoringRow[]> {
  operations: McpOperation[];
  has_more_operations: boolean;
  metrics: ToolMetric[];
  recent_calls: RecentCall[];
  summary: MonitoringMetrics;
  window: { from: string; to: string };
  collection: { enabled: boolean; retention_days: number; started_at: string | null; last_flush: string | null; dropped: number; stale_writers: number; disabled_writers: number };
  series: { start: string; calls: number | null; coverage: "complete" | "partial" | "unmeasured" }[];
  has_more: Record<Breakdown, boolean>;
}

export interface McpCallTrace {
  trace_id: string;
  spans: { name: string; span_id: string; start: string; duration_ms: number }[];
}

export const outcomes = { ok: "Success", tool_error: "Tool error", rpc_error: "RPC error", cancelled: "Cancelled", unknown: "Unknown" };
export const callTime = new Intl.DateTimeFormat(undefined, {
  year: "numeric", month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit",
  second: "2-digit", fractionalSecondDigits: 3,
});
