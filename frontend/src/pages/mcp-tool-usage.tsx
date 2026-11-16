import { Button } from "@/components/ui/button";
import { Panel, PanelHeader } from "@/components/ui/panel";
import { SelectMenu } from "@/components/ui/select-menu";
import type { ToolMetric } from "@/lib/mcp-monitoring";

type ViewChange = (metric: string, group: string, page: number) => void;

function LatencyTable({ metric, onView }: { metric: ToolMetric; onView: ViewChange }) {
  const id = `${metric.tool}/${metric.key}`;
  const group = metric.group ?? "*";
  const axes = metric.dimensions.filter(d => group === "*" || group === d.key);
  return <>
    {metric.dimensions.length > 1 && <div className="px-4 py-2">
      <SelectMenu aria-label={`Group ${metric.label} by`} value={group}
        options={[{ value: "*", label: "All conditions" }, ...metric.dimensions.map(d => ({ value: d.key, label: d.label }))]}
        onValueChange={value => onView(id, value, 0)} />
    </div>}
    {metric.rows.length ? <div className="overflow-x-auto">
      <table aria-label={metric.label} className="w-full text-left text-sm tabular-nums">
        <thead className="text-xs text-foreground-muted"><tr>
          {axes.map(d => <th key={d.key} scope="col" className="px-4 py-2">{d.label}</th>)}
          <th scope="col" className="px-4 py-2">Calls</th><th scope="col" className="px-4 py-2">p95</th>
        </tr></thead>
        <tbody className="divide-y divide-border">{metric.rows.map(row => <tr key={JSON.stringify(row.values)}>
          {axes.map(d => <td key={d.key} className="px-4 py-2">
            {d.labels[String(row.values[d.key])] ?? String(row.values[d.key] ?? "—")}
          </td>)}
          <td className="px-4 py-2">{row.calls}</td><td className="whitespace-nowrap px-4 py-2">{row.p95_ms} ms</td>
        </tr>)}</tbody>
      </table></div> : <p className="p-4 text-sm text-foreground-muted">No measurements on this page.</p>}
    {(metric.page > 0 || metric.has_more) && <div className="flex items-center gap-3 border-t border-border p-3">
      <Button size="sm" variant="outline" disabled={!metric.page} onClick={() => onView(id, group, metric.page - 1)}>Previous</Button>
      <span className="text-sm">Page {metric.page + 1}</span>
      <Button size="sm" variant="outline" disabled={!metric.has_more} onClick={() => onView(id, group, metric.page + 1)}>Next</Button>
    </div>}
  </>;
}

export function McpToolUsage({ metrics, onView }: { metrics: ToolMetric[]; onView: ViewChange }) {
  return <div className="grid items-start gap-4 lg:grid-cols-2">{metrics.map(metric =>
    <Panel key={`${metric.tool}/${metric.key}`}>
      <PanelHeader label={metric.label} />
      <p className="px-4 pt-3 text-sm text-foreground-muted">{metric.tool}{!metric.active && " · Historical metric"}</p>
      {metric.kind === "latency" ? <LatencyTable metric={metric} onView={onView} /> :
        <div className="space-y-1 p-4">
          <p className="text-2xl font-semibold tabular-nums">{metric.calls ? `${(100 * metric.matched / metric.calls).toFixed(1)}%` : "—"}</p>
          <p className="text-sm text-foreground-muted">{metric.calls
            ? `${metric.matched} of ${metric.calls} measured successful calls returned zero.`
            : "No measured successful calls in this selection."}</p>
        </div>}
    </Panel>)}</div>;
}
