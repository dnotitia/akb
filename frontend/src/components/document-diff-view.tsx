import { useEffect, useMemo, useRef, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { useVirtualizer } from "@tanstack/react-virtual";
import {
  ArrowDown,
  ArrowLeft,
  ArrowRight,
  ArrowUp,
  Check,
  CircleSlash2,
  Copy,
  FileWarning,
  GitCompareArrows,
  History,
  RotateCcw,
  ShieldAlert,
} from "lucide-react";

import {
  DocumentRevisionApiError,
  getDocumentDiff,
  type DocumentHistoryEntry,
} from "@/lib/api";
import {
  DOCUMENT_DIFF_VIRTUALIZE_THRESHOLD,
  inspectDocumentDiffPatch,
  parseDocumentDiffPatch,
  type DocumentDiffLine,
  type ParsedDocumentDiff,
} from "@/lib/document-diff";
import { cn } from "@/lib/utils";
import { Button } from "@/components/ui/button";
import { DocumentIconButton, DocumentTimestamp } from "@/components/document-reading-controls";
import { LoadingState } from "@/components/ui/loading-state";
import { Skeleton } from "@/components/ui/skeleton";

interface DocumentDiffViewProps {
  vault: string;
  docId: string;
  revision: string;
  baseRevision?: string;
  targetEntry?: DocumentHistoryEntry;
  onBackToVersion: () => void;
  onBackToLatest: () => void;
  onOpenBase?: () => void;
}

type ParseResult =
  | { parsed: ParsedDocumentDiff; error: null }
  | { parsed: null; error: Error };

export default function DocumentDiffView({
  vault,
  docId,
  revision,
  baseRevision,
  targetEntry,
  onBackToVersion,
  onBackToLatest,
  onOpenBase,
}: DocumentDiffViewProps) {
  const scrollRef = useRef<HTMLDivElement | null>(null);
  const [activeHunk, setActiveHunk] = useState(0);
  const [copied, setCopied] = useState(false);

  const diffQuery = useQuery({
    queryKey: ["document-diff", vault, docId, revision],
    queryFn: () => getDocumentDiff(vault, docId, revision),
    staleTime: Number.POSITIVE_INFINITY,
    retry: false,
  });

  const guard = useMemo(
    () => (diffQuery.data ? inspectDocumentDiffPatch(diffQuery.data.diff) : null),
    [diffQuery.data],
  );
  const parseResult = useMemo<ParseResult | null>(() => {
    const response = diffQuery.data;
    if (
      !response ||
      guard?.tooLarge ||
      response.truncated ||
      response.type === "unknown" ||
      response.type === "unchanged" ||
      !response.diff
    ) {
      return null;
    }
    try {
      return { parsed: parseDocumentDiffPatch(response), error: null };
    } catch (error) {
      return {
        parsed: null,
        error: error instanceof Error ? error : new Error("Unable to read this diff."),
      };
    }
  }, [diffQuery.data, guard]);
  const parsed = parseResult?.parsed ?? null;
  const rows = parsed?.rows ?? [];
  const hunks = parsed?.hunks ?? [];
  const shouldVirtualize = rows.length > DOCUMENT_DIFF_VIRTUALIZE_THRESHOLD;

  const rowVirtualizer = useVirtualizer({
    count: rows.length,
    getScrollElement: () => scrollRef.current,
    estimateSize: (index) => (rows[index]?.kind === "hunk" ? 32 : 24),
    overscan: 16,
    enabled: shouldVirtualize,
  });

  useEffect(() => {
    setActiveHunk(0);
    setCopied(false);
  }, [revision]);

  const targetShort = shortRevision(revision);
  const baseShort = baseRevision ? shortRevision(baseRevision) : "Previous revision";
  const response = diffQuery.data;
  const additions = response?.additions ?? parsed?.additions ?? 0;
  const deletions = response?.deletions ?? parsed?.deletions ?? 0;
  const baseLabel = response?.type === "added" ? "First version" : baseShort;
  const revisionAuthor = targetEntry?.author_name || targetEntry?.author;

  function goToHunk(nextIndex: number) {
    if (!hunks.length) return;
    const bounded = Math.max(0, Math.min(hunks.length - 1, nextIndex));
    setActiveHunk(bounded);
    const rowIndex = hunks[bounded].rowIndex;
    if (shouldVirtualize) {
      rowVirtualizer.scrollToIndex(rowIndex, { align: "start" });
    } else {
      document.getElementById(`document-diff-hunk-${bounded}`)?.scrollIntoView({
        behavior: "auto",
        block: "start",
      });
    }
  }

  async function copyPatch() {
    if (!response?.diff) return;
    try {
      await navigator.clipboard?.writeText(response.diff);
      setCopied(true);
      window.setTimeout(() => setCopied(false), 2_000);
    } catch {
      // Insecure origins may not expose Clipboard. The selectable patch stays available.
    }
  }

  const queryError = diffQuery.error;
  const revisionError =
    queryError instanceof DocumentRevisionApiError ? queryError : null;

  return (
    <section
      aria-labelledby="document-diff-heading"
      className="@container/diff flex min-h-0 flex-1 flex-col overflow-hidden bg-surface"
    >
      <header data-slot="document-diff-header" className="flex shrink-0 flex-wrap items-center justify-between gap-x-4 gap-y-1 border-b border-border bg-surface px-3 py-1.5 sm:px-4">
        <div className="flex min-w-0 flex-[1_1_19rem] flex-wrap items-center gap-x-3 gap-y-1">
          <h2 id="document-diff-heading" aria-label={`Changes from ${baseLabel} to ${targetShort}`}
            className="inline-flex items-center gap-1.5 whitespace-nowrap text-xs font-semibold text-foreground">
            <GitCompareArrows className="h-4 w-4 shrink-0 text-link" aria-hidden />
            <span className={baseRevision && response?.type !== "added" ? "font-mono" : undefined}>{baseLabel}</span>
            <ArrowRight className="h-3.5 w-3.5 shrink-0 text-foreground-muted" aria-hidden />
            <code className="rounded-[var(--radius-sm)] bg-surface-selected px-1.5 py-0.5 font-mono text-surface-selected-foreground">{targetShort}</code>
          </h2>
          {targetEntry && <div role="group" aria-label="Revision metadata" className="flex min-w-0 flex-wrap items-center gap-x-2 gap-y-0.5 text-xs text-foreground-muted">
            {targetEntry.message && <span className="sr-only min-w-0 max-w-64 font-medium text-foreground @[34rem]/diff:not-sr-only @[34rem]/diff:truncate" title={targetEntry.message}>{targetEntry.message}</span>}
            {revisionAuthor && <span className="min-w-0 max-w-40 truncate" title={revisionAuthor}>{revisionAuthor}</span>}
            <DocumentTimestamp value={targetEntry.date} label="Version saved" compact showLabel={false} />
          </div>}
        </div>
        <div className="flex w-full min-w-0 flex-wrap items-center justify-between gap-x-3 gap-y-0.5 @[34rem]/diff:w-auto">
          <div className="flex items-center gap-2 text-xs tabular-nums">
            {diffQuery.isPending ? (
              <span className="text-foreground-muted">Loading change…</span>
            ) : parsed ? (
              <>
                <span className="inline-flex items-center gap-1 font-medium text-success" aria-label={`${additions} added lines`}>
                  <span aria-hidden>+</span>{additions}<span className="sr-only"> added</span>
                </span>
                <span className="inline-flex items-center gap-1 font-medium text-destructive" aria-label={`${deletions} removed lines`}>
                  <span aria-hidden>−</span>{deletions}<span className="sr-only"> removed</span>
                </span>
              </>
            ) : null}
          </div>
          <div role="group" className="ml-auto flex items-center gap-1" aria-label="Change navigation">
            <DocumentIconButton
              className="size-11 sm:[@media(pointer:fine)]:size-8"
              onClick={() => goToHunk(activeHunk - 1)}
              disabled={!hunks.length || activeHunk === 0}
              label={`Previous change${hunks.length ? `. Change ${activeHunk + 1} of ${hunks.length}` : ""}`}
            >
              <ArrowUp className="h-3.5 w-3.5" aria-hidden />
            </DocumentIconButton>
            <DocumentIconButton
              className="size-11 sm:[@media(pointer:fine)]:size-8"
              onClick={() => goToHunk(activeHunk + 1)}
              disabled={!hunks.length || activeHunk >= hunks.length - 1}
              label={`Next change${hunks.length ? `. Change ${activeHunk + 1} of ${hunks.length}` : ""}`}
            >
              <ArrowDown className="h-3.5 w-3.5" aria-hidden />
            </DocumentIconButton>
            <DocumentIconButton
              className="size-11 sm:[@media(pointer:fine)]:size-8"
              onClick={() => void copyPatch()}
              disabled={!response?.diff}
              label={copied ? "Patch copied" : "Copy patch"}
            >
              {copied ? <Check className="h-3.5 w-3.5 text-success" aria-hidden /> : <Copy className="h-3.5 w-3.5" aria-hidden />}
            </DocumentIconButton>
          </div>
        </div>
        <span className="sr-only" role="status" aria-live="polite" aria-atomic="true">
          {hunks.length ? `Change ${activeHunk + 1} of ${hunks.length}` : ""}
        </span>
      </header>

      {diffQuery.isPending ? (
        <DiffLoading />
      ) : revisionError?.status === 404 || revisionError?.status === 405 ? (
        <DiffState
          icon={CircleSlash2}
          title="Changes are not available on this server"
          description="You can still open the complete revision. Diff support will become available after the backend is updated."
          primaryLabel="Open version"
          onPrimary={onBackToVersion}
        />
      ) : revisionError?.status === 403 ? (
        <DiffState
          icon={ShieldAlert}
          title="Access to this change was denied"
          description="Your Vault access may have changed. Return to the latest document or the Vault to continue."
          primaryLabel="Back to latest"
          onPrimary={onBackToLatest}
        />
      ) : diffQuery.isError ? (
        <DiffState
          icon={RotateCcw}
          title="Couldn't load this change"
          description="The server or network did not complete the request. Retry without leaving this document."
          primaryLabel="Retry"
          onPrimary={() => void diffQuery.refetch()}
          secondaryLabel="Open version"
          onSecondary={onBackToVersion}
        />
      ) : response?.type === "unknown" || response?.error ? (
        <DiffState
          icon={FileWarning}
          title="This revision cannot be compared"
          description="The revision may no longer be available or its history cannot be resolved safely."
          primaryLabel="Return to history"
          onPrimary={onBackToVersion}
        />
      ) : response?.truncated || guard?.tooLarge ? (
        <DiffState
          icon={FileWarning}
          title="This change is too large to display safely"
          description={guard ? `${formatCount(guard.lineCount)} patch lines · ${formatBytes(guard.byteCount)}. Open either complete version instead.` : "Open either complete version instead."}
          primaryLabel="Open selected version"
          onPrimary={onBackToVersion}
          secondaryLabel={onOpenBase ? "Open previous version" : undefined}
          onSecondary={onOpenBase}
        />
      ) : parseResult?.error ? (
        <DiffState
          icon={FileWarning}
          title="This change uses an unsupported diff format"
          description="The complete revision is still available to read."
          primaryLabel="Open version"
          onPrimary={onBackToVersion}
        />
      ) : response?.type === "unchanged" || !response?.diff || !rows.length ? (
        <DiffState
          icon={History}
          title="No content changes in this revision"
          description="The revision may contain metadata-only changes. Open the complete version to inspect it."
          primaryLabel="Open version"
          onPrimary={onBackToVersion}
        />
      ) : (
        <div
          ref={scrollRef}
          role="table"
          aria-label={`Unified document changes for revision ${targetShort}`}
          aria-rowcount={rows.length + 1}
          tabIndex={0}
          className="min-h-0 flex-1 overflow-auto bg-surface font-mono text-xs leading-6 rail-scroll focus:outline-none focus-visible:ring-2 focus-visible:ring-inset focus-visible:ring-ring"
        >
          <DiffColumnHeaders />
          {shouldVirtualize ? (
            <div
              role="rowgroup"
              className="relative min-w-[48rem]"
              style={{ height: rowVirtualizer.getTotalSize() }}
            >
              {rowVirtualizer.getVirtualItems().map((virtualRow) => {
                const row = rows[virtualRow.index];
                return (
                  <div
                    key={row.id}
                    ref={rowVirtualizer.measureElement}
                    data-index={virtualRow.index}
                    className="absolute left-0 top-0 w-full"
                    style={{ transform: `translateY(${virtualRow.start}px)` }}
                  >
                    <DiffRow row={row} rowIndex={virtualRow.index} />
                  </div>
                );
              })}
            </div>
          ) : (
            <div role="rowgroup" className="min-w-[48rem]">
              {rows.map((row, index) => (
                <DiffRow key={row.id} row={row} rowIndex={index} />
              ))}
            </div>
          )}
        </div>
      )}
    </section>
  );
}

function DiffColumnHeaders() {
  return (
    <div
      role="row"
      className="sticky top-0 z-[var(--z-sticky)] grid min-w-[48rem] grid-cols-[3.5rem_3.5rem_2rem_minmax(0,1fr)] border-b border-border bg-surface-2 text-foreground-muted"
    >
      <span role="columnheader" className="px-2 text-right">Old</span>
      <span role="columnheader" className="border-l border-border px-2 text-right">New</span>
      <span role="columnheader" className="border-l border-border text-center"><span className="sr-only">Change type</span></span>
      <span role="columnheader" className="border-l border-border px-3 font-sans font-medium">Markdown</span>
    </div>
  );
}

function DiffRow({ row, rowIndex }: { row: DocumentDiffLine; rowIndex: number }) {
  if (row.kind === "hunk") {
    return (
      <div
        id={`document-diff-hunk-${row.hunkIndex}`}
        role="row"
        aria-rowindex={rowIndex + 2}
        aria-label={`Change ${row.hunkIndex + 1}: ${row.content}`}
        className="grid min-h-8 grid-cols-[3.5rem_3.5rem_2rem_minmax(0,1fr)] border-b border-info/30 bg-info-soft text-info-soft-foreground"
      >
        <span aria-hidden className="col-span-3 border-r border-info/30 px-2 text-right">{row.hunkIndex + 1}</span>
        <code aria-hidden className="px-3 font-mono font-medium">{row.content}</code>
      </div>
    );
  }

  const marker = row.kind === "added" ? "+" : row.kind === "removed" ? "−" : "";
  const label =
    row.kind === "added"
      ? `Added line ${row.newLine}: ${row.content}`
      : row.kind === "removed"
        ? `Removed line ${row.oldLine}: ${row.content}`
        : `Unchanged line ${row.oldLine}: ${row.content}`;
  return (
    <div
      role="row"
      aria-rowindex={rowIndex + 2}
      aria-label={label}
      className={cn(
        "grid min-h-6 grid-cols-[3.5rem_3.5rem_2rem_minmax(0,1fr)] border-b border-border/70",
        row.kind === "added" && "bg-success-soft text-success-soft-foreground",
        row.kind === "removed" && "bg-destructive-soft text-destructive-soft-foreground",
      )}
    >
      <span aria-hidden className="select-none px-2 text-right tabular-nums text-foreground-muted">{row.oldLine ?? ""}</span>
      <span aria-hidden className="select-none border-l border-border/70 px-2 text-right tabular-nums text-foreground-muted">{row.newLine ?? ""}</span>
      <span aria-hidden className="select-none border-l border-border/70 text-center font-semibold">{marker}</span>
      <code aria-hidden className="whitespace-pre border-l border-border/70 px-3 font-mono text-foreground">{row.content || " "}</code>
    </div>
  );
}

function DiffLoading() {
  return (
    <LoadingState label="Loading document changes" className="min-h-0 flex-1 bg-surface p-4">
      <div className="space-y-2">
        {Array.from({ length: 12 }, (_, index) => (
          <Skeleton key={index} className={cn("h-5 rounded-[var(--radius-sm)]", index % 4 === 0 ? "w-4/5" : "w-full")} />
        ))}
      </div>
    </LoadingState>
  );
}

function DiffState({
  icon: Icon,
  title,
  description,
  primaryLabel,
  onPrimary,
  secondaryLabel,
  onSecondary,
}: {
  icon: typeof FileWarning;
  title: string;
  description: string;
  primaryLabel: string;
  onPrimary: () => void;
  secondaryLabel?: string;
  onSecondary?: () => void;
}) {
  return (
    <div className="flex min-h-0 flex-1 items-center justify-center bg-surface px-5 py-10">
      <div className="w-full max-w-xl text-center">
        <span className="mx-auto inline-flex h-11 w-11 items-center justify-center rounded-[var(--radius-lg)] border border-border bg-surface-2 text-foreground-muted">
          <Icon className="h-5 w-5" aria-hidden />
        </span>
        <h3 className="mt-4 font-display text-base font-semibold text-foreground">{title}</h3>
        <p className="mx-auto mt-1.5 max-w-lg text-sm leading-relaxed text-foreground-muted">{description}</p>
        <div className="mt-5 flex flex-wrap justify-center gap-2">
          <Button type="button" variant="outline" size="sm" onClick={onPrimary}>
            <ArrowLeft className="h-3.5 w-3.5" aria-hidden />
            {primaryLabel}
          </Button>
          {secondaryLabel && onSecondary && (
            <Button type="button" variant="ghost" size="sm" onClick={onSecondary}>
              {secondaryLabel}
            </Button>
          )}
        </div>
      </div>
    </div>
  );
}

function shortRevision(revision: string) {
  return revision.slice(0, 7);
}

function formatCount(value: number) {
  return new Intl.NumberFormat().format(value);
}

function formatBytes(bytes: number) {
  if (bytes < 1024) return `${bytes} ${bytes === 1 ? "Byte" : "Bytes"}`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}
