import { CircleAlert } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Panel, PanelHeader } from "@/components/ui/panel";
import { SelectMenu } from "@/components/ui/select-menu";
import type { MonitoringReport, MonitoringRow } from "@/lib/mcp-monitoring";

const order = [{ value: "calls", label: "Most calls" }, { value: "errors", label: "Most errors" }, { value: "latency", label: "Highest p95" }];
export function McpTools({ report, sort, setSort, select, inspectErrors, page, setPage }: {
  report: MonitoringReport; sort: string; setSort: (sort: string) => void;
  select: (tool: MonitoringRow) => void; inspectErrors: (tool: string) => void; page: number; setPage: (page: number) => void;
}) {
  return <Panel><PanelHeader label="Tools" right={<SelectMenu className="w-40 max-w-full" aria-label="Sort tools" value={sort} options={order} onValueChange={setSort} />} />
    {!report.tools.length ? <p className="p-4 text-sm text-foreground-muted">No observed tools in this selection.</p> :
      <div className="overflow-x-auto"><table aria-label="Tools" className="w-full text-left text-sm tabular-nums">
        <thead className="border-b border-border text-xs text-foreground-muted"><tr>
          {["Tool", "Calls · share of traffic", "Errors", "p95 duration"].map(x => <th key={x} scope="col" className="px-4 py-2">{x}</th>)}
        </tr></thead>
        <tbody className="divide-y divide-border">{report.tools.map(row => {
          const tool = row.tool;
          const share = report.summary.calls ? 100 * row.calls / report.summary.calls : 0;
          return <tr key={row.tool} className="hover:bg-surface-2 focus-within:bg-surface-2">
            <th scope="row" className="px-4 py-2 font-medium"><Button variant="link" size="sm" onClick={() => select(row)}>{row.tool}</Button></th>
            <td className="min-w-32 px-4 py-2"><div className="flex max-w-56 items-baseline justify-between gap-3"><strong>{row.calls.toLocaleString()}</strong>
              <span className="text-xs text-foreground-muted">{share.toFixed(1)}%</span></div>
              <div aria-hidden className="mt-1 h-1.5 max-w-56 overflow-hidden rounded-full bg-surface-muted">
                <div className="h-full rounded-full bg-primary" style={{ width: `${share}%` }} /></div></td>
            <td className="px-4">{row.errors > 0 && tool
              ? <Button variant="link" size="sm" className="text-destructive" aria-label={`Inspect ${row.errors} ${row.errors === 1 ? "error" : "errors"} for ${row.tool}`}
                  onClick={() => inspectErrors(tool)}><CircleAlert size={14} aria-hidden />{row.errors} · {(100 * row.errors / row.decided).toFixed(1)}%</Button>
              : <span className="text-foreground-muted">0</span>}</td>
            <td className="whitespace-nowrap px-4">{row.p95_ms == null ? "—" : `${row.p95_ms} ms`}</td>
          </tr>;
        })}</tbody></table></div>}
    {(page > 0 || report.has_more.tools) && <div className="flex items-center gap-3 border-t border-border p-3">
      <Button size="sm" variant="outline" disabled={!page} onClick={() => setPage(page - 1)}>Previous</Button>
      <span className="text-sm">Page {page + 1}</span><Button size="sm" variant="outline" disabled={!report.has_more.tools} onClick={() => setPage(page + 1)}>Next</Button>
    </div>}
  </Panel>;
}
