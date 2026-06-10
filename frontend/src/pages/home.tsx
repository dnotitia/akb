import { useEffect, useMemo, useState } from "react";
import { Link, useLocation, useNavigate } from "react-router-dom";
import * as DropdownMenu from "@radix-ui/react-dropdown-menu";
import {
  ArrowRight,
  ChevronDown,
  Copy,
  Eye,
  EyeOff,
  File as FileIcon,
  FilePlus,
  FileText,
  Plus,
  Search as SearchIcon,
  Table as TableIcon,
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
import {
  listVaults,
  getRecent,
  createPAT,
  listPATs,
  revokePAT,
} from "@/lib/api";
import { timeAgo } from "@/lib/utils";
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

// Leading icon for a recent change, by resource kind. Tables/files use their
// own glyphs; everything else (notes, specs, decisions, …) reads as a document.
function recentIcon(type?: string) {
  if (type === "table" || type === "table_query") return TableIcon;
  if (type === "file") return FileIcon;
  return FileText;
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
  const location = useLocation();
  const navigate = useNavigate();
  const [homeSearch, setHomeSearch] = useState("");
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
      setPats(d.tokens || []);
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

  // First run: no vaults AND no tokens. Lead the main column with the connect
  // flow (the actual first job) instead of two dead-end empty lists.
  const firstRun = vaults.length === 0 && pats.length === 0;

  // Main column — Recent + Vaults. Right rail — summary + connect.
  return (
    <div className="fade-up">
      {/* Page header — centralized design-system primitive */}
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

      {firstRun && (
        <Panel className="max-w-2xl p-6 sm:p-8">
          <h2 className="text-lg font-semibold tracking-tight text-foreground">
            Connect your first agent
          </h2>
          <p className="mt-1 text-sm text-foreground-muted leading-relaxed">
            Mint a personal access token, drop the snippet into your agent, and it can
            start reading and writing your knowledge base. Vaults and recent activity
            show up here once you do.
          </p>

          <div className="mt-5">
            <div className="coord-spark mb-2">1 · Mint a token</div>
            <form onSubmit={handleCreatePAT} className="flex gap-2">
              <Label htmlFor="onboard-pat" className="sr-only">Token name</Label>
              <Input
                id="onboard-pat"
                type="text"
                placeholder="Token name (e.g. my-laptop)"
                value={newName}
                onChange={(e) => setNewName(e.target.value)}
                aria-invalid={mintError ? true : undefined}
                className="flex-1"
              />
              <Button type="submit" variant="accent" loading={creating} disabled={!newName.trim()}>
                {!creating && <Plus className="h-4 w-4" aria-hidden />}
                {creating ? "Minting…" : "Mint token"}
              </Button>
            </form>
            {mintError && <Alert variant="destructive" className="mt-2 text-xs">{mintError}</Alert>}
            {activePat && (
              <div
                className="mt-3 rounded-[var(--radius-md)] border border-accent/40 bg-accent/5 p-2.5"
                role="status"
                aria-live="polite"
              >
                <div className="coord-spark mb-1">New token — copy now</div>
                <div className="flex items-center gap-1.5">
                  <code className="flex-1 font-mono text-[11px] text-foreground break-all leading-snug">
                    {showPat ? activePat : activePat.slice(0, 10) + "•".repeat(14)}
                  </code>
                  {!showPat && <span className="sr-only">Token value: {activePat}</span>}
                  <button
                    onClick={() => setShowPat(!showPat)}
                    aria-label={showPat ? "Hide token" : "Show token"}
                    className="coord hover:text-primary cursor-pointer shrink-0 rounded-[var(--radius-sm)] focus:outline-none focus-visible:ring-2 focus-visible:ring-ring"
                  >
                    {showPat ? <EyeOff className="h-3.5 w-3.5" aria-hidden /> : <Eye className="h-3.5 w-3.5" aria-hidden />}
                  </button>
                  <button
                    onClick={() => copy(activePat, "pat")}
                    aria-label={copied === "pat" ? "Token copied" : "Copy token"}
                    className="coord hover:text-primary cursor-pointer shrink-0 rounded-[var(--radius-sm)] focus:outline-none focus-visible:ring-2 focus-visible:ring-ring"
                  >
                    {copied === "pat" ? <span aria-hidden>OK</span> : <Copy className="h-3.5 w-3.5" aria-hidden />}
                  </button>
                </div>
              </div>
            )}
          </div>

          <div className="mt-5">
            <div className="coord-spark mb-2">2 · Drop the snippet</div>
            <Tabs value={tab} onValueChange={(v) => setTab(v as Tab)}>
              <TabsList className="flex-wrap">
                <TabsTrigger value="claude">Claude Code</TabsTrigger>
                <TabsTrigger value="cursor">Cursor</TabsTrigger>
                <TabsTrigger value="codex">Codex</TabsTrigger>
                <TabsTrigger value="vscode">VS Code</TabsTrigger>
                <TabsTrigger value="openclaw">OpenClaw</TabsTrigger>
              </TabsList>
              <TabsContent value={tab}>
                <CodeSnippet code={snippets[tab]} filename={MCP_AGENT_FILES[tab]} />
              </TabsContent>
            </Tabs>
          </div>
        </Panel>
      )}

      {!firstRun && (
      <div className="grid grid-cols-1 lg:grid-cols-[minmax(0,1fr)_340px] gap-x-10 gap-y-10">
      {/* ── Main column ──────────────────────────────────────── */}
      <div className="space-y-10 min-w-0">
        {/* Search — Home is self-sufficient even when the header search is
            hidden (narrow widths); routes to the full /search page. */}
        <form
          role="search"
          aria-label="Search knowledge base"
          onSubmit={(e) => {
            e.preventDefault();
            const query = homeSearch.trim();
            if (query) navigate(`/search?q=${encodeURIComponent(query)}`);
          }}
          className="relative"
        >
          <SearchIcon
            className="pointer-events-none absolute left-3.5 top-1/2 -translate-y-1/2 h-4 w-4 text-foreground-muted"
            aria-hidden
          />
          <Input
            type="search"
            value={homeSearch}
            onChange={(e) => setHomeSearch(e.target.value)}
            placeholder="Search documents, tables, and notes…"
            aria-label="Search knowledge base"
            className="h-11 pl-10"
          />
        </form>
        {/* § 01 Recent — top priority, the jump-back-in list. */}
        <section id="recent" className="scroll-mt-24" aria-busy={recentLoading}>
          <header className="flex items-baseline gap-3 pb-3 border-b border-border">
            <h2 className="text-xl font-semibold tracking-tight">Recent activity</h2>
            {!recentLoading && !recentError && (
              <span className="coord tabular-nums">
                {recent.length}{recentCapped ? "+" : ""}
              </span>
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
              title="Nothing touched yet"
              description="Recent document writes across all your vaults will appear here."
            />
          ) : (
            <Panel className="mt-3">
              <ol className="divide-y divide-border stagger">
                {recent.map((c, i) => {
                  const Icon = recentIcon(c.type);
                  return (
                  <li key={`${c.doc_id}:${c.changed_at ?? ""}:${i}`}>
                    <Link
                      to={`/vault/${c.vault}/doc/${c.doc_id}`}
                      className="group grid grid-cols-[20px_minmax(0,1fr)_auto_56px] items-baseline gap-x-3 gap-y-1 px-4 py-3 bg-surface hover:bg-surface-muted transition-colors focus:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2 focus-visible:ring-offset-background"
                    >
                      <Icon className="h-4 w-4 self-baseline translate-y-0.5 text-foreground-muted shrink-0" aria-hidden />
                      <div className="min-w-0">
                        <div title={c.title} className="text-sm font-medium tracking-tight truncate text-foreground group-hover:text-primary transition-colors">
                          {c.title}
                        </div>
                        <div title={c.path} className="coord truncate">{c.path}</div>
                      </div>
                      <Badge variant="secondary" title={c.vault} className="shrink-0 max-w-[140px] truncate self-baseline">
                        {c.vault}
                      </Badge>
                      <span className="coord tabular-nums text-right self-baseline">
                        {timeAgo(c.changed_at)}
                      </span>
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
              <span className="coord tabular-nums">{vaults.length}</span>
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
        {/* Summary stats */}
        <section className="rounded-[var(--radius-lg)] border border-border bg-surface shadow-sm overflow-hidden" aria-labelledby="rail-glance">
          <div className="border-b border-border px-4 py-2">
            <h2 id="rail-glance" className="coord-ink">At a glance</h2>
          </div>
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
        <section className="rounded-[var(--radius-lg)] border border-border bg-surface shadow-sm overflow-hidden" aria-labelledby="rail-connect">
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
      )}

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
      <dd className="text-xl font-semibold tabular-nums text-foreground group-hover:text-primary transition-colors">
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
