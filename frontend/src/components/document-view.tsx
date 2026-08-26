import { useMemo, useState } from "react";
import { Check, Code2, Copy, Eye, Loader2, Pencil } from "lucide-react";
import { useQuery } from "@tanstack/react-query";
import { getDocument } from "@/lib/api";
import { MarkdownRender } from "@/components/markdown-render";
import { Alert } from "@/components/ui/alert";
import { TooltipText } from "@/components/ui/tooltip-text";
import { cn } from "@/lib/utils";

type ViewMode = "rendered" | "raw";

interface DocumentViewProps {
  vault: string;
  docId: string;
  /** Controlled view — provide this + onViewChange to sync with URL params */
  view?: ViewMode;
  onViewChange?: (next: ViewMode) => void;
  /**
   * Optional extra segmented-control tab appended after RENDERED/RAW.
   * The parent owns the click handler — DocumentView does not switch
   * its own view state when the extra tab is clicked. Used by
   * DocumentPage to inject the body-editor entry point without
   * folding the editor into this read-focused component.
   */
  extraTab?: { label: string; onClick: () => void };
  /**
   * Optional git commit hash. When set, the body is fetched at that
   * commit via getDocument(..., version) and the queryKey carries the
   * hash so commit-log / history selections render the historical body
   * instead of HEAD. Parent (DocumentPage) reads it from ?commit= URL
   * state; uncontrolled callers (VaultSkillPage) omit it and get HEAD.
   */
  version?: string;
  /** Framed file-viewer treatment used by the Vault document workspace. */
  appearance?: "plain" | "file";
}

/**
 * Self-sufficient doc body: fetches the document, renders the
 * rendered/raw segmented control, and shows the markdown content.
 *
 * Query key is ["document", vault, docId, version] — matches DocumentPage
 * exactly so TanStack Query dedupes when both are mounted. Without
 * `version` in the key, historical-view URLs would render HEAD because
 * the un-versioned key collides with DocumentPage's versioned fetch
 * and serves whichever landed first.
 *
 * view/onViewChange are optional: when omitted, DocumentView manages
 * its own local toggle state (useful for embeds). When provided, the
 * caller drives view state (DocumentPage uses this to sync ?view= URL
 * params).
 */
export function DocumentView({
  vault,
  docId,
  view: viewProp,
  onViewChange,
  extraTab,
  version,
  appearance = "plain",
}: DocumentViewProps) {
  const [localView, setLocalView] = useState<ViewMode>("rendered");

  // Controlled vs. uncontrolled view mode
  const view = viewProp ?? localView;
  const setView = (next: ViewMode) => {
    if (onViewChange) {
      onViewChange(next);
    } else {
      setLocalView(next);
    }
  };

  const { data: doc, isLoading, error } = useQuery({
    queryKey: ["document", vault, docId, version],
    queryFn: () => getDocument(vault, docId, version),
    enabled: !!vault && !!docId,
    retry: false,
  });

  const [copiedRaw, setCopiedRaw] = useState(false);
  const contentStats = useMemo(
    () => getDocumentStats(doc?.content || ""),
    [doc?.content],
  );

  async function copyRaw() {
    try {
      await navigator.clipboard.writeText(doc?.content || "");
      setCopiedRaw(true);
      setTimeout(() => setCopiedRaw(false), 1500);
    } catch {
      // clipboard API may be unavailable; silently no-op
    }
  }

  if (isLoading) {
    return (
      <div className="py-8 coord" role="status" aria-live="polite">
        <Loader2 className="h-4 w-4 inline animate-spin mr-2" aria-hidden />
        Loading…
      </div>
    );
  }

  if (error || !doc) {
    return (
      <Alert variant="destructive" className="my-4">
        Couldn't load this document body.
        {error instanceof Error ? ` ${error.message}` : ""}
      </Alert>
    );
  }

  const fileAppearance = appearance === "file";

  return (
    <section
      aria-label="Document content"
      className={cn(
        fileAppearance &&
          "overflow-hidden rounded-[var(--radius-lg)] border border-border bg-surface shadow-sm",
      )}
    >
      {/* ── Rendered/Raw segmented control ────────────────────────
         WAI-ARIA tabs pattern: ArrowLeft/ArrowRight (and Home/End)
         move focus between tabs; Enter/Space activates. Each tab
         points at its panel via aria-controls so screen readers
         announce the relationship. The extra tab (e.g. EDIT) is
         a navigation trigger, not a panel, so it owns no panel id. */}
      <TabStrip
        view={view}
        onSelect={setView}
        extraTab={extraTab}
        appearance={appearance}
        copiedRaw={copiedRaw}
        onCopyRaw={copyRaw}
        lineCount={contentStats.lineCount}
        byteCount={contentStats.byteCount}
        summary={doc.summary}
      />

      {/* ── Doc body ──────────────────────────────────────────────── */}
      {view === "rendered" ? (
        <div
          id="docview-panel-rendered"
          role="tabpanel"
          aria-labelledby="docview-tab-rendered"
          className={cn(
            "min-w-0",
            fileAppearance && "min-h-80 px-5 py-7 sm:px-8 sm:py-9 lg:px-10",
          )}
          style={{ maxWidth: "100%" }}
        >
          <MarkdownRender
            markdown={doc.content || ""}
            className={fileAppearance ? "document-reading-flow" : undefined}
            assetContext={{
              mode: "authenticated",
              vault,
              document: doc.path,
              commit: doc.current_commit,
            }}
          />
        </div>
      ) : (
        <div
          id="docview-panel-raw"
          role="tabpanel"
          aria-labelledby="docview-tab-raw"
          className={cn(
            "relative",
            fileAppearance && "min-h-80 bg-surface-muted/40 p-4 sm:p-6",
          )}
        >
          {!fileAppearance && (
            <button
              type="button"
              onClick={copyRaw}
              aria-label={copiedRaw ? "Markdown copied" : "Copy markdown"}
              className="absolute top-2 right-2 inline-flex items-center gap-1 px-2 py-1 text-[11px] font-medium text-foreground-muted hover:text-link border border-border bg-surface rounded-[var(--radius-sm)] transition-colors cursor-pointer focus:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2 focus-visible:ring-offset-background"
            >
              {copiedRaw ? "Copied" : "Copy"}
            </button>
          )}
          <pre
            data-testid="doc-raw"
            className={cn(
              "font-mono text-[13px] leading-[1.65] whitespace-pre-wrap overflow-x-auto",
              fileAppearance
                ? "m-0 min-h-64 wrap-anywhere text-foreground"
                : "bg-surface-muted p-4 border border-border rounded-[var(--radius-lg)]",
            )}
          >
            {doc.content || ""}
          </pre>
        </div>
      )}
    </section>
  );
}

// ── Segmented control with WAI-ARIA tabs keyboard handling ──────
interface TabStripProps {
  view: ViewMode;
  onSelect: (next: ViewMode) => void;
  extraTab?: { label: string; onClick: () => void };
  appearance: "plain" | "file";
  copiedRaw: boolean;
  onCopyRaw: () => void;
  lineCount: number;
  byteCount: number;
  summary?: string | null;
}

function TabStrip({
  view,
  onSelect,
  extraTab,
  appearance,
  copiedRaw,
  onCopyRaw,
  lineCount,
  byteCount,
  summary,
}: TabStripProps) {
  const tabs: Array<{
    key: ViewMode | "extra";
    label: string;
    selected: boolean;
    onActivate: () => void;
    icon: typeof Eye;
  }> = [
    { key: "rendered", label: "Rendered", selected: view === "rendered", onActivate: () => onSelect("rendered"), icon: Eye },
    { key: "raw", label: "Raw", selected: view === "raw", onActivate: () => onSelect("raw"), icon: Code2 },
  ];
  if (extraTab) {
    tabs.push({ key: "extra", label: extraTab.label, selected: false, onActivate: extraTab.onClick, icon: Pencil });
  }

  function onKey(e: React.KeyboardEvent<HTMLButtonElement>, idx: number) {
    let next: number;
    if (e.key === "ArrowRight") next = (idx + 1) % tabs.length;
    else if (e.key === "ArrowLeft") next = (idx - 1 + tabs.length) % tabs.length;
    else if (e.key === "Home") next = 0;
    else if (e.key === "End") next = tabs.length - 1;
    else return;
    e.preventDefault();
    const target = e.currentTarget.parentElement?.children[next] as HTMLElement | undefined;
    target?.focus();
  }

  const fileAppearance = appearance === "file";

  return (
    <div
      className={cn(
        "flex items-center",
        fileAppearance
          ? "min-h-11 flex-wrap justify-end gap-2 border-b border-border bg-surface-2/60 px-3 py-1.5 sm:gap-3"
          : "justify-end mb-3",
      )}
    >
      {fileAppearance && (
        <>
          <div className="mr-auto flex min-w-0 flex-1 items-center gap-3">
            <div
              aria-label={`Document statistics: ${formatLineCount(lineCount)}, ${formatByteSize(byteCount)}`}
              className="inline-flex min-h-8 shrink-0 items-center gap-2 text-xs tabular-nums text-foreground-muted"
            >
              <span>{formatLineCount(lineCount)}</span>
              <span aria-hidden>·</span>
              <span>{formatByteSize(byteCount)}</span>
            </div>
            {summary && (
              <div
                role="note"
                aria-label="Document summary"
                className="hidden min-w-0 flex-1 items-center gap-2 border-l border-border pl-3 lg:flex"
              >
                <span className="coord shrink-0 font-medium text-foreground-muted">
                  Summary
                </span>
                <TooltipText
                  as="p"
                  tip={summary}
                  className="truncate text-xs leading-relaxed text-foreground-muted"
                >
                  {summary}
                </TooltipText>
              </div>
            )}
          </div>
          <button
            type="button"
            onClick={onCopyRaw}
            aria-label={copiedRaw ? "Markdown copied" : "Copy markdown"}
            className="inline-flex h-8 w-8 shrink-0 cursor-pointer items-center justify-center gap-1.5 rounded-[var(--radius-md)] border border-border bg-surface text-xs font-medium text-foreground-muted transition-token hover:border-border-strong hover:bg-surface-hover hover:text-link focus:outline-none focus-visible:ring-2 focus-visible:ring-ring sm:w-auto sm:px-2.5"
          >
            {copiedRaw ? (
              <Check className="h-3.5 w-3.5 text-success" aria-hidden />
            ) : (
              <Copy className="h-3.5 w-3.5" aria-hidden />
            )}
            <span className="hidden sm:inline">
              {copiedRaw ? "Copied" : "Copy"}
            </span>
          </button>
        </>
      )}
      <div
        role="tablist"
        aria-label="Document view"
        className={cn(
          "inline-flex items-center gap-1",
          fileAppearance
            ? "bg-transparent p-0"
            : "rounded-[var(--radius-md)] bg-surface-2 p-1",
        )}
      >
        {tabs.map((t, i) => {
          const isPanelTab = t.key !== "extra";
          const Icon = t.icon;
          const cls = cn(
            "inline-flex items-center gap-1.5 px-3 py-1 text-xs font-medium rounded-[var(--radius-sm)] transition-token cursor-pointer focus:outline-none focus-visible:ring-2 focus-visible:ring-ring",
            t.selected
              ? fileAppearance
                ? "bg-surface-selected text-surface-selected-foreground"
                : "bg-surface text-foreground shadow-sm"
              : "text-foreground-muted hover:bg-surface-hover hover:text-foreground",
          );
          return (
            <button
              key={t.key}
              role="tab"
              id={isPanelTab ? `docview-tab-${t.key}` : undefined}
              aria-selected={t.selected}
              aria-controls={isPanelTab ? `docview-panel-${t.key}` : undefined}
              tabIndex={t.selected || (!tabs.some((x) => x.selected) && i === 0) ? 0 : -1}
              onClick={t.onActivate}
              onKeyDown={(e) => onKey(e, i)}
              className={cls}
            >
              {fileAppearance && <Icon className="h-3.5 w-3.5" aria-hidden />}
              {t.label}
            </button>
          );
        })}
      </div>
    </div>
  );
}

function getDocumentStats(content: string) {
  const normalized = content.replace(/\r\n?/g, "\n");
  const withoutTerminalNewline = normalized.endsWith("\n")
    ? normalized.slice(0, -1)
    : normalized;

  return {
    lineCount:
      normalized.length === 0 ? 0 : withoutTerminalNewline.split("\n").length,
    byteCount: new TextEncoder().encode(content).byteLength,
  };
}

function formatLineCount(count: number) {
  return `${count} ${count === 1 ? "line" : "lines"}`;
}

function formatByteSize(bytes: number) {
  if (bytes < 1024) return `${bytes} ${bytes === 1 ? "Byte" : "Bytes"}`;
  if (bytes < 1024 * 1024) return `${formatUnit(bytes / 1024)} KB`;
  return `${formatUnit(bytes / (1024 * 1024))} MB`;
}

function formatUnit(value: number) {
  return value >= 100 ? value.toFixed(0) : value >= 10 ? value.toFixed(1) : value.toFixed(2);
}
