import { useEffect, useRef, useState, type ComponentProps } from "react";
import { Check, Copy } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Dialog, DialogContent, DialogDescription, DialogTitle } from "@/components/ui/dialog";
import { Tooltip, TooltipContent, TooltipProvider, TooltipTrigger } from "@/components/ui/tooltip";
import { cn, timeAgo } from "@/lib/utils";
import { formatByteSize, formatLineCount, getDocumentStats } from "@/lib/document-statistics";

export function DocumentTimestamp({ value, label = "Last edited", compact = false }: {
  value?: string | null; label?: string; compact?: boolean;
}) {
  const date = value ? new Date(value) : null;
  if (!date || Number.isNaN(date.getTime())) return compact ? null : <span>Not available</span>;
  const exact = date.toLocaleString(undefined, { year: "numeric", month: "short", day: "numeric", hour: "numeric", minute: "2-digit", second: "2-digit", timeZoneName: "short" });
  if (!compact) return <time dateTime={date.toISOString()}>{exact}</time>;
  return <TooltipProvider delayDuration={250}><Tooltip>
    <TooltipTrigger asChild>
      <time dateTime={date.toISOString()} tabIndex={0}
        className="min-w-0 truncate @[48rem]/reader:shrink-0 rounded-[var(--radius-sm)] text-xs text-foreground-muted focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring">
        <span aria-hidden>{label} {timeAgo(value)}</span>
        <span className="sr-only">{label}: {exact}</span>
      </time>
    </TooltipTrigger>
    <TooltipContent side="bottom">{label}: {exact}</TooltipContent>
  </Tooltip></TooltipProvider>;
}

export function DocumentStatistics({ content, labelled = true }: { content: string; labelled?: boolean }) {
  const { lineCount, byteCount } = getDocumentStats(content);
  const label = `${formatLineCount(lineCount)}, ${formatByteSize(byteCount)}`;
  return <span aria-label={labelled ? `Document statistics: ${label}` : undefined} className="shrink-0 whitespace-nowrap text-xs tabular-nums text-foreground-muted">
    {formatLineCount(lineCount)} <span aria-hidden>·</span> {formatByteSize(byteCount)}
  </span>;
}

export function DocumentSummary({ summary }: { summary?: string | null }) {
  const [open, setOpen] = useState(false);
  if (!summary) return null;
  return <>
    <button type="button" aria-label="Read document summary" onClick={() => setOpen(true)}
      className="block max-w-full truncate rounded-[var(--radius-sm)] text-left text-xs text-foreground-muted hover:text-link focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring">
      <span className="font-medium">Summary:</span> {summary}
    </button>
    <Dialog open={open} onOpenChange={setOpen}>
      <DialogContent>
        <DialogTitle>Document summary</DialogTitle>
        <DialogDescription className="whitespace-pre-wrap break-words leading-relaxed">{summary}</DialogDescription>
      </DialogContent>
    </Dialog>
  </>;
}

export function DocumentIconButton({ label, children, ...props }: Omit<ComponentProps<typeof Button>, "variant" | "size" | "aria-label"> & { label: string }) {
  return <TooltipProvider delayDuration={300}><Tooltip>
    <TooltipTrigger asChild>
      <Button {...props} type="button" variant="ghost" size="icon" data-reader-control data-reader-icon aria-label={label}
        className={cn("text-foreground-muted hover:text-foreground", props.className)}>{children}</Button>
    </TooltipTrigger>
    <TooltipContent side="bottom">{label}</TooltipContent>
  </Tooltip></TooltipProvider>;
}

export function DocumentCopyButton({ content }: { content: string }) {
  const [copied, setCopied] = useState(false);
  const [failed, setFailed] = useState(false);
  const resetTimer = useRef<number | undefined>(undefined);
  useEffect(() => () => window.clearTimeout(resetTimer.current), []);
  async function copy() {
    try {
      await navigator.clipboard.writeText(content);
      setCopied(true);
      window.clearTimeout(resetTimer.current);
      resetTimer.current = window.setTimeout(() => setCopied(false), 1500);
    } catch { setFailed(true); }
  }
  return <>
    <DocumentIconButton label={copied ? "Markdown copied" : "Copy markdown"} onClick={() => void copy()}>
      {copied ? <Check className="h-4 w-4 text-success" aria-hidden /> : <Copy className="h-4 w-4" aria-hidden />}
    </DocumentIconButton>
    <Dialog open={failed} onOpenChange={setFailed}>
      <DialogContent>
        <DialogTitle>Copy markdown</DialogTitle>
        <DialogDescription>Clipboard access is unavailable. Select and copy the text below.</DialogDescription>
        <textarea readOnly aria-label="Markdown to copy" value={content} onFocus={event => event.currentTarget.select()}
          className="h-56 w-full rounded-[var(--radius-sm)] border border-border bg-surface p-3 font-mono text-sm focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring" />
      </DialogContent>
    </Dialog>
  </>;
}

export function DocumentReadModes({ view, onChange, idPrefix }: {
  view: "rendered" | "raw"; onChange: (view: "rendered" | "raw") => void; idPrefix: string;
}) {
  return <div role="tablist" aria-label="Document view" className="document-read-modes inline-flex shrink-0 items-center gap-0.5 rounded-[var(--radius-sm)] bg-surface-2 ring-1 ring-inset ring-border">
    {(["rendered", "raw"] as const).map((mode, index) => <button key={mode} type="button" role="tab"
      id={`${idPrefix}-tab-${mode}`} aria-selected={view === mode} aria-controls={`${idPrefix}-panel-${mode}`}
      aria-label={mode === "rendered" ? "Preview" : "Raw"}
      tabIndex={view === mode ? 0 : -1} onClick={() => onChange(mode)}
      onKeyDown={event => {
        const next = event.key === "Home" ? 0 : event.key === "End" ? 1 : ["ArrowLeft", "ArrowRight"].includes(event.key) ? 1 - index : null;
        if (next === null) return;
        event.preventDefault();
        (event.currentTarget.parentElement?.children[next] as HTMLElement)?.focus();
      }}
      data-reader-control
      className={cn("relative inline-flex items-center justify-center px-2.5 font-medium transition-token cursor-pointer focus-visible:z-10 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring",
        view === mode ? "bg-surface-selected text-surface-selected-foreground ring-1 ring-inset ring-border-strong" : "text-foreground-muted hover:bg-surface-hover hover:text-foreground")}>
      {mode === "rendered" ? "Preview" : "Raw"}
    </button>)}
  </div>;
}
