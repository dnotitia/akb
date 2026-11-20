import { useId, useRef, useState } from "react";
import { Panel, PanelHeader } from "@/components/ui/panel";
import type { MonitoringReport } from "@/lib/mcp-monitoring";

const stamp = (value: string) => new Date(value).toLocaleString();
const intervalTime = new Intl.DateTimeFormat(undefined, { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" });
const coverageText = { complete: "Complete coverage", partial: "Partial coverage", unmeasured: "No coverage" };
const countText = (n: number | null) => n === null ? "Unmeasured" : `${n.toLocaleString()} ${n === 1 ? "call" : "calls"}`;

export function CallTrend({ series, end }: { series: MonitoringReport["series"]; end: string }) {
  const [active, setActive] = useState<number | null>(null);
  const [cursor, setCursor] = useState(0);
  const tip = useId();
  const pointer = useRef<{ x: number; y: number } | null>(null);
  const keyboard = useRef(false);
  const buttons = useRef<(HTMLButtonElement | null)[]>([]);
  const point = active === null ? undefined : series[active];
  const peak = Math.max(1, ...series.map(p => p.calls ?? 0));
  const interval = (i: number) => `${intervalTime.format(new Date(series[i].start))} – ${intervalTime.format(new Date(series[i + 1]?.start ?? end))}`;
  const move = (i: number) => buttons.current[Math.max(0, Math.min(series.length - 1, i))]?.focus();
  return <Panel>
    <PanelHeader label="Calls over time" />
    <div className="p-4">
      <div className="relative" onPointerLeave={e => {
        if (e.pointerType === "mouse" && !keyboard.current) setActive(null);
        pointer.current = null;
      }}
        onBlur={e => { if (!e.currentTarget.contains(e.relatedTarget)) setActive(null); }}>
        <div aria-hidden="true" className="pointer-events-none absolute inset-x-0 inset-y-0 flex flex-col justify-between">
          {[0, 1, 2].map(i => <div key={i} className="border-t border-dashed border-border" />)}
        </div>
        <div role="group" aria-label="Call intervals" className="relative flex h-36 items-end gap-px border-b border-border sm:gap-1">
          {series.map(({ start, calls, coverage }, i) =>
            <button key={start} ref={el => { buttons.current[i] = el; }} type="button"
              aria-label={`${interval(i)}, ${countText(calls)}, ${coverageText[coverage]}`}
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
              {calls === null ? <span aria-hidden className="mb-0.5 text-xs text-foreground-muted">—</span> :
                <span aria-hidden className={`w-full max-w-24 rounded-t-sm motion-safe:transition-colors ${active === i ? "bg-link" : "bg-primary"}`}
                  style={{ height: calls ? `max(3px, ${100 * calls / peak}%)` : "2px" }} />}
            </button>)}
        </div>
        {point && active !== null && <div role="tooltip" id={tip}
          className="absolute top-1 z-10 w-56 max-w-full -translate-x-1/2 rounded-[var(--radius-md)] border border-border-strong bg-surface px-3 py-2 text-foreground shadow-md"
          style={{ left: `clamp(min(7rem, 50%), ${(active + 0.5) * 100 / series.length}%, max(calc(100% - 7rem), 50%))` }}>
          <div className="text-xs text-foreground-muted">{interval(active)}</div>
          <div className="mt-1 flex items-baseline justify-between gap-2">
            <strong className="text-sm tabular-nums">{countText(point.calls)}</strong>
            <span className="text-xs text-foreground-muted">{coverageText[point.coverage]}</span>
          </div>
        </div>}
      </div>
      {series.length > 0 && <div className="mt-2 flex justify-between gap-2 text-xs text-foreground-muted">
        <time dateTime={series[0].start}>{stamp(series[0].start)}</time><time dateTime={end}>{stamp(end)}</time>
      </div>}
      <details className="mt-3 text-xs text-foreground-muted">
        <summary className="w-fit cursor-pointer font-medium text-link">View interval counts and coverage</summary>
        <ul className="mt-2 divide-y divide-border">
          {series.map(p => <li key={p.start} className="flex flex-wrap justify-between gap-2 py-1">
            <time dateTime={p.start}>{stamp(p.start)}</time><span>{p.calls ?? "—"} calls · {p.coverage}</span>
          </li>)}
        </ul>
      </details>
    </div>
  </Panel>;
}
