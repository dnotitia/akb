import { McpOperations } from "./mcp-operations";
import { McpTools } from "./mcp-tools";
import { CallOutcome } from "./mcp-outcome";
import { callTime } from "@/lib/mcp-monitoring";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { McpToolUsage } from "./mcp-tool-usage";
import { CallTrace } from "./mcp-call-trace";
import { CallTrend } from "./mcp-call-trend";
import { useEffect, useRef, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { Link } from "react-router-dom";
import { ChevronLeft, RefreshCw, Workflow, X } from "lucide-react";
import { PageHeader } from "@/components/ui/page-header";
import { Panel, PanelHeader } from "@/components/ui/panel";
import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";
import { Alert } from "@/components/ui/alert";
import { StatTile } from "@/components/ui/stat-tile";
import { SelectMenu } from "@/components/ui/select-menu";
import { LoadingState } from "@/components/ui/loading-state";
import { Skeleton } from "@/components/ui/skeleton";
import { ApiError, getMcpMonitoring } from "@/lib/api";
import type { AuthKind, CallerKind, CredentialInfo, MonitoringReport, MonitoringRow } from "@/lib/mcp-monitoring";

const groups = ["users", "credentials"] as const;
const firstPages = { users: 0, credentials: 0, tools: 0, operations: 0 };
const callerOptions = [{ value: "human", label: "Users" }, { value: "service", label: "Service callers" }];
const periods = [{ value: "1", label: "Last 24 hours" }, { value: "7", label: "Last 7 days" }, { value: "30", label: "Last 30 days" }];
const authOptions = [{ value: "", label: "All authentication" }, { value: "oauth", label: "SSO" }, { value: "pat", label: "PAT" }];
const serviceAuthOptions = [authOptions[0], { value: "service", label: "Service key" }, { value: "oauth", label: "OAuth" }, authOptions[2]];
type Selection = { id: string; label: string };
type Filters = { user?: Selection; pat?: Selection; tool?: string; auth?: AuthKind };

function rowLabel(row: MonitoringRow, service: boolean): string {
  if (row.user_id) return row.label || `Former account · ${row.user_id.slice(0, 8)}`;
  if (row.tool) return row.tool;
  if (row.pat_id) return `${row.label || "PAT"} · ${row.pat_id.slice(0, 8)}`;
  if (row.auth_kind === "service") return "Service key";
  return row.auth_kind === "oauth" ? (service ? "OAuth" : "SSO") : "PAT";
}

const statusLabels = {
  active: "Active", expired: "Expired", deleted: "Deleted", account_suspended: "Account suspended",
  session_unverified: "Session unverified",
} as const;

function CredentialState({ info }: { info: CredentialInfo }) {
  const state = info.credential_status;
  if (!state) return <span className="text-foreground-muted" aria-label="No individual credential status">—</span>;
  const variant = state === "active" ? "active" : state === "session_unverified" ? "default" : "archived";
  return <div className="space-y-1 py-1">
    <Badge variant={variant}>{statusLabels[state]}</Badge>
    {info.pat_expires_at && <div className="whitespace-nowrap text-xs text-foreground-muted">
      {state === "expired" ? "Expired" : "Expires"} <time dateTime={info.pat_expires_at}>{new Date(info.pat_expires_at).toLocaleString()}</time>
    </div>}
  </div>;
}

function RecentCalls({ report, service }: { report: MonitoringReport; service: boolean }) {
  const calls = report.recent_calls;
  const [selected, setSelected] = useState<MonitoringReport["recent_calls"][number] | null>(null);
  return (
    <Panel>
      <PanelHeader label="Recent calls" />
      {!calls.length ? <p className="p-4 text-sm text-foreground-muted">No observed calls in this selection.</p> : <>
        {calls.length < report.summary.calls && <p className="px-4 pt-3 text-sm text-foreground-muted">
          Showing the latest {calls.length} of {report.summary.calls} calls.
        </p>}
        <div className="overflow-x-auto"><table aria-label="Recent calls" className="w-full text-left text-sm tabular-nums">
          <thead className="text-xs text-foreground-muted"><tr>
            {["Completed", service ? "Service caller" : "User", "Credential", "Current status", "Tool", "Outcome", "Duration", "Tempo trace"].map(label =>
              <th key={label} scope="col" className="px-4 py-2">{label}</th>)}
          </tr></thead>
          <tbody className="divide-y divide-border">{calls.map(call =>
            <tr key={call.observation_id}>
              <td className="whitespace-nowrap px-4 py-2"><time dateTime={call.finished_at}>{callTime.format(new Date(call.finished_at))}</time></td>
              <td className="px-4">{call.user_label || `Former account · ${call.actor_id.slice(0, 8)}`}</td>
              <td className="px-4">{call.auth_kind === "service" ? "Service key" : call.auth_kind === "oauth" ? (service ? "OAuth" : "SSO") : `${call.pat_label || "PAT"} · ${call.pat_id?.slice(0, 8)}`}</td>
              <td className="px-4"><CredentialState info={call} /></td>
              <td className="px-4">{call.tool}</td><td className="px-4"><CallOutcome call={call} codesSince={report.collection.error_codes_supported_from} /></td>
              <td className="whitespace-nowrap px-4">{call.duration_ms} ms</td>
              <td className="whitespace-nowrap px-4">{call.trace_available
                ? <Button variant="link" size="sm" onClick={() => setSelected(call)}><Workflow aria-hidden size={15} />View trace</Button>
                : <span aria-label="No trace recorded" className="text-foreground-muted">—</span>}</td>
            </tr>)}</tbody>
        </table></div>
      </>}
      {selected && <CallTrace call={selected} close={() => setSelected(null)} />}
    </Panel>
  );
}

export default function McpMonitoringPage() {
  const [caller, setCaller] = useState<CallerKind>("human");
  const service = caller === "service";
  const titles = { users: service ? "Service callers" : "Users", credentials: "Credentials" };
  const [days, setDays] = useState("7");
  const jumpToHistory = useRef(false);
  const historyRef = useRef<HTMLDivElement>(null);
  const [errorsOnly, setErrorsOnly] = useState(false);
  const [toolSort, setToolSort] = useState("calls");
  const [historyView, setHistoryView] = useState("operations");
  const [until, setUntil] = useState(Date.now);
  const [filters, setFilters] = useState<Filters>({});
  const [metricViews, setMetricViews] = useState<Record<string, { group: string; page: number }>>({});
  const [pages, setPages] = useState(firstPages);
  const params = new URLSearchParams({
    from: new Date(until - Number(days) * 86_400_000).toISOString(), to: new Date(until).toISOString(),
    caller_kind: caller, operation_page: String(pages.operations),
    user_page: String(pages.users), credential_page: String(pages.credentials), tool_page: String(pages.tools),
  });
  for (const [metric, view] of Object.entries(metricViews)) {
    params.append("metric_view", `${metric}/${view.group}/${view.page}`);
  }
  if (errorsOnly) params.set("history_errors_only", "true");
  if (toolSort !== "calls") params.set("tool_sort", toolSort);
  if (filters.user) params.set("user_id", filters.user.id);
  if (filters.auth) params.set("auth_kind", filters.auth);
  if (filters.pat) params.set("pat_id", filters.pat.id);
  if (filters.tool) params.set("tool", filters.tool);
  const query = useQuery({
    queryKey: ["admin-mcp-monitoring", params.toString()],
    queryFn: ({ signal }) => getMcpMonitoring(params, signal),
    retry: false, gcTime: 0, staleTime: 0, refetchOnWindowFocus: true,
  });
  const update = (next: Filters) => { setFilters(next); setPages(firstPages); setMetricViews({}); };
  const select = (row: MonitoringRow) => {
    const label = rowLabel(row, service);
    if (row.user_id) update({ ...filters, pat: filters.user?.id === row.user_id ? filters.pat : undefined, user: { id: row.user_id, label } });
    else if (row.tool) update({ ...filters, tool: row.tool });
    else if (row.auth_kind) update({ ...filters, auth: row.auth_kind, pat: row.pat_id ? { id: row.pat_id, label } : undefined });
  };
  function inspectErrors(tool = filters.tool) {
    update({ ...filters, tool });
    setHistoryView("recent");
    setErrorsOnly(true);
    jumpToHistory.current = true;
  }
  const report = query.data;
  useEffect(() => {
    if (report && jumpToHistory.current) {
      jumpToHistory.current = false;
      historyRef.current?.querySelector<HTMLElement>('[role="tab"][aria-selected="true"]')?.focus({ preventScroll: true });
      historyRef.current?.scrollIntoView?.({ block: "start" });
    }
  }, [report, historyView, errorsOnly]);
  const measured = report?.series.some((point) => point.coverage !== "unmeasured");
  const denied = query.error instanceof ApiError && [401, 403].includes(query.error.status);
  return (
    <main className="min-h-screen bg-background px-4 py-6 sm:px-8">
      <div className="mx-auto max-w-6xl space-y-4">
        <Link to="/admin" className="inline-flex items-center gap-1 text-sm text-link"><ChevronLeft size={16} /> Product administration</Link>
        <PageHeader compact title="MCP monitoring" subtitle="Tool calls from users and services observed by the AKB server."
          actions={<Button variant="outline" onClick={() => setUntil(Date.now())}><RefreshCw size={16} /> Refresh</Button>} />
        {!denied && <div className="flex flex-wrap items-center gap-2" aria-label="Report filters">
          <SelectMenu className="w-full sm:w-44" aria-label="Caller type" value={caller} options={callerOptions}
            onValueChange={(value) => { setCaller(value === "service" ? "service" : "human"); update({ tool: filters.tool }); }} />
          <SelectMenu className="w-full sm:w-40" aria-label="Time window" value={days} options={periods}
            onValueChange={(value) => { setDays(value); setPages(firstPages); setMetricViews({}); }} />
          <SelectMenu className="w-full sm:w-48" aria-label="Authentication" value={filters.auth ?? ""} options={service ? serviceAuthOptions : authOptions}
            onValueChange={(value) => update({ ...filters, auth: value === "pat" || value === "oauth" || value === "service" ? value : undefined, pat: undefined })} />
          {filters.user && <Button variant="secondary" onClick={() => update({ ...filters, user: undefined, pat: undefined, auth: filters.pat ? undefined : filters.auth })}>
            {filters.user.label}<X size={14} aria-label="Clear user" /></Button>}
          {filters.pat && <Button variant="secondary" onClick={() => update({ ...filters, pat: undefined })}>
            {filters.pat.label}<X size={14} aria-label="Clear PAT" /></Button>}
          {filters.tool && <Button variant="secondary" onClick={() => update({ ...filters, tool: undefined })}>
            {filters.tool}<X size={14} aria-label="Clear tool" /></Button>}
          {Object.values(filters).some(Boolean) && <Button variant="ghost" onClick={() => update({})}>Clear filters</Button>}
        </div>}
        {query.isPending && <LoadingState label="Loading MCP report"><Skeleton className="h-24 w-full" /></LoadingState>}
        {query.isError && <Alert variant="destructive">{denied
          ? <Link className="underline" to="/admin">Sign in as a product administrator to view this report.</Link>
          : "The MCP report could not be loaded. Refresh to retry."}</Alert>}
        {report && !query.isError && <>
          <div className="grid grid-cols-2 gap-3 lg:grid-cols-4">
            <StatTile label="Observed calls" value={measured ? report.summary.calls : "—"} kind={measured && !service ? `SSO ${report.summary.sso_calls} · PAT ${report.summary.pat_calls}` : undefined} />
            <StatTile label={service ? "Active service callers" : "Active users"} value={measured ? report.summary.active_users : "—"} />
            <button type="button" className="rounded-[var(--radius-lg)] text-left focus-visible:outline-2 focus-visible:outline-ring disabled:cursor-default"
              disabled={!report.summary.errors} onClick={() => inspectErrors()}>
              <StatTile className={report.summary.errors ? "h-full border-destructive/50 text-destructive" : "h-full"} label="Error rate" value={report.summary.decided ? `${(100 * report.summary.errors / report.summary.decided).toFixed(1)}%` : "—"} kind={measured ? `${report.summary.errors} ${report.summary.errors === 1 ? "error" : "errors"}` : undefined} />
            </button>
            <StatTile label="p95 duration" value={report.summary.p95_ms === null ? "—" : `${report.summary.p95_ms} ms`} />
          </div>
          <p className="text-sm text-foreground-muted" role="status">
            {!report.collection.enabled && "Collection disabled. "}
            {measured ? (report.series.every((p) => p.coverage === "complete") ? "Complete coverage." : "Partial coverage. Counts include observed calls.") : "No collection coverage for this period."}
            {" "}Last flush {report.collection.last_flush ? new Date(report.collection.last_flush).toLocaleString() : "unavailable"}.
            {" "}Retention {report.collection.retention_days} days.
            {report.collection.enabled && report.collection.disabled_writers > 0 && ` Replicas with collection disabled: ${report.collection.disabled_writers}.`}
            {report.collection.stale_writers > 0 && ` Collectors with overdue heartbeats: ${report.collection.stale_writers}.`}
            {report.collection.dropped > 0 && ` Dropped by retained collectors: ${report.collection.dropped}.`}
          </p>
          <CallTrend series={report.series} end={report.window.to} />
          <McpTools report={report} sort={toolSort} setSort={value => { setToolSort(value); setPages({ ...pages, tools: 0 }); }}
            select={select} inspectErrors={inspectErrors} page={pages.tools} setPage={tools => setPages({ ...pages, tools })} />
          <Tabs ref={historyRef} value={historyView} onValueChange={setHistoryView}>
            <div className="flex flex-wrap items-center justify-between gap-3">
              <TabsList aria-label="Call history views">
                <TabsTrigger value="operations">Operations</TabsTrigger>
                <TabsTrigger value="recent">Recent calls</TabsTrigger>
              </TabsList>
              <label className="flex cursor-pointer items-center gap-2 rounded-md px-2 py-2 text-sm font-medium">
                <input type="checkbox" checked={errorsOnly} className="size-4 accent-primary"
                  onChange={event => { setErrorsOnly(event.target.checked); setPages({ ...pages, operations: 0 }); }} />Errors only
              </label>
            </div>
            <TabsContent value="operations">
              <McpOperations key={params.toString()} report={report} filters={params} page={pages.operations}
                setPage={operations => setPages({ ...pages, operations })} />
            </TabsContent>
            <TabsContent value="recent"><RecentCalls report={report} service={service} /></TabsContent>
          </Tabs>
          <McpToolUsage metrics={report.metrics} onView={(key, group, page) =>
            setMetricViews(previous => ({ ...previous, [key]: { group, page } }))} />
          {groups.map((group) => (
            <Panel key={group}>
              <PanelHeader label={titles[group]} />
              {group === "credentials" && report.credential_status_as_of &&
                <p className="px-4 pt-3 text-sm text-foreground-muted">Status as of <time dateTime={report.credential_status_as_of}>{new Date(report.credential_status_as_of).toLocaleString()}</time>. Last observed is the latest authenticated call in this selection. Current SSO session state is unknown.</p>}
              {group === "credentials" && !filters.user && report.summary.pat_calls > 0 &&
                <p className="px-4 pt-3 text-sm text-foreground-muted">Select {service ? "a service caller" : "a user"} to see their individual PATs.</p>}
              {!report[group].length ? <p className="p-4 text-sm text-foreground-muted">No observed calls in this selection.</p> :
                <div className="overflow-x-auto"><table aria-label={titles[group]} className="w-full text-left text-sm tabular-nums">
                  <thead className="text-xs text-foreground-muted"><tr>
                    {(group === "users" ? [service ? "Service caller" : "User", "Calls", ...(service ? ["Service keys", "OAuth"] : ["SSO"]), "PAT", "Errors"] :
                      ["Credential", "Current status", "Last observed", "Calls", "Errors", "p95"]).map((label) =>
                      <th key={label} scope="col" className="px-4 py-2">{label}</th>)}
                  </tr></thead>
                  <tbody className="divide-y divide-border">{report[group].map((row) => (
                    <tr key={row.user_id || row.pat_id || row.tool || row.auth_kind}>
                      <td className="px-4 py-1"><Button variant="link" onClick={() => select(row)}>{rowLabel(row, service)}</Button></td>
                      {group === "credentials" && <>
                        <td className="px-4"><CredentialState info={row} /></td>
                        <td className="whitespace-nowrap px-4">{row.last_observed_at
                          ? <time dateTime={row.last_observed_at}>{new Date(row.last_observed_at).toLocaleString()}</time> : "—"}</td>
                      </>}
                      <td className="px-4">{row.calls}</td>
                      {group === "users" && <>{service && <td className="px-4">{row.service_key_calls}</td>}<td className="px-4">{row.sso_calls}</td><td className="px-4">{row.pat_calls}</td></>}
                      <td className="px-4">{row.errors}</td>
                      {group !== "users" && <td className="px-4">{row.p95_ms} ms</td>}
                    </tr>
                  ))}</tbody>
                </table></div>}
              {(pages[group] > 0 || report.has_more[group]) && <div className="flex items-center gap-3 border-t border-border p-3">
                <Button size="sm" variant="outline" disabled={!pages[group]} onClick={() => setPages({ ...pages, [group]: pages[group] - 1 })}>Previous</Button>
                <span className="text-sm">Page {pages[group] + 1}</span>
                <Button size="sm" variant="outline" disabled={!report.has_more[group]} onClick={() => setPages({ ...pages, [group]: pages[group] + 1 })}>Next</Button>
              </div>}
            </Panel>
          ))}

        </>}
      </div>
    </main>
  );
}
