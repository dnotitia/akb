import { useQuery } from "@tanstack/react-query";
import { ApiError, getMcpCallTrace } from "@/lib/api";
import { Alert } from "@/components/ui/alert";
import { Button } from "@/components/ui/button";
import { Dialog, DialogContent, DialogDescription, DialogHeader, DialogTitle } from "@/components/ui/dialog";

function RecordedId({ id }: { id: string }) {
  return <p className="break-all text-xs text-foreground-muted">Recorded trace ID <code className="text-foreground">{id}</code></p>;
}

function TraceNotice({ error, retry }: { error: Error; retry: () => void }) {
  const retryButton = <Button variant="link" onClick={retry}>Retry</Button>;
  if (error instanceof ApiError) {
    const detail = error.detail && typeof error.detail === "object" ? error.detail : null;
    const code = detail && "code" in detail ? detail.code : null;
    const recorded = detail && "trace_id" in detail && typeof detail.trace_id === "string" && <RecordedId id={detail.trace_id} />;
    if (code === "trace_expired" || code === "trace_outside_previous_retention") return <>
      <Alert variant="info" title={code === "trace_expired" ? "Trace expired" : "Outside the previous Tempo retention window"}>{error.message}</Alert>
      {recorded}
    </>;
    if (code === "trace_export_pending") return <><Alert variant="info" title="Awaiting trace export">{error.message} {retryButton}</Alert>{recorded}</>;
    if (error.status === 404) return <>
      <Alert variant="warning" title={code === "trace_span_not_found" ? "MCP span missing from trace" : "Trace not found"}>{error.message} {retryButton}</Alert>{recorded}
    </>;
    if (error.status === 401 || error.status === 403) return <Alert>A product administrator session is required.</Alert>;
  }
  return <Alert>The trace store could not be reached. {retryButton}</Alert>;
}

export function CallTrace({ call, close }: { call: { observation_id: string; tool: string }; close: () => void }) {
  const query = useQuery({
    queryKey: ["admin-mcp-trace", call.observation_id], queryFn: ({ signal }) => getMcpCallTrace(call.observation_id, signal),
    retry: false, gcTime: 0,
  });
  return <Dialog open onOpenChange={open => { if (!open) close(); }}>
    <DialogContent>
      <DialogHeader><DialogTitle>Call trace</DialogTitle><DialogDescription>Tempo · {call.tool}</DialogDescription></DialogHeader>
      {query.isPending && <p role="status">Loading trace…</p>}
      {query.isError && <TraceNotice error={query.error} retry={() => void query.refetch()} />}
      {query.data && !query.isError && <>
        <RecordedId id={query.data.trace_id} />
        <p className="text-sm font-medium">Server spans from Tempo</p>
        <ol aria-label="Server spans" className="divide-y divide-border">{query.data.spans.map(span =>
          <li key={span.span_id} className="flex flex-wrap items-baseline justify-between gap-2 py-3 text-sm">
            <span className="font-medium">{span.name}</span>
            <span className="tabular-nums">{span.duration_ms.toLocaleString(undefined, { maximumFractionDigits: 2 })} ms</span>
          </li>)}</ol>
      </>}
    </DialogContent>
  </Dialog>;
}
