import { useEffect, useMemo, useState } from "react";
import { Link } from "react-router-dom";
import {
  ChevronRight,
  Copy,
  Eye,
  EyeOff,
  Plus,
  RotateCw,
  Trash2,
  X,
} from "lucide-react";
import { createPAT, revokePAT } from "@/lib/api";
import { formatDate, timeAgo } from "@/lib/utils";
import { Alert } from "@/components/ui/alert";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { CodeSnippet } from "@/components/ui/code-snippet";
import { ConfirmDialog } from "@/components/ui/confirm-dialog";
import { EmptyState } from "@/components/empty-state";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import {
  mcpInstallSnippets,
  MCP_AGENT_FILES,
  MCP_AGENT_LABELS,
  type McpAgent,
} from "@/lib/mcp-snippets";

type ClientTab = McpAgent;

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
  onReloadPats: () => void;
}

export function TokensSection({ pats, patsError, onReloadPats }: Props) {
  const [newName, setNewName] = useState("");
  const [newPat, setNewPat] = useState<string | null>(null);
  const [showPat, setShowPat] = useState<boolean>(true);
  const [copied, setCopied] = useState<string | null>(null);
  const [creating, setCreating] = useState(false);
  const [mintError, setMintError] = useState<string | null>(null);
  // Reissue = revoke-then-mint. Routed through a ConfirmDialog (the old token
  // dies immediately) with a per-row pending guard so a double-click can't
  // fire two revoke/mint pairs, and an error channel for the dangerous
  // half-failure where revoke lands but the replacement mint rejects.
  const [pendingReissue, setPendingReissue] = useState<PAT | null>(null);
  const [reissuingId, setReissuingId] = useState<string | null>(null);
  const [reissueError, setReissueError] = useState<string | null>(null);
  const [pendingRevokePat, setPendingRevokePat] = useState<PAT | null>(null);

  const [clientTab, setClientTab] = useState<ClientTab>("claude");
  const [setupOpen, setSetupOpen] = useState<boolean | null>(() => {
    const saved = localStorage.getItem("akb:tokens-setup-open");
    if (saved === "true") return true;
    if (saved === "false") return false;
    return null;
  });

  // Smart default: open setup guide when user has no PATs, closed otherwise.
  // Only applies when localStorage has no saved preference (setupOpen === null).
  useEffect(() => {
    if (setupOpen !== null) return;
    if (pats === null) return;
    setSetupOpen(pats.length === 0);
  }, [pats, setupOpen]);

  function toggleSetup() {
    const next = !setupOpen;
    setSetupOpen(next);
    localStorage.setItem("akb:tokens-setup-open", String(next));
  }

  async function copy(text: string, label: string) {
    // clipboard is undefined on insecure (plain-HTTP) origins — and AKB ships
    // an `--insecure` snippet, so that deploy shape is real. Guard with `?.` so
    // copying a show-once secret never throws an uncaught TypeError with no
    // feedback; the value stays on screen to copy manually.
    try {
      await navigator.clipboard?.writeText(text);
      setCopied(label);
      setTimeout(() => setCopied(null), 2000);
    } catch {
      /* clipboard blocked — value remains visible for manual copy */
    }
  }

  async function handleCreatePAT(e: React.FormEvent) {
    e.preventDefault();
    if (!newName.trim()) return;
    setMintError(null);
    setCreating(true);
    try {
      const r = await createPAT(newName);
      setNewPat(r.token);
      setShowPat(true);
      setNewName("");
      onReloadPats();
    } catch (err) {
      // No app-wide toast — surface inline or the button settles with no token
      // and no explanation on a secret the user is waiting for.
      setMintError(
        err instanceof Error ? err.message : "Couldn't mint a token. Please try again.",
      );
    } finally {
      setCreating(false);
    }
  }

  // Reissue = revoke the live token, then mint a replacement. Confirmed first
  // (the old token stops working the instant revoke lands). If the mint half
  // rejects after revoke succeeded, the deployed token is already gone — we
  // surface that explicitly instead of swallowing it.
  async function handleReissue(p: PAT) {
    setReissuingId(p.token_id);
    setReissueError(null);
    try {
      await revokePAT(p.token_id);
      const r = await createPAT(p.name);
      setNewPat(r.token);
      setShowPat(true);
      onReloadPats();
    } catch {
      setReissueError(
        `"${p.name}" was revoked but a replacement could not be minted — mint a new token now to restore access.`,
      );
      onReloadPats();
    } finally {
      setReissuingId(null);
    }
  }

  async function confirmRevokePat() {
    if (!pendingRevokePat) return;
    await revokePAT(pendingRevokePat.token_id);
    onReloadPats();
  }

  // Pat used in snippets: prefer fresh mint, else first active, else placeholder.
  const snippetPat = newPat || (pats?.[0] ? pats[0].prefix + "…" : "<YOUR_PAT>");
  const snippets = useMemo(() => mcpInstallSnippets(snippetPat), [snippetPat]);
  // Fresh-token banner embeds the real, un-masked token in its config block.
  const freshSnippet = useMemo(
    () => (newPat ? mcpInstallSnippets(newPat).cursor : ""),
    [newPat],
  );

  return (
    <>
      {newPat && (
        <section
          className="rounded-[var(--radius-lg)] border border-accent/40 bg-accent/5 shadow-sm overflow-hidden"
          role="status"
          aria-live="polite"
        >
          <div className="border-b border-accent/40 px-4 py-2 flex items-baseline justify-between gap-2 flex-wrap">
            <div>
              <span className="coord-spark">Fresh token — copy now</span>
              <span className="coord ml-2">Shown once. If you dismiss without copying, you'll need to reissue.</span>
            </div>
            <button
              onClick={() => setNewPat(null)}
              aria-label="Dismiss fresh token"
              className="inline-flex items-center justify-center min-h-[36px] min-w-[36px] coord hover:text-primary cursor-pointer rounded-[var(--radius-sm)] focus:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2 focus-visible:ring-offset-background"
            >
              <X className="h-3 w-3" aria-hidden />
            </button>
          </div>
          <div className="p-6 space-y-4">
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

            <CodeSnippet code={freshSnippet} filename={MCP_AGENT_FILES.cursor} />
          </div>
        </section>
      )}

      {/* Active tokens — primary content on this tab (management). */}
      <section className="rounded-[var(--radius-lg)] border border-border bg-surface shadow-sm overflow-hidden">
        <header className="border-b border-border px-6 py-3 flex items-baseline gap-3">
          <span className="coord-ink">Active tokens</span>
          <span className="coord tabular-nums">[{pats ? pats.length : "··"}]</span>
        </header>
        <div className="p-6 space-y-4">
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
                className="rounded-[var(--radius-md)] border border-border divide-y divide-border overflow-hidden"
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
              description="Mint your first token to connect an agent."
              action={
                !setupOpen ? (
                  <Button variant="outline" size="sm" onClick={() => setSetupOpen(true)}>
                    Set up a token
                  </Button>
                ) : undefined
              }
            />
          ) : (
            <div className="rounded-[var(--radius-md)] border border-border divide-y divide-border overflow-hidden">
              {(pats ?? []).map((p, i) => (
                <div key={p.token_id} className="px-4 py-3 space-y-1.5">
                  {/* Line 1 — identity */}
                  <div className="flex items-baseline gap-3 min-w-0">
                    <span className="coord tabular-nums shrink-0">
                      {String(i + 1).padStart(2, "0")}
                    </span>
                    <span title={p.name} className="text-sm font-medium truncate text-foreground">
                      {p.name}
                    </span>
                    <code className="font-mono text-[11px] text-foreground-muted">
                      {p.prefix}••••
                    </code>
                  </div>
                  {/* Line 2 — meta + actions */}
                  <div className="flex items-center justify-between gap-3 flex-wrap pl-7">
                    <div className="flex items-center gap-3 text-foreground-muted">
                      <span
                        className="coord tabular-nums"
                        title={`Created ${formatDate(p.created_at)}`}
                      >
                        Created {timeAgo(p.created_at)}
                      </span>
                      <span
                        className="coord tabular-nums"
                        title={p.last_used_at ? `Last used ${formatDate(p.last_used_at)}` : undefined}
                      >
                        {p.last_used_at ? `Used ${timeAgo(p.last_used_at)}` : "Never used"}
                      </span>
                    </div>
                    <div className="flex items-center gap-1 ml-auto">
                      <button
                        onClick={() => setPendingReissue(p)}
                        disabled={reissuingId === p.token_id}
                        aria-label={`Reissue token ${p.name}`}
                        className="inline-flex items-center gap-1 px-2 min-h-[36px] rounded-[var(--radius-sm)] text-xs text-foreground-muted hover:text-primary hover:bg-surface-hover disabled:opacity-50 transition-colors cursor-pointer focus:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2 focus-visible:ring-offset-surface"
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
                        className="inline-flex items-center gap-1 px-2 min-h-[36px] rounded-[var(--radius-sm)] text-xs text-destructive hover:bg-surface-hover transition-colors cursor-pointer focus:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2 focus-visible:ring-offset-surface"
                      >
                        <Trash2 className="h-3 w-3" aria-hidden />
                        Revoke
                      </button>
                    </div>
                  </div>
                </div>
              ))}
            </div>
          )}
        </div>
      </section>

      {/* Collapsible setup guide */}
      <div className="rounded-[var(--radius-lg)] border border-border bg-surface shadow-sm overflow-hidden">
        <button
          type="button"
          onClick={toggleSetup}
          aria-expanded={!!setupOpen}
          aria-controls="setup-guide-body"
          className="w-full flex items-center justify-between px-6 py-3 border-b border-border hover:bg-surface-hover cursor-pointer focus:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2 focus-visible:ring-offset-background"
        >
          <span className="coord-ink">Setup guide — 3 steps</span>
          <ChevronRight
            className={`h-4 w-4 transition-transform ${setupOpen ? "rotate-90" : ""}`}
            aria-hidden
          />
        </button>
        {setupOpen && (
          <div id="setup-guide-body" className="p-6 space-y-6">

            {/* STEP 01 — Mint a token */}
            <div>
              <header className="flex items-baseline justify-between flex-wrap gap-2 mb-3">
                <div className="flex items-baseline gap-3">
                  <span className="coord-spark">Step 01</span>
                  <h2 className="text-base font-semibold tracking-tight text-foreground">
                    Mint a token
                  </h2>
                </div>
                <span className="coord">Personal Access Token</span>
              </header>
              <div className="space-y-3">
                <p className="text-sm text-foreground-muted leading-relaxed max-w-prose">
                  A Personal Access Token authorizes your agent against the base.
                  You can reissue or revoke it any time.
                </p>
                <form onSubmit={handleCreatePAT} className="flex gap-2">
                  <Label htmlFor="new-pat-name" className="sr-only">
                    Token name
                  </Label>
                  <Input
                    id="new-pat-name"
                    placeholder="Token name (e.g. claude-code-macbook)"
                    value={newName}
                    onChange={(e) => setNewName(e.target.value)}
                    aria-invalid={mintError ? true : undefined}
                    className="flex-1"
                  />
                  <Button
                    type="submit"
                    variant="accent"
                    loading={creating}
                    disabled={!newName.trim()}
                  >
                    {!creating && <Plus className="h-4 w-4" aria-hidden />}
                    {creating ? "Minting" : "Mint"}
                  </Button>
                </form>
                {mintError && (
                  <Alert variant="destructive">{mintError}</Alert>
                )}
              </div>
            </div>

            <div className="border-t border-border" />

            {/* STEP 02 — Drop the snippet */}
            <div>
              <header className="flex items-baseline justify-between flex-wrap gap-2 mb-3">
                <div className="flex items-baseline gap-3">
                  <span className="coord-spark">Step 02</span>
                  <h2 className="text-base font-semibold tracking-tight text-foreground">
                    Drop the snippet
                  </h2>
                </div>
                <span className="coord">npm: akb-mcp</span>
              </header>
              <div className="space-y-3">
                <p className="text-sm text-foreground-muted leading-relaxed max-w-prose">
                  Pick your client. Paste once. Your agent learns the base on the
                  next launch.
                </p>

                {/* Client picker + snippet — Tabs gives roving tabindex,
                    role=tab/aria-selected, arrow-key nav, and the teal
                    raised-pill active state for free. CodeSnippet supplies
                    the insecure-origin-guarded copy + teal hover. */}
                <Tabs value={clientTab} onValueChange={(v) => setClientTab(v as ClientTab)}>
                  <TabsList className="flex-wrap">
                    {(Object.keys(MCP_AGENT_LABELS) as ClientTab[]).map((id) => (
                      <TabsTrigger key={id} value={id}>
                        {MCP_AGENT_LABELS[id]}
                      </TabsTrigger>
                    ))}
                  </TabsList>
                  <TabsContent value={clientTab} className="space-y-2">
                    <CodeSnippet
                      code={snippets[clientTab]}
                      filename={MCP_AGENT_FILES[clientTab]}
                    />
                    {clientTab === "cursor" && (
                      <div className="rounded-[var(--radius-md)] border border-border px-4 py-2 text-[11px] font-mono bg-surface-muted text-foreground-muted space-y-0.5">
                        <div><span className="coord mr-2">Cursor</span>~/.cursor/mcp.json</div>
                        <div><span className="coord mr-2">Windsurf</span>~/.codeium/windsurf/mcp_config.json</div>
                        <div><span className="coord mr-2">Gemini</span>~/.gemini/settings.json</div>
                        <div>
                          <span className="coord mr-2">Claude Desktop</span>
                          ~/Library/Application Support/Claude/claude_desktop_config.json{" "}
                          <span className="text-subtle">(macOS)</span>
                        </div>
                      </div>
                    )}
                  </TabsContent>
                </Tabs>
                {snippetPat === "<YOUR_PAT>" && (
                  <p className="coord text-foreground-muted">
                    ↑ Replace <span className="text-accent-strong">&lt;YOUR_PAT&gt;</span> with the
                    token string shown after Step 01.
                  </p>
                )}
              </div>
            </div>

            <div className="border-t border-border" />

            {/* STEP 03 — Talk to your agent */}
            <div>
              <header className="flex items-baseline justify-between flex-wrap gap-2 mb-3">
                <div className="flex items-baseline gap-3">
                  <span className="coord-spark">Step 03</span>
                  <h2 className="text-base font-semibold tracking-tight text-foreground">
                    Talk to your agent
                  </h2>
                </div>
                <Link
                  to="/search?q=AKB+usage+guide"
                  className="coord hover:text-link rounded-[var(--radius-sm)] focus:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2 focus-visible:ring-offset-surface"
                >
                  Full guide
                </Link>
              </header>
              <div className="grid grid-cols-1 md:grid-cols-2 gap-x-8 gap-y-3 text-sm">
                <PromptExample
                  text='"Show me how to use AKB with akb_help()"'
                  label="tools + quickstart"
                />
                <PromptExample
                  text='"Search the dnotitia vault for the remote-work policy"'
                  label="internal knowledge"
                />
                <PromptExample
                  text='"From the sales vault, show deals with win-rate ≥ 60%"'
                  label="data analysis"
                />
                <PromptExample
                  text='"Create a todo for Jinwoo: please upload materials"'
                  label="task assignment"
                />
              </div>
            </div>

          </div>
        )}
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

function PromptExample({ text, label }: { text: string; label: string }) {
  return (
    <div className="flex flex-col gap-1">
      <div className="coord">{label}</div>
      {/* Conversational example prompts — sans, not code (they are sentences to
          say to an agent, not a snippet to paste). */}
      <span className="text-[13px] text-foreground leading-relaxed">{text}</span>
    </div>
  );
}
