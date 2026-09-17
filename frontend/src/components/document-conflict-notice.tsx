import { useState } from "react";
import { Check, Copy, GitCompareArrows } from "lucide-react";
import { Alert } from "@/components/ui/alert";
import { Button } from "@/components/ui/button";
import { cn } from "@/lib/utils";

export interface DocumentConflictSnapshot {
  label: string;
  commit: string | null;
  title: string;
  body: string;
}

interface DocumentConflictNoticeProps {
  base: DocumentConflictSnapshot;
  local: DocumentConflictSnapshot;
  latest: DocumentConflictSnapshot | null;
  latestError?: string;
  rebasing: boolean;
  onRebase: () => void;
  onRetryLatest: () => void;
}

function Snapshot({ snapshot }: { snapshot: DocumentConflictSnapshot }) {
  const [copied, setCopied] = useState(false);

  async function copySnapshot() {
    try {
      await navigator.clipboard?.writeText(snapshot.body);
      setCopied(true);
      window.setTimeout(() => setCopied(false), 2_000);
    } catch {
      // The visible pre remains selectable when clipboard access is blocked.
    }
  }

  return (
    <div
      data-conflict-snapshot
      className="min-w-0 rounded-[var(--radius-md)] border border-border bg-surface p-3"
    >
      <div className="flex items-start justify-between gap-2">
        <div className="min-w-0">
          <p
            data-conflict-metadata="label"
            className="text-xs font-semibold text-foreground"
          >
            {snapshot.label}
          </p>
          <p data-conflict-metadata="title" className="mt-0.5 text-xs text-foreground-muted">
            Title: <span className="text-foreground">{snapshot.title || "(untitled)"}</span>
          </p>
          <p data-conflict-metadata="revision" className="text-xs text-foreground-muted">
            Revision: <code className="break-all font-mono text-foreground">
              {snapshot.commit || "unavailable"}
            </code>
          </p>
        </div>
        <Button
          type="button"
          variant="ghost"
          size="sm"
          className="shrink-0 focus-ring-instant"
          onClick={() => void copySnapshot()}
          aria-label={`Copy ${snapshot.label} markdown`}
        >
          {copied ? <Check className="h-3.5 w-3.5 text-success" aria-hidden /> : <Copy className="h-3.5 w-3.5" aria-hidden />}
          {copied ? "Copied" : "Copy"}
        </Button>
      </div>
      <pre className="mt-2 max-h-32 overflow-auto whitespace-pre-wrap break-words rounded-[var(--radius-sm)] bg-surface-2 p-2 font-mono text-xs leading-relaxed text-foreground">
        {snapshot.body || "(empty)"}
      </pre>
    </div>
  );
}

export function DocumentConflictNotice({
  base,
  local,
  latest,
  latestError,
  rebasing,
  onRebase,
  onRetryLatest,
}: DocumentConflictNoticeProps) {
  return (
    <Alert variant="warning" title="This document changed on the server" className="mt-4">
      <p>
        Your draft is still protected. Review the original base, your local draft, and the latest server version before choosing a new base.
      </p>
      <div className="mt-3 @container">
        <div className="grid gap-3 @lg:grid-cols-2 @2xl:grid-cols-3">
          <Snapshot snapshot={base} />
          <Snapshot snapshot={local} />
          {latest ? (
            <Snapshot snapshot={latest} />
          ) : (
            <div
              data-conflict-snapshot
              className="min-w-0 rounded-[var(--radius-md)] border border-border bg-surface p-3"
            >
              <p
                data-conflict-metadata="label"
                className="flex items-center gap-2 text-xs font-semibold text-foreground"
              >
                <GitCompareArrows className="h-3.5 w-3.5 text-warning" aria-hidden />
                Latest server version
              </p>
              <p className="mt-2 text-xs text-foreground-muted">
                {latestError || "The latest version could not be loaded. Your draft remains available."}
              </p>
            </div>
          )}
        </div>
      </div>
      <div className="mt-3 flex flex-wrap items-center gap-2">
        <Button
          type="button"
          variant="default"
          size="sm"
          className="shrink-0 focus-ring-instant"
          onClick={onRebase}
          disabled={!latest?.commit || rebasing}
          loading={rebasing}
        >
          Apply draft to latest
        </Button>
        {!latest && (
          <Button
            type="button"
            variant="outline"
            size="sm"
            className="shrink-0 focus-ring-instant"
            onClick={onRetryLatest}
            disabled={rebasing}
          >
            Try loading latest
          </Button>
        )}
      </div>
      <p className={cn("mt-2 text-xs text-warning-soft-foreground", !latest && "font-medium")}>
        Applying the draft to latest is explicit; saving will still be rejected if the server changes again.
      </p>
    </Alert>
  );
}
