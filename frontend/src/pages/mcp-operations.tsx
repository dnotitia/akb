import { Fragment, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { ChevronDown, ChevronRight, Workflow } from "lucide-react";
import { Alert } from "@/components/ui/alert";
import { Button } from "@/components/ui/button";
import { Panel, PanelHeader } from "@/components/ui/panel";
import { getMcpOperationCalls } from "@/lib/api";
import { callTime, type McpOperation, type MonitoringReport, type RecentCall } from "@/lib/mcp-monitoring";
import { CallOutcome, OperationOutcome } from "./mcp-outcome";
import { CallTrace } from "./mcp-call-trace";

const cell = "px-3 py-2 align-top";
const groupLabel = { conversation: "Conversation", turn: "Agent turn", trace: "Trace", individual: "Individual call" };
function Timestamp({ value }: { value: string }) {
  return <time dateTime={value}>{callTime.format(new Date(value))}</time>;
}

function Pages({ page, more, change }: { page: number; more: boolean; change: (page: number) => void }) {
  return page > 0 || more ? <div className="flex items-center gap-3 border-t border-border p-3">
    <Button size="sm" variant="outline" disabled={page === 0} onClick={() => change(page - 1)}>Previous</Button>
    <span className="text-sm">Page {page + 1}</span>
    <Button size="sm" variant="outline" disabled={!more} onClick={() => change(page + 1)}>Next</Button>
  </div> : null;
}

function CallSequence({ operation, filters, codesSince }: { operation: McpOperation; filters: URLSearchParams; codesSince?: string | null }) {
  const [page, setPage] = useState(0);
  const [selected, setSelected] = useState<Pick<RecentCall, "observation_id" | "tool"> | null>(null);
  const params = new URLSearchParams({ page: String(page) });
  if (operation.kind === "turn" || operation.kind === "conversation") params.set("kind", operation.kind);
  for (const key of ["from", "to", "caller_kind", "auth_kind", "pat_id", "tool"]) {
    const value = filters.get(key);
    if (value) params.set(key, value);
  }
  const query = useQuery({
    queryKey: ["mcp-operation-calls", operation.actor_id, operation.id, params.toString()],
    queryFn: ({ signal }) => getMcpOperationCalls(operation.actor_id, operation.id, params, signal),
    retry: false, gcTime: 0,
  });
  if (query.isPending) return <p className="p-3" role="status">Loading calls…</p>;
  if (query.isError) return <Alert variant="destructive">Could not load the call sequence.
    <Button variant="link" onClick={() => void query.refetch()}>Retry</Button></Alert>;
  const start = Date.parse(operation.started_at);
  const span = Math.max(1, Date.parse(operation.finished_at) - start);
  return <div className="border-l-2 border-primary bg-surface-2">
    <p className="break-all px-4 pt-3 text-xs text-foreground-muted">
      {operation.kind === "trace" ? "Trace ID" : "Operation ID"} <code>{operation.id.replaceAll("-", "")}</code>
    </p>
    <table aria-label="Call sequence" className="w-full text-left text-sm tabular-nums">
      <colgroup>{["7%", "17%", "20%", "13%", "27%", "16%"].map(width => <col key={width} style={{ width }} />)}</colgroup>
      <thead className="text-xs text-foreground-muted"><tr>
        {["Order", "Tool", "Started", "Duration", "Outcome", "Tempo trace"].map(label => <th scope="col" key={label} className={cell}>{label}</th>)}
      </tr></thead>
      <tbody>{query.data.calls.map(call => <tr className="mcp-call-row" key={call.observation_id}>
        <td data-label="Order" className={cell}><span className="inline-grid size-6 place-items-center rounded-full border border-border-strong bg-surface font-medium">{call.position}</span></td><td data-label="Tool" className={cell}>{call.tool}</td>
        <td data-label="Started" className={cell}><Timestamp value={call.started_at} /></td><td data-label="Duration" className={cell}>{call.duration_ms} ms
          <div aria-hidden className="relative mt-1.5 h-1.5 w-24 max-w-full overflow-hidden rounded-full bg-border">
            <span className="absolute inset-y-0 min-w-px rounded-full bg-primary"
              style={{ left: `${Math.min(99, Math.max(0, 100 * (Date.parse(call.started_at) - start) / span))}%`,
                width: `${Math.min(100, 100 * call.duration_ms / span)}%` }} /></div>
        </td>
        <td data-label="Outcome" className={cell}><CallOutcome call={call} codesSince={codesSince} /></td>
        <td data-label="Tempo trace" className={cell}>{call.trace_available
          ? <Button variant="link" size="sm" aria-label={`View trace for call ${call.position}, ${call.tool}`}
              onClick={() => setSelected(call)}><Workflow aria-hidden size={15} />View trace</Button>
          : <span aria-label="No trace recorded" className="text-foreground-muted">—</span>}</td>
      </tr>)}</tbody>
    </table>
    <Pages page={page} more={query.data.has_more} change={setPage} />
    {selected && <CallTrace call={selected} close={() => setSelected(null)} />}
  </div>;
}

export function McpOperations({ report, filters, page, setPage }: {
  report: MonitoringReport; filters: URLSearchParams; page: number; setPage: (page: number) => void;
}) {
  const service = filters.get("caller_kind") === "service";
  const [selected, setSelected] = useState<Pick<RecentCall, "observation_id" | "tool"> | null>(null);
  const [expanded, setExpanded] = useState<string | null>(null);
  return <Panel className="mcp-operations">
    <PanelHeader label="Operations" />
    {report.operations.length === 0 ? <p className="p-4 text-sm text-foreground-muted">No operations in this selection.</p> : <>
      <p className="px-4 py-3 text-sm text-foreground-muted">Elapsed time runs from the first matching call’s start to the last completion.</p>
      <div><table aria-label="Operations" className="w-full text-left text-sm tabular-nums">
        <colgroup>{[24, 21, 13, 14, 6, 8, 14].map((width, index) => <col key={index} style={{ width: `${width}%` }} />)}</colgroup>
        <thead className="text-xs text-foreground-muted"><tr>
          {["Operation", "Outcome", service ? "Service caller" : "User", "Started", "Calls", "Elapsed", "Tempo trace"].map(label => <th scope="col" key={label} className={cell}>{label}</th>)}
        </tr></thead>
        <tbody className="divide-y divide-border">{report.operations.map(op => {
          const id = `${op.actor_id}-${op.kind}-${op.id}`;
          const open = id === expanded;
          const singleTrace = op.trace_observation_id && op.tool
            ? { observation_id: op.trace_observation_id, tool: op.tool } : null;
          return <Fragment key={id}><tr className="mcp-operation-row hover:bg-surface-2 focus-within:bg-surface-2">
            <td data-label="Operation" className={cell}>{op.correlated
              ? <Button size="sm" variant="link" aria-expanded={open} aria-controls={open ? id : undefined}
                  onClick={() => setExpanded(open ? null : id)}>
                  {open ? <ChevronDown aria-hidden size={16} /> : <ChevronRight aria-hidden size={16} />}
                  {service && op.kind === "conversation" ? "Job" : groupLabel[op.kind]} {op.id.slice(0, 8)}
                </Button>
              : <><span className="font-medium">{op.tool}</span><span className="block text-xs text-foreground-muted">Individual call</span></>}
              {op.kind === "conversation" && <span className="block text-xs text-foreground-muted">{op.agent}</span>}</td>
            <td data-label="Outcome" className={cell}><OperationOutcome op={op} codesSince={report.collection.error_codes_supported_from} /></td>
            <td data-label={service ? "Service caller" : "User"} className={cell}>{op.user_label || `Former account · ${op.actor_id.slice(0, 8)}`}</td>
            <td data-label="Started" className={cell}><Timestamp value={op.started_at} /></td><td data-label="Calls" className={cell}>{op.calls}</td><td data-label="Elapsed" className={cell}>{op.elapsed_ms} ms</td>
            <td data-label="Tempo trace" className={cell}>{singleTrace
              ? <Button size="sm" variant="link" onClick={() => setSelected(singleTrace)}><Workflow aria-hidden size={15} />View trace</Button>
              : op.correlated && op.trace_calls > 0
                ? <Button size="sm" variant="link" aria-expanded={open} aria-controls={open ? id : undefined}
                    onClick={() => setExpanded(open ? null : id)}><Workflow aria-hidden size={15} />{op.trace_calls} {op.trace_calls === 1 ? "trace" : "traces"}</Button>
                : <span aria-label="No recorded trace to open" className="text-foreground-muted">—</span>}</td>
          </tr>{open && <tr className="mcp-expanded"><td colSpan={7} className="p-0" id={id}><CallSequence operation={op} filters={filters} codesSince={report.collection.error_codes_supported_from} /></td></tr>}</Fragment>;
        })}</tbody>
      </table></div>
    </>}
    <Pages page={page} more={report.has_more_operations} change={setPage} />
    {selected && <CallTrace call={selected} close={() => setSelected(null)} />}
  </Panel>;
}
