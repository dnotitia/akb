import { useEffect, useId, useState, type ReactNode, type RefCallback } from "react";
import { ArrowLeft, ArrowRight, Check, CircleHelp } from "lucide-react";
import { authSessionSnapshot, isCurrentAuthSession, type AuthSessionSnapshot } from "@/lib/api";
import { ConnectionTroubleshooting } from "@/components/connection-troubleshooting";
import { CONNECTION_GUIDES, CONNECTION_TEST_PROMPT } from "@/lib/connection-guide";
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
import { useSetupStepNavigation, type SetupStepNumber } from "@/hooks/use-setup-step-navigation";

function SetupStep({ id, number, title, description, current, sectionRef, children }: {
  id: string; number: SetupStepNumber; title: string; description: string; current: boolean;
  sectionRef: RefCallback<HTMLElement>; children: ReactNode;
}) {
  return <section ref={sectionRef} id={`${id}-section`} aria-labelledby={id} hidden={!current} inert={!current}
    className="connection-step" data-current={current}>
    <header className="connection-step-heading"><span aria-hidden className="connection-step-number">{number}</span>
      <div className="min-w-0"><h3 id={id} tabIndex={-1} aria-label={`${number}. ${title}`}><span className="sr-only">{number}. </span>{title}</h3><p>{description}</p></div>
    </header>
    <div className="connection-step-body space-y-4">{children}</div>
  </section>;
}

/** One labelled choice, without another select box or nested setup branch. */
function SetupChoice({ label, name, value, options, disabled, onChange, appearance = "segmented" }: {
  label: string; name: string; value: string;
  options: { value: string; label: string; accessibleName?: string }[];
  disabled: boolean; onChange: (value: string) => void;
  appearance?: "tabs" | "segmented" | "compact";
}) {
  const compact = appearance === "compact";
  return <fieldset disabled={disabled} className={cn("min-w-0", compact && "connection-compact-choices")}>
    <legend className="mb-2 text-xs font-medium text-foreground-muted">{label}</legend>
    <div className={cn("flex flex-wrap gap-1", compact ? "connection-choice-row" : appearance === "tabs" ? "border-b border-border" : "rounded-[var(--radius-md)] border border-border bg-surface-2 p-1")}>
      {options.map(option => <label key={option.value} data-selected={value === option.value} data-disabled={disabled} className={compact ? "connection-choice-option" : cn(
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

export function ConnectionSetup({ mcpOauthEnabled, onTokenCreated, onSecretCreated, onBusyChange, invalidatedTokenId, onDirtyChange, initialDraft, replacement = false, onReceipt, layout = "compact", ownsOperation }: {
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
  ownsOperation?: () => boolean;
}) {
  const id = useId();
  const { rootRef, navigationRef, stepsRef, currentStep, jumpToStep } = useSetupStepNavigation();
  const [configurationRevision, setConfigurationRevision] = useState(0);
  const [copied, setCopied] = useState<{ revision: number; agent: McpAgent; oauth: boolean } | null>(null);
  const [confirmation, setConfirmation] = useState<{ revision: number; agent: McpAgent; oauth: boolean } | null>(null);
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
  useEffect(() => () => { if (ownsOperation?.() !== true) { onDirtyChange?.(false); onBusyChange?.(false); } }, [onDirtyChange, onBusyChange, ownsOperation]);
  useEffect(() => {
    const check = () => {
      if (isCurrentAuthSession(session)) return;
      setFreshToken(""); setFreshTokenId(null); setReceipt(null); setExistingToken("");
      setConfigurationRevision(value => value + 1);
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
  if (currentStep === 2 && !useOauth && credentialMode === "create" && !issuanceStarted) setIssuanceStarted(true);
  const token = isCurrentAuthSession(session) ? freshToken || (credentialMode === "existing" ? existingToken.trim() : "") : "";
  // A visible prefix or placeholder is never a usable configuration.
  const usableToken = /^[A-Za-z0-9_-]+$/.test(token);

  // Adjust before committing this render, so a revoked credential is never
  // briefly offered by the copy control. Unrelated fresh secrets stay intact.
  if (lastInvalidatedId !== invalidatedTokenId) {
    setLastInvalidatedId(invalidatedTokenId);
    if (invalidatedTokenId) {
      setConfigurationRevision(value => value + 1);
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

  const configVisible = useOauth || (usableToken && (!freshToken || showSecret));
  const configuration = configVisible ? useOauth ? oauthSnippet! : mcpInstallSnippets(token)[agent] : null;
  const guide = CONNECTION_GUIDES[agent];
  const matches = (record: typeof copied) => isCurrentAuthSession(session) && record?.revision === configurationRevision && record.agent === agent && record.oauth === useOauth;
  const record = { revision: configurationRevision, agent, oauth: useOauth };
  const terminal = MCP_AGENT_FILES[agent] === "terminal";
  return <div ref={rootRef} className="connection-setup min-w-0" data-layout={layout}>
    <nav ref={navigationRef} className="connection-progress" aria-label="Connection setup steps">
      <ol aria-label="Connection setup overview">
        {[
          { number: 1, label: "Choose a tool", short: "Tool", target: `${id}-tool` },
          { number: 2, label: "Add AKB", short: "Setup", target: `${id}-add` },
          { number: 3, label: "Check in your tool", short: "Check", target: `${id}-check` },
        ].map(step => <li key={step.number}><button type="button" aria-label={`Go to step ${step.number}: ${step.label}`} aria-controls={`${step.target}-section`}
          disabled={creating} aria-current={currentStep === step.number ? "step" : undefined} onClick={() => jumpToStep(step.number as SetupStepNumber)}>
          <span aria-hidden className="connection-progress-number">{step.number}</span><span aria-hidden className="connection-progress-label">{step.label}</span><span aria-hidden className="connection-progress-short">{step.short}</span>
        </button></li>)}
      </ol>
    </nav>
    <div className="connection-steps">
      <SetupStep id={`${id}-tool`} number={1} title="Choose your AI tool" description="Where would you like to use your AKB knowledge?"
        current={currentStep === 1} sectionRef={element => { stepsRef.current[0] = element; }}>
        <SetupChoice label="AI tool" appearance="compact" name={`${id}-agent`} value={agent} disabled={creating}
          onChange={value => { setAgent(value as McpAgent); setConfigurationRevision(value => value + 1); }} options={Object.entries(MCP_AGENT_LABELS).map(([value, label]) => ({ value, label }))} />
        <p className="text-sm leading-relaxed text-foreground-muted">{guide.introduction}</p>
        <p className="text-xs leading-relaxed text-foreground-muted">Have this application installed and make sure that computer can reach your AKB address, including any required VPN.</p>
        <details className="connection-inline-help"><summary>Using Claude Desktop or Claude on the web?</summary>
          <p>These presets include Claude Code. Desktop local servers and Claude remote connectors have separate setup procedures. Remote connectors need access from Claude’s cloud, even when you use the Desktop app. Check your network requirements before choosing that route.</p>
        </details>
        <div className="connection-step-actions"><Button onClick={() => jumpToStep(2)}>Continue with {MCP_AGENT_LABELS[agent]}<ArrowRight className="h-4 w-4" aria-hidden /></Button></div>
      </SetupStep>

      <SetupStep id={`${id}-add`} number={2} title={`Add AKB to ${MCP_AGENT_LABELS[agent]}`} description="Follow the instructions on the computer where this AI tool runs."
        current={currentStep === 2} sectionRef={element => { stepsRef.current[1] = element; }}>
        {oauthAvailable ? <SetupChoice label="How to connect" appearance="compact" name={`${id}-access`} value={useOauth ? "oauth" : "key"} disabled={creating}
          onChange={value => { setAuth(value === "oauth" ? "oauth" : "pat"); setConfigurationRevision(value => value + 1); }}
          options={[{ value: "oauth", label: "Sign in with AKB" }, { value: "key", label: "Use a connection key" }]} />
          : <p className="text-sm text-foreground-muted">This AKB guide uses a personal connection key for {MCP_AGENT_LABELS[agent]}.</p>}
        {invalidationNotice && <Alert variant="warning">{invalidationNotice}</Alert>}
        <details className="connection-inline-help"><summary>Which connection method should I use?</summary>
          <p>Sign in with AKB lets your AI tool connect directly after you approve access. You do not create a personal key. The connection-key preset runs a local helper with npx and also supports uploading and downloading files on that computer. Your account’s Vault permissions apply to both.</p>
          <p>For a key-based setup, install a supported Node.js release if npx is unavailable. Web browsing in AKB is ready to use without setting up an AI connection.</p>
        </details>
        <div hidden={useOauth} inert={useOauth} className="space-y-3">
          <p className="text-xs leading-relaxed text-foreground-muted">Before using a connection key, check that Node.js (including npx) is installed on that computer. It runs the local AKB helper for your AI tool.</p>
          {!freshToken && <SetupChoice label="Connection key" appearance="compact" name={`${id}-credential`} value={credentialMode} disabled={creating}
            onChange={value => { setCredentialMode(value); setConfigurationRevision(value => value + 1); }}
            options={[{ value: "create", label: "Create a key" }, { value: "existing", label: "Use an existing key" }]} />}
          {!freshToken && credentialMode === "existing" && <div className="space-y-2">
            <Label htmlFor={`${id}-saved`}>Full connection key</Label>
            <Input id={`${id}-saved`} type="password" value={existingToken} onChange={event => { setExistingToken(event.target.value); setInvalidationNotice(null); setConfigurationRevision(value => value + 1); }} autoComplete="off" spellCheck={false} aria-describedby={`${id}-saved-help`} />
            <p id={`${id}-saved-help`} className="text-xs text-foreground-muted">Paste the full key you saved. The prefix shown in your key list cannot sign in. It stays in this setup window only.</p>
            {existingToken && !usableToken && <Alert variant="destructive">Enter the full key using only letters, numbers, underscores, and hyphens. A prefix or placeholder cannot authenticate.</Alert>}
          </div>}
          {/* Keep the issuance session mounted when another method or step is visible. */}
          {!freshToken && issuanceStarted && <div hidden={credentialMode !== "create"} inert={credentialMode !== "create"}>
            <PatIssuanceForm ownsOperation={ownsOperation} initial={initialDraft} replacement={replacement} onDirtyChange={setFormDirty} onBusyChange={value => { setCreating(value); onBusyChange?.(value); }} onMetadataChanged={onTokenCreated} onCreated={(result, snapshot) => {
              setFreshToken(result.token); setFreshTokenId(result.token_id); setReceipt(result); setSession(snapshot); setAuth("pat"); setCredentialMode("create"); setShowSecret(true); setInvalidationNotice(null); setConfigurationRevision(value => value + 1); onSecretCreated?.(); onReceipt?.(result, snapshot);
            }} />
          </div>}
          {freshToken && isCurrentAuthSession(session) && <div className="space-y-2 rounded-[var(--radius-md)] border border-accent/40 bg-accent/5 p-3">
            <p role="status" className="text-sm font-medium">Connection key created</p>
            <p className="text-xs leading-relaxed text-foreground-muted">Your setup below includes this key. Keep this window open until you have saved the key or configuration privately. Closing clears the displayed copy; it does not revoke the key.</p>
            <p className="text-xs text-foreground-muted">{receipt?.verified ? `${receipt.scopes?.includes("write") ? "Read and write" : receipt.scopes?.includes("read") ? "Read only" : "Permissions unavailable"} · ${typeof receipt.expires_at === "string" ? `Expires ${receipt.expires_at}` : receipt.expires_at === null ? "No expiration" : "Expiration unavailable"} · ${scopeSummary(receipt.vault_scope)}` : "Created with server defaults; restrictions were not verified."}</p>
            <Button type="button" variant="outline" size="sm" onClick={() => setShowSecret(!showSecret)}>{showSecret ? "Hide key and configuration" : "Show key and configuration"}</Button>
            {showSecret && !useOauth && currentStep === 2 && <details className="connection-inline-help"><summary>Copy the key separately</summary><CodeSnippet code={freshToken} filename="Personal connection key" /></details>}
          </div>}
        </div>
        {configuration && currentStep === 2 ? <div className="space-y-3">
          <div className="connection-instruction"><span>1</span><div><h4>Open the right place</h4><p>{guide.location}</p></div></div>
          <div className="connection-instruction"><span>2</span><div><h4>Add this setup</h4><p>{guide.add}</p></div></div>
          <CodeSnippet key={`${agent}:${useOauth}:${configurationRevision}`} code={configuration} filename={terminal ? `Terminal · ${MCP_AGENT_LABELS[agent]}` : MCP_AGENT_FILES[agent]} onCopied={() => setCopied(record)} />
          {matches(copied) && <p role="status" className="text-xs text-foreground-muted">Setup copied. Apply it in {MCP_AGENT_LABELS[agent]} before checking the connection.</p>}
          {useOauth && <div className="connection-instruction"><span>3</span><div><h4>Approve access</h4><p>{guide.signIn}</p></div></div>}
          {!useOauth && <p className="text-xs leading-relaxed text-foreground-muted">The local helper uses this key to reach AKB. If npx asks to download akb-mcp, allow it to continue.</p>}
        </div> : <p role="status" className="rounded-[var(--radius-md)] bg-surface-muted p-3 text-sm text-foreground-muted">{freshToken && !showSecret ? "Your key and setup are hidden. Show them above when you are ready to copy." : "Create a connection key or enter the full key you saved. Your setup will appear here."}</p>}
        <div className="connection-step-actions"><Button variant="outline" disabled={creating} onClick={() => jumpToStep(1)}><ArrowLeft className="h-4 w-4" aria-hidden />Choose a tool</Button>
          <Button disabled={creating || !configuration} onClick={() => jumpToStep(3)}>Check in {MCP_AGENT_LABELS[agent]}<ArrowRight className="h-4 w-4" aria-hidden /></Button></div>
      </SetupStep>

      <SetupStep id={`${id}-check`} number={3} title="Check the connection" description="The result appears in your AI tool."
        current={currentStep === 3} sectionRef={element => { stepsRef.current[2] = element; }}>
        <p className="text-sm leading-relaxed text-foreground-muted">{guide.check}</p>
        <CodeSnippet code={CONNECTION_TEST_PROMPT} filename={`Send in ${MCP_AGENT_LABELS[agent]}`} />
        <div className="rounded-[var(--radius-md)] bg-surface-muted p-3 text-sm leading-relaxed"><p className="font-medium">What you should see</p><p className="mt-1 text-foreground-muted">An AKB tool response with your accessible Vaults. An empty list can also be a valid result. If a Vault is missing, check which account signed in and ask its owner for access.</p></div>
        <label className="flex min-h-11 cursor-pointer items-start gap-3 py-2 text-sm"><input className="mt-1 h-4 w-4 shrink-0 accent-primary" type="checkbox" checked={matches(confirmation)} onChange={event => setConfirmation(event.target.checked ? record : null)} /><span>I received an AKB response in my AI tool</span></label>
        <p role="status" className="text-xs leading-relaxed text-foreground-muted">{matches(confirmation) ? "Confirmed by you. This browser has not independently verified the connection." : "Not checked here. This browser cannot verify an external AI connection."}</p>
        <details className="connection-inline-help"><summary><CircleHelp className="mr-1 inline h-4 w-4" aria-hidden />Something not working?</summary><ConnectionTroubleshooting /></details>
        <div className="connection-step-actions"><Button variant="outline" disabled={creating} onClick={() => jumpToStep(2)}><ArrowLeft className="h-4 w-4" aria-hidden />Back to setup</Button></div>
      </SetupStep>
    </div>
  </div>;
}
