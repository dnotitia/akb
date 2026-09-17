import { GitCompareArrows, GitCommitHorizontal } from "lucide-react";

import type { DocumentHistoryEntry } from "@/lib/api";
import { sameCommitRef } from "@/lib/commit";
import { cn, timeAgo } from "@/lib/utils";
import { TooltipText } from "@/components/ui/tooltip-text";

export type HistoryEntry = DocumentHistoryEntry;

interface HistoryListProps {
  entries: HistoryEntry[];
  /** Open the complete document at a revision. */
  onSelect?: (hash: string) => void;
  /** Compare a revision with its immediate parent. */
  onCompare?: (hash: string, trigger: HTMLButtonElement) => void;
  selectedHash?: string;
  diffHash?: string;
}

export function HistoryList({
  entries,
  onSelect,
  onCompare,
  selectedHash,
  diffHash,
}: HistoryListProps) {
  if (entries.length === 0) {
    return <p className="text-sm text-foreground-muted">No versions yet.</p>;
  }

  return (
    <ol className="overflow-hidden rounded-[var(--radius-md)] border border-border bg-surface divide-y divide-border">
      {entries.map((entry) => {
        const selected = sameCommitRef(entry.hash, selectedHash);
        const comparing = sameCommitRef(entry.hash, diffHash);
        const author = entry.author_name || entry.author || "Unknown author";
        const shortHash = entry.hash.slice(0, 7);
        return (
          <li
            key={entry.hash}
            className={cn(
              "grid min-w-0 grid-cols-[minmax(0,1fr)_auto] items-center gap-2 px-2 py-2 transition-token",
              (selected || comparing) && "bg-surface-selected text-surface-selected-foreground",
            )}
          >
            <button
              type="button"
              onClick={() => onSelect?.(entry.hash)}
              disabled={!onSelect}
              aria-pressed={selected && !comparing}
              aria-label={`View version ${shortHash}: ${entry.message}`}
              className="min-w-0 rounded-[var(--radius-sm)] px-1 py-0.5 text-left transition-token hover:bg-surface-hover focus:outline-none focus-visible:ring-2 focus-visible:ring-ring disabled:cursor-default disabled:opacity-100"
            >
              <TooltipText tip={entry.message} className="block truncate text-sm font-medium text-foreground">
                {entry.message || "Document update"}
              </TooltipText>
              <span className="mt-0.5 flex min-w-0 items-center gap-1.5 text-xs text-foreground-muted">
                <GitCommitHorizontal className="h-3 w-3 shrink-0" aria-hidden />
                <code className="shrink-0 font-mono text-link">{shortHash}</code>
                <span aria-hidden>·</span>
                <span className="min-w-0 truncate">{author}</span>
                {entry.date && (
                  <>
                    <span aria-hidden>·</span>
                    <span className="shrink-0 tabular-nums">{timeAgo(entry.date)}</span>
                  </>
                )}
              </span>
            </button>

            <div className="flex shrink-0 items-center gap-1">
              {onCompare && (
                <button
                  type="button"
                  data-document-diff-trigger={entry.hash}
                  onClick={(event) => onCompare(entry.hash, event.currentTarget)}
                  aria-pressed={comparing}
                  aria-label={`View changes in version ${shortHash}`}
                  className={cn(
                    "inline-flex h-8 items-center gap-1.5 rounded-[var(--radius-md)] border px-2 text-xs font-medium transition-token focus:outline-none focus-visible:ring-2 focus-visible:ring-ring",
                    comparing
                      ? "border-primary bg-primary text-primary-foreground"
                      : "border-border bg-surface text-foreground-muted hover:border-border-strong hover:bg-surface-hover hover:text-link",
                  )}
                >
                  <GitCompareArrows className="h-3.5 w-3.5" aria-hidden />
                  Changes
                </button>
              )}
            </div>
          </li>
        );
      })}
    </ol>
  );
}
