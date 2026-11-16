import { useId, useRef, useState } from "react";
import { Panel, PanelHeader } from "@/components/ui/panel";
import type { MonitoringReport } from "@/lib/mcp-monitoring";

type Interval = MonitoringReport["series"][number];
const outcomes = [
  { key: "succeeded", label: "Succeeded", fill: "bg-success" },
  { key: "failed", label: "Failed", fill: "mcp-failed-bar bg-destructive" },
  { key: "other", label: "Cancelled / unknown", fill: "bg-foreground-muted" },
] as const;

function breakdown(p: Interval) {
  if (p.calls == null || p.succeeded == null || p.failed == null) return null;
  return { succeeded: p.succeeded, failed: p.failed, other: p.calls - p.succeeded - p.failed };
}

function outcomeText(p: Interval) {
  const counts = breakdown(p);
  if (!counts) return p.calls === null ? "" : "Outcome breakdown unavailable";
  return outcomes.filter(o => o.key !== "other" || counts.other > 0)
    .map(o => `${o.label}: ${counts[o.key].toLocaleString()}`).join(" · ");
}

const stamp = (value: string) => new Date(value).toLocaleString();
const intervalTime = new Intl.DateTimeFormat(undefined, { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" });
const coverageText = { complete: "Complete coverage", partial: "Partial coverage", unmeasured: "No coverage" };
const countText = (n: number | null) => n === null ? "Unmeasured" : `${n.toLocaleString()} ${n === 1 ? "call" : "calls"}`;

export function CallTrend({ series, end }: { series: MonitoringReport["series"]; end: string }) {
  const [active, setActive] = useState<number | null>(null);
  const [cursor, setCursor] = useState(0);
  const [failuresOnly, setFailuresOnly] = useState(false);
  const tip = useId();
  const pointer = useRef<{ x: number; y: number } | null>(null);
  const keyboard = useRef(false);
  const buttons = useRef<(HTMLButtonElement | null)[]>([]);
  const point = active === null ? undefined : series[active];
  const peak = Math.max(1, ...series.map(p => (failuresOnly ? p.failed : p.calls) ?? 0));
  const hasOther = series.some(p => (breakdown(p)?.other ?? 0) > 0);
  const visible = outcomes.filter(o => failuresOnly ? o.key === "failed" : o.key !== "other" || hasOther);
  const interval = (i: number) => `${intervalTime.format(new Date(series[i].start))} – ${intervalTime.format(new Date(series[i + 1]?.start ?? end))}`;
  const move = (i: number) => buttons.current[Math.max(0, Math.min(series.length - 1, i))]?.focus();
  return <Panel>
    <PanelHeader label="Calls over time" right={
      <label className="flex cursor-pointer items-center gap-2 py-1 text-sm">
        <input type="checkbox" className="size-4 accent-primary" checked={failuresOnly} onChange={e => setFailuresOnly(e.target.checked)} />Show failures only
      </label>} />
    <div className="p-4">
      <div className="mb-2 flex flex-wrap items-center gap-x-4 gap-y-1 text-xs text-foreground-muted">
        {visible.map(o => <span key={o.key} className="inline-flex items-center gap-1.5"><span aria-hidden className={`size-2.5 rounded-sm ${o.fill}`} />{o.label}</span>)}
        <span className="ml-auto">Scale 0–{countText(peak)}</span>
      </div>
      <div className="relative" onPointerLeave={e => {
        if (e.pointerType === "mouse" && !keyboard.current) setActive(null);
        pointer.current = null;
      }}
        onBlur={e => { if (!e.currentTarget.contains(e.relatedTarget)) setActive(null); }}>
        <div aria-hidden="true" className="pointer-events-none absolute inset-x-0 inset-y-0 flex flex-col justify-between">
          {[0, 1, 2].map(i => <div key={i} className="border-t border-dashed border-border" />)}
        </div>
        <div role="group" aria-label="Call intervals" className="relative flex h-36 items-end gap-px border-b border-border sm:gap-1">
          {series.map((p, i) => {
            const { start, calls, coverage } = p;
            const counts = breakdown(p);
            const height = failuresOnly ? counts?.failed : calls;
            return (
            <button key={start} ref={el => { buttons.current[i] = el; }} type="button"
              aria-label={`${interval(i)}, ${countText(calls)}, ${outcomeText(p)}, ${coverageText[coverage]}`}
              aria-describedby={active === i ? tip : undefined} tabIndex={Math.min(cursor, series.length - 1) === i ? 0 : -1}
              onPointerMove={e => {
                if (e.pointerType === "touch" || (pointer.current?.x === e.clientX && pointer.current.y === e.clientY)) return;
                pointer.current = { x: e.clientX, y: e.clientY };
                keyboard.current = false;
                setActive(i);
              }}
              onFocus={() => { setCursor(i); setActive(i); }} onClick={() => setActive(i)}
              onKeyDown={e => {
                keyboard.current = true;
                if (e.key === "Escape") { setActive(null); return; }
                const next = { ArrowLeft: i - 1, ArrowRight: i + 1, Home: 0, End: series.length - 1 }[e.key];
                if (next !== undefined) { e.preventDefault(); move(next); }
              }}
              className={`relative flex h-full min-w-0 flex-1 cursor-pointer items-end justify-center rounded-t-sm focus-visible:outline-2 focus-visible:outline-ring ${active === i ? "bg-surface-selected" : ""}`}>
              {height == null ? <span aria-hidden className="mb-0.5 text-xs text-foreground-muted">—</span> :
                <span aria-hidden className={`flex w-full max-w-24 flex-col-reverse overflow-hidden rounded-t-sm ${height === 0 ? "bg-border-strong" : counts ? "" : "bg-foreground-muted"}`}
                  style={{ height: height ? `max(4px, ${100 * height / peak}%)` : "2px" }}>
                  {counts && visible.filter(o => counts[o.key] > 0).map(o => <span key={o.key} className={`min-h-px ${o.fill}`} style={{ flexGrow: counts[o.key] }} />)}
                </span>}
            </button>);
          })}
        </div>
        {point && active !== null && <div role="tooltip" id={tip}
          className="pointer-events-none absolute top-1 z-10 w-56 max-w-full -translate-x-1/2 rounded-[var(--radius-md)] border border-border-strong bg-surface px-3 py-2 text-foreground shadow-md"
          style={{ left: `clamp(min(7rem, 50%), ${(active + 0.5) * 100 / series.length}%, max(calc(100% - 7rem), 50%))` }}>
          <div className="text-xs text-foreground-muted">{interval(active)}</div>
          <div className="mt-1 flex items-baseline justify-between gap-2">
            <strong className="text-sm tabular-nums">{countText(point.calls)}</strong>
            <span className="text-xs text-foreground-muted">{coverageText[point.coverage]}</span>
          </div>
          {outcomeText(point) && <p className="mt-1 text-xs tabular-nums">{outcomeText(point)}</p>}
        </div>}
      </div>
      {series.length > 0 && <div className="mt-2 flex justify-between gap-2 text-xs text-foreground-muted">
        <time dateTime={series[0].start}>{stamp(series[0].start)}</time><time dateTime={end}>{stamp(end)}</time>
      </div>}
      <details className="mt-3 text-xs text-foreground-muted">
        <summary className="w-fit cursor-pointer font-medium text-link">View interval counts and coverage</summary>
        <ul className="mt-2 divide-y divide-border">
          {series.map(p => <li key={p.start} className="flex flex-wrap justify-between gap-2 py-1">
            <time dateTime={p.start}>{stamp(p.start)}</time><span>{p.calls ?? "—"} calls · {p.coverage}</span><span>{outcomeText(p)}</span>
          </li>)}
        </ul>
      </details>
    </div>
  </Panel>;
}
