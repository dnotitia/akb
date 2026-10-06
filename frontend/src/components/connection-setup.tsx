import { useEffect, useId, useState, type ReactNode } from "react";
import { Check, Terminal } from "lucide-react";
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
import { cn } from "@/lib/utils";

function SetupStep({ id, number, title, children }: { id: string; number: number; title: string; children: ReactNode }) {
  return <section aria-labelledby={id} className="grid min-w-0 grid-cols-[1.75rem_minmax(0,1fr)] gap-x-3 py-4 sm:gap-x-4">
    <span aria-hidden className="flex h-7 w-7 items-center justify-center rounded-[var(--radius-sm)] bg-primary/10 text-xs font-semibold tabular-nums text-link">{number}</span>
    <h3 id={id} aria-label={`${number}. ${title}`} className="pt-0.5 text-sm font-semibold leading-6 text-foreground"><span className="sr-only">{number}. </span>{title}</h3>
    <div className="col-span-2 min-w-0 space-y-3 pt-3 sm:col-span-1 sm:col-start-2">
      {children}
    </div>
  </section>;
}

/** One labelled choice, without another select box or nested setup branch. */
function SetupChoice({ label, name, value, options, disabled, onChange, appearance = "segmented" }: {
  label: string; name: string; value: string;
  options: { value: string; label: string; accessibleName?: string }[];
  disabled: boolean; onChange: (value: string) => void;
  appearance?: "tabs" | "segmented";
}) {
  return <fieldset disabled={disabled} className="min-w-0">
    <legend className="mb-2 text-xs font-medium text-foreground-muted">{label}</legend>
    <div className={cn("flex flex-wrap gap-1", appearance === "tabs" ? "border-b border-border" : "rounded-[var(--radius-md)] border border-border bg-surface-2 p-1")}>
      {options.map(option => <label key={option.value} className={cn(
        "relative flex min-h-11 flex-[1_0_auto] cursor-pointer items-center justify-center gap-2 whitespace-nowrap px-3 text-sm transition-token has-[:focus-visible]:z-10 has-[:focus-visible]:ring-2 has-[:focus-visible]:ring-ring has-[:focus-visible]:ring-offset-2 has-[:focus-visible]:ring-offset-surface",
        appearance === "tabs" ? "rounded-t-[var(--radius-sm)] border-b-2" : "rounded-[var(--radius-sm)] border",
        disabled && "cursor-not-allowed opacity-50",
        value === option.value
          ? cn("bg-surface-selected font-semibold text-surface-selected-foreground", appearance === "tabs" ? "border-link" : "border-border-strong")
          : "border-transparent text-foreground-muted hover:bg-surface-hover hover:text-foreground",
      )}>
        <input type="radio" className="sr-only" name={name} value={option.value} aria-label={option.accessibleName} checked={value === option.value} tabIndex={value === option.value ? 0 : -1} onChange={() => onChange(option.value)} />
        {appearance === "segmented" && <Check aria-hidden className={cn("h-3.5 w-3.5 shrink-0", value !== option.value && "invisible")} />}
        {option.label}
      </label>)}
    </div>
  </fieldset>;
}

export function ConnectionSetup({ mcpOauthEnabled, onTokenCreated, onSecretCreated, onBusyChange, invalidatedTokenId, onDirtyChange, initialDraft, replacement = false, onReceipt, layout = "compact" }: {
  layout?: "compact" | "workspace" | "settings";
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

  const accessMethod = useOauth ? "oauth" : freshToken ? "create" : credentialMode;
  const configVisible = useOauth || (usableToken && (!freshToken || showSecret));
  return <div className="min-w-0 text-sm" data-layout={layout}>
    <div>
      <SetupChoice label="AI tool" appearance="tabs" name={`${id}-agent`} value={agent} disabled={creating} onChange={value => setAgent(value as McpAgent)} options={Object.entries(MCP_AGENT_LABELS).map(([value, label]) => ({ value, label }))} />
    </div>
    <div className="divide-y divide-border">
    <SetupStep id={`${id}-choose`} number={1} title="Prepare access">
      <SetupChoice label="Access method" name={`${id}-access`} value={accessMethod} disabled={creating} onChange={value => {
        if (value === "oauth") setAuth("oauth");
        else { setAuth("pat"); setCredentialMode(value); }
      }} options={[
        ...(oauthAvailable ? [{ value: "oauth", label: "Browser sign-in" }] : []),
        { value: "create", label: freshToken ? "Access token" : "New token", accessibleName: freshToken ? "Access token" : "Create a new token" },
        ...(!freshToken ? [{ value: "existing", label: "Saved token", accessibleName: "Use a saved token" }] : []),
      ]} />
      {invalidationNotice && <Alert variant="warning">{invalidationNotice}</Alert>}
      {useOauth ? <p className="leading-relaxed text-foreground-muted">Your tool will open AKB's browser sign-in after you add the server. No access token is needed. Your existing vault permissions still apply.</p> : <>
        {!oauthAvailable && mcpOauthEnabled && <p className="text-xs text-foreground-muted">{MCP_AGENT_LABELS[agent]} uses an access token for this connection.</p>}
        {!freshToken && credentialMode === "existing" && <div className="space-y-2">
            <Label htmlFor={`${id}-saved`}>Full saved token</Label>
            <Input id={`${id}-saved`} type="password" value={existingToken} onChange={event => { setExistingToken(event.target.value); setInvalidationNotice(null); }} autoComplete="off" spellCheck={false} aria-describedby={`${id}-saved-help`} />
            <p id={`${id}-saved-help`} className="text-xs text-foreground-muted">Use the full secret, not the prefix in the token list. It stays only in this setup session. Your existing vault permissions still apply.</p>
            {existingToken && !usableToken && <Alert variant="destructive">Enter the full token using only letters, numbers, underscores, and hyphens. A prefix or placeholder cannot authenticate.</Alert>}
        </div>}
      </>}
      {/* Keep issuance state mounted across credential/OAuth switches. Only a
          completed issuance or leaving setup may discard this form session. */}
      {!freshToken && issuanceStarted && <div hidden={useOauth || credentialMode !== "create"}>
        <PatIssuanceForm initial={initialDraft} replacement={replacement} onDirtyChange={setFormDirty} onBusyChange={value => { setCreating(value); onBusyChange?.(value); }} onMetadataChanged={onTokenCreated} onCreated={(result, snapshot) => {
            setFreshToken(result.token); setFreshTokenId(result.token_id); setReceipt(result); setSession(snapshot); setCredentialMode("create"); setShowSecret(true); setInvalidationNotice(null); onSecretCreated?.(); onReceipt?.(result, snapshot);
          }} />
      </div>}
      {freshToken && isCurrentAuthSession(session) && <div className="space-y-2 rounded-[var(--radius-md)] border border-accent/40 bg-accent/5 p-3">
        <p role="status" className="font-medium">Token created — save it now</p>
        <p className="text-xs text-foreground-muted">This secret is shown only during this setup. Store it privately before leaving. The configuration below also contains the token when using token sign-in.</p>
        <p className="text-xs text-foreground-muted">{receipt?.verified ? `${receipt.scopes?.join(" + ")} · ${receipt.expires_at ? `Expires ${receipt.expires_at}` : "No expiration"} · ${scopeSummary(receipt.vault_scope)}` : "Created with server defaults; advanced restrictions were not verified."}</p>
        <div className="flex flex-wrap gap-2"><Button type="button" variant="outline" size="sm" onClick={() => setShowSecret(!showSecret)}>{showSecret ? "Hide token and configuration" : "Show token and configuration"}</Button><Button type="button" variant="ghost" size="sm" onClick={() => { setFreshToken(""); setFreshTokenId(null); setReceipt(null); }}>I've saved it — dismiss token</Button></div>
        {showSecret ? <CodeSnippet code={freshToken} filename="Access token" /> : <p>Token and token-bearing configuration are hidden.</p>}
      </div>}
    </SetupStep>

    <SetupStep id={`${id}-configure`} number={2} title={`Configure ${MCP_AGENT_LABELS[agent]}`}>
      {configVisible ? <>
        <p className="text-foreground-muted">{MCP_AGENT_FILES[agent] === "terminal" ? "Run this in your terminal." : `Merge this configuration into your tool's ${MCP_AGENT_FILES[agent]}; keep any existing servers.`}</p>
        <CodeSnippet code={useOauth ? oauthSnippet! : mcpInstallSnippets(token)[agent]} filename={MCP_AGENT_FILES[agent]} />
      </> : <div className="flex items-start gap-3 rounded-[var(--radius-md)] border border-dashed border-border bg-surface-2/50 p-4">
        <Terminal className="mt-0.5 h-4 w-4 shrink-0 text-foreground-muted" aria-hidden />
        <div className="space-y-1"><p className="font-medium">Your configuration will appear here</p><p className="text-xs leading-relaxed text-foreground-muted">{freshToken && !showSecret ? "Show your token and configuration in step 1 to copy it." : "Create or enter a token in step 1. No placeholder credentials are included."}</p></div>
      </div>}
    </SetupStep>

    <SetupStep id={`${id}-try`} number={3} title="Try it in your agent">
      <p className="leading-relaxed text-foreground-muted">{useOauth ? agent === "claude" ? "Open Claude Code and use /mcp to sign in to AKB in your browser. Then send this read-only request:" : "Reload your tool, enable AKB and complete its browser sign-in. Then send this read-only request:" : "Reload your tool if needed, enable AKB, then send this read-only request:"}</p>
      <CodeSnippet code="Use akb_help to show me how AKB works, then list the vaults I can access. Do not create or change anything." filename="Prompt for your agent" />
      <p className="text-xs leading-relaxed text-foreground-muted">Expected response: AKB's usage guide and the vaults you can access. This browser cannot verify an external agent connection.</p>
      <details className="text-xs text-foreground-muted"><summary className="w-fit cursor-pointer rounded-[var(--radius-sm)] py-2 font-medium text-link focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring">Connection not working?</summary><p className="pb-2 leading-relaxed">Check that the AKB server is enabled in your tool, this AKB address is reachable, and sign-in completed.</p></details>
    </SetupStep>
    </div>
  </div>;
}
