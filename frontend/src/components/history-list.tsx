import { Check, GitCompareArrows } from "lucide-react";
import type { Ref } from "react";

import type { DocumentHistoryEntry } from "@/lib/api";
import { sameCommitRef } from "@/lib/commit";
import { cn, timeAgo } from "@/lib/utils";
import { DocumentIconButton } from "@/components/document-reading-controls";

export type HistoryEntry = DocumentHistoryEntry;

interface HistoryListProps {
  entries: HistoryEntry[];
  /** Open the complete document at a revision. */
  onSelect?: (hash: string) => void;
  /** Compare a revision with its immediate parent. */
  onCompare?: (hash: string, trigger: HTMLButtonElement) => void;
  selectedHash?: string;
  diffHash?: string;
  compareFocusHash?: string;
  compareFocusRef?: Ref<HTMLButtonElement>;
}

export function HistoryList({
  entries,
  onSelect,
  onCompare,
  selectedHash,
  diffHash,
  compareFocusHash,
  compareFocusRef,
}: HistoryListProps) {
  if (entries.length === 0) {
    return <p className="text-sm text-foreground-muted">No versions yet.</p>;
  }

  return (
    <ol className="min-w-0 divide-y divide-border">
      {entries.map((entry) => {
        const selected = sameCommitRef(entry.hash, selectedHash);
        const comparing = sameCommitRef(entry.hash, diffHash);
        const author = entry.author_name || entry.author || "Unknown author";
        const shortHash = entry.hash.slice(0, 7);
        return (
          <li
            key={entry.hash}
            className={cn(
              "grid min-w-0 grid-cols-[minmax(0,1fr)_auto] items-start gap-1 py-1.5 transition-token",
              (selected || comparing) && "bg-surface-selected text-surface-selected-foreground",
            )}
          >
            <button
              type="button"
              onClick={() => onSelect?.(entry.hash)}
              disabled={!onSelect}
              aria-pressed={selected && !comparing}
              aria-label={`View version ${shortHash}: ${entry.message || "Document update"}`}
              className="min-h-11 min-w-0 rounded-[var(--radius-sm)] px-2 py-1 text-left transition-token hover:bg-surface-hover focus:outline-none focus-visible:ring-2 focus-visible:ring-inset focus-visible:ring-ring disabled:cursor-default disabled:opacity-100"
            >
              <span className="block text-[.8125rem] leading-snug font-medium text-foreground wrap-anywhere">
                {entry.message || "Document update"}
              </span>
              <span className="mt-1 flex min-w-0 flex-wrap items-baseline gap-x-2 gap-y-0.5 text-xs leading-snug text-foreground-muted">
                <code className="font-mono text-link">{shortHash}</code>
                <span className="min-w-0 wrap-anywhere">{author}</span>
                {entry.date && <time dateTime={entry.date} className="whitespace-nowrap tabular-nums">{timeAgo(entry.date)}</time>}
              </span>
            </button>

            {onCompare && (
              <DocumentIconButton
                ref={entry.hash === compareFocusHash ? compareFocusRef : undefined}
                data-document-diff-trigger={entry.hash}
                onClick={(event) => onCompare(entry.hash, event.currentTarget)}
                aria-pressed={comparing}
                label={`View changes in version ${shortHash}`}
                className={cn("size-11 shrink-0 sm:[@media(pointer:fine)]:size-8", comparing && "bg-surface-selected text-link")}
              >
                {comparing ? <Check className="h-4 w-4" aria-hidden /> : <GitCompareArrows className="h-4 w-4" aria-hidden />}
              </DocumentIconButton>
            )}
          </li>
        );
      })}
    </ol>
  );
}
