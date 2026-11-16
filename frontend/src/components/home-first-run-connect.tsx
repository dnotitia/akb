import { useEffect, useId, useRef, useState, type ReactNode } from "react";
import { Link } from "react-router-dom";
import { ArrowUpRight, Check, CheckCircle2, Loader2, PlugZap } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Panel } from "@/components/ui/panel";
import { Segmented } from "@/components/ui/segmented";
import { CodeSnippet } from "@/components/ui/code-snippet";
import { CopyButton } from "@/components/ui/copy-button";
import { PatIssuanceForm } from "@/components/pat-issuance-form";
import { QuickstartDialog } from "@/components/quickstart-dialog";
import { isCurrentAuthSession, listPATs, type AuthConfig, type AuthSessionSnapshot } from "@/lib/api";
import type { PatReceipt } from "@/lib/api-pat-issuance";
import { defaultPatDraft } from "@/lib/pat-draft";
import {
  HANDOFF_APP_LABELS, firstVaultRequest, followUpLink, setupLink, setupRequest, signInLabel, supportsSignIn, tryRequests,
  type HandoffApp, type HandoffCredential,
} from "@/lib/ai-app-handoff";
import { cn } from "@/lib/utils";

export type FirstRunChoice = HandoffApp | "other";

const CHOICES: { value: FirstRunChoice; label: string; description: string }[] = [
  { value: "claude-code", label: "Claude Code", description: "Code in the Claude app, or the terminal" },
  { value: "codex", label: "Codex", description: "The Codex or ChatGPT app, or the terminal" },
  { value: "claude-desktop", label: "Claude Desktop", description: "Chats in the Claude app" },
  { value: "other", label: "Other tools", description: "Cursor, VS Code and more" },
];

/** How often the guide re-checks for a used key or a new vault while it waits. */
export const FIRST_RUN_POLL_MS = 5000;

type StepState = "todo" | "current" | "done";

function Step({ number, state, title, children, action }: { number: number; state: StepState; title: string; children?: ReactNode; action?: ReactNode }) {
  return <li className={cn("flex flex-wrap items-start gap-x-3 gap-y-3 rounded-[var(--radius-md)] border p-3 sm:p-4",
    state === "current" ? "border-border-strong bg-surface-selected" : "border-border bg-surface")}
    aria-current={state === "current" ? "step" : undefined}>
    <span aria-hidden className={cn("mt-0.5 inline-flex h-6 w-6 shrink-0 items-center justify-center rounded-full text-xs font-semibold",
      state === "done" ? "bg-success text-success-foreground" : state === "current" ? "bg-primary text-primary-foreground" : "bg-surface-2 text-foreground-muted")}>
      {state === "done" ? <Check className="h-3.5 w-3.5" /> : number}
    </span>
    <div className="min-w-0 flex-[1_1_16rem] space-y-1">
      <h3 className="text-sm font-semibold leading-6">{state === "done" && <span className="sr-only">Done: </span>}{title}</h3>
      {children && <div className="space-y-2 text-sm leading-relaxed text-foreground-muted">{children}</div>}
    </div>
    {action && <div className="flex shrink-0 flex-wrap items-center gap-2">{action}</div>}
  </li>;
}

function OpenLink({ href, label, primary, onOpen }: { href: string; label: string; primary: boolean; onOpen: () => void }) {
  return <Button asChild variant={primary ? "accent" : "outline"} className="min-h-11">
    <a href={href} onClick={onOpen}>{label}<ArrowUpRight className="h-4 w-4" aria-hidden /></a>
  </Button>;
}

/**
 * First-run guide on Home: pick an AI app, open it with a prepared setup
 * request, approve once, then ask that app for the first vault. It advances on
 * signals AKB can observe today: a connection key's last use and a vault
 * appearing in the directory.
 */
export function HomeFirstRunConnect({ authConfig, vaults, onRefreshVaults, onStarted, onDismiss }: {
  authConfig: AuthConfig;
  vaults: { name: string }[];
  /** Re-read the vault directory; called while waiting for the first vault. */
  onRefreshVaults: () => void;
  /** The person started a hand-off, so Home keeps this guide once a vault appears. */
  onStarted: () => void;
  onDismiss: () => void;
}) {
  const id = useId();
  const [choice, setChoice] = useState<FirstRunChoice>("claude-code");
  const [keys, setKeys] = useState<Partial<Record<HandoffApp, { receipt: PatReceipt; snapshot: AuthSessionSnapshot }>>>({});
  const [opened, setOpened] = useState<Partial<Record<HandoffApp, boolean>>>({});
  const [usedTokens, setUsedTokens] = useState<Record<string, true>>({});
  const [waitingForVault, setWaitingForVault] = useState(false);
  const [guideOpen, setGuideOpen] = useState(false);
  const guideTrigger = useRef<HTMLButtonElement>(null);
  const refreshVaults = useRef(onRefreshVaults);
  refreshVaults.current = onRefreshVaults;

  const app = choice === "other" ? null : choice;
  const stored = app ? keys[app] : undefined;
  // A key belongs to the sign-in session that created it; never reuse it after a switch.
  const key = stored && isCurrentAuthSession(stored.snapshot) ? stored.receipt : null;
  const signIn = app ? supportsSignIn(app, authConfig) : false;
  const credential: HandoffCredential | null = !app ? null : signIn ? { kind: "oauth", signInWith: signInLabel(authConfig) } : key ? { kind: "key", token: key.token } : null;
  const request = app && credential ? setupRequest(app, credential) : null;
  const pendingTokenId = app && key && opened[app] && !usedTokens[key.token_id] ? key.token_id : null;
  const firstVault = vaults.find(vault => vault.name.endsWith("-notes")) ?? vaults[0];

  useEffect(() => {
    if (!pendingTokenId) return;
    let cancelled = false;
    const check = () => {
      listPATs().then(data => {
        if (cancelled) return;
        const token = Array.isArray(data.tokens) ? data.tokens.find(item => item.token_id === pendingTokenId) : undefined;
        if (token?.last_used_at) setUsedTokens(current => ({ ...current, [pendingTokenId]: true }));
      }).catch(() => { /* Retry on the next tick; a lookup failure is not a connection failure. */ });
    };
    check();
    const timer = window.setInterval(check, FIRST_RUN_POLL_MS);
    window.addEventListener("focus", check);
    return () => { cancelled = true; window.clearInterval(timer); window.removeEventListener("focus", check); };
  }, [pendingTokenId]);

  const hasVault = vaults.length > 0;
  useEffect(() => {
    if (!waitingForVault || hasVault) return;
    const refresh = () => refreshVaults.current();
    const timer = window.setInterval(refresh, FIRST_RUN_POLL_MS);
    window.addEventListener("focus", refresh);
    return () => { window.clearInterval(timer); window.removeEventListener("focus", refresh); };
  }, [waitingForVault, hasVault]);

  if (hasVault && firstVault) {
    const label = app ? HANDOFF_APP_LABELS[app] : null;
    return <section aria-labelledby={`${id}-ready`}>
      <Panel className="space-y-4 p-5 sm:p-6">
        <div className="flex items-start gap-3">
          <CheckCircle2 className="mt-0.5 h-5 w-5 shrink-0 text-success" aria-hidden />
          <div className="min-w-0">
            <h2 id={`${id}-ready`} className="text-lg font-semibold tracking-tight">Your first vault is ready</h2>
            <p className="mt-1 text-sm leading-relaxed text-foreground-muted">
              {firstVault.name} is in AKB. {label ? `Try one of these in ${label}.` : "Try one of these in your AI tool."}
            </p>
          </div>
        </div>
        <ul className="divide-y divide-border rounded-[var(--radius-md)] border border-border">
          {tryRequests(firstVault.name).map(item => <li key={item.title} className="flex flex-wrap items-center justify-between gap-x-4 gap-y-2 px-4 py-3">
            <div className="min-w-0 flex-[1_1_16rem]">
              <p className="text-sm font-medium">{item.title}</p>
              <p className="mt-0.5 text-xs leading-relaxed text-foreground-muted">{item.request}</p>
            </div>
            <div className="flex items-center gap-1">
              {app && <Button asChild variant="outline" size="sm"><a href={followUpLink(app, item.request)}>Open in {app === "claude-desktop" ? "Claude" : label}<ArrowUpRight className="h-3.5 w-3.5" aria-hidden /></a></Button>}
              <CopyButton value={item.request} label={`Copy request: ${item.title}`} />
            </div>
          </li>)}
        </ul>
        <div className="flex flex-wrap items-center gap-2">
          <Button asChild variant="accent"><Link to={`/vault/${encodeURIComponent(firstVault.name)}`}>Open {firstVault.name}</Link></Button>
          <Button variant="ghost" onClick={onDismiss}>Done</Button>
        </div>
      </Panel>
    </section>;
  }

  function markOpened(target: HandoffApp) {
    setOpened(current => ({ ...current, [target]: true }));
    onStarted();
  }

  return <section aria-labelledby={`${id}-title`}>
    <Panel className="space-y-5 p-5 sm:p-6">
      <div className="flex items-start gap-3">
        <PlugZap className="mt-1 h-5 w-5 shrink-0 text-link" aria-hidden />
        <div className="min-w-0">
          <h2 id={`${id}-title`} className="text-lg font-semibold tracking-tight">Use AKB from your AI app</h2>
          <p className="mt-1 max-w-2xl text-sm leading-relaxed text-foreground-muted">
            Pick the app you use. AKB prepares the setup request, the app runs it, and you approve it once. Then ask the app for your first vault.
          </p>
        </div>
      </div>

      <Segmented aria-label="AI app" value={choice} onChange={value => setChoice(value as FirstRunChoice)}
        className="grid-cols-1 sm:grid-cols-2 xl:grid-cols-4" options={CHOICES} />

      {app ? <AppSteps key={app} app={app} signIn={signIn} signInWith={signInLabel(authConfig)} keyReceipt={key} request={request}
        opened={!!opened[app]} keyUsed={key ? !!usedTokens[key.token_id] : false} waitingForVault={waitingForVault}
        onKeyCreated={(receipt, snapshot) => setKeys(current => ({ ...current, [app]: { receipt, snapshot } }))}
        onOpenSetup={() => markOpened(app)}
        onOpenVaultRequest={() => { setWaitingForVault(true); onStarted(); }} />
        : <div className="flex flex-wrap items-center justify-between gap-3 rounded-[var(--radius-md)] border border-border p-4">
          <p className="min-w-0 flex-[1_1_18rem] text-sm leading-relaxed text-foreground-muted">
            Cursor, VS Code, OpenClaw and other MCP tools each have their own settings. The full setup guide shows what to add for each one.
          </p>
          <Button ref={guideTrigger} variant="outline" onClick={() => setGuideOpen(true)} aria-haspopup="dialog">Open the full setup guide</Button>
        </div>}
    </Panel>
    <QuickstartDialog open={guideOpen} onOpenChange={setGuideOpen} mcpOauthEnabled={authConfig.available && authConfig.mcp_oauth.enabled} returnFocusRef={guideTrigger} />
  </section>;
}

function AppSteps({ app, signIn, signInWith, keyReceipt, request, opened, keyUsed, waitingForVault, onKeyCreated, onOpenSetup, onOpenVaultRequest }: {
  app: HandoffApp; signIn: boolean; signInWith: string; keyReceipt: PatReceipt | null; request: string | null;
  opened: boolean; keyUsed: boolean; waitingForVault: boolean;
  onKeyCreated: (receipt: PatReceipt, snapshot: AuthSessionSnapshot) => void;
  onOpenSetup: () => void; onOpenVaultRequest: () => void;
}) {
  const label = HANDOFF_APP_LABELS[app];
  // Claude Desktop chats are configured from a Claude Code session in the same app.
  const setupApp = app === "claude-desktop" ? "Claude Code" : label;
  const setupPlace = app === "codex" ? "chat" : "session";
  const vaultRequest = firstVaultRequest();
  const steps: { title: string; state: StepState; body?: ReactNode; action?: ReactNode }[] = [];

  if (!signIn) {
    steps.push({
      title: "Create a connection key",
      state: keyReceipt ? "done" : "current",
      body: keyReceipt
        ? <p role="status">Key “{keyReceipt.name}” created{typeof keyReceipt.expires_at === "string" ? `, expires ${new Date(keyReceipt.expires_at).toLocaleDateString()}` : ""}. The setup request below includes it.</p>
        : <>
          <p>{app === "claude-desktop"
            ? "Claude Desktop chats reach AKB through a small local helper that uses a connection key."
            : "This AKB connects AI apps with a connection key."} Your account permissions still apply.</p>
          <PatIssuanceForm initial={{ ...defaultPatDraft(), name: label, expiration: "30" }} advancedOpen={false} onCreated={onKeyCreated} />
        </>,
    });
  }

  steps.push({
    title: `Open ${setupApp} with the setup request`,
    state: !request ? "todo" : opened ? "done" : "current",
    body: <p>A new {setupApp === "Codex" ? "chat" : "session"} opens with the request filled in. Check it and press Send. If your browser asks to open the app, allow it.</p>,
    action: request
      ? <OpenLink href={setupLink(app, request)} label={`Open in ${setupApp}`} primary={!opened} onOpen={onOpenSetup} />
      : <Button variant="outline" disabled>Open in {setupApp}</Button>,
  });

  // A connection key reports its first use; an OAuth sign-in is not visible to
  // the browser, so asking for the vault is the next observable step.
  const connected = signIn ? waitingForVault : keyUsed;
  steps.push({
    title: signIn ? `Approve and sign in with ${signInWith}` : `Approve in ${setupApp}`,
    state: !opened ? "todo" : connected ? "done" : "current",
    body: <>
      <p>{signIn
        ? `Allow the command when ${setupApp} asks. A browser window opens for the sign-in${app === "claude-code" ? "; finish it within 5 minutes" : ""}.`
        : `Allow the command when ${setupApp} asks.${app === "claude-desktop" ? " It also installs Node.js if that is missing." : ""}`}</p>
      {!signIn && opened && <p role="status" className="flex items-center gap-2">
        {keyUsed ? <><CheckCircle2 className="h-4 w-4 text-success" aria-hidden />{label} is connected.</>
          : <><Loader2 className="h-4 w-4 animate-spin" aria-hidden />Waiting for {label} to use the key…</>}
      </p>}
    </>,
  });

  steps.push({
    title: "Ask for your first vault",
    state: waitingForVault || (opened && connected) ? "current" : "todo",
    body: <>
      <p>{app === "claude-desktop"
        ? "Quit and reopen the Claude app first, then open this request in a new chat."
        : `A new ${setupPlace} opens so ${label} loads AKB. Press Send.`}</p>
      {waitingForVault && <p role="status" className="flex items-center gap-2"><Loader2 className="h-4 w-4 animate-spin" aria-hidden />This page updates when your first vault appears.</p>}
    </>,
    action: <OpenLink href={followUpLink(app, vaultRequest)} label="Open the vault request" primary={false} onOpen={onOpenVaultRequest} />,
  });

  return <div className="space-y-4">
    <ol className="space-y-2" aria-label={`Connect ${label}`}>
      {steps.map((step, index) => <Step key={step.title} number={index + 1} state={step.state} title={step.title} action={step.action}>{step.body}</Step>)}
    </ol>
    <details className="text-sm">
      <summary className="cursor-pointer text-link hover:text-link-hover">Link not opening? Copy the requests instead</summary>
      <div className="mt-3 space-y-3">
        {request ? <CodeSnippet code={request} filename={`Setup request · paste into ${setupApp}`} />
          : <p className="text-foreground-muted">Create the connection key first. The setup request appears here.</p>}
        <CodeSnippet code={vaultRequest} filename="First vault request" />
      </div>
    </details>
  </div>;
}
