import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { expect, it, vi } from "vitest";
import { ApiError, getMcpCallTrace, getMcpMonitoring, getMcpOperationCalls } from "@/lib/api";
import type { MonitoringReport, MonitoringRow } from "@/lib/mcp-monitoring";
import McpMonitoringPage from "../mcp-monitoring";

vi.mock("@/lib/api", async (original) => ({
  ...await original<typeof import("@/lib/api")>(), getMcpMonitoring: vi.fn(), getMcpOperationCalls: vi.fn(), getMcpCallTrace: vi.fn(),
}));

const summary = { calls: 3, sso_calls: 1, pat_calls: 2, service_key_calls: 0, active_users: 1, errors: 0, decided: 3, p95_ms: 2 };
const base: MonitoringRow = { ...summary, user_id: null, pat_id: null, tool: null, auth_kind: null, label: null };
const noFailure = { error_code: null, error_reason: null };
const report: MonitoringReport = {
  operations: [], has_more_operations: false,
  metrics: [
    { tool: "akb_fixture", key: "empty", label: "Fixture misses", kind: "zero_rate", calls: 4, matched: 1,
      dimensions: [], rows: [], active: false, group: null, page: 0, has_more: false },
    { tool: "akb_fixture", key: "latency", label: "Fixture latency", kind: "latency", calls: 3, matched: 0,
      dimensions: [{ key: "page", label: "Page position", labels: { true: "Subsequent" } }, { key: "depth", label: "Depth", labels: {} }],
      rows: [{ values: { page: true, depth: 1 }, calls: 3, p95_ms: 17 }], active: true, group: null, page: 0, has_more: false },
  ],
  recent_calls: [1, 2].map(i => ({
    ...noFailure, observation_id: String(i), finished_at: `2026-01-01T00:00:0${i}Z`, actor_id: "user-1", user_label: "Fixture user",
    auth_kind: "pat", pat_id: "01234567-abcd", pat_label: "Desktop", tool: "akb_help", outcome: "ok", duration_ms: i, trace_available: false,
  })), summary, window: { from: "2026-01-01T00:00:00Z", to: "2026-01-02T00:00:00Z" },
  collection: { enabled: true, retention_days: 30, started_at: null, last_flush: null, dropped: 0, stale_writers: 0, disabled_writers: 0 },
  series: [{ start: "2026-01-01T00:00:00Z", calls: 3, coverage: "complete" }],
  users: [{ ...base, user_id: "user-1", label: "Fixture user" }],
  credentials: [{ ...base, auth_kind: "pat", pat_id: "01234567-abcd", label: "Desktop" }],
  tools: [{ ...base, tool: "akb_help" }], has_more: { users: false, tools: false, credentials: false },
};

function renderPage() {
  render(<QueryClientProvider client={new QueryClient()}><MemoryRouter><McpMonitoringPage /></MemoryRouter></QueryClientProvider>);
}

it("pins the interval while drilling and clears private data when authorization expires", async () => {
  const requests: URLSearchParams[] = [];
  vi.mocked(getMcpMonitoring).mockImplementation(async (params) => {
    requests.push(params);
    if (params.has("tool")) throw new ApiError("Forbidden", 403, null);
    return report;
  });
  renderPage();
  const clickRow = async (table: string, name: RegExp) => {
    await userEvent.click(within(await screen.findByRole("table", { name: table })).getByRole("button", { name }));
  };
  await userEvent.click(await screen.findByRole("tab", { name: "Recent calls" }));
  await clickRow("Users", /Fixture user/);
  await clickRow("Credentials", /Desktop/);
  const recent = within(await screen.findByRole("table", { name: "Recent calls" }));
  expect(recent.getAllByRole("cell", { name: "akb_help" })).toHaveLength(2);
  expect(recent.getAllByRole("cell", { name: "Desktop · 01234567" })).toHaveLength(2);
  await clickRow("Tools", /akb_help/);
  await screen.findByRole("link", { name: /sign in as a product administrator/i });
  const latest = requests.at(-1)!;
  expect(Object.fromEntries(latest)).toMatchObject({ user_id: "user-1", auth_kind: "pat", pat_id: "01234567-abcd", tool: "akb_help" });
  expect(latest.get("to")).toEqual(requests[0].get("to"));
  expect(screen.queryByRole("table")).not.toBeInTheDocument();
  expect(screen.queryByText("Fixture user")).not.toBeInTheDocument();
});

it("shows current PAT state separately from the last authenticated SSO call", async () => {
  const expiry = "2026-02-01T00:00:00Z";
  vi.mocked(getMcpMonitoring).mockResolvedValue({
    ...report, credential_status_as_of: expiry,
    credentials: [
      { ...report.credentials[0], credential_status: "expired", pat_expires_at: expiry },
      { ...base, auth_kind: "oauth", credential_status: "session_unverified", last_observed_at: report.window.from },
    ],
    recent_calls: [{ ...report.recent_calls[0], pat_label: null, credential_status: "deleted" }],
  });
  renderPage();
  const creds = within(await screen.findByRole("table", { name: "Credentials" }));
  const pat = within(creds.getByRole("row", { name: /Desktop/ }));
  expect(pat.getByRole("cell", { name: /^Expired/ })).toHaveTextContent("Expired");
  expect(pat.getByText(new Date(expiry).toLocaleString())).toHaveAttribute("dateTime", expiry);
  const sso = within(creds.getByRole("row", { name: /SSO/ }));
  expect(sso.getByText("Session unverified")).toBeVisible();
  expect(sso.getByText(new Date(report.window.from).toLocaleString())).toHaveAttribute("dateTime", report.window.from);
  await userEvent.click(screen.getByRole("tab", { name: "Recent calls" }));
  const recent = within(await screen.findByRole("table", { name: "Recent calls" }));
  expect(recent.getByRole("row", { name: /PAT · 01234567/ })).toHaveTextContent("Deleted");
});

it("shows an unmeasured interval separately from a measured zero", async () => {
  vi.mocked(getMcpMonitoring).mockResolvedValue({
    ...report, users: [], tools: [], credentials: [], recent_calls: [],
    series: [
      { start: "2026-01-01T00:00:00Z", calls: null, coverage: "unmeasured" },
      { start: "2026-01-01T01:00:00Z", calls: 0, coverage: "complete" },
    ],
  });
  renderPage();
  const intervals = within(await screen.findByRole("group", { name: "Call intervals" })).getAllByRole("button");
  await userEvent.hover(intervals[0]);
  expect(screen.getByRole("tooltip")).toHaveTextContent("Unmeasured");
  await userEvent.click(intervals[0]);
  await userEvent.keyboard("{ArrowRight}");
  expect(intervals[1]).toHaveFocus();
  expect(screen.getByRole("tooltip")).toHaveTextContent("0 calls");
  await userEvent.keyboard("{Escape}");
  expect(screen.queryByRole("tooltip")).not.toBeInTheDocument();
  await userEvent.click(await screen.findByText("View interval counts and coverage"));
  expect(screen.getByText("— calls · unmeasured")).toBeVisible();
  expect(screen.getByText("0 calls · complete")).toBeVisible();
});

it("shows the measured search denominator, handles missing data, and groups browse latency", async () => {
  const requests: URLSearchParams[] = [];
  vi.mocked(getMcpMonitoring).mockImplementation(async params => {
    requests.push(params);
    return params.has("metric_view")
      ? { ...report, metrics: report.metrics.map(m => ({ ...m, calls: 0 })) } : report;
  });
  renderPage();
  expect(await screen.findByText("25.0%")).toBeVisible();
  expect(screen.getByText("1 of 4 measured successful calls returned zero.")).toBeVisible();
  const table = within(screen.getByRole("table", { name: "Fixture latency" }));
  expect(table.getByText("Subsequent")).toBeVisible();
  expect(table.getByText("17 ms")).toBeVisible();
  await userEvent.click(screen.getByLabelText("Group Fixture latency by"));
  await userEvent.click(await screen.findByRole("menuitemradio", { name: "Page position" }));
  expect(await screen.findByText("No measured successful calls in this selection.")).toBeVisible();
  expect(requests.at(-1)?.get("metric_view")).toBe("akb_fixture/latency/page/0");
});

it.each(["trace", "turn", "conversation"] as const)("expands a %s in start order with per-call traces", async kind => {
  const op = { ...noFailure, succeeded: 1, errors: 1, cancelled: 0, trace_calls: 0, trace_observation_id: null, id: "12345678-parent", kind, actor_id: "actor", user_label: "Operator", calls: 2, correlated: true,
    started_at: report.window.from, finished_at: report.window.to, elapsed_ms: 250, tool: null,
    agent: kind === "conversation" ? "Codex" : null };
  vi.mocked(getMcpMonitoring).mockResolvedValue({ ...report, operations: [
    op, ...["a", "b"].map(id => ({ ...op, id, kind: "individual" as const, errors: 0, calls: 1, correlated: false, tool: "akb_help", elapsed_ms: 1 })),
  ] });
  vi.mocked(getMcpOperationCalls).mockResolvedValue({ has_more: false, calls: ["akb_browse", "akb_help"].map((tool, i) => ({
    ...noFailure, observation_id: String(i), tool, outcome: "ok", finished_at: report.window.to, started_at: report.window.from,
    duration_ms: i ? 7 : 120, position: i + 1, trace_available: i === 1,
  })) });
  renderPage();
  const operations = within(await screen.findByRole("table", { name: "Operations" }));
  expect(operations.getAllByText("Individual call")).toHaveLength(2);
  expect(operations.getByText("Has errors")).toBeVisible();
  expect(operations.getByText("1 failed · 1 succeeded")).toBeVisible();
  expect(operations.getByText("250 ms")).toBeVisible();
  if (kind === "conversation") expect(operations.getByText("Codex")).toBeVisible();
  const label = { conversation: "Conversation", trace: "Trace", turn: "Agent turn" }[kind];
  const expand = operations.getByRole("button", { name: label + " 12345678" });
  await userEvent.click(expand);
  const rows = within(await screen.findByRole("table", { name: "Call sequence" })).getAllByRole("row");
  expect(rows[1]).toHaveTextContent("akb_browse"); expect(rows[1]).toHaveTextContent("120 ms");
  expect(rows[2]).toHaveTextContent("akb_help"); expect(rows[2]).toHaveTextContent("7 ms");
  expect(vi.mocked(getMcpOperationCalls).mock.lastCall?.[2].get("kind")).toBe(kind === "trace" ? null : kind);
  expect(within(rows[1]).queryByRole("button")).toBeNull();
  vi.mocked(getMcpCallTrace).mockResolvedValue({ trace_id: "abc123", spans: [] });
  await userEvent.click(within(rows[2]).getByRole("button", { name: "View trace for call 2, akb_help" }));
  expect(await within(await screen.findByRole("dialog")).findByText("abc123")).toBeVisible();
  expect(vi.mocked(getMcpCallTrace).mock.lastCall?.[0]).toBe("1");
  await userEvent.keyboard("{Escape}");
  expect(expand).toHaveAttribute("aria-expanded", "true");
  await userEvent.click(expand);
  expect(screen.queryByRole("table", { name: "Call sequence" })).not.toBeInTheDocument();
});

it("switches from a human PAT to service jobs and scopes their call sequence", async () => {
  const op = { ...noFailure, succeeded: 3, errors: 0, cancelled: 0, trace_calls: 0, trace_observation_id: null, id: "12345678-job", actor_id: "svc", user_label: "Importer", calls: 3, correlated: true,
    kind: "conversation" as const, agent: "import-app", tool: null, elapsed_ms: 14,
    started_at: report.window.from, finished_at: report.window.to };
  vi.mocked(getMcpMonitoring).mockImplementation(async params => params.get("caller_kind") === "service"
    ? { ...report, users: [{ ...base, user_id: "svc", label: "Importer" }], operations: [op],
        recent_calls: [{ ...report.recent_calls[0], auth_kind: "service", pat_id: null, pat_label: null }] }
    : report);
  vi.mocked(getMcpOperationCalls).mockResolvedValue({ has_more: false, calls: [] });
  renderPage();
  await userEvent.click(within(await screen.findByRole("table", { name: "Users" })).getByRole("button", { name: "Fixture user" }));
  await userEvent.click(within(await screen.findByRole("table", { name: "Credentials" })).getByRole("button", { name: /Desktop/ }));
  await userEvent.click(screen.getByLabelText("Caller type"));
  await userEvent.click(await screen.findByRole("menuitemradio", { name: "Service callers" }));
  await screen.findByRole("table", { name: "Service callers" });
  const params = vi.mocked(getMcpMonitoring).mock.lastCall![0];
  expect(params.get("caller_kind")).toBe("service");
  expect(["user_id", "auth_kind", "pat_id"].some(key => params.has(key))).toBe(false);
  await userEvent.click(await screen.findByRole("button", { name: "Job 12345678" }));
  await screen.findByRole("table", { name: "Call sequence" });
  expect(vi.mocked(getMcpOperationCalls).mock.lastCall?.[2].get("caller_kind")).toBe("service");
  await userEvent.click(screen.getByRole("tab", { name: "Recent calls" }));
  expect(within(await screen.findByRole("table", { name: "Recent calls" })).getByText("Service key")).toBeVisible();
});

it.each([
  [404, "trace_export_pending", "Awaiting trace export", true],
  [410, "trace_expired", "Trace expired", false],
  [410, "trace_outside_previous_retention", "Outside the previous Tempo retention window", false],
  [404, "trace_span_not_found", "MCP span missing from trace", true],
  [404, "trace_not_found", "Trace not found", true],
  [503, "unavailable", "The trace store could not be reached.", true],
] as const)("displays the %s %s trace state", async (status, code, title, canRetry) => {
  vi.mocked(getMcpMonitoring).mockResolvedValue({ ...report, recent_calls: [{ ...report.recent_calls[0], trace_available: true }] });
  vi.mocked(getMcpCallTrace).mockRejectedValueOnce(new ApiError("Status detail.", status, { code, trace_id: "recorded-id" }))
    .mockResolvedValueOnce({ trace_id: "abc123", spans: [] });
  renderPage();
  await userEvent.click(await screen.findByRole("tab", { name: "Recent calls" }));
  await userEvent.click(await screen.findByRole("button", { name: "View trace" }));
  const dialog = within(await screen.findByRole("dialog"));
  expect(await dialog.findByText(title)).toBeVisible();
  if (status !== 503) expect(dialog.getByText("recorded-id")).toBeVisible();
  if (canRetry) {
    await userEvent.click(dialog.getByRole("button", { name: "Retry" }));
    expect(await dialog.findByText("abc123")).toBeVisible();
  } else {
    expect(dialog.queryByRole("button", { name: "Retry" })).toBeNull();
  }
  vi.mocked(getMcpCallTrace).mockReset();
});

it("dates failures that predate error-code support and leaves newer uncoded failures unexplained", async () => {
  const since = "2026-01-02T00:00:00Z";
  vi.mocked(getMcpMonitoring).mockResolvedValue({ ...report, collection: { ...report.collection, error_codes_supported_from: since },
    recent_calls: report.recent_calls.map((call, index) => ({ ...call, outcome: "tool_error", finished_at: index ? since : report.window.from })) });
  renderPage();
  await userEvent.click(await screen.findByRole("tab", { name: "Recent calls" }));
  const recent = within(await screen.findByRole("table", { name: "Recent calls" }));
  expect(recent.getByText(/This call predates error-code collection/)).toHaveTextContent(new Date(since).toLocaleString());
  expect(recent.getByText("AKB recorded the failure without an error code.")).toBeVisible();
});

it("drills from tool errors into a failed call and opens the single operation's Tempo trace", async () => {
  const failure = { error_code: "permission_denied", error_reason: "The caller lacks permission for this resource." };
  const call = { ...report.recent_calls[0], ...failure, outcome: "tool_error" as const, trace_available: true };
  const op = { ...failure, succeeded: 0, cancelled: 0, errors: 1, calls: 1, correlated: false,
    id: "single", tool: call.tool, kind: "individual" as const, agent: null, actor_id: call.actor_id,
    started_at: report.window.from, finished_at: report.window.to, elapsed_ms: 1, user_label: call.user_label,
    trace_calls: 1, trace_observation_id: call.observation_id };
  vi.mocked(getMcpMonitoring).mockResolvedValue({ ...report, operations: [op], recent_calls: [call],
    tools: [{ ...report.tools[0], errors: 1 }] });
  vi.mocked(getMcpCallTrace).mockResolvedValue({ trace_id: "test-trace", spans: [] });
  renderPage();
  const operations = within(await screen.findByRole("table", { name: "Operations" }));
  expect(operations.getByText("Failed")).toBeVisible();
  await userEvent.click(operations.getByRole("button", { name: "View trace" }));
  expect(await within(await screen.findByRole("dialog")).findByText("test-trace")).toBeVisible();
  await userEvent.keyboard("{Escape}");
  await userEvent.click(screen.getByRole("button", { name: "Inspect 1 error for akb_help" }));
  const recent = within(await screen.findByRole("table", { name: "Recent calls" }));
  expect(recent.getByText("permission_denied")).toBeVisible();
  expect(recent.getByText(failure.error_reason)).toBeVisible();
  expect(screen.getByRole("checkbox", { name: "Errors only" })).toBeChecked();
  const params = vi.mocked(getMcpMonitoring).mock.lastCall![0];
  expect(params.get("tool")).toBe("akb_help");
  expect(params.get("history_errors_only")).toBe("true");
});
