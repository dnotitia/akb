import { useState } from "react";
import { Check, Copy, GitCompareArrows } from "lucide-react";
import { Alert } from "@/components/ui/alert";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Textarea } from "@/components/ui/textarea";
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { ConfirmDialog } from "@/components/ui/confirm-dialog";
import { planDocumentConflict, type DocumentConflictField, type DocumentConflictResolution, type DocumentConflictResult, type DocumentConflictSnapshot } from "@/lib/document-conflict";

export type { DocumentConflictSnapshot } from "@/lib/document-conflict";

interface DocumentConflictNoticeProps {
  base: DocumentConflictSnapshot;
  local: DocumentConflictSnapshot;
  latest: DocumentConflictSnapshot | null;
  latestError?: string;
  loading: boolean;
  newerRevision?: string | null;
  accessError?: string;
  checkingAccess?: boolean;
  onCheckAccess?: () => void;
  resolution?: DocumentConflictResolution;
  onResolutionChange?: (resolution: DocumentConflictResolution) => void;
  onApply: (result: DocumentConflictResult) => void;
  onRetryLatest: () => void;
  onCancel: () => void;
}

function Snapshot({ snapshot }: { snapshot: DocumentConflictSnapshot }) {
  const [copied, setCopied] = useState(false);
  const [copyError, setCopyError] = useState(false);

  async function copySnapshot() {
    try {
      if (!navigator.clipboard) throw new Error("Clipboard unavailable");
      await navigator.clipboard.writeText(snapshot.body);
      setCopied(true);
      setCopyError(false);
      window.setTimeout(() => setCopied(false), 2_000);
    } catch {
      setCopyError(true);
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
      {copyError && <p role="status" className="mt-1 text-xs text-foreground-muted">Clipboard unavailable. Select the preserved text to copy it.</p>}
      {snapshot.details && <details className="mt-2 text-xs">
        <summary className="cursor-pointer font-medium text-link focus-ring-instant">Document details</summary>
        <dl className="mt-2 space-y-1.5 break-words text-foreground-muted">
          {Object.entries(snapshot.details).map(([key, value]) => <div key={key}>
            <dt className="font-medium capitalize">{key}</dt>
            <dd>{(Array.isArray(value) ? value.join(", ") : value) || "Not set"}</dd>
          </div>)}
        </dl>
      </details>}
    </div>
  );
}

export function DocumentConflictNotice({
  base,
  local,
  latest,
  latestError,
  loading,
  newerRevision,
  accessError,
  checkingAccess = false,
  onCheckAccess,
  resolution,
  onResolutionChange,
  onApply,
  onRetryLatest,
  onCancel,
}: DocumentConflictNoticeProps) {
  const plan = planDocumentConflict(base, local, latest ?? local);
  // Production editors own the resolution alongside the reviewed snapshot.
  // Hiding/remounting this presentation must not discard composed text.
  const [internalResolution, setInternalResolution] = useState<DocumentConflictResolution>(() => ({ result: plan.result, reviewed: [] }));
  const { result, reviewed } = resolution ?? internalResolution;
  const [confirmRefresh, setConfirmRefresh] = useState(false);
  const unresolved = plan.conflicts.filter((field) => !reviewed.includes(field));
  const candidateChanged = JSON.stringify(result) !== JSON.stringify(plan.result) || reviewed.length > 0;
  const updateResult = (next: DocumentConflictResult, field: DocumentConflictField) => {
    if (loading) return;
    const nextResolution = { result: next, reviewed: reviewed.includes(field) ? reviewed : [...reviewed, field] };
    if (onResolutionChange) onResolutionChange(nextResolution);
    else setInternalResolution(nextResolution);
  };
  const refresh = () => {
    if (candidateChanged) setConfirmRefresh(true);
    else onRetryLatest();
  };
  const detailConflicts = plan.conflicts.filter((field) => field !== "title" && field !== "body");

  return (
    <>
    <Dialog open onOpenChange={(open) => { if (!open) onCancel(); }}>
    <DialogContent className="flex max-h-[calc(100dvh-2rem)] max-w-5xl flex-col overflow-hidden p-0">
      <DialogHeader className="shrink-0 border-b border-border p-4 pr-12 sm:p-6">
        <DialogTitle>Review document changes</DialogTitle>
        <DialogDescription>Your original draft is preserved. Combine changes against the displayed server version; applying the result does not save it.</DialogDescription>
      </DialogHeader>
      <div className="min-h-0 overflow-y-auto p-4 sm:p-6">
      <Alert variant="warning" title="This document changed on the server" role="status" aria-live="polite">
        Review the original, your draft, and the server version before applying a combined result. No automatic text merge is performed.
      </Alert>
      {accessError && <Alert variant="warning" title="Access needs verification" role="status" aria-live="polite" className="mt-3">
        <p>{accessError} Your combined result is preserved. Applying is disabled until access is verified.</p>
        {onCheckAccess && <Button type="button" variant="outline" size="sm" className="mt-2" onClick={onCheckAccess} loading={checkingAccess}>Check access</Button>}
      </Alert>}
      {newerRevision && newerRevision !== latest?.commit && <Alert variant="info" className="mt-3">
        The server changed again. This comparison stays pinned to the version shown below. Reload the comparison, or finish it and let the save check reject any newer change.
      </Alert>}
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
      {latest?.commit && <div className="mt-5 space-y-4 border-t border-border pt-4">
        <h3 className="text-sm font-semibold">Combined result</h3>
        <p className="text-sm text-foreground-muted">Independent fields are combined. Fields changed differently on both sides start with the server value and require an explicit choice or edit.</p>
        <div className="space-y-2">
          <Label htmlFor="conflict-result-title">Combined title</Label>
          <Input id="conflict-result-title" value={result.title} disabled={loading} onChange={(event) => updateResult({ ...result, title: event.target.value }, "title")} />
          {plan.conflicts.includes("title") && <div className="flex flex-wrap gap-2">
            <Button size="sm" variant="outline" disabled={loading} onClick={() => updateResult({ ...result, title: latest.title }, "title")}>Use latest title</Button>
            <Button size="sm" variant="outline" disabled={loading} onClick={() => updateResult({ ...result, title: local.title }, "title")}>Use my title</Button>
          </div>}
        </div>
        {detailConflicts.map((field) => {
          const key = field as keyof DocumentConflictResult["details"];
          return <fieldset key={field} disabled={loading} className="space-y-2 rounded-[var(--radius-sm)] border border-border p-3">
            <legend className="px-1 text-sm font-medium">Choose {field}</legend>
            <p className="break-words text-xs text-foreground-muted">Latest: {String(latest.details?.[key] ?? "Not set")} · Mine: {String(local.details?.[key] ?? "Not set")}</p>
            <div className="flex flex-wrap gap-2">
              <Button size="sm" variant="outline" onClick={() => updateResult({ ...result, details: { ...result.details, [key]: latest.details?.[key] ?? result.details[key] } }, field)}>Use latest {field}</Button>
              <Button size="sm" variant="outline" onClick={() => updateResult({ ...result, details: { ...result.details, [key]: local.details?.[key] ?? result.details[key] } }, field)}>Use my {field}</Button>
            </div>
          </fieldset>;
        })}
        <div className="space-y-2">
          <Label htmlFor="conflict-result-body">Combined document body</Label>
          {plan.conflicts.includes("body") && <p className="text-sm text-foreground-muted">Both versions changed the body. Edit this result to include the changes you want to keep, or explicitly keep the latest body and discard your body changes.</p>}
          <Textarea id="conflict-result-body" rows={10} value={result.body} disabled={loading} className="font-mono text-sm" onChange={(event) => updateResult({ ...result, body: event.target.value }, "body")} />
          {plan.conflicts.includes("body") && <Button size="sm" variant="outline" disabled={loading} onClick={() => updateResult({ ...result, body: latest.body }, "body")}>Keep latest body</Button>}
        </div>
        {unresolved.length > 0 && <p role="status" className="text-sm text-warning">Review before applying: {unresolved.join(", ")}.</p>}
      </div>}
      </div>
      <DialogFooter data-slot="document-conflict-actions" className="shrink-0 border-t border-border p-4 sm:p-6">
        <Button type="button" variant="outline" className="focus-ring-instant" onClick={onCancel}>Keep editing</Button>
        <Button type="button" variant="outline" className="focus-ring-instant" onClick={refresh} disabled={loading || !!accessError} loading={loading}>{latest ? "Reload comparison" : "Try loading latest"}</Button>
        <Button type="button" className="focus-ring-instant" onClick={() => onApply(result)} disabled={!latest?.commit || loading || !!accessError || unresolved.length > 0 || !result.title.trim()}>Apply combined changes</Button>
      </DialogFooter>
    </DialogContent>
    </Dialog>
    <ConfirmDialog open={confirmRefresh} onOpenChange={setConfirmRefresh} title="Reload this comparison?"
      description="This replaces the in-progress combined result with a fresh comparison. Your original editor draft remains unchanged."
      confirmLabel="Reload comparison" confirmDisabled={loading || !!accessError} onConfirm={() => { setConfirmRefresh(false); onRetryLatest(); }} />
    </>
  );
}
