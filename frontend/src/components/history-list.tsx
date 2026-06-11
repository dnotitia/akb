import { useRef } from "react";
import { useVirtualizer } from "@tanstack/react-virtual";
import { cn } from "@/lib/utils";
import { timeAgo } from "@/lib/utils";

export interface HistoryEntry {
  hash?: string;
  agent?: string;
  author?: string;
  subject?: string;
  timestamp?: string;
}

interface HistoryListProps {
  entries: HistoryEntry[];
  /**
   * Called with the commit hash when a row is clicked. When omitted,
   * rows stay read-only (legacy behavior).
   */
  onSelect?: (hash: string) => void;
  /**
   * Commit hash currently being viewed — that row gets the active style.
   */
  selectedHash?: string;
}

const ROW_HEIGHT = 22;
const VIRTUALIZE_THRESHOLD = 60;

export function HistoryList({ entries, onSelect, selectedHash }: HistoryListProps) {
  if (entries.length === 0) {
    return <div className="coord">No history yet.</div>;
  }
  if (entries.length < VIRTUALIZE_THRESHOLD) {
    return (
      <ol className="font-mono text-[11px] leading-[1.9] space-y-0.5">
        {entries.map((p, i) => (
          <Row
            key={p.hash || i}
            entry={p}
            onSelect={onSelect}
            active={!!selectedHash && p.hash === selectedHash}
          />
        ))}
      </ol>
    );
  }
  return (
    <VirtualHistoryList
      entries={entries}
      onSelect={onSelect}
      selectedHash={selectedHash}
    />
  );
}

function VirtualHistoryList({
  entries,
  onSelect,
  selectedHash,
}: {
  entries: HistoryEntry[];
  onSelect?: (hash: string) => void;
  selectedHash?: string;
}) {
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
          const isActive = !!selectedHash && p.hash === selectedHash;
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
            >
              <RowInner entry={p} onSelect={onSelect} active={isActive} />
            </li>
          );
        })}
      </ol>
    </div>
  );
}

function Row({
  entry,
  onSelect,
  active,
}: {
  entry: HistoryEntry;
  onSelect?: (hash: string) => void;
  active?: boolean;
}) {
  return (
    <li>
      <RowInner entry={entry} onSelect={onSelect} active={active} />
    </li>
  );
}

function RowInner({
  entry,
  onSelect,
  active,
}: {
  entry: HistoryEntry;
  onSelect?: (hash: string) => void;
  active?: boolean;
}) {
  const baseLayout = "grid grid-cols-[54px_1fr_auto] gap-2 w-full px-1";
  // No commit hash → never clickable (e.g. unparseable git log entry).
  if (!entry.hash || !onSelect) {
    return (
      <div
        className={cn(
          baseLayout,
          active && "bg-surface-selected text-surface-selected-foreground",
        )}
      >
        <RowContent entry={entry} active={active} />
      </div>
    );
  }
  return (
    <button
      type="button"
      onClick={() => onSelect(entry.hash!)}
      aria-pressed={active}
      aria-label={`View document at commit ${entry.hash.slice(0, 7)}`}
      title={`Open this version (${entry.hash.slice(0, 7)})`}
      className={cn(
        baseLayout,
        "text-left cursor-pointer transition-colors focus:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2 focus-visible:ring-offset-surface",
        active
          ? "bg-surface-selected text-surface-selected-foreground"
          : "hover:bg-surface-hover",
      )}
    >
      <RowContent entry={entry} active={active} />
    </button>
  );
}

function RowContent({
  entry,
  active,
}: {
  entry: HistoryEntry;
  active?: boolean;
}) {
  // When active, the row container carries text-surface-selected-foreground;
  // inner spans drop their own color so it cascades (no per-span orange).
  return (
    <>
      <span className={active ? undefined : "text-link"}>
        {(entry.hash || "").slice(0, 7)}
      </span>
      <span
        title={`${entry.agent || entry.author || "unknown"}${entry.subject ? ` · ${entry.subject}` : ""}`}
        className="truncate"
      >
        <span className={active ? undefined : "text-foreground-muted"}>
          {entry.agent || entry.author || "unknown"}
        </span>
        {entry.subject && (
          <>
            {" "}
            <span className={active ? undefined : "text-foreground"}>
              · {entry.subject}
            </span>
          </>
        )}
      </span>
      <span
        className={cn(
          "tabular-nums text-right shrink-0",
          !active && "text-foreground-muted",
        )}
      >
        {timeAgo(entry.timestamp)}
      </span>
    </>
  );
}
