import { useEffect, useMemo, useRef, useState } from "react";
import { Link, useLocation } from "react-router-dom";
import * as DropdownMenu from "@radix-ui/react-dropdown-menu";
import {
  AlertTriangle,
  ArrowRight,
  ChevronDown,
  Copy,
  Eye,
  EyeOff,
  FileClock,
  FilePlus,
  FileText,
  FolderPlus,
  Plus,
  Trash2,
} from "lucide-react";
import { Alert } from "@/components/ui/alert";
import { Button } from "@/components/ui/button";
import { PageHeader } from "@/components/ui/page-header";
import { Panel } from "@/components/ui/panel";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Tabs, TabsList, TabsTrigger, TabsContent } from "@/components/ui/tabs";
import { Badge } from "@/components/ui/badge";
import { CodeSnippet } from "@/components/ui/code-snippet";
import { ConfirmDialog } from "@/components/ui/confirm-dialog";
import { EmptyState } from "@/components/empty-state";
import { VaultList, type VaultRow } from "@/components/vault-list";
import { VaultChip } from "@/components/ui/vault-chip";
import { RelativeTime } from "@/components/ui/relative-time";
import { QuickstartDialog, QUICKSTART_DISMISS_KEY } from "@/components/quickstart-dialog";
import {
  listVaults,
  getRecent,
  createPAT,
  listPATs,
  revokePAT,
} from "@/lib/api";
import { timeAgo } from "@/lib/utils";
import { recentIcon, recentTone } from "@/lib/recent";
import { mcpInstallSnippets, MCP_AGENT_FILES } from "@/lib/mcp-snippets";

type Tab = "claude" | "cursor" | "codex" | "vscode" | "openclaw";

// Recent-activity fetch size. The list shows this many; when the result is
// full we render the count as "N+" rather than implying it's the grand total.
const RECENT_LIMIT = 8;
// How many vaults the Home preview shows before linking out to /vault.
const VAULT_PREVIEW_LIMIT = 6;

interface RecentRow {
  doc_id: string;
  vault: string;
  path: string;
  title: string;
  type?: string;
  commit?: string;
  changed_at?: string;
}

interface PATRow {
  token_id: string;
  name: string;
  prefix: string;
  last_used_at?: string;
}

export default function HomePage() {
  const [vaults, setVaults] = useState<VaultRow[]>([]);
  const [recent, setRecent] = useState<RecentRow[]>([]);
  const [recentLoading, setRecentLoading] = useState(true);
  const [recentError, setRecentError] = useState(false);
  const [pats, setPats] = useState<PATRow[]>([]);
  const [pendingRevoke, setPendingRevoke] = useState<PATRow | null>(null);
  const [activePat, setActivePat] = useState<string | null>(null);
  const [showPat, setShowPat] = useState(false);
  const [copied, setCopied] = useState<string | null>(null);
  const [creating, setCreating] = useState(false);
  const [mintError, setMintError] = useState<string | null>(null);
  const [newName, setNewName] = useState("");
  const [tab, setTab] = useState<Tab>("claude");
  const [quickstartOpen, setQuickstartOpen] = useState(false);
  const quickstartChecked = useRef(false);
  const location = useLocation();
  const recentCapped = recent.length >= RECENT_LIMIT;

  useEffect(() => {
    let cancelled = false;
    // Per-vault content counts are fetched by <VaultList> for whatever rows it
    // renders, so Home only needs the bare list (for the count + preview).
    listVaults()
      .then((d) => {
        if (!cancelled) setVaults(d.vaults || []);
      })
      .catch(console.error);
    loadRecent(() => cancelled);
    loadPATs();
    return () => {
      cancelled = true;
    };
  }, []);

  async function loadRecent(isCancelled: () => boolean = () => false) {
    setRecentLoading(true);
    setRecentError(false);
    try {
      const d = await getRecent(undefined, RECENT_LIMIT);
      if (isCancelled()) return;
      setRecent(d.changes || []);
    } catch {
      if (isCancelled()) return;
      setRecentError(true);
    } finally {
      if (!isCancelled()) setRecentLoading(false);
    }
  }

  // Scroll to #vaults / #recent when a link lands here with that hash. Keyed on
  // location.key too so re-clicking the same in-page hash re-scrolls (a bare
  // [hash] dep wouldn't fire when the hash is unchanged).
  useEffect(() => {
    const target = location.hash.slice(1);
    if (target === "vaults" || target === "recent") {
      // scrollIntoView's `behavior` is a JS option the CSS reduced-motion guard
      // can't reach, so honor the OS preference explicitly here.
      const reduce = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
      requestAnimationFrame(() => {
        document
          .getElementById(target)
          ?.scrollIntoView({ behavior: reduce ? "auto" : "smooth", block: "start" });
      });
    }
  }, [location.hash, location.key]);

  async function loadPATs() {
    try {
      const d = await listPATs();
      const toks = d.tokens || [];
      setPats(toks);
      // First-run quickstart: proactively surface the connect flow once when a
      // fresh account has no tokens yet (unless the user opted out).
      if (!quickstartChecked.current) {
        quickstartChecked.current = true;
        if (toks.length === 0 && localStorage.getItem(QUICKSTART_DISMISS_KEY) !== "1") {
          setQuickstartOpen(true);
        }
      }
    } catch {
      /* non-fatal: leave pats empty */
    }
  }

  async function copy(text: string, label: string) {
    // clipboard is undefined on insecure (plain-HTTP) origins — guard so a copy
    // never throws an uncaught TypeError mid-render with no feedback.
    try {
      await navigator.clipboard?.writeText(text);
      setCopied(label);
      setTimeout(() => setCopied(null), 2000);
    } catch {
      /* clipboard blocked — value stays on screen to copy manually */
    }
  }

  async function handleCreatePAT(e: React.FormEvent) {
    e.preventDefault();
    const name = newName.trim();
    if (!name) return;
    setMintError(null);
    setCreating(true);
    try {
      const r = await createPAT(name);
      setActivePat(r.token);
      setShowPat(true);
      setNewName("");
      await loadPATs();
    } catch (err) {
      // No app-wide toast — surface inline or the button just settles with no
      // token and no explanation.
      setMintError(err instanceof Error ? err.message : "Couldn't mint a token. Please try again.");
    } finally {
      setCreating(false);
    }
  }

  const pat = activePat || "<YOUR_PAT>";
  const snippets = useMemo(() => mcpInstallSnippets(pat), [pat]);

  // Home shows a preview of the vault directory; the full list (with filter)
  // lives on /vault. Memoized so <VaultList> doesn't re-fetch metrics on every
  // unrelated render.
  const previewVaults = useMemo(
    () => vaults.slice(0, VAULT_PREVIEW_LIMIT),
    [vaults],
  );

  // Main column — Recent + Vaults. Right rail — summary + connect.
  return (
    <div className="fade-up">
      {/* Page header — centralized primitive, wrapped in a header-local aurora
          wash so the masthead carries brand depth (the global mesh is anchored
          off-screen). Static, decorative, behind the header content. */}
      <div className="aurora-header">
        <PageHeader
          title="Workspace"
          subtitle="Your knowledge base for AI agents — browse vaults, see recent changes, and manage agent connections."
          actions={
            <div className="flex items-center gap-2">
              {vaults.length > 0 && <NewDocAction vaults={vaults} />}
              <Button asChild variant={vaults.length > 0 ? "outline" : "accent"} size="md">
                <Link to="/vault/new">
                  <Plus className="h-4 w-4" aria-hidden />
                  New vault
                </Link>
              </Button>
            </div>
          }
        />
      </div>

      <div className="grid grid-cols-1 lg:grid-cols-[minmax(0,1fr)_340px] gap-x-10 gap-y-10">
      {/* ── Main column ──────────────────────────────────────── */}
      <div className="space-y-10 min-w-0">
        {/* § 01 Recent — top priority, the jump-back-in list. */}
        <section id="recent" className="scroll-mt-24" aria-busy={recentLoading}>
          <header className="flex items-baseline gap-3 pb-3 border-b border-border">
            <h2 className="text-xl font-semibold tracking-tight">Recent activity</h2>
            {!recentLoading && !recentError && (
              <Badge variant="default" className="tabular-nums">
                {recent.length}{recentCapped ? "+" : ""}
              </Badge>
            )}
          </header>
          <span className="sr-only" role="status" aria-live="polite">
            {recentLoading
              ? "Loading recent activity"
              : recentError
                ? "Could not load recent activity"
                : `${recent.length} recent change${recent.length === 1 ? "" : "s"}`}
          </span>

          {recentLoading ? (
            <Panel className="mt-3" aria-hidden>
              <ul className="divide-y divide-border">
                {Array.from({ length: 4 }).map((_, i) => (
                  <li key={i} className="flex items-center gap-4 px-4 py-3">
                    <span className="h-2.5 w-6 rounded bg-surface-muted" />
                    <span className="h-3 flex-1 rounded bg-surface-muted" />
                    <span className="h-2.5 w-10 rounded bg-surface-muted" />
                  </li>
                ))}
              </ul>
            </Panel>
          ) : recentError ? (
            <EmptyState
              icon={
                <span className="feature-tile feat-neutral h-14 w-14">
                  <AlertTriangle className="h-6 w-6" aria-hidden />
                </span>
              }
              title="Couldn't load recent activity"
              description="Something went wrong fetching your latest changes."
              action={
                <Button variant="outline" size="sm" onClick={() => loadRecent()}>
                  Retry
                </Button>
              }
            />
          ) : recent.length === 0 ? (
            <EmptyState
              icon={
                <span className="feature-tile feat-knowledge h-14 w-14">
                  <FileClock className="h-6 w-6" aria-hidden />
                </span>
              }
              title="Nothing touched yet"
              description="Recent document writes across all your vaults will appear here."
            />
          ) : (
            <Panel
              className="mt-3"
              inset={false}
            >
              {/* inset={false} so a hovered row's lift + shadow aren't clipped;
                  the end rows are re-rounded to keep the divided-panel look at
                  rest. */}
              <ol className="divide-y divide-border stagger [&>li:first-child>a]:rounded-t-[var(--radius-lg)] [&>li:last-child>a]:rounded-b-[var(--radius-lg)]">
                {recent.map((c, i) => {
                  const Icon = recentIcon(c.type);
                  const tone = recentTone(c.type);
                  return (
                  <li key={`${c.doc_id}:${c.changed_at ?? ""}:${i}`}>
                    <Link
                      to={`/vault/${c.vault}/doc/${c.doc_id}`}
                      className="group card-hover relative z-0 hover:z-10 grid grid-cols-[20px_minmax(0,1fr)_auto_64px] items-center gap-x-3 gap-y-1 px-4 py-3 bg-surface hover:bg-surface-muted focus:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2 focus-visible:ring-offset-background"
                    >
                      <span
                        className="inline-flex h-5 w-5 items-center justify-center rounded-[var(--radius-sm)] shrink-0"
                        style={{
                          color: tone,
                          backgroundColor: `color-mix(in srgb, ${tone} 12%, transparent)`,
                        }}
                        aria-hidden
                      >
                        <Icon className="h-3 w-3" aria-hidden />
                      </span>
                      <div className="min-w-0">
                        <div title={c.title} className="text-sm font-medium tracking-tight truncate text-foreground group-hover:text-link transition-colors">
                          {c.title}
                        </div>
                        <div title={c.path} className="coord truncate">{c.path}</div>
                      </div>
                      <span className="flex items-center gap-1.5 shrink-0 max-w-[150px]" title={c.vault}>
                        <VaultChip name={c.vault} size="sm" />
                        <span className="truncate text-xs text-foreground-muted">{c.vault}</span>
                      </span>
                      <RelativeTime iso={c.changed_at} className="justify-end text-right" />
                    </Link>
                  </li>
                  );
                })}
              </ol>
            </Panel>
          )}
        </section>

        {/* § 02 Vaults — a preview of the directory; the full list lives at /vault. */}
        <section id="vaults" className="scroll-mt-24">
          <header className="flex items-baseline justify-between gap-4 flex-wrap pb-3 border-b border-border">
            <div className="flex items-baseline gap-3">
              <h2 className="text-xl font-semibold tracking-tight">Your vaults</h2>
              <Badge variant="default" className="tabular-nums">{vaults.length}</Badge>
            </div>
            {vaults.length > VAULT_PREVIEW_LIMIT && (
              <Link
                to="/vault"
                className="coord inline-flex items-center gap-1 hover:text-link rounded-[var(--radius-sm)] focus:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2 focus-visible:ring-offset-background"
              >
                View all {vaults.length}
                <ArrowRight className="h-3 w-3" aria-hidden />
              </Link>
            )}
          </header>

          {vaults.length === 0 ? (
            <EmptyState
              icon={
                <span className="feature-tile feat-memory h-14 w-14">
                  <FolderPlus className="h-6 w-6" aria-hidden />
                </span>
              }
              title="No vaults yet"
              description="Mint a token, then ask your agent to create one — or use the button above."
              action={
                <Button asChild variant="accent" size="sm">
                  <Link to="/vault/new">
                    <Plus className="h-4 w-4" aria-hidden />
                    Create first vault
                  </Link>
                </Button>
              }
            />
          ) : (
            <VaultList vaults={previewVaults} />
          )}
        </section>
      </div>

      {/* ── Right rail ───────────────────────────────────────── */}
      <aside className="space-y-8 min-w-0" aria-label="Workspace summary and connection">
        {/* Summary stats — glass shell so the masthead aurora tints the rail,
            raised one tier (shadow-md) above the content field. */}
        <section className="rounded-[var(--radius-lg)] border glass shadow-md overflow-hidden" aria-labelledby="rail-glance">
          <div className="border-b border-border px-4 py-2">
            <h2 id="rail-glance" className="coord-ink">At a glance</h2>
          </div>
          <ActivitySparkline recent={recent} />
          <dl className="divide-y divide-border">
            <RailStat label="Vaults" value={vaults.length} />
            <RailStat label="Tokens" value={pats.length} />
            <RailStat
              label="Recent"
              value={recentCapped ? `${RECENT_LIMIT}+` : recent.length}
            />
          </dl>
        </section>

        {/* Connect — always open. Mint + snippet tabs kept; prompt examples
            moved to docs-territory since they're one-time guidance. */}
        <section className="rounded-[var(--radius-lg)] border glass shadow-md overflow-hidden" aria-labelledby="rail-connect">
          <div className="flex items-baseline justify-between gap-2 px-4 py-3">
            <h2 id="rail-connect" className="coord-ink">Connect</h2>
            <div className="flex items-baseline gap-3">
              {pats.length > 0 ? (
                <span className="coord tabular-nums">{pats.length}</span>
              ) : (
                <Badge variant="pending">needs setup</Badge>
              )}
              <Link
                to="/settings?tab=tokens"
                className="coord hover:text-primary rounded-[var(--radius-sm)] focus:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2 focus-visible:ring-offset-surface"
              >
                Manage
              </Link>
            </div>
          </div>

          <div className="border-t border-border">
              {/* Mint */}
              <div className="p-4 border-b border-border">
                <div className="coord-spark mb-2">Mint token</div>
                <form onSubmit={handleCreatePAT} className="space-y-2">
                  <Label htmlFor="pat-name" className="sr-only">Token name</Label>
                  <Input
                    id="pat-name"
                    type="text"
                    placeholder="Token name (e.g. my-laptop)"
                    value={newName}
                    onChange={(e) => setNewName(e.target.value)}
                    aria-invalid={mintError ? true : undefined}
                    className="h-8 text-xs"
                  />
                  <Button
                    type="submit"
                    loading={creating}
                    disabled={!newName.trim()}
                    variant="default"
                    size="sm"
                    className="w-full"
                  >
                    {!creating && <Plus className="h-3 w-3" aria-hidden />}
                    {creating ? "Minting…" : "Mint token"}
                  </Button>
                </form>
                {mintError && (
                  <Alert variant="destructive" className="mt-2 text-xs">{mintError}</Alert>
                )}

                {activePat && (
                  <div
                    className="mt-3 rounded-[var(--radius-md)] border border-accent/40 bg-accent/5 p-2"
                    role="status"
                    aria-live="polite"
                  >
                    <div className="coord-spark mb-1">New token — copy now</div>
                    <div className="flex items-center gap-1.5">
                      <code className="flex-1 font-mono text-[10px] text-foreground break-all leading-snug">
                        {showPat ? activePat : activePat.slice(0, 10) + "•".repeat(14)}
                      </code>
                      {/* full token always reachable by SR even while masked */}
                      {!showPat && <span className="sr-only">Token value: {activePat}</span>}
                      <button
                        onClick={() => setShowPat(!showPat)}
                        aria-label={showPat ? "Hide token" : "Show token"}
                        className="coord hover:text-primary cursor-pointer shrink-0 rounded-[var(--radius-sm)] focus:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2 focus-visible:ring-offset-surface"
                      >
                        {showPat ? (
                          <EyeOff className="h-3 w-3" aria-hidden />
                        ) : (
                          <Eye className="h-3 w-3" aria-hidden />
                        )}
                      </button>
                      <button
                        onClick={() => copy(activePat, "pat")}
                        aria-label={copied === "pat" ? "Token copied" : "Copy token"}
                        className="coord hover:text-primary cursor-pointer shrink-0 rounded-[var(--radius-sm)] focus:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2 focus-visible:ring-offset-surface"
                      >
                        {copied === "pat" ? <span aria-hidden>OK</span> : <Copy className="h-3 w-3" aria-hidden />}
                      </button>
                    </div>
                  </div>
                )}
              </div>

              {/* Snippet — compact tabs, one panel. */}
              <div className="p-4 border-b border-border">
                <div className="coord-spark mb-2">Drop snippet</div>
                <Tabs value={tab} onValueChange={(v) => setTab(v as Tab)}>
                  <TabsList className="flex-wrap gap-0 mb-2">
                    <TabsTrigger value="claude" className="px-2 py-1 text-[10px]">Claude Code</TabsTrigger>
                    <TabsTrigger value="cursor" className="px-2 py-1 text-[10px]">Cursor</TabsTrigger>
                    <TabsTrigger value="codex" className="px-2 py-1 text-[10px]">Codex</TabsTrigger>
                    <TabsTrigger value="vscode" className="px-2 py-1 text-[10px]">VS Code</TabsTrigger>
                    <TabsTrigger value="openclaw" className="px-2 py-1 text-[10px]">OpenClaw</TabsTrigger>
                  </TabsList>
                  <TabsContent value={tab}>
                    <CodeSnippet code={snippets[tab]} filename={MCP_AGENT_FILES[tab]} />
                  </TabsContent>
                </Tabs>
              </div>

              {/* Active tokens — one-line rows; manage link sits in the
                  Connect section header to keep related actions together. */}
              <div className="p-4">
                <div className="coord-spark mb-2">Active tokens</div>
                {pats.length === 0 ? (
                  <div className="coord">— none —</div>
                ) : (
                  <ul className="divide-y divide-border rounded-[var(--radius-md)] border border-border overflow-hidden">
                    {pats.slice(0, 4).map((p) => (
                      <li
                        key={p.token_id}
                        className="flex items-center justify-between gap-2 px-2 py-1.5 text-xs"
                      >
                        <span title={p.name} className="truncate text-foreground font-medium">{p.name}</span>
                        <div className="flex items-center gap-2 shrink-0">
                          <span className="coord tabular-nums">
                            {p.last_used_at ? timeAgo(p.last_used_at) : "—"}
                          </span>
                          <button
                            onClick={() => setPendingRevoke(p)}
                            aria-label={`Revoke token ${p.name}`}
                            className="text-foreground-muted hover:text-destructive transition-colors cursor-pointer rounded-[var(--radius-sm)] focus:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2 focus-visible:ring-offset-surface"
                          >
                            <Trash2 className="h-3 w-3" aria-hidden />
                          </button>
                        </div>
                      </li>
                    ))}
                  </ul>
                )}
                {pats.length > 4 && (
                  <Link
                    to="/settings?tab=tokens"
                    className="coord mt-2 inline-flex items-center gap-1 hover:text-primary rounded-[var(--radius-sm)] focus:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2 focus-visible:ring-offset-surface"
                  >
                    +{pats.length - 4} more
                    <ArrowRight className="h-3 w-3" aria-hidden />
                  </Link>
                )}
              </div>
          </div>
        </section>
      </aside>
      </div>

      <ConfirmDialog
        open={pendingRevoke !== null}
        onOpenChange={(o) => !o && setPendingRevoke(null)}
        title={pendingRevoke ? `Revoke "${pendingRevoke.name}"?` : ""}
        description={
          "Any agent currently using this token will lose access immediately.\nThis cannot be undone."
        }
        confirmLabel="Revoke token"
        variant="destructive"
        onConfirm={async () => {
          if (!pendingRevoke) return;
          await revokePAT(pendingRevoke.token_id);
          await loadPATs();
        }}
      />

      <QuickstartDialog
        open={quickstartOpen}
        onOpenChange={setQuickstartOpen}
        onTokenCreated={loadPATs}
      />
    </div>
  );
}

/**
 * Primary "write something" action. Creating a vault is rare/structural;
 * writing a document is the daily job, so this is the marquee accent CTA.
 * One vault → straight to its new-doc form; several → a small vault picker.
 */
function NewDocAction({ vaults }: { vaults: VaultRow[] }) {
  if (vaults.length === 1) {
    return (
      <Button asChild variant="accent" size="md">
        <Link to={`/vault/${vaults[0].name}/doc/new`}>
          <FilePlus className="h-4 w-4" aria-hidden />
          New document
        </Link>
      </Button>
    );
  }
  return (
    <DropdownMenu.Root>
      <DropdownMenu.Trigger asChild>
        <Button variant="accent" size="md">
          <FilePlus className="h-4 w-4" aria-hidden />
          New document
          <ChevronDown className="h-4 w-4 opacity-80" aria-hidden />
        </Button>
      </DropdownMenu.Trigger>
      <DropdownMenu.Portal>
        <DropdownMenu.Content
          align="end"
          sideOffset={6}
          className="z-50 max-h-[60vh] overflow-y-auto min-w-[220px] rounded-[var(--radius-md)] border border-border bg-surface p-1 shadow-md"
        >
          <div className="px-3 py-1.5 coord">Choose a vault</div>
          {vaults.map((v) => (
            <DropdownMenu.Item key={v.id} asChild>
              <Link
                to={`/vault/${v.name}/doc/new`}
                className="flex cursor-pointer items-center gap-2 px-3 py-2 text-sm text-foreground outline-none rounded-[var(--radius-sm)] data-[highlighted]:bg-surface-hover"
              >
                <FileText className="h-4 w-4 text-foreground-muted" aria-hidden />
                <span className="truncate">{v.name}</span>
              </Link>
            </DropdownMenu.Item>
          ))}
        </DropdownMenu.Content>
      </DropdownMenu.Portal>
    </DropdownMenu.Root>
  );
}

function RailStat({
  label,
  value,
  to,
}: {
  label: string;
  value: number | string;
  to?: string;
}) {
  const display = String(value);
  const body = (
    <>
      <dt className="coord group-hover:text-primary transition-colors">{label}</dt>
      <dd className="text-xl font-normal tabular-nums text-foreground group-hover:text-primary transition-colors">
        {display}
      </dd>
    </>
  );
  if (to) {
    return (
      <Link
        to={to}
        className="group flex items-baseline justify-between px-4 py-3 hover:bg-surface-muted transition-colors focus:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2 focus-visible:ring-offset-surface"
      >
        {body}
      </Link>
    );
  }
  return (
    <div className="flex items-baseline justify-between px-4 py-3">{body}</div>
  );
}

// How many calendar days the rail sparkline spans.
const SPARK_DAYS = 14;

/**
 * The single rail data-viz: thin day-bucketed bars of recent write activity
 * (teal, with the most recent active day tipped in accent). Built entirely
 * from the recent[] already in state — no extra fetch. Suppressed when the
 * signal is too sparse to read as a shape, so the rail never carries a row of
 * dead zero-height bars on a quiet account. Purely decorative: the bars are
 * aria-hidden with an sr-only summary.
 */
function ActivitySparkline({ recent }: { recent: RecentRow[] }) {
  const { bars, total, activeDays } = useMemo(() => {
    const counts = new Array(SPARK_DAYS).fill(0);
    const now = new Date();
    const startOfToday = new Date(
      now.getFullYear(),
      now.getMonth(),
      now.getDate(),
    ).getTime();
    const dayMs = 86_400_000;
    const seen = new Set<number>();
    let n = 0;
    for (const c of recent) {
      if (!c.changed_at) continue;
      const t = new Date(c.changed_at).getTime();
      if (Number.isNaN(t)) continue;
      const idx = SPARK_DAYS - 1 - Math.floor((startOfToday - t) / dayMs);
      if (idx < 0 || idx >= SPARK_DAYS) continue;
      counts[idx] += 1;
      seen.add(idx);
      n += 1;
    }
    return { bars: counts, total: n, activeDays: seen.size };
  }, [recent]);

  // Need at least a few changes across more than one day to read as a trend.
  if (total < 3 || activeDays < 2) return null;

  const max = Math.max(...bars);
  let tip = -1;
  for (let i = bars.length - 1; i >= 0; i--) {
    if (bars[i] > 0) {
      tip = i;
      break;
    }
  }

  return (
    <div className="border-b border-border px-4 pt-3 pb-2.5">
      <div className="flex h-8 items-end gap-[3px]" aria-hidden>
        {bars.map((n, i) => (
          <span key={i} className="flex h-full flex-1 items-end">
            <span
              className="w-full rounded-t-[2px]"
              style={{
                height: n === 0 ? "2px" : `${Math.max(12, Math.round((n / max) * 100))}%`,
                opacity: n === 0 ? 0.55 : 1,
                backgroundColor:
                  n === 0
                    ? "var(--color-border)"
                    : i === tip
                      ? "var(--color-accent)"
                      : "var(--color-primary)",
              }}
            />
          </span>
        ))}
      </div>
      <span className="sr-only">
        {total} recent change{total === 1 ? "" : "s"} across the last {SPARK_DAYS} days
      </span>
    </div>
  );
}
