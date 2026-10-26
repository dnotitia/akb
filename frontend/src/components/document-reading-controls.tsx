import { useEffect, useImperativeHandle, useRef, useState, type ComponentProps, type Ref, type RefObject } from "react";
import { Check, Code2, Copy, Eye } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Dialog, DialogContent, DialogDescription, DialogTitle } from "@/components/ui/dialog";
import { Tooltip, TooltipContent, TooltipProvider, TooltipTrigger } from "@/components/ui/tooltip";
import { cn, timeAgo } from "@/lib/utils";
import { formatByteSize, formatLineCount, getDocumentStats } from "@/lib/document-statistics";

export { DocumentSummary } from "@/components/document-summary";

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
        className="min-w-0 truncate @[48rem]/reader:shrink-0 rounded-[var(--radius-sm)] text-sm text-foreground-muted focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring">
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

export function DocumentIconButton({ label, children, ...props }: Omit<ComponentProps<typeof Button>, "variant" | "size" | "aria-label"> & { label: string }) {
  return <TooltipProvider delayDuration={300}><Tooltip>
    <TooltipTrigger asChild>
      <Button {...props} type="button" variant="ghost" size="icon" data-reader-control data-reader-icon aria-label={label}
        className={cn("text-foreground-muted hover:text-foreground", props.className)}>{children}</Button>
    </TooltipTrigger>
    <TooltipContent side="bottom">{label}</TooltipContent>
  </Tooltip></TooltipProvider>;
}

export interface DocumentCopyHandle { copy(): void }

function canRestoreFocus(element: HTMLElement | null | undefined): element is HTMLElement {
  if (!element?.isConnected || element.closest("[hidden], [inert]")) return false;
  for (let current: HTMLElement | null = element; current; current = current.parentElement) {
    const style = window.getComputedStyle(current);
    if (style.display === "none" || style.visibility === "hidden") return false;
  }
  return true;
}

export function DocumentCopyButton({ content, actionRef, returnFocusRef, hidden = false, labelled = false, kind = "markdown", onCopied }: {
  content: string;
  actionRef?: Ref<DocumentCopyHandle>;
  returnFocusRef?: RefObject<HTMLElement | null>;
  hidden?: boolean;
  labelled?: boolean;
  kind?: "markdown" | "link";
  onCopied?: (label: string) => void;
}) {
  const [copied, setCopied] = useState(false);
  const [failed, setFailed] = useState(false);
  const copyOrigin = useRef<HTMLElement | null>(null);
  const resetTimer = useRef<number | undefined>(undefined);
  useEffect(() => () => window.clearTimeout(resetTimer.current), []);
  useImperativeHandle(actionRef, () => ({ copy: () => { void copy(returnFocusRef?.current ?? null); } }));
  async function copy(origin: HTMLElement | null) {
    copyOrigin.current = origin;
    try {
      await navigator.clipboard.writeText(content);
    } catch { setFailed(true); return; }
    setCopied(true);
    window.clearTimeout(resetTimer.current);
    resetTimer.current = window.setTimeout(() => setCopied(false), 1500);
    onCopied?.(copiedLabel);
  }
  const copyLabel = kind === "link" ? "Copy document link" : "Copy markdown";
  const copiedLabel = kind === "link" ? "Link copied" : "Markdown copied";
  const manualLabel = kind === "link" ? "Document link to copy" : "Markdown to copy";
  const icon = copied ? <Check className="h-4 w-4 text-success" aria-hidden /> : <Copy className="h-4 w-4" aria-hidden />;
  return <>
    {labelled ? <Button type="button" variant="ghost" size="sm" hidden={hidden}
      data-reader-control data-reader-labelled aria-label={copied ? copiedLabel : copyLabel}
      className="document-copy-button document-copy-labelled text-foreground-muted hover:text-link"
      onClick={event => void copy(event.currentTarget)}>
      {icon}<span>{copied ? (kind === "link" ? "Link copied" : "Copied") : copyLabel}</span>
    </Button> : <DocumentIconButton label={copied ? copiedLabel : copyLabel} hidden={hidden}
      className="document-copy-button" onClick={event => void copy(event.currentTarget)}>
      {icon}
    </DocumentIconButton>}
    <Dialog open={failed} onOpenChange={setFailed}>
      <DialogContent onCloseAutoFocus={event => {
        const target = canRestoreFocus(copyOrigin.current) ? copyOrigin.current : returnFocusRef?.current;
        if (!canRestoreFocus(target)) return;
        event.preventDefault();
        target.focus();
      }}>
        <DialogTitle>{copyLabel}</DialogTitle>
        <DialogDescription>Clipboard access is unavailable. Select and copy the text below.</DialogDescription>
        <textarea readOnly aria-label={manualLabel} value={content} onFocus={event => event.currentTarget.select()}
          className="h-56 w-full rounded-[var(--radius-sm)] border border-border bg-surface p-3 font-mono text-sm focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring" />
      </DialogContent>
    </Dialog>
  </>;
}

export function DocumentReadModes({ view, onChange, idPrefix, contentId }: {
  view: "rendered" | "raw"; onChange: (view: "rendered" | "raw") => void; idPrefix: string; contentId?: string;
}) {
  const buttons = useRef<Array<HTMLButtonElement | null>>([]);
  const suppressTooltip = useRef(false);
  const [tooltipMode, setTooltipMode] = useState<"rendered" | "raw" | null>(null);
  const modes = [
    { value: "rendered" as const, label: "Preview", Icon: Eye },
    { value: "raw" as const, label: "Raw markdown", Icon: Code2 },
  ];
  return <TooltipProvider delayDuration={600} skipDelayDuration={0}>
    <div role="radiogroup" aria-label="Document view mode" className="document-read-modes shrink-0" data-mode={view}
      onKeyDown={event => {
        const direction = ["ArrowRight", "ArrowDown"].includes(event.key) ? 1 : ["ArrowLeft", "ArrowUp"].includes(event.key) ? -1 : 0;
        const next = event.key === "Home" ? 0 : event.key === "End" ? modes.length - 1
          : direction ? (modes.findIndex(mode => mode.value === view) + direction + modes.length) % modes.length : null;
        if (next === null) return;
        event.preventDefault();
        suppressTooltip.current = true;
        setTooltipMode(null);
        onChange(modes[next].value);
        buttons.current[next]?.focus();
      }}>
      {modes.map((mode, index) => <Tooltip key={mode.value} open={tooltipMode === mode.value}
        onOpenChange={open => {
          if (open && suppressTooltip.current) return;
          setTooltipMode(current => open ? mode.value : current === mode.value ? null : current);
        }}>
        <TooltipTrigger asChild onFocus={event => event.preventDefault()}
          onPointerEnter={() => { suppressTooltip.current = false; }}
          onPointerDown={() => { suppressTooltip.current = true; setTooltipMode(null); }}>
          <Button ref={element => { buttons.current[index] = element; }} type="button" variant="ghost" size="icon" role="radio"
            id={`${idPrefix}-mode-${mode.value}`} aria-label={mode.label} aria-checked={view === mode.value} aria-controls={contentId}
            tabIndex={view === mode.value ? 0 : -1} onClick={() => { suppressTooltip.current = true; setTooltipMode(null); onChange(mode.value); }} data-reader-control data-reader-icon>
            <mode.Icon className="h-4 w-4" aria-hidden />
          </Button>
        </TooltipTrigger>
        <TooltipContent side="bottom">{mode.label}</TooltipContent>
      </Tooltip>)}
    </div>
  </TooltipProvider>;
}
