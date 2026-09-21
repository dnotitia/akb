import { useEffect, useRef, useState } from "react";
import { KeyRound, RotateCw, Trash2 } from "lucide-react";
import { authSessionSnapshot, isCurrentAuthSession, type AuthSessionSnapshot } from "@/lib/api";
import { canonicalScope, revokeIssuedPat, type PatMetadata } from "@/lib/api-pat-issuance";
import { replacementDraft, scopeSummary, type PatDraft } from "@/lib/pat-draft";
import { formatDate, timeAgo } from "@/lib/utils";
import { Alert } from "@/components/ui/alert";
import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";
import { ConfirmDialog } from "@/components/ui/confirm-dialog";
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { EmptyState } from "@/components/empty-state";
import { ConnectionSetup } from "@/components/connection-setup";

export type PAT = PatMetadata;
interface Props {
  pats: PAT[] | null; patsError: boolean; mcpOauthEnabled: boolean; onReloadPats: () => void;
  onDirtyChange?: (dirty: boolean) => void; onBusyChange?: (busy: boolean) => void;
}
export function TokensSection({ pats, patsError, mcpOauthEnabled, onReloadPats, onDirtyChange, onBusyChange }: Props) {
  const [invalidatedTokenId, setInvalidatedTokenId] = useState<string>();
  const [setupDirty, setSetupDirty] = useState(false);
  const [setupBusy, setSetupBusy] = useState(false);
  const [replacement, setReplacement] = useState<{ token: PAT; draft: PatDraft; snapshot: AuthSessionSnapshot } | null>(null);
  const [replacementDirty, setReplacementDirty] = useState(false);
  const [replacementBusy, setReplacementBusy] = useState(false);
  const [replacementCreated, setReplacementCreated] = useState(false);
  const [originalRevoked, setOriginalRevoked] = useState(false);
  const [closeReplacement, setCloseReplacement] = useState(false);
  const [pendingRevoke, setPendingRevoke] = useState<{ token: PAT; snapshot: AuthSessionSnapshot } | null>(null);
  const [revoking, setRevoking] = useState(false);
  const revokePending = useRef(false);
  const [notice, setNotice] = useState("");
  useEffect(() => { onDirtyChange?.(setupDirty || replacementDirty); }, [setupDirty, replacementDirty, onDirtyChange]);
  useEffect(() => { onBusyChange?.(setupBusy || replacementBusy || revoking); }, [setupBusy, replacementBusy, revoking, onBusyChange]);
  useEffect(() => () => { onDirtyChange?.(false); onBusyChange?.(false); }, [onDirtyChange, onBusyChange]);
  useEffect(() => {
    if (!replacement) return;
    const check = () => { if (!isCurrentAuthSession(replacement.snapshot)) { setReplacement(null); setReplacementCreated(false); setNotice("Your sign-in session changed. Review token management again."); } };
    window.addEventListener("focus", check); window.addEventListener("storage", check);
    const timer = window.setInterval(check, 1000);
    return () => { window.removeEventListener("focus", check); window.removeEventListener("storage", check); window.clearInterval(timer); };
  }, [replacement]);
  function requestCloseReplacement() {
    if (replacementBusy || revoking) return;
    if (replacementDirty) setCloseReplacement(true); else setReplacement(null);
  }
  async function revoke() {
    if (!pendingRevoke || revokePending.current) return;
    revokePending.current = true; setRevoking(true);
    try {
      await revokeIssuedPat(pendingRevoke.snapshot, pendingRevoke.token.token_id);
      if (!isCurrentAuthSession(pendingRevoke.snapshot)) return;
      setInvalidatedTokenId(pendingRevoke.token.token_id);
      if (replacement?.token.token_id === pendingRevoke.token.token_id) setOriginalRevoked(true);
      onReloadPats();
    } finally { revokePending.current = false; setRevoking(false); }
  }
  return <>
    <div className="space-y-8">
      {notice && <Alert variant="warning">{notice}</Alert>}
      <section aria-labelledby="connection-heading"><header className="mb-3 flex flex-wrap items-center justify-between gap-3 border-b border-border pb-3"><div><h2 id="connection-heading" className="text-base font-semibold">Connect an agent</h2><p className="mt-1 text-sm text-foreground-muted">Prepare access, add the configuration, then try a read-only request in your agent.</p></div><Badge variant="default">3 steps</Badge></header>
        <div id="setup-guide-body" className="py-2"><ConnectionSetup layout="workspace" mcpOauthEnabled={mcpOauthEnabled} onTokenCreated={onReloadPats} onDirtyChange={setSetupDirty} onBusyChange={setSetupBusy} invalidatedTokenId={invalidatedTokenId} /></div>
      </section>
      <section aria-labelledby="tokens-heading"><header className="mb-3 flex items-start justify-between gap-4 border-b border-border pb-3"><div><h2 id="tokens-heading" className="text-base font-semibold">Tokens</h2><p className="mt-1 text-sm text-foreground-muted">Last use does not indicate a live AI connection. Replacement keeps the original active until you revoke it.</p></div><Badge variant="default">{pats?.length ?? "··"}</Badge></header>
        {patsError ? <EmptyState title="Couldn't load tokens" description="Something went wrong fetching your tokens." action={<Button variant="outline" onClick={onReloadPats}>Retry</Button>} /> : !pats ? <p role="status">Loading tokens…</p> : pats.length === 0 ? <EmptyState title="No tokens yet" description="A token is only needed for token-based connections. Browser sign-in does not require one." /> : <div className="divide-y divide-border overflow-hidden rounded-[var(--radius-md)] border border-border">{pats.map(token => {
          const replacementOptions = replacementDraft(token);
          const expired = typeof token.expires_at === "string" && Date.parse(token.expires_at) <= Date.now();
          return <div key={token.token_id} className="flex min-w-0 flex-wrap items-center justify-between gap-3 px-4 py-3">
            <div className="flex min-w-0 flex-1 items-start gap-3"><KeyRound className="h-4 w-4 shrink-0 text-foreground-muted" aria-hidden /><div className="min-w-0 flex-1 space-y-1">
              <div className="flex flex-wrap items-center gap-2"><span className="break-all text-sm font-semibold">{token.name}</span><code className="text-xs text-foreground-muted">{token.prefix}••••</code><Badge variant={expired ? "warning" : "default"}>{expired ? "Expired" : token.expires_at === undefined ? "Expiration unknown" : "Active"}</Badge><span className="text-xs text-foreground-muted">{token.key_class === "pat" ? "PAT" : token.key_class === "service" ? "Service key" : "Credential type unknown"}</span></div>
              <p className="break-words text-xs text-foreground-muted">{token.scopes?.length ? `Permissions: ${token.scopes.join(" + ")}` : "Permissions unknown"} · {scopeSummary(canonicalScope(token.vault_scope))}</p>
              <p className="text-xs text-foreground-muted">{token.expires_at === null ? "No expiration" : token.expires_at === undefined ? "Expiration unavailable" : `${expired ? "Expired" : "Expires"} ${formatDate(token.expires_at)}`}</p>
              <p className="text-xs text-foreground-muted">Created {timeAgo(token.created_at)} · {token.last_used_at ? `Used ${timeAgo(token.last_used_at)}` : "Never used"}</p>
              {!replacementOptions && <p className="text-xs text-foreground-muted">Replacement requires complete PAT metadata. Create a new token independently and revoke this one separately.</p>}
            </div></div>
            <div className="flex flex-wrap items-center gap-1"><Button variant="ghost" size="sm" disabled={!replacementOptions || setupBusy || replacementBusy || revoking} aria-label={`Replace token ${token.name}`} onClick={() => { if (replacementOptions) { setReplacement({ token, draft: replacementOptions, snapshot: authSessionSnapshot() }); setReplacementCreated(false); setOriginalRevoked(false); } }}><RotateCw className="h-3 w-3" aria-hidden />Replace</Button><Button variant="ghost" size="sm" disabled={setupBusy || replacementBusy || revoking} aria-label={`Revoke token ${token.name}`} onClick={() => setPendingRevoke({ token, snapshot: authSessionSnapshot() })}><Trash2 className="h-3 w-3" aria-hidden />Revoke</Button></div>
          </div>;
        })}</div>}
      </section>
    </div>
    <Dialog open={replacement !== null} onOpenChange={open => { if (!open) requestCloseReplacement(); }}><DialogContent className="max-h-[90dvh] max-w-3xl overflow-y-auto"><DialogHeader><DialogTitle>Replace token {replacement?.token.name}</DialogTitle><DialogDescription>Review expiration, permissions and Vault write restrictions before creating a replacement. The original is retained.</DialogDescription></DialogHeader>
      {replacement && <ConnectionSetup key={replacement.token.token_id} mcpOauthEnabled={false} initialDraft={replacement.draft} replacement onTokenCreated={onReloadPats} onDirtyChange={setReplacementDirty} onBusyChange={setReplacementBusy} invalidatedTokenId={invalidatedTokenId} onReceipt={() => setReplacementCreated(true)} />}
      {replacementCreated && <Alert variant="info">{originalRevoked ? "The original token was revoked. Keep the replacement secret somewhere private." : "Both tokens now exist. Update your agents, save the replacement, then explicitly revoke the original."}</Alert>}
      <DialogFooter><Button variant="outline" disabled={replacementBusy || revoking} onClick={requestCloseReplacement}>Close replacement</Button>{replacementCreated && !originalRevoked && replacement && <Button variant="destructive" disabled={replacementBusy || revoking} onClick={() => setPendingRevoke({ token: replacement.token, snapshot: replacement.snapshot })}>Revoke original token</Button>}</DialogFooter>
    </DialogContent></Dialog>
    <ConfirmDialog open={pendingRevoke !== null} onOpenChange={open => { if (!open) setPendingRevoke(null); }} title={pendingRevoke ? `Revoke "${pendingRevoke.token.name}"?` : "Revoke token?"} description="Any agent using this token will lose access immediately. This cannot be undone. Other tokens stay active." confirmLabel="Revoke token" variant="destructive" busy={revoking} onConfirm={revoke} />
    <ConfirmDialog open={closeReplacement} onOpenChange={setCloseReplacement} title="Have you saved your replacement?" description="Closing discards draft changes and the one-time replacement secret. It does not revoke either token." confirmLabel="I've saved it — close" onConfirm={() => { setReplacement(null); setCloseReplacement(false); }} />
  </>;
}
