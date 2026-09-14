import { useEffect, useState } from "react";
import {
  Copy,
  Eye,
  EyeOff,
  KeyRound,
  RotateCw,
  Trash2,
  X,
} from "lucide-react";
import { createPAT, revokePAT } from "@/lib/api";
import { formatDate, timeAgo } from "@/lib/utils";
import { Alert } from "@/components/ui/alert";
import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";
import { ConfirmDialog } from "@/components/ui/confirm-dialog";
import { EmptyState } from "@/components/empty-state";
import { ConnectionSetup } from "@/components/connection-setup";

export interface PAT {
  token_id: string;
  name: string;
  prefix: string;
  created_at?: string;
  last_used_at?: string;
}

interface Props {
  pats: PAT[] | null;
  patsError: boolean;
  mcpOauthEnabled: boolean;
  onReloadPats: () => void;
  onDirtyChange?: (dirty: boolean) => void;
  onBusyChange?: (busy: boolean) => void;
}

export function TokensSection({
  pats,
  patsError,
  mcpOauthEnabled,
  onReloadPats,
  onDirtyChange,
  onBusyChange,
}: Props) {
  const [newPat, setNewPat] = useState<string | null>(null);
  const [showPat, setShowPat] = useState<boolean>(true);
  const [copied, setCopied] = useState<string | null>(null);
  // Reissue = revoke-then-mint. Routed through a ConfirmDialog (the old token
  // dies immediately) with a per-row pending guard so a double-click can't
  // fire two revoke/mint pairs, and an error channel for the dangerous
  // half-failure where revoke lands but the replacement mint rejects.
  const [pendingReissue, setPendingReissue] = useState<PAT | null>(null);
  const [reissuingId, setReissuingId] = useState<string | null>(null);
  const [reissueError, setReissueError] = useState<string | null>(null);
  const [pendingRevokePat, setPendingRevokePat] = useState<PAT | null>(null);

  const [copyError, setCopyError] = useState(false);
  const [invalidatedTokenId, setInvalidatedTokenId] = useState<string>();
  const [setupSecret, setSetupSecret] = useState(false);
  const [setupBusy, setSetupBusy] = useState(false);
  useEffect(() => { onDirtyChange?.(!!newPat || setupSecret); }, [newPat, setupSecret, onDirtyChange]);
  useEffect(() => { onBusyChange?.(setupBusy || reissuingId !== null); }, [setupBusy, reissuingId, onBusyChange]);
  useEffect(() => () => { onDirtyChange?.(false); onBusyChange?.(false); }, [onDirtyChange, onBusyChange]);

  async function copy(text: string, label: string) {
    // clipboard is undefined on insecure (plain-HTTP) origins — and AKB ships
    // an `--insecure` snippet, so that deploy shape is real. Guard with `?.` so
    // copying a show-once secret never throws an uncaught TypeError with no
    // feedback; the value stays on screen to copy manually.
    try {
      setCopyError(false);
      if (!navigator.clipboard?.writeText) throw new Error("Clipboard unavailable");
      await navigator.clipboard.writeText(text);
      setCopied(label);
      setTimeout(() => setCopied(null), 2000);
    } catch {
      setCopyError(true);
    }
  }

  // Reissue = revoke the live token, then mint a replacement. Confirmed first
  // (the old token stops working the instant revoke lands). If the mint half
  // rejects after revoke succeeded, the deployed token is already gone — we
  // surface that explicitly instead of swallowing it.
  async function handleReissue(p: PAT) {
    setReissuingId(p.token_id);
    setReissueError(null);
    let revoked = false;
    try {
      await revokePAT(p.token_id);
      revoked = true;
      setInvalidatedTokenId(p.token_id);
      const r = await createPAT(p.name);
      setNewPat(r.token);
      setShowPat(true);
      onReloadPats();
    } catch {
      setReissueError(
        revoked ? `"${p.name}" was revoked, but replacement creation failed. Open connection setup to create a new token.` : `"${p.name}" could not be revoked. No replacement was created.`,
      );
      onReloadPats();
    } finally {
      setReissuingId(null);
    }
  }

  async function confirmRevokePat() {
    if (!pendingRevokePat) return;
    await revokePAT(pendingRevokePat.token_id);
    setInvalidatedTokenId(pendingRevokePat.token_id);
    onReloadPats();
  }

  return (
    <>
      <div className="space-y-8">
      {newPat && (
        <section
          className="overflow-hidden rounded-[var(--radius-lg)] border border-accent/40 bg-accent/5 shadow-sm xl:col-span-2"
          role="status"
          aria-live="polite"
        >
          <div className="flex flex-wrap items-start justify-between gap-3 border-b border-accent/40 px-5 py-4 sm:px-6">
            <div>
              <h2 className="text-base font-semibold text-foreground">Fresh token — copy now</h2>
              <p className="mt-1 text-sm text-foreground-muted">
                This secret is shown once. Store it before leaving this page.
              </p>
            </div>
            <button
              onClick={() => setNewPat(null)}
              aria-label="Dismiss fresh token"
              className="inline-flex items-center justify-center min-h-[36px] min-w-[36px] coord hover:text-primary cursor-pointer rounded-[var(--radius-sm)] focus:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2 focus-visible:ring-offset-background"
            >
              <X className="h-3 w-3" aria-hidden />
            </button>
          </div>
          <div className="space-y-4 p-5 sm:p-6">
            <div className="flex items-start gap-3">
              <code className="flex-1 font-mono text-xs text-foreground break-all rounded-[var(--radius-md)] border border-border px-3 py-2 bg-surface">
                {showPat ? newPat : newPat.slice(0, 12) + "•".repeat(20)}
              </code>
              {/* Full token stays reachable to a screen reader even masked. */}
              {!showPat && <span className="sr-only">Token value: {newPat}</span>}
              <button
                onClick={() => setShowPat(!showPat)}
                aria-label={showPat ? "Hide token" : "Show token"}
                className="inline-flex items-center justify-center min-h-[36px] px-2 coord hover:text-primary cursor-pointer shrink-0 rounded-[var(--radius-sm)] focus:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2 focus-visible:ring-offset-background"
              >
                {showPat ? (
                  <EyeOff className="h-3 w-3" aria-hidden />
                ) : (
                  <Eye className="h-3 w-3" aria-hidden />
                )}
              </button>
              <button
                onClick={() => copy(newPat, "pat")}
                aria-label={copied === "pat" ? "Token copied" : "Copy token"}
                className="inline-flex items-center justify-center min-h-[36px] px-2 coord hover:text-primary cursor-pointer shrink-0 rounded-[var(--radius-sm)] focus:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2 focus-visible:ring-offset-background"
              >
                {copied === "pat" ? <span aria-hidden>Copied</span> : <Copy className="h-3 w-3" aria-hidden />}
              </button>
            </div>

            {copyError && <Alert variant="warning">Copy was blocked. Select and copy the token manually.</Alert>}
          </div>
        </section>
      )}


      <section aria-labelledby="connection-heading">
        <header className="mb-3 flex flex-wrap items-center justify-between gap-3 border-b border-border pb-3">
          <div>
            <h2 id="connection-heading" className="text-base font-semibold">Connect an agent</h2>
            <p className="mt-1 text-sm text-foreground-muted">Prepare access, add the configuration, then try a read-only request in your agent.</p>
          </div>
          <Badge variant="default">3 steps</Badge>
        </header>
        <div id="setup-guide-body" className="py-2">
          <ConnectionSetup layout="workspace" mcpOauthEnabled={mcpOauthEnabled} onTokenCreated={onReloadPats} onSecretCreated={() => setSetupSecret(true)} onBusyChange={setSetupBusy} invalidatedTokenId={invalidatedTokenId} />
        </div>
      </section>

      {/* Active tokens — primary content on this tab (management). */}
      <section aria-labelledby="active-tokens-heading">
        <header className="mb-3 flex items-start justify-between gap-4 border-b border-border pb-3">
          <div>
            <h2 id="active-tokens-heading" className="text-base font-semibold text-foreground">Active tokens</h2>
            <p className="mt-1 text-sm text-foreground-muted">
              Manage credentials separately from setup. Last use does not indicate a live AI connection.
            </p>
          </div>
          <Badge variant="default" className="shrink-0 tabular-nums">
            {pats ? pats.length : "··"}
          </Badge>
        </header>
        <div className="space-y-4">
          {reissueError && <Alert variant="destructive">{reissueError}</Alert>}
          {patsError ? (
            <EmptyState
              title="Couldn't load tokens"
              description="Something went wrong fetching your tokens."
              action={
                <Button variant="outline" size="sm" onClick={onReloadPats}>
                  Retry
                </Button>
              }
            />
          ) : !pats ? (
            <>
              <span className="sr-only" role="status" aria-live="polite">
                Loading tokens
              </span>
              <div
                className="divide-y divide-border"
                aria-hidden
              >
                {Array.from({ length: 3 }).map((_, i) => (
                  <div key={i} className="px-4 py-3 space-y-2">
                    <div className="flex items-center gap-3">
                      <span className="h-3 w-5 rounded bg-surface-muted animate-pulse" />
                      <span className="h-4 w-32 rounded bg-surface-muted animate-pulse" />
                    </div>
                    <div className="h-3 w-40 rounded bg-surface-muted animate-pulse ml-7" />
                  </div>
                ))}
              </div>
            </>
          ) : pats.length === 0 ? (
            <EmptyState
              title="No tokens yet"
              description="A token is only needed for token-based connections. Browser sign-in does not require one."
            />
          ) : (
            <div className="rounded-[var(--radius-md)] border border-border divide-y divide-border overflow-hidden">
              {(pats ?? []).map((p) => (
                <div key={p.token_id} className="flex flex-wrap items-center justify-between gap-x-4 gap-y-2 px-3 py-3 sm:px-4">
                  <div className="flex min-w-0 flex-1 items-start gap-3">
                    <span className="flex h-5 w-5 shrink-0 items-center justify-center text-foreground-muted">
                      <KeyRound className="h-4 w-4" aria-hidden />
                    </span>
                    <div className="min-w-0 flex-1">
                      <div className="flex min-w-0 flex-wrap items-baseline gap-x-3 gap-y-1">
                        <span title={p.name} className="truncate text-sm font-semibold text-foreground">
                          {p.name}
                        </span>
                        <code className="font-mono text-xs text-foreground-muted">
                          {p.prefix}••••
                        </code>
                      </div>
                      <div className="mt-1 flex flex-wrap items-center gap-x-2 gap-y-1 text-xs text-foreground-muted">
                        <span className="tabular-nums" title={`Created ${formatDate(p.created_at)}`}>
                          Created {timeAgo(p.created_at)}
                        </span>
                        <span aria-hidden>·</span>
                        <span
                          className="tabular-nums"
                          title={p.last_used_at ? `Last used ${formatDate(p.last_used_at)}` : undefined}
                        >
                          {p.last_used_at ? `Used ${timeAgo(p.last_used_at)}` : "Never used"}
                        </span>
                      </div>
                    </div>
                  </div>
                  <div className="flex items-center justify-end gap-1">
                    <button
                      onClick={() => setPendingReissue(p)}
                      disabled={reissuingId === p.token_id}
                      aria-label={`Reissue token ${p.name}`}
                      className="inline-flex min-h-[36px] cursor-pointer items-center gap-1 rounded-[var(--radius-sm)] px-2 text-xs text-foreground-muted transition-colors hover:bg-surface-hover hover:text-primary focus:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2 focus-visible:ring-offset-surface disabled:opacity-50"
                    >
                      <RotateCw
                        className={`h-3 w-3 ${reissuingId === p.token_id ? "animate-spin" : ""}`}
                        aria-hidden
                      />
                      {reissuingId === p.token_id ? "Reissuing" : "Reissue"}
                    </button>
                    <button
                      onClick={() => setPendingRevokePat(p)}
                      aria-label={`Revoke token ${p.name}`}
                      className="inline-flex min-h-[36px] cursor-pointer items-center gap-1 rounded-[var(--radius-sm)] px-2 text-xs text-destructive transition-colors hover:bg-surface-hover focus:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2 focus-visible:ring-offset-surface"
                    >
                      <Trash2 className="h-3 w-3" aria-hidden />
                      Revoke
                    </button>
                  </div>
                </div>
              ))}
            </div>
          )}
        </div>
      </section>
      </div>

      <ConfirmDialog
        open={pendingRevokePat !== null}
        onOpenChange={(o) => !o && setPendingRevokePat(null)}
        title={pendingRevokePat ? `Revoke "${pendingRevokePat.name}"?` : ""}
        description={
          "Any agent currently using this token will lose access immediately.\nThis cannot be undone."
        }
        confirmLabel="Revoke token"
        variant="destructive"
        onConfirm={confirmRevokePat}
      />

      <ConfirmDialog
        open={pendingReissue !== null}
        onOpenChange={(o) => !o && setPendingReissue(null)}
        title={pendingReissue ? `Reissue "${pendingReissue.name}"?` : ""}
        description={
          "The current token stops working the instant this runs — a fresh token is minted to replace it. Any agent still using the old value will lose access until you paste the new one."
        }
        confirmLabel="Reissue token"
        variant="destructive"
        onConfirm={() => {
          if (pendingReissue) return handleReissue(pendingReissue);
        }}
      />
    </>
  );
}
