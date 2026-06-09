import { useEffect, useMemo, useRef, useState } from "react";
import { Link, useLocation } from "react-router-dom";
import {
  AlertCircle,
  ArrowRight,
  Copy,
  Eye,
  EyeOff,
  File as FileIcon,
  FileText,
  Plus,
  Table as TableIcon,
  Trash2,
} from "lucide-react";
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
import { RoleBadge } from "@/components/status-badge";
import { QuickstartDialog, QUICKSTART_DISMISS_KEY } from "@/components/quickstart-dialog";
import {
  listVaults,
  getRecent,
  createPAT,
  listPATs,
  revokePAT,
  getVaultInfo,
} from "@/lib/api";
import { timeAgo } from "@/lib/utils";
import { mcpInstallSnippets, MCP_AGENT_FILES } from "@/lib/mcp-snippets";

type Tab = "claude" | "cursor" | "codex" | "vscode" | "openclaw";

// Recent-activity fetch size. The list shows this many; when the result is
// full we render the count as "N+" rather than implying it's the grand total.
const RECENT_LIMIT = 8;
// Cap concurrent /vaults/{v}/info calls — each one fans out into ~10 pooled
// COUNT queries server-side, so an unbounded forEach over many vaults can
// exhaust the connection pool.
const VAULT_INFO_CONCURRENCY = 5;

interface VaultRow {
  id: string;
  name: string;
  description?: string;
  role?: "owner" | "admin" | "writer" | "reader";
  status?: string;
}

interface VaultMetrics {
  document_count?: number;
  table_count?: number;
  file_count?: number;
  last_activity?: string;
}

interface RecentRow {
  doc_id: string;
  vault: string;
  path: string;
  title: string;
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
  const [vaultMetrics, setVaultMetrics] = useState<Record<string, VaultMetrics>>({});
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
  const [vaultFilter, setVaultFilter] = useState("");
  const [quickstartOpen, setQuickstartOpen] = useState(false);
  const quickstartChecked = useRef(false);
  const location = useLocation();
  const recentCapped = recent.length >= RECENT_LIMIT;

  useEffect(() => {
    let cancelled = false;
    listVaults()
      .then((d) => {
        if (cancelled) return;
        const vs: VaultRow[] = d.vaults || [];
        setVaults(vs);
        // Enrich each vault row with counts + last-activity. Run in bounded
        // batches — each /info call fans out server-side, so an unbounded
        // forEach risks pool exhaustion. Incremental setState keeps the list
        // usable before every batch resolves.
        void (async () => {
          for (let i = 0; i < vs.length; i += VAULT_INFO_CONCURRENCY) {
            if (cancelled) return;
            await Promise.all(
              vs.slice(i, i + VAULT_INFO_CONCURRENCY).map((v) =>
                getVaultInfo(v.name)
                  .then((info) => {
                    if (cancelled) return;
                    setVaultMetrics((prev) => ({
                      ...prev,
                      [v.name]: {
                        document_count: info?.document_count,
                        table_count: info?.table_count,
                        file_count: info?.file_count,
                        last_activity: info?.last_activity,
                      },
                    }));
                  })
                  .catch(() => {}),
              ),
            );
          }
        })();
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
      requestAnimationFrame(() => {
        document
          .getElementById(target)
          ?.scrollIntoView({ behavior: "smooth", block: "start" });
      });
    }
  }, [location.hash, location.key]);

  async function loadPATs() {
    try {
      const d = await listPATs();
      const toks = d.tokens || [];
      setPats(toks);
      // First-run quickstart: surface the connect flow once when a fresh
      // account has no tokens yet (unless the user opted out).
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

  const q = vaultFilter.trim().toLowerCase();
  const filteredVaults = q
    ? vaults.filter(
        (v) =>
          v.name?.toLowerCase().includes(q) || v.description?.toLowerCase().includes(q),
      )
    : vaults;

  // Main column — Recent + Vaults. Right rail — summary + connect.
  return (
    <div className="fade-up">
      {/* Page header — centralized design-system primitive */}
      <PageHeader
        title="Workspace"
        subtitle="Wire your agent into the base — vaults, recent activity, and connection tokens."
        actions={
          <Button asChild variant="accent" size="md">
            <Link to="/vault/new">
              <Plus className="h-4 w-4" aria-hidden />
              New vault
            </Link>
          </Button>
        }
      />

      <div className="grid grid-cols-1 lg:grid-cols-[minmax(0,1fr)_340px] gap-x-10 gap-y-10">
      {/* ── Main column ──────────────────────────────────────── */}
      <div className="space-y-10 min-w-0">
        {/* § 01 Recent — top priority, the jump-back-in list. */}
        <section id="recent" className="scroll-mt-24">
          <header className="flex items-baseline gap-3 pb-3 border-b border-border">
            <span className="coord-ink">§ 01</span>
            <h2 className="text-xl font-semibold tracking-tight">Recent activity</h2>
            {!recentLoading && !recentError && (
              <span className="coord tabular-nums">
                [{recent.length}{recentCapped ? "+" : ""}]
              </span>
            )}
          </header>

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
              <ol className="divide-y divide-border">
                {recent.map((c, i) => (
                  <li key={`${c.doc_id}:${c.changed_at ?? ""}:${i}`}>
                    <Link
                      to={`/vault/${c.vault}/doc/${c.doc_id}`}
                      className="group grid grid-cols-[40px_minmax(72px,140px)_minmax(0,1fr)_auto] items-baseline gap-4 px-4 py-3 bg-surface hover:bg-surface-muted transition-colors focus:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2 focus-visible:ring-offset-background"
                    >
                      <span className="coord tabular-nums">
                        {String(i + 1).padStart(2, "0")}
                      </span>
                      <span title={c.vault} className="coord font-mono tabular-nums truncate">
                        {c.vault}
                      </span>
                      <div className="min-w-0">
                        <div title={c.title} className="text-sm font-medium tracking-tight truncate text-foreground group-hover:text-primary transition-colors">
                          {c.title}
                        </div>
                        <div title={c.path} className="coord truncate">{c.path}</div>
                      </div>
                      <span className="coord tabular-nums w-[52px] text-right">
                        {timeAgo(c.changed_at)}
                      </span>
                    </Link>
                  </li>
                ))}
              </ol>
            </Panel>
          )}
        </section>

        {/* § 02 Vaults — list rows with explicit Open → affordance. */}
        <section id="vaults" className="scroll-mt-24">
          <header className="flex items-baseline justify-between gap-4 flex-wrap pb-3 border-b border-border">
            <div className="flex items-baseline gap-3">
              <span className="coord-ink">§ 02</span>
              <h2 className="text-xl font-semibold tracking-tight">Your vaults</h2>
              <span className="coord tabular-nums">[{vaults.length}]</span>
            </div>
            <div className="flex items-center gap-3">
              {vaults.length > 5 && (
                <Input
                  type="search"
                  placeholder="Filter vaults"
                  value={vaultFilter}
                  onChange={(e) => setVaultFilter(e.target.value)}
                  aria-label="Filter vaults"
                  className="h-9 w-40 sm:w-48"
                />
              )}
              <Button asChild variant="outline" size="sm">
                <Link to="/vault/new">
                  <Plus className="h-4 w-4" aria-hidden />
                  New vault
                </Link>
              </Button>
            </div>
          </header>

          {q && vaults.length > 0 && (
            <p className="coord mt-3" aria-live="polite">
              Showing {filteredVaults.length} of {vaults.length}
            </p>
          )}

          {vaults.length === 0 ? (
            <EmptyState
              title="No vaults yet"
              description="Mint a token, then ask your agent to create one — or use the button above."
              action={
                <Button asChild variant="default" size="sm">
                  <Link to="/vault/new">
                    <Plus className="h-4 w-4" aria-hidden />
                    Create first vault
                  </Link>
                </Button>
              }
            />
          ) : filteredVaults.length === 0 ? (
            <EmptyState
              title={`No matches for "${vaultFilter}"`}
              action={
                <Button variant="outline" size="sm" onClick={() => setVaultFilter("")}>
                  Clear filter
                </Button>
              }
            />
          ) : (
            <Panel className="mt-3">
              <ol className="divide-y divide-border">
                {filteredVaults.map((v, i) => {
                  const m = vaultMetrics[v.name];
                  const lastActivity = m?.last_activity;
                  return (
                  <li key={v.id}>
                    <Link
                      to={`/vault/${v.name}`}
                      className="group grid grid-cols-[40px_minmax(0,1fr)_auto] items-baseline gap-x-4 gap-y-1 px-4 py-3 bg-surface hover:bg-surface-muted transition-colors focus:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2 focus-visible:ring-offset-background"
                    >
                      <span className="coord tabular-nums self-baseline">
                        {String(i + 1).padStart(2, "0")}
                      </span>
                      <div className="min-w-0 pr-4">
                        <div className="flex items-baseline gap-2 flex-wrap mb-1">
                          <span className="font-mono text-base font-semibold text-foreground group-hover:text-primary transition-colors">
                            {v.name}
                          </span>
                          {v.status === "archived" && (
                            <Badge variant="archived">archived</Badge>
                          )}
                        </div>
                        {v.description && (
                          <p
                            className="text-xs text-foreground-muted leading-relaxed line-clamp-1"
                            title={v.description}
                          >
                            {v.description}
                          </p>
                        )}
                      </div>
                      {/* Trailing meta as one shrink-0 group so it never eats the
                          name column. Stats hide below xl where width is tight. */}
                      <div className="flex items-center gap-3 shrink-0 self-baseline">
                        <span className="hidden xl:inline-flex">
                          <VaultStatsCell m={m} />
                        </span>
                        <span className="coord tabular-nums whitespace-nowrap w-[56px] text-right">
                          {lastActivity ? timeAgo(lastActivity) : m ? "—" : ""}
                        </span>
                        {v.role && <RoleBadge role={v.role} />}
                        <ArrowRight
                          className="h-4 w-4 shrink-0 text-foreground-muted opacity-40 group-hover:opacity-100 group-hover:translate-x-0.5 group-hover:text-primary transition-all"
                          aria-hidden
                        />
                      </div>
                    </Link>
                  </li>
                  );
                })}
              </ol>
            </Panel>
          )}
        </section>
      </div>

      {/* ── Right rail ───────────────────────────────────────── */}
      <aside className="space-y-8 min-w-0">
        {/* Summary stats */}
        <section className="rounded-[var(--radius-lg)] border border-border bg-surface shadow-sm overflow-hidden">
          <div className="border-b border-border px-4 py-2">
            <span className="coord-ink">§ AT A GLANCE</span>
          </div>
          <dl className="divide-y divide-border">
            <RailStat label="VAULTS" value={vaults.length} to="/#vaults" />
            <RailStat
              label="TOKENS"
              value={pats.length}
              to="/settings?tab=tokens"
            />
            <RailStat
              label="RECENT"
              value={recentCapped ? `${RECENT_LIMIT}+` : recent.length}
              to="/#recent"
            />
          </dl>
        </section>

        {/* Connect — always open. Mint + snippet tabs kept; prompt examples
            moved to docs-territory since they're one-time guidance. */}
        <section className="rounded-[var(--radius-lg)] border border-border bg-surface shadow-sm overflow-hidden">
          <div className="flex items-baseline justify-between gap-2 px-4 py-3">
            <span className="coord-ink">§ CONNECT</span>
            <div className="flex items-baseline gap-3">
              {pats.length > 0 ? (
                <span className="coord tabular-nums">[{pats.length}]</span>
              ) : (
                <Badge variant="pending">needs setup</Badge>
              )}
              <Link
                to="/settings?tab=tokens"
                className="coord hover:text-primary rounded-[var(--radius-sm)] focus:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2 focus-visible:ring-offset-surface"
              >
                <span aria-hidden>↗ </span>MANAGE
              </Link>
            </div>
          </div>

          <div className="border-t border-border">
              {/* Mint */}
              <div className="p-4 border-b border-border">
                <div className="coord-spark mb-2">MINT TOKEN</div>
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
                  <p role="alert" className="mt-2 flex items-start gap-1.5 text-xs text-destructive">
                    <AlertCircle className="h-3.5 w-3.5 shrink-0 mt-px" aria-hidden />
                    {mintError}
                  </p>
                )}

                {activePat && (
                  <div
                    className="mt-3 rounded-[var(--radius-md)] border border-accent/40 bg-accent/5 p-2"
                    role="status"
                    aria-live="polite"
                  >
                    <div className="coord-spark mb-1">FRESH — COPY NOW</div>
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
                <div className="coord-spark mb-2">DROP SNIPPET</div>
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
                <div className="coord-spark mb-2">ACTIVE TOKENS</div>
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
 * Compact stats cell: icon + count per non-empty category. Blank while
 * metrics are loading; "—" when the vault has no content. Icons replace
 * the "DOCS / TABLES / FILES" labels to keep the cell narrow and scannable.
 */
function VaultStatsCell({ m }: { m?: VaultMetrics }) {
  if (!m) {
    return (
      <span
        className="coord tabular-nums whitespace-nowrap self-baseline"
        aria-hidden
      />
    );
  }
  const d = m.document_count ?? 0;
  const t = m.table_count ?? 0;
  const f = m.file_count ?? 0;
  const title = `${d} document${d === 1 ? "" : "s"} · ${t} table${t === 1 ? "" : "s"} · ${f} file${f === 1 ? "" : "s"}`;
  if (d + t + f === 0) {
    return (
      <span
        className="coord tabular-nums whitespace-nowrap self-baseline"
        title={title}
      >
        —
      </span>
    );
  }
  return (
    <span
      className="coord tabular-nums whitespace-nowrap self-baseline inline-flex items-center gap-2"
      title={title}
    >
      {d > 0 && (
        <span className="inline-flex items-center gap-1">
          <FileText className="h-3 w-3" aria-hidden />
          {d}
        </span>
      )}
      {t > 0 && (
        <span className="inline-flex items-center gap-1">
          <TableIcon className="h-3 w-3" aria-hidden />
          {t}
        </span>
      )}
      {f > 0 && (
        <span className="inline-flex items-center gap-1">
          <FileIcon className="h-3 w-3" aria-hidden />
          {f}
        </span>
      )}
    </span>
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
  // Pad raw numbers to 2 digits; pass strings (e.g. "8+") through verbatim.
  const display = typeof value === "number" ? String(value).padStart(2, "0") : value;
  const body = (
    <>
      <dt className="coord group-hover:text-primary transition-colors">{label}</dt>
      <dd className="font-mono text-xl font-semibold tabular-nums text-foreground group-hover:text-primary transition-colors">
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
