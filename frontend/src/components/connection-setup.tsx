import { useEffect, useId, useState } from "react";
import { authSessionSnapshot, isCurrentAuthSession, type AuthSessionSnapshot } from "@/lib/api";
import { type PatReceipt } from "@/lib/api-pat-issuance";
import { scopeSummary, type PatDraft } from "@/lib/pat-draft";
import { PatIssuanceForm } from "@/components/pat-issuance-form";
import { MCP_AGENT_FILES, MCP_AGENT_LABELS, mcpInstallSnippets, mcpOAuthSnippets, type McpAgent } from "@/lib/mcp-snippets";
import { Alert } from "@/components/ui/alert";
import { Button } from "@/components/ui/button";
import { CodeSnippet } from "@/components/ui/code-snippet";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { SelectMenu } from "@/components/ui/select-menu";
import { cn } from "@/lib/utils";

export function ConnectionSetup({ mcpOauthEnabled, onTokenCreated, onSecretCreated, onBusyChange, invalidatedTokenId, onDirtyChange, initialDraft, replacement = false, onReceipt, layout = "compact" }: {
  layout?: "compact" | "workspace";
  mcpOauthEnabled: boolean;
  onTokenCreated?: () => void;
  onSecretCreated?: () => void;
  onBusyChange?: (busy: boolean) => void;
  invalidatedTokenId?: string;
  onDirtyChange?: (dirty: boolean) => void;
  initialDraft?: PatDraft;
  replacement?: boolean;
  onReceipt?: (receipt: PatReceipt, snapshot: AuthSessionSnapshot) => void;
}) {
  const id = useId();
  const [agent, setAgent] = useState<McpAgent>("claude");
  const [auth, setAuth] = useState("oauth");
  const [credentialMode, setCredentialMode] = useState("create");
  const [freshToken, setFreshToken] = useState("");
  const [freshTokenId, setFreshTokenId] = useState<string | null>(null);
  const [existingToken, setExistingToken] = useState("");
  const [lastInvalidatedId, setLastInvalidatedId] = useState(invalidatedTokenId);
  const [invalidationNotice, setInvalidationNotice] = useState<string | null>(null);
  const [creating, setCreating] = useState(false);
  const [receipt, setReceipt] = useState<PatReceipt | null>(null);
  const [showSecret, setShowSecret] = useState(true);
  const [formDirty, setFormDirty] = useState(false);
  const [issuanceStarted, setIssuanceStarted] = useState(false);
  const [session, setSession] = useState(authSessionSnapshot);
  const dirty = formDirty || !!freshToken || !!existingToken;
  useEffect(() => { onDirtyChange?.(dirty); }, [dirty, onDirtyChange]);
  useEffect(() => () => { onDirtyChange?.(false); onBusyChange?.(false); }, [onDirtyChange, onBusyChange]);
  useEffect(() => {
    const check = () => {
      if (isCurrentAuthSession(session)) return;
      setFreshToken(""); setFreshTokenId(null); setReceipt(null); setExistingToken("");
      setInvalidationNotice("Your sign-in session changed. Previous secrets and configuration have been cleared. Review creation again.");
      setSession(authSessionSnapshot());
    };
    window.addEventListener("storage", check); window.addEventListener("focus", check);
    document.addEventListener("visibilitychange", check);
    const timer = window.setInterval(check, 1000);
    return () => { window.removeEventListener("storage", check); window.removeEventListener("focus", check); document.removeEventListener("visibilitychange", check); window.clearInterval(timer); };
  }, [session]);
  const oauthSnippet = mcpOAuthSnippets()[agent];
  const oauthAvailable = mcpOauthEnabled && oauthSnippet !== undefined;
  const useOauth = oauthAvailable && auth === "oauth";
  // Start discovery only when token creation is first opened, then preserve
  // the form session even while another credential mode is visible.
  if (!useOauth && credentialMode === "create" && !issuanceStarted) setIssuanceStarted(true);
  const token = isCurrentAuthSession(session) ? freshToken || (credentialMode === "existing" ? existingToken.trim() : "") : "";
  // A visible prefix or placeholder is never a usable configuration.
  const usableToken = /^[A-Za-z0-9_-]+$/.test(token);

  // Adjust before committing this render, so a revoked credential is never
  // briefly offered by the copy control. Unrelated fresh secrets stay intact.
  if (lastInvalidatedId !== invalidatedTokenId) {
    setLastInvalidatedId(invalidatedTokenId);
    if (invalidatedTokenId) {
      if (freshTokenId === invalidatedTokenId) {
        setFreshToken("");
        setFreshTokenId(null);
        setReceipt(null);
        setInvalidationNotice("This setup token was revoked. Create a new token or enter its replacement to configure your tool.");
      } else if (existingToken && !freshToken) {
        setInvalidationNotice("A token was revoked. Enter your current full token again before copying a configuration.");
      }
      setExistingToken("");
    }
  }

  const workspace = layout === "workspace";
  const stepClass = workspace ? "min-w-0 space-y-4 rounded-[var(--radius-md)] border border-border bg-surface p-4 sm:p-5" : "space-y-3";
  return <div className={cn("text-sm", workspace ? "grid items-start gap-4 2xl:grid-cols-[minmax(0,1fr)_minmax(0,1.3fr)_minmax(0,1fr)]" : "space-y-5")}>
    <section className={stepClass} aria-labelledby={`${id}-choose`}>
      <h3 id={`${id}-choose`} className="font-semibold text-foreground">{workspace ? "1. Prepare access" : "1. Choose your tool"}</h3>
      <div className="space-y-1.5">
        <Label htmlFor={`${id}-agent`}>AI tool</Label>
        <SelectMenu id={`${id}-agent`} value={agent} disabled={creating} onValueChange={value => setAgent(value as McpAgent)} options={Object.entries(MCP_AGENT_LABELS).map(([value, label]) => ({ value, label }))} />
      </div>
      {oauthAvailable && <div className="space-y-1.5">
        <Label htmlFor={`${id}-auth`}>Sign-in method</Label>
        <SelectMenu id={`${id}-auth`} value={auth} disabled={creating} onValueChange={setAuth} options={[{ value: "pat", label: "Access token" }, { value: "oauth", label: "Browser sign-in (OAuth)" }]} />
      </div>}
      <p className="text-xs text-foreground-muted">Your AI tool can read or change content within your existing vault permissions. The test request below is read-only.</p>
      {invalidationNotice && <Alert variant="warning">{invalidationNotice}</Alert>}
      {useOauth ? <p className="text-foreground-muted">{agent === "claude" ? "After running the command, open Claude Code and use /mcp to authenticate AKB in your browser." : "Add this server in your tool, then complete its browser sign-in."} No access token is needed.</p> : <>
        {!freshToken && <>
          <div className="space-y-1.5">
            <Label htmlFor={`${id}-credential`}>Access token</Label>
            <SelectMenu id={`${id}-credential`} value={credentialMode} disabled={creating} onValueChange={setCredentialMode} options={[{ value: "create", label: "Create a new token" }, { value: "existing", label: "Use a saved token" }]} />
          </div>
          {credentialMode === "existing" && <div className="space-y-2">
            <Label htmlFor={`${id}-saved`}>Full saved token</Label>
            <Input id={`${id}-saved`} type="password" value={existingToken} onChange={event => { setExistingToken(event.target.value); setInvalidationNotice(null); }} autoComplete="off" spellCheck={false} aria-describedby={`${id}-saved-help`} />
            <p id={`${id}-saved-help`} className="text-xs text-foreground-muted">Use the full secret you saved earlier. The prefix in the token list cannot authenticate. It stays only in this setup session.</p>
            {existingToken && !usableToken && <Alert variant="destructive">Enter the full token using only letters, numbers, underscores, and hyphens. A prefix or placeholder cannot authenticate.</Alert>}
          </div>}
        </>}
      </>}
      {/* Keep issuance state mounted across credential/OAuth switches. Only a
          completed issuance or leaving setup may discard this form session. */}
      {!freshToken && issuanceStarted && <div hidden={useOauth || credentialMode !== "create"}>
        <PatIssuanceForm initial={initialDraft} replacement={replacement} onDirtyChange={setFormDirty} onBusyChange={value => { setCreating(value); onBusyChange?.(value); }} onMetadataChanged={onTokenCreated} onCreated={(result, snapshot) => {
            setFreshToken(result.token); setFreshTokenId(result.token_id); setReceipt(result); setSession(snapshot); setShowSecret(true); setInvalidationNotice(null); onSecretCreated?.(); onReceipt?.(result, snapshot);
          }} />
      </div>}
      {freshToken && isCurrentAuthSession(session) && <div className="space-y-2 rounded-[var(--radius-md)] border border-accent/40 bg-accent/5 p-3">
        <p role="status" className="font-medium">Token created — save it now</p>
        <p className="text-xs text-foreground-muted">This secret is shown only during this setup. Store it privately before leaving. The configuration below also contains the token when using token sign-in.</p>
        <p className="text-xs text-foreground-muted">{receipt?.verified ? `${receipt.scopes?.join(" + ")} · ${receipt.expires_at ? `Expires ${receipt.expires_at}` : "No expiration"} · ${scopeSummary(receipt.vault_scope)}` : "Created with server defaults; advanced restrictions were not verified."}</p>
        <div className="flex flex-wrap gap-2"><Button type="button" variant="outline" onClick={() => setShowSecret(!showSecret)}>{showSecret ? "Hide token and configuration" : "Show token and configuration"}</Button><Button type="button" variant="outline" onClick={() => { setFreshToken(""); setFreshTokenId(null); setReceipt(null); }}>I've saved it — dismiss token</Button></div>
        {showSecret ? <CodeSnippet code={freshToken} filename="Access token" /> : <p>Token and token-bearing configuration are hidden.</p>}
      </div>}
    </section>

    <section className={cn(stepClass, !workspace && "border-t border-border pt-4")} aria-labelledby={`${id}-configure`}>
      <h3 id={`${id}-configure`} className="font-semibold text-foreground">2. Configure {MCP_AGENT_LABELS[agent]}</h3>
      {useOauth || (usableToken && (!freshToken || showSecret)) ? <>
        <p className="text-foreground-muted">{MCP_AGENT_FILES[agent] === "terminal" ? "Run this in your terminal." : `Merge this configuration into your tool's ${MCP_AGENT_FILES[agent]}; keep any existing servers.`}</p>
        <CodeSnippet code={useOauth ? oauthSnippet! : mcpInstallSnippets(token)[agent]} filename={MCP_AGENT_FILES[agent]} />
      </> : <p className="text-xs text-foreground-muted">Create or enter a token to reveal your configuration.</p>}
    </section>

    {(workspace || useOauth || usableToken) && <section className={cn(stepClass, !workspace && "border-t border-border pt-4")} aria-labelledby={`${id}-try`}>
      <h3 id={`${id}-try`} className="font-semibold text-foreground">3. Try it in your agent</h3>
      <p className="text-foreground-muted">Reload your tool if needed, enable the AKB server, then send this read-only request:</p>
      <CodeSnippet code="Use akb_help to show me how AKB works, then list the vaults I can access. Do not create or change anything." filename="Prompt for your agent" />
      <p className="text-xs text-foreground-muted">Your agent should return AKB's usage guide and your accessible vaults. This browser cannot verify an external agent connection. If it fails, check that the server is enabled, this AKB address is reachable, and sign-in completed.</p>
    </section>}
  </div>;
}
