import { useEffect, useId, useState, type ReactNode, type RefCallback } from "react";
import { ArrowLeft, ArrowRight, CircleHelp } from "lucide-react";
import { authSessionSnapshot, isCurrentAuthSession, type AuthConfig, type AuthSessionSnapshot } from "@/lib/api";
import { ConnectionTroubleshooting } from "@/components/connection-troubleshooting";
import { AUTHENTICATION_LABELS, CONNECTION_GUIDES, CONNECTION_TEST_PROMPT, TRANSPORT_LABELS } from "@/lib/connection-guide";
import { type PatReceipt } from "@/lib/api-pat-issuance";
import { scopeSummary, type PatDraft } from "@/lib/pat-draft";
import { PatIssuanceForm } from "@/components/pat-issuance-form";
import { MCP_SNIPPET_LABELS, MCP_AGENT_LABELS, hasMcpRecipe, mcpLoginCommand, mcpSnippet, supportsSecureMcpEndpoint, type McpAgent, type McpAuthentication, type McpTransport } from "@/lib/mcp-snippets";
import { Alert } from "@/components/ui/alert";
import { Button } from "@/components/ui/button";
import { CodeSnippet } from "@/components/ui/code-snippet";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
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

function SetupChoice<Value extends string>({ label, name, value, options, disabled, onChange }: {
  label: string; name: string; value: Value | null;
  options: { value: Value; label: string; disabled?: boolean }[];
  disabled: boolean; onChange: (value: Value) => void;
}) {
  return <fieldset disabled={disabled} className="connection-compact-choices min-w-0">
    <legend className="mb-2 text-xs font-medium text-foreground-muted">{label}</legend>
    <div className="connection-choice-row flex flex-wrap gap-1" data-pair={options.length === 2 || undefined}>
      {options.map(option => <label key={option.value} data-selected={value === option.value} data-disabled={disabled || Boolean(option.disabled)} className="connection-choice-option">
        <input type="radio" className="sr-only" name={name} value={option.value} disabled={option.disabled} checked={value === option.value} onChange={() => onChange(option.value)} />
        {option.label}
      </label>)}
    </div>
  </fieldset>;
}

const TRANSPORTS = ["stdio", "http"] as const;
const AUTHENTICATION_METHODS = ["oauth", "pat"] as const;
const AGENTS = (Object.keys(MCP_AGENT_LABELS) as McpAgent[]).map(value => ({ value, label: MCP_AGENT_LABELS[value] }));

export function ConnectionSetup({ authConfig, onRetryAuthConfig, onTokenCreated, onSecretCreated, onBusyChange, invalidatedTokenId, onDirtyChange, initialDraft, replacement = false, onReceipt, layout = "compact", ownsOperation }: {
  layout?: "compact" | "workspace" | "settings";
  authConfig: AuthConfig;
  onRetryAuthConfig?: () => void;
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
  const [copied, setCopied] = useState<number | null>(null);
  const [confirmation, setConfirmation] = useState<number | null>(null);
  const [agent, setAgent] = useState<McpAgent>("claude");
  const [transport, setTransport] = useState<McpTransport>("stdio");
  const [authentication, setAuthentication] = useState<McpAuthentication | null>(null);
  const [patRejected, setPatRejected] = useState(false);
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
  const [issuanceMounted, setIssuanceMounted] = useState(false);
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
  const endpoint = authConfig.mcp_endpoint_url;
  const patEnabled = authConfig.available && authConfig.personal_access_tokens.enabled && !patRejected;
  const oauthEnabled = authConfig.available && !replacement && authConfig.mcp_oauth.enabled;
  const methods = AUTHENTICATION_METHODS.filter(method => method === "pat" ? patEnabled : oauthEnabled);
  const supports = (selectedTransport: McpTransport, method: McpAuthentication) =>
    methods.includes(method) && hasMcpRecipe(agent, selectedTransport, method);
  const transportAvailable = (option: McpTransport) => methods.some(method => supports(option, method));
  const authenticationOptions = methods.filter(method => supports(transport, method));
  const selectedAuthentication = authentication !== null && authenticationOptions.includes(authentication) ? authentication : null;
  const secureEndpoint = endpoint !== null && supportsSecureMcpEndpoint(endpoint);
  const showPatControls = secureEndpoint && selectedAuthentication === "pat";
  const formVisible = credentialMode === "create" && (showPatControls || !patEnabled && authentication === "pat");
  const loginCommand = endpoint && secureEndpoint && transport === "stdio" && selectedAuthentication === "oauth" ? mcpLoginCommand(endpoint) : null;
  const capabilityKey = JSON.stringify([endpoint, patEnabled, oauthEnabled]);
  const [previousCapabilities, setPreviousCapabilities] = useState(capabilityKey);
  if (capabilityKey !== previousCapabilities) {
    setPreviousCapabilities(capabilityKey);
    setConfigurationRevision(value => value + 1);
  }
  if (authentication === null && authenticationOptions.length > 0) setAuthentication(authenticationOptions[0]);
  if (currentStep === 2 && showPatControls && credentialMode === "create" && !issuanceMounted) setIssuanceMounted(true);
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

  const configuration = endpoint && secureEndpoint && selectedAuthentication === "oauth"
    ? mcpSnippet(agent, { transport, authentication: "oauth", endpoint })
    : endpoint && showPatControls && usableToken && (!freshToken || showSecret)
      ? mcpSnippet(agent, { transport, authentication: "pat", endpoint, pat: token })
      : undefined;
  const guide = CONNECTION_GUIDES[agent];
  const matches = (revision: number | null) => isCurrentAuthSession(session) && revision === configurationRevision && Boolean(configuration);
  const missingMethods = methods.filter(method => !hasMcpRecipe(agent, transport, method));
  return <div ref={rootRef} className="connection-setup min-w-0" data-layout={layout}>
    {!authConfig.available ? <Alert variant="warning">Connection options could not be verified. {onRetryAuthConfig ? <Button variant="link" onClick={onRetryAuthConfig}>Retry connection options</Button> : "Reopen connection setup to retry."}</Alert>
      : methods.length === 0 && <Alert variant="info">{replacement
        ? "Personal connection keys are disabled. You can still revoke existing keys."
        : "This deployment has no human MCP connection method enabled."}</Alert>}
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
        <SetupChoice label="AI tool" name={`${id}-agent`} value={agent} disabled={creating}
          onChange={value => { setAgent(value); setConfigurationRevision(value => value + 1); }} options={AGENTS} />
        <p className="text-sm leading-relaxed text-foreground-muted">{guide.introduction}</p>
        <p className="text-xs leading-relaxed text-foreground-muted">Have this application installed and make sure that computer can reach your AKB address, including any required VPN.</p>
        <details className="connection-inline-help"><summary>Using Claude Desktop or Claude on the web?</summary>
          <p>These presets include Claude Code. Desktop local servers and Claude remote connectors have separate setup procedures. Remote connectors need access from Claude’s cloud, even when you use the Desktop app. Check your network requirements before choosing that route.</p>
        </details>
        <div className="connection-step-actions"><Button onClick={() => jumpToStep(2)}>Continue with {MCP_AGENT_LABELS[agent]}<ArrowRight className="h-4 w-4" aria-hidden /></Button></div>
      </SetupStep>

      <SetupStep id={`${id}-add`} number={2} title={`Add AKB to ${MCP_AGENT_LABELS[agent]}`} description="Follow the instructions on the computer where this AI tool runs."
        current={currentStep === 2} sectionRef={element => { stepsRef.current[1] = element; }}>
        {endpoint && !loginCommand && <p className="break-all text-xs text-foreground-muted">Installation endpoint <code className="text-foreground">{endpoint}</code></p>}
        {methods.length > 0 && <div className="space-y-4">
          <div className="space-y-2">
            <SetupChoice label="Connection transport" name={`${id}-transport`} value={transportAvailable(transport) ? transport : null} disabled={creating}
              options={TRANSPORTS.map(value => ({ value, label: TRANSPORT_LABELS[value], disabled: !transportAvailable(value) }))}
              onChange={value => { setTransport(value); setConfigurationRevision(revision => revision + 1); }} />
            {!loginCommand && <p className="text-xs leading-relaxed text-foreground-muted">{transport === "stdio"
              ? "The helper runs on your computer and adds local file tools."
              : "Your AI tool connects to AKB directly. Local file tools require the helper."}</p>}
            {transport !== "http" && !transportAvailable("http") && <p className="text-xs text-foreground-muted">This guide has no tested direct HTTP preset for {MCP_AGENT_LABELS[agent]} with the available authentication methods.</p>}
          </div>
          {authenticationOptions.length > 0 && <SetupChoice label="Authentication" name={`${id}-auth`} value={selectedAuthentication} disabled={creating}
            options={authenticationOptions.map(value => ({ value, label: AUTHENTICATION_LABELS[value] }))}
            onChange={value => { setAuthentication(value); setConfigurationRevision(revision => revision + 1); }} />}
          {missingMethods.length > 0 && <p className="text-xs leading-relaxed text-foreground-muted">This guide has no tested {TRANSPORT_LABELS[transport]} preset for {MCP_AGENT_LABELS[agent]} with {missingMethods.map(method => AUTHENTICATION_LABELS[method]).join(" or ")}. {!selectedAuthentication && "Choose an available transport or authentication method."}</p>}
          {authentication !== null && !methods.includes(authentication) && <Alert variant="info">This installation no longer allows the selected authentication method. Choose an available method or ask your administrator about the policy.</Alert>}
        </div>}
        {invalidationNotice && <Alert variant="warning">{invalidationNotice}</Alert>}
        {endpoint && methods.length > 0 && !secureEndpoint && <Alert variant="warning">Connection setup requires HTTPS or loopback HTTP. Ask your administrator to check the installation’s public MCP address and use a trusted certificate.</Alert>}
        <div hidden={!showPatControls} inert={!showPatControls} className="space-y-3">
          {transport === "http" && selectedAuthentication === "pat" && <p className="text-xs leading-relaxed text-foreground-muted">Use your personal configuration file and keep its Authorization header private.</p>}
          {transport === "stdio" && <p className="text-xs leading-relaxed text-foreground-muted">Install a supported Node.js release with npx on the computer that runs the helper.</p>}
          {!freshToken && <SetupChoice label="Connection key" name={`${id}-credential`} value={credentialMode} disabled={creating}
            onChange={value => { setCredentialMode(value); setConfigurationRevision(value => value + 1); }}
            options={[{ value: "create", label: "Create a key" }, { value: "existing", label: "Use an existing key" }]} />}
          {!freshToken && credentialMode === "existing" && <div className="space-y-2">
            <Label htmlFor={`${id}-saved`}>Full connection key</Label>
            <Input id={`${id}-saved`} type="password" value={existingToken} onChange={event => { setExistingToken(event.target.value); setInvalidationNotice(null); setConfigurationRevision(value => value + 1); }} autoComplete="off" spellCheck={false} aria-describedby={`${id}-saved-help`} />
            <p id={`${id}-saved-help`} className="text-xs text-foreground-muted">Paste the full key you saved. The prefix shown in your key list cannot sign in. It stays in this setup window only.</p>
            {existingToken && !usableToken && <Alert variant="destructive">Enter the full key using only letters, numbers, underscores, and hyphens. A prefix or placeholder cannot authenticate.</Alert>}
          </div>}
        </div>
        {!freshToken && issuanceMounted && <div hidden={!formVisible} inert={!formVisible}>
          <PatIssuanceForm issuanceEnabled={patEnabled && secureEndpoint} ownsOperation={ownsOperation} initial={initialDraft} replacement={replacement} onDirtyChange={setFormDirty} onBusyChange={value => { setCreating(value); onBusyChange?.(value); }} onMetadataChanged={onTokenCreated} onPolicyRejected={() => {
            setPatRejected(true);
            onRetryAuthConfig?.();
          }} onCreated={(result, snapshot) => {
            setFreshToken(result.token); setFreshTokenId(result.token_id); setReceipt(result); setSession(snapshot); setCredentialMode("create"); setShowSecret(true); setInvalidationNotice(null); setConfigurationRevision(value => value + 1); onSecretCreated?.(); onReceipt?.(result, snapshot);
          }} />
        </div>}
        {freshToken && (showPatControls || !patEnabled) && isCurrentAuthSession(session) && <div className="space-y-2 rounded-[var(--radius-md)] border border-accent/40 bg-accent/5 p-3">
          <p role="status" className="text-sm font-medium">Connection key created</p>
          <p className="text-xs leading-relaxed text-foreground-muted">{patEnabled ? "Keep this window open until you have saved the key or configuration privately." : "You can save this one-time key. Key-based setup is currently unavailable."} Closing clears the displayed copy; it does not revoke the key.</p>
          <p className="text-xs text-foreground-muted">{receipt?.verified ? `${receipt.scopes?.includes("write") ? "Read and write" : receipt.scopes?.includes("read") ? "Read only" : "Permissions unavailable"} · ${typeof receipt.expires_at === "string" ? `Expires ${receipt.expires_at}` : receipt.expires_at === null ? "No expiration" : "Expiration unavailable"} · ${scopeSummary(receipt.vault_scope)}` : "Created with server defaults; restrictions were not verified."}</p>
          <Button type="button" variant="outline" size="sm" onClick={() => { setShowSecret(!showSecret); setConfigurationRevision(revision => revision + 1); }}>{showSecret ? "Hide key" : "Show key"}{patEnabled && " and configuration"}</Button>
          {showSecret && currentStep === 2 && <details className="connection-inline-help"><summary>Copy the key separately</summary><CodeSnippet code={freshToken} filename="Personal connection key" /></details>}
        </div>}
        {configuration && currentStep === 2 ? <div className="space-y-3">
          {loginCommand && <>
            <div className="connection-instruction"><span>1</span><div><h4>Sign in from your terminal</h4><p>Clear <code>AKB_PAT</code> in your terminal and AI tool first.</p></div></div>
            <CodeSnippet key={`login:${endpoint}:${configurationRevision}`} code={loginCommand} filename="Terminal · AKB sign-in" />
            <div className="space-y-1 text-xs leading-relaxed text-foreground-muted">
              <p>The helper runs on your computer and adds local file tools. Run the command as the same OS user that runs the helper. Complete browser sign-in and wait for the command to finish.</p>
              <p className="break-all">Installation endpoint <code className="text-foreground">{endpoint}</code></p>
              <p>Requires akb-mcp 2.4.0 or later and a supported Node.js release on macOS or Linux.</p>
            </div>
          </>}
          <div className="connection-instruction"><span>{loginCommand ? 2 : 1}</span><div><h4>Open the right place</h4><p>{guide.location}</p></div></div>
          <div className="connection-instruction"><span>{loginCommand ? 3 : 2}</span><div><h4>Add this setup</h4><p>{guide.add}</p></div></div>
          <CodeSnippet key={configurationRevision} code={configuration.code} filename={MCP_SNIPPET_LABELS[agent]} onCopied={() => setCopied(previous => Math.max(previous ?? -1, configurationRevision))} />
          {matches(copied) && <p role="status" className="text-xs text-foreground-muted">Setup copied. Apply it in {MCP_AGENT_LABELS[agent]} before checking the connection.</p>}
          {configuration.signIn && <div className="connection-instruction"><span>3</span><div><h4>Approve access</h4><p>{configuration.signIn}</p></div></div>}
          {transport === "http" && selectedAuthentication === "pat" && <p className="text-xs leading-relaxed text-foreground-muted">Use your personal configuration file and keep its Authorization header private.</p>}
          {transport === "stdio" && <p className="text-xs leading-relaxed text-foreground-muted">{loginCommand
            ? "The local helper uses the sign-in saved by the terminal command."
            : "The local helper uses your personal connection key."} If npx asks to download akb-mcp, allow it to continue.</p>}
        </div> : showPatControls && <p role="status" className="rounded-[var(--radius-md)] bg-surface-muted p-3 text-sm text-foreground-muted">{freshToken && !showSecret ? "Your key and setup are hidden. Show them above when you are ready to copy." : "Create a connection key or enter the full key you saved. Your setup will appear here."}</p>}
        {selectedAuthentication === "oauth" && <p className="text-xs leading-relaxed text-foreground-muted">The installation’s authorization server handles sign-in and may use your organization’s SSO. Your AI tool or helper signs in separately from this website.</p>}
        <div className="connection-step-actions"><Button variant="outline" disabled={creating} onClick={() => jumpToStep(1)}><ArrowLeft className="h-4 w-4" aria-hidden />Choose a tool</Button>
          <Button disabled={creating || !configuration} onClick={() => jumpToStep(3)}>Check in {MCP_AGENT_LABELS[agent]}<ArrowRight className="h-4 w-4" aria-hidden /></Button></div>
      </SetupStep>

      <SetupStep id={`${id}-check`} number={3} title="Check the connection" description="The result appears in your AI tool."
        current={currentStep === 3} sectionRef={element => { stepsRef.current[2] = element; }}>
        <p className="text-sm leading-relaxed text-foreground-muted">{guide.check}</p>
        <CodeSnippet code={CONNECTION_TEST_PROMPT} filename={`Send in ${MCP_AGENT_LABELS[agent]}`} />
        <div className="rounded-[var(--radius-md)] bg-surface-muted p-3 text-sm leading-relaxed"><p className="font-medium">What you should see</p><p className="mt-1 text-foreground-muted">An AKB tool response with your accessible Vaults. An empty list can also be a valid result. If a Vault is missing, check which account signed in and ask its owner for access.</p></div>
        <label className="flex min-h-11 cursor-pointer items-start gap-3 py-2 text-sm"><input className="mt-1 h-4 w-4 shrink-0 accent-primary" type="checkbox" disabled={!configuration} checked={matches(confirmation)} onChange={event => setConfirmation(event.target.checked ? configurationRevision : null)} /><span>I received an AKB response in my AI tool</span></label>
        <p role="status" className="text-xs leading-relaxed text-foreground-muted">{matches(confirmation) ? "Confirmed by you. This browser has not independently verified the connection." : "Not checked here. This browser cannot verify an external AI connection."}</p>
        <details className="connection-inline-help"><summary><CircleHelp className="mr-1 inline h-4 w-4" aria-hidden />Something not working?</summary><ConnectionTroubleshooting authentication={selectedAuthentication} transport={transport} loginCommand={loginCommand} /></details>
        <div className="connection-step-actions"><Button variant="outline" disabled={creating} onClick={() => jumpToStep(2)}><ArrowLeft className="h-4 w-4" aria-hidden />Back to setup</Button></div>
      </SetupStep>
    </div>
  </div>;
}
