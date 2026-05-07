import { useRef } from "react";
import { useVirtualizer } from "@tanstack/react-virtual";
import { timeAgo } from "@/lib/utils";

export interface HistoryEntry {
  hash?: string;
  agent?: string;
  author?: string;
  subject?: string;
  timestamp?: string;
}

const ROW_HEIGHT = 22;
const VIRTUALIZE_THRESHOLD = 60;

export function HistoryList({ entries }: { entries: HistoryEntry[] }) {
  if (entries.length === 0) {
    return <div className="coord">No history yet.</div>;
  }
  if (entries.length < VIRTUALIZE_THRESHOLD) {
    return (
      <ol className="font-mono text-[11px] leading-[1.9] space-y-0.5">
        {entries.map((p, i) => (
          <Row key={p.hash || i} entry={p} />
        ))}
      </ol>
    );
  }
  return <VirtualHistoryList entries={entries} />;
}

function VirtualHistoryList({ entries }: { entries: HistoryEntry[] }) {
  const parentRef = useRef<HTMLDivElement | null>(null);
  const rowVirtualizer = useVirtualizer({
    count: entries.length,
    getScrollElement: () => parentRef.current,
    estimateSize: () => ROW_HEIGHT,
    overscan: 8,
  });

  return (
    <div
      ref={parentRef}
      className="font-mono text-[11px] leading-[1.9] overflow-y-auto rail-scroll"
      style={{ maxHeight: "100%" }}
    >
      <ol
        style={{
          height: rowVirtualizer.getTotalSize(),
          position: "relative",
          width: "100%",
        }}
      >
        {rowVirtualizer.getVirtualItems().map((vRow) => {
          const p = entries[vRow.index];
          return (
            <li
              key={p.hash || vRow.index}
              data-index={vRow.index}
              ref={rowVirtualizer.measureElement}
              style={{
                position: "absolute",
                top: 0,
                left: 0,
                width: "100%",
                transform: `translateY(${vRow.start}px)`,
              }}
              className="grid grid-cols-[54px_1fr_auto] gap-2"
            >
              <RowContent entry={p} />
            </li>
          );
        })}
      </ol>
    </div>
  );
}

function Row({ entry }: { entry: HistoryEntry }) {
  return (
    <li className="grid grid-cols-[54px_1fr_auto] gap-2">
      <RowContent entry={entry} />
    </li>
  );
}

function RowContent({ entry }: { entry: HistoryEntry }) {
  return (
    <>
      <span className="text-accent">{(entry.hash || "").slice(0, 7)}</span>
      <span className="truncate text-foreground-muted">
        <span className="text-foreground-muted">
          {entry.agent || entry.author || "unknown"}
        </span>
        {entry.subject && (
          <>
            {" "}
            <span className="text-foreground">· {entry.subject}</span>
          </>
        )}
      </span>
      <span className="text-foreground-muted tabular-nums text-right shrink-0">
        {timeAgo(entry.timestamp)}
      </span>
    </>
  );
}
