import { useEffect, useMemo, useRef, useState } from "react";
import { Link, useLocation, useNavigate, useOutletContext } from "react-router-dom";
import {
  AlertTriangle,
  ArrowRight,
  ArrowUpRight,
  ChevronRight,
  Copy,
  Eye,
  EyeOff,
  FileClock,
  FolderPlus,
  Plus,
  Star,
  Trash2,
} from "lucide-react";
import { HomeSearchHero } from "@/components/home-search-hero";
import { Alert } from "@/components/ui/alert";
import { Button } from "@/components/ui/button";
import { CodeSnippet } from "@/components/ui/code-snippet";
import { ConfirmDialog } from "@/components/ui/confirm-dialog";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Panel } from "@/components/ui/panel";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { Badge } from "@/components/ui/badge";
import { EmptyState } from "@/components/empty-state";
import type { VaultRow } from "@/components/vault-list";
import { useVaultFavorites } from "@/hooks/use-vault-favorites";
import { RelativeTime } from "@/components/ui/relative-time";
import { TooltipText } from "@/components/ui/tooltip-text";
import { RoleBadge } from "@/components/status-badge";
import { QuickstartDialog, QUICKSTART_DISMISS_KEY } from "@/components/quickstart-dialog";
import {
  listVaults,
  getRecent,
  getVaultInfo,
  createPAT,
  listPATs,
  revokePAT,
  getAuthConfig,
} from "@/lib/api";
import { mcpInstallSnippets, mcpOAuthSnippets, MCP_AGENT_FILES } from "@/lib/mcp-snippets";
import { recentIcon, recentTone } from "@/lib/recent";
import { timeAgo } from "@/lib/utils";
import type { HealthSnapshot } from "@/hooks/use-health";

type SearchMode = "dense" | "literal";
type ConnectMode = "pat" | "oauth";
type ConnectTab = "claude" | "cursor" | "codex" | "vscode" | "openclaw";

// Recent-activity fetch size. The list starts with this many; "Show more"
// grows it (doubling — "this many again") up to RECENT_MAX. When a fetch comes
// back full we render the count as "N+" rather than implying it's the total.
const RECENT_LIMIT = 8;
// Backend /recent caps `limit` at 100, so that's the ceiling for "Show more".
const RECENT_MAX = 100;
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

interface VaultMetrics {
  document_count?: number;
  table_count?: number;
  file_count?: number;
  last_activity?: string;
}

export default function HomePage() {
  const { health } = useOutletContext<{ health: HealthSnapshot | null }>();
  const [vaults, setVaults] = useState<VaultRow[]>([]);
  const [vaultsLoading, setVaultsLoading] = useState(true);
  const [vaultsError, setVaultsError] = useState(false);
  // The Home directory is a bounded preview. This state only preserves row
  // stability if an extra visible favorite is unpinned; discovery of the full
  // directory belongs to the always-visible "View all vaults" route.
  const [vaultLimit, setVaultLimit] = useState(VAULT_PREVIEW_LIMIT);
  // Per-browser favorited vault IDs (localStorage) — same source the vault rail
  // uses, so pinning here and in the rail stay in sync.
  const { isFavorite, toggleFavorite, favOrder } = useVaultFavorites();
  const [recent, setRecent] = useState<RecentRow[]>([]);
  const [recentLoading, setRecentLoading] = useState(true);
  const [recentError, setRecentError] = useState(false);
  const [recentLimit, setRecentLimit] = useState(RECENT_LIMIT);
  const [recentLoadingMore, setRecentLoadingMore] = useState(false);
  const [pats, setPats] = useState<PATRow[]>([]);
  const [pendingRevoke, setPendingRevoke] = useState<PATRow | null>(null);
  const [activePat, setActivePat] = useState<string | null>(null);
  const [showPat, setShowPat] = useState(false);
  const [copied, setCopied] = useState<string | null>(null);
  const [creating, setCreating] = useState(false);
  const [mintError, setMintError] = useState<string | null>(null);
  const [newName, setNewName] = useState("");
  const [connectTab, setConnectTab] = useState<ConnectTab>("claude");
  const [connectMode, setConnectMode] = useState<ConnectMode>("pat");
  const [vaultMetrics, setVaultMetrics] = useState<Record<string, VaultMetrics>>({});
  const metricsRequested = useRef<Set<string>>(new Set());
  const [homeQuery, setHomeQuery] = useState("");
  const [homeSearchMode, setHomeSearchMode] = useState<SearchMode>("dense");
  const [quickstartOpen, setQuickstartOpen] = useState(false);
  const quickstartChecked = useRef(false);
  const location = useLocation();
  const navigate = useNavigate();
  const recentCapped = recent.length >= recentLimit;
  const canLoadMore =
    !recentLoading && !recentError && recent.length >= recentLimit && recentLimit < RECENT_MAX;

  useEffect(() => {
    let cancelled = false;
    // The directory endpoint stays lightweight; Home enriches only the vault
    // cards it renders with bounded /info requests below.
    setVaultsLoading(true);
    setVaultsError(false);
    listVaults()
      .then((d) => {
        if (!cancelled) setVaults(d.vaults || []);
      })
      .catch(() => {
        if (!cancelled) setVaultsError(true);
      })
      .finally(() => {
        if (!cancelled) setVaultsLoading(false);
      });
    loadRecent(() => cancelled);
    loadPATs();
    return () => {
      cancelled = true;
    };
  }, []);

  async function retryVaults() {
    setVaultsLoading(true);
    setVaultsError(false);
    try {
      const data = await listVaults();
      setVaults(data.vaults || []);
    } catch {
      setVaultsError(true);
    } finally {
      setVaultsLoading(false);
    }
  }

  async function loadRecent(
    isCancelled: () => boolean = () => false,
    targetLimit: number = RECENT_LIMIT,
    { more = false }: { more?: boolean } = {},
  ) {
    // "Show more" keeps the current list visible (spinner on the button);
    // a fresh/initial load shows the skeleton.
    if (more) setRecentLoadingMore(true);
    else {
      setRecentLoading(true);
      setRecentError(false);
    }
    try {
      const d = await getRecent(undefined, targetLimit);
      if (isCancelled()) return;
      setRecent(d.changes || []);
      setRecentLimit(targetLimit);
    } catch {
      if (isCancelled()) return;
      // On a "Show more" failure keep the existing list; only a fresh load
      // surfaces the error panel.
      if (!more) setRecentError(true);
    } finally {
      if (!isCancelled()) {
        if (more) setRecentLoadingMore(false);
        else setRecentLoading(false);
      }
    }
  }

  function loadMoreRecent() {
    // Grow by the current count ("this many again"), capped at the backend max.
    loadRecent(() => false, Math.min(recentLimit * 2, RECENT_MAX), { more: true });
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
    try {
      await navigator.clipboard?.writeText(text);
      setCopied(label);
      setTimeout(() => setCopied(null), 2000);
    } catch {
      /* Clipboard can be blocked on insecure origins; the value stays visible. */
    }
  }

  async function handleCreatePAT(event: React.FormEvent) {
    event.preventDefault();
    const name = newName.trim();
    if (!name) return;
    setMintError(null);
    setCreating(true);
    try {
      const result = await createPAT(name);
      setActivePat(result.token);
      setShowPat(true);
      setNewName("");
      await loadPATs();
    } catch (caught) {
      setMintError(
        caught instanceof Error ? caught.message : "Couldn't mint a token. Please try again.",
      );
    } finally {
      setCreating(false);
    }
  }

  const pat = activePat || "<YOUR_PAT>";
  const snippets = useMemo(() => mcpInstallSnippets(pat), [pat]);
  const oauthSnippetsMap = useMemo(() => mcpOAuthSnippets(), []);

  const [oauthEnabled, setOauthEnabled] = useState(false);
  useEffect(() => {
    let cancelled = false;
    getAuthConfig().then((cfg) => {
      if (!cancelled) setOauthEnabled(cfg.available && cfg.mcp_oauth.enabled);
    });
    return () => {
      cancelled = true;
    };
  }, []);

  function runHomeSearch(query = homeQuery) {
    const value = query.trim();
    if (!value) return;
    const params = new URLSearchParams({ q: value });
    if (homeSearchMode === "literal") params.set("mode", "literal");
    navigate(`/search?${params.toString()}`);
  }

  // Home shows a preview of the vault directory; the full list (with filter)
  // lives on /vault. Favorites float to the top (mirroring the vault rail) and
  // are always visible: the preview cap is a FLOOR, not a hard slice — a
  // favorite is never hidden behind "Show more".
  const orderedVaults = useMemo(() => {
    // Filter to the LIVE list so a favorited-but-deleted/revoked vault id drops
    // silently (same as the rail); newest-favorited first within the group.
    const favs = vaults
      .filter((v) => isFavorite(v.id))
      .sort((a, b) => favOrder(a.id) - favOrder(b.id));
    const rest = vaults.filter((v) => !isFavorite(v.id));
    return [...favs, ...rest];
  }, [vaults, isFavorite, favOrder]);
  const liveFavCount = useMemo(
    () => vaults.reduce((n, v) => (isFavorite(v.id) ? n + 1 : n), 0),
    [vaults, isFavorite],
  );
  // Show at least `vaultLimit`, but never fewer than the live favorites.
  const visibleVaultCount = Math.max(vaultLimit, liveFavCount);
  const previewVaults = useMemo(
    () => orderedVaults.slice(0, visibleVaultCount),
    [orderedVaults, visibleVaultCount],
  );

  // Home cards need the same small set of live counts as the shared vault
  // directory. Keep the enrichment bounded: /vaults/{v}/info fans out into
  // several count queries, so loading every card at once can saturate the pool.
  useEffect(() => {
    let cancelled = false;
    const requested = metricsRequested.current;
    const completed = new Set<string>();
    const todo = previewVaults.filter((v) => !requested.has(v.name));
    todo.forEach((v) => requested.add(v.name));
    void (async () => {
      for (let i = 0; i < todo.length; i += 4) {
        if (cancelled) return;
        await Promise.all(
          todo.slice(i, i + 4).map((v) =>
            getVaultInfo(v.name)
              .then((info) => {
                if (cancelled) return;
                completed.add(v.name);
                setVaultMetrics((current) => ({
                  ...current,
                  [v.name]: {
                    document_count: info?.document_count,
                    table_count: info?.table_count,
                    file_count: info?.file_count,
                    last_activity: info?.last_activity,
                  },
                }));
              })
              .catch(() => {
                requested.delete(v.name);
              }),
          ),
        );
      }
    })();
    return () => {
      cancelled = true;
      todo.forEach((v) => {
        if (!completed.has(v.name)) requested.delete(v.name);
      });
    };
  }, [previewVaults]);

  const archivedVaults = vaults.filter((v) => v.status === "archived").length;
  const vaultAccess = useMemo(
    () =>
      vaults.reduce(
        (counts, vault) => {
          if (vault.role === "owner") counts.owned += 1;
          if (vault.role === "admin" || vault.role === "writer") counts.editable += 1;
          if (vault.role === "reader") counts.readOnly += 1;
          return counts;
        },
        { owned: 0, editable: 0, readOnly: 0 },
      ),
    [vaults],
  );

  function toggleVaultFavorite(v: VaultRow) {
    // Lock in the current row count before toggling so unpinning a cap-exempt
    // favorite can't make its own row vanish (row reorders in place; the keyed
    // <li> keeps keyboard focus on the star). Per Codex design review.
    setVaultLimit((n) => Math.max(n, visibleVaultCount));
    toggleFavorite(v.id);
  }

  const accessValue = (value: number) =>
    vaultsLoading || vaultsError ? "—" : value.toLocaleString();
  const indexUpsert = health?.vector_store?.backfill?.upsert;
  const indexedCount = indexUpsert?.indexed ?? null;
  const indexingAbandoned = indexUpsert?.abandoned ?? 0;
  const indexingPending = indexUpsert
    ? Math.max(0, (indexUpsert.pending ?? 0) - indexingAbandoned)
    : 0;
  const indexStatusTone = indexingAbandoned > 0
    ? "bg-warning"
    : indexingPending > 0
      ? "bg-info"
      : "bg-success";
  const homeSearchSuggestions = [
    { scope: "type:", value: "decision" },
    { scope: "type:", value: "runbook" },
    { scope: "type:", value: "spec" },
    ...(vaults[0] ? [{ scope: "vault:", value: vaults[0].name }] : []),
    { scope: "tag:", value: "deploy" },
  ];

  // Search is the masthead; the primary column carries the vault directory and
  // activity detail while the supporting rail holds compact workspace context.
  return (
    <div className="fade-up">
      <HomeSearchHero
        query={homeQuery}
        mode={homeSearchMode}
        suggestions={homeSearchSuggestions}
        indexStatus={
          indexedCount !== null && !vaultsLoading && !vaultsError
            ? {
                vaultCount: vaults.length,
                indexedCount,
                pending: indexingPending,
                abandoned: indexingAbandoned,
                toneClassName: indexStatusTone,
              }
            : undefined
        }
        onQueryChange={setHomeQuery}
        onModeChange={setHomeSearchMode}
        onSearch={runHomeSearch}
      />

      <div className="grid grid-cols-1 items-start gap-x-8 gap-y-10 xl:grid-cols-[minmax(0,1fr)_340px]">
        <section
          id="vaults"
          className="min-w-0 scroll-mt-24"
          aria-busy={vaultsLoading}
        >
          <header className="flex flex-wrap items-baseline justify-between gap-4 border-b border-border pb-3">
            <div className="flex items-baseline gap-3">
              <h2 className="text-xl font-semibold tracking-tight">Your vaults</h2>
              <Badge variant="default" className="tabular-nums">{vaults.length}</Badge>
            </div>
            <Link
              to="/vault"
              className="inline-flex min-h-9 items-center gap-1 rounded-[var(--radius-sm)] text-xs text-link hover:text-link-hover focus:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2 focus-visible:ring-offset-background"
            >
              View all vaults
              <ArrowRight className="h-3 w-3" aria-hidden />
            </Link>
          </header>
          <span className="sr-only" role="status" aria-live="polite">
            {vaultsLoading
              ? "Loading your vaults"
              : vaultsError
                ? "Could not load your vaults"
                : `${vaults.length} accessible vault${vaults.length === 1 ? "" : "s"}`}
          </span>

          {vaultsLoading ? (
            <div className="mt-3 grid gap-2 md:grid-cols-2 xl:grid-cols-4" aria-hidden>
              {Array.from({ length: 4 }).map((_, index) => (
                <Panel key={index} className="min-h-36 p-3">
                  <span className="block h-4 w-24 animate-pulse rounded bg-surface-muted" />
                  <span className="mt-3 block h-3 w-4/5 animate-pulse rounded bg-surface-muted" />
                  <span className="mt-2 block h-3 w-3/5 animate-pulse rounded bg-surface-muted" />
                </Panel>
              ))}
            </div>
          ) : vaultsError ? (
            <EmptyState
              icon={
                <span className="feature-tile feat-neutral h-14 w-14">
                  <AlertTriangle className="h-6 w-6" aria-hidden />
                </span>
              }
              title="Couldn't load your vaults"
              description="The vault directory is temporarily unavailable."
              action={<Button variant="outline" size="sm" onClick={retryVaults}>Retry</Button>}
            />
          ) : vaults.length === 0 ? (
            <EmptyState
              icon={
                <span className="feature-tile feat-memory h-14 w-14">
                  <FolderPlus className="h-6 w-6" aria-hidden />
                </span>
              }
              title="No vaults yet"
              description="Create a vault to give your team and agents a shared knowledge space."
              action={
                <Button asChild variant="outline" size="sm">
                  <Link to="/vault/new">
                    <Plus className="h-4 w-4" aria-hidden />
                    Create a vault
                  </Link>
                </Button>
              }
            />
          ) : (
            <div className="mt-3 grid gap-2 md:grid-cols-2 xl:grid-cols-4 stagger">
              {previewVaults.map((vault) => (
                <HomeVaultCard
                  key={vault.id}
                  vault={vault}
                  metrics={vaultMetrics[vault.name]}
                  favorite={isFavorite(vault.id)}
                  onToggleFavorite={() => toggleVaultFavorite(vault)}
                />
              ))}
            </div>
          )}
        </section>

        <aside
          className="order-3 space-y-4 xl:order-none xl:col-start-2 xl:row-span-2 xl:row-start-1"
          aria-label="Workspace summary and agent connection"
        >
          <Panel aria-labelledby="vault-access-heading">
            <div className="flex items-start justify-between gap-3 border-b border-border px-4 py-3">
              <div>
                <h2 id="vault-access-heading" className="text-sm font-semibold text-foreground">Vault access</h2>
                <p className="mt-0.5 text-xs text-foreground-muted">Your roles across accessible vaults</p>
              </div>
              <Badge variant="default" className="shrink-0 tabular-nums">
                {vaultsLoading || vaultsError ? "—" : `${vaults.length.toLocaleString()} total`}
              </Badge>
            </div>
            <dl className="divide-y divide-border">
              <RailStat
                label="Owned by you"
                value={accessValue(vaultAccess.owned)}
              />
              <RailStat
                label="Shared, editable"
                value={accessValue(vaultAccess.editable)}
              />
              <RailStat
                label="Read only"
                value={accessValue(vaultAccess.readOnly)}
              />
              <RailStat label="Favorites" value={accessValue(liveFavCount)} />
              {archivedVaults > 0 && <RailStat label="Archived" value={accessValue(archivedVaults)} />}
            </dl>
          </Panel>

          <RecentChangesSummary
            recent={recent}
            loading={recentLoading}
            error={recentError}
            capped={recentCapped}
            onRetry={() => loadRecent()}
          />

          <Panel aria-labelledby="rail-connect">
            <div className="flex items-baseline justify-between gap-2 border-b border-border px-4 py-3">
              <h2 id="rail-connect" className="text-sm font-semibold text-foreground">Connect</h2>
              <div className="flex items-baseline gap-3">
                {pats.length > 0 ? (
                  <span className="tabular-nums text-xs text-foreground-muted">{pats.length} active</span>
                ) : (
                  <Badge variant="pending">needs setup</Badge>
                )}
                <Link
                  to="/settings?tab=tokens"
                  className="rounded-[var(--radius-sm)] text-xs text-link hover:text-link-hover focus:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2 focus-visible:ring-offset-surface"
                >
                  Manage
                </Link>
              </div>
            </div>

            <div className="border-b border-border p-4">
              <div className="mb-2 text-xs font-semibold text-foreground">Mint token</div>
              <form onSubmit={handleCreatePAT} className="space-y-2">
                <Label htmlFor="pat-name" className="sr-only">Token name</Label>
                <Input
                  id="pat-name"
                  type="text"
                  placeholder="Token name (e.g. my-laptop)"
                  value={newName}
                  onChange={(event) => setNewName(event.target.value)}
                  aria-invalid={mintError ? true : undefined}
                  className="h-8 text-xs"
                />
                <Button
                  type="submit"
                  loading={creating}
                  disabled={!newName.trim()}
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
                  <div className="mb-1 text-xs font-semibold text-accent-strong">New token — copy now</div>
                  <div className="flex items-center gap-1.5">
                    <code className="flex-1 break-all font-mono text-[10px] leading-snug text-foreground">
                      {showPat ? activePat : activePat.slice(0, 10) + "•".repeat(14)}
                    </code>
                    {!showPat && <span className="sr-only">Token value: {activePat}</span>}
                    <button
                      type="button"
                      onClick={() => setShowPat(!showPat)}
                      aria-label={showPat ? "Hide token" : "Show token"}
                      className="shrink-0 cursor-pointer rounded-[var(--radius-sm)] text-foreground-muted transition-token hover:text-link focus:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2 focus-visible:ring-offset-surface"
                    >
                      {showPat ? (
                        <EyeOff className="h-3 w-3" aria-hidden />
                      ) : (
                        <Eye className="h-3 w-3" aria-hidden />
                      )}
                    </button>
                    <button
                      type="button"
                      onClick={() => copy(activePat, "pat")}
                      aria-label={copied === "pat" ? "Token copied" : "Copy token"}
                      className="shrink-0 cursor-pointer rounded-[var(--radius-sm)] text-foreground-muted transition-token hover:text-link focus:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2 focus-visible:ring-offset-surface"
                    >
                      {copied === "pat" ? <span aria-hidden>OK</span> : <Copy className="h-3 w-3" aria-hidden />}
                    </button>
                  </div>
                </div>
              )}
            </div>

            <div className="border-b border-border p-4">
              <div className="mb-2 text-xs font-semibold text-foreground">Client config</div>
              {oauthEnabled && (
                <div className="mb-2 flex items-center gap-2 text-[10px]">
                  <div className="inline-flex overflow-hidden rounded-[var(--radius-sm)] border border-border">
                    {(["pat", "oauth"] as const).map((mode) => (
                      <button
                        key={mode}
                        type="button"
                        onClick={() => setConnectMode(mode)}
                        className={`px-2 py-1 font-medium transition-token ${
                          connectMode === mode
                            ? "bg-primary text-primary-foreground"
                            : "bg-surface text-foreground-muted hover:bg-surface-hover hover:text-foreground"
                        }`}
                        aria-pressed={connectMode === mode}
                      >
                        {mode === "pat" ? "PAT" : "OAuth"}
                      </button>
                    ))}
                  </div>
                  <span className="text-foreground-muted">
                    {connectMode === "oauth" ? "Browser sign-in." : "Paste a minted token."}
                  </span>
                </div>
              )}
              <Tabs value={connectTab} onValueChange={(value) => setConnectTab(value as ConnectTab)}>
                <TabsList className="mb-2 flex-wrap gap-0">
                  <TabsTrigger value="claude" className="px-2 py-1 text-[10px]">Claude Code</TabsTrigger>
                  <TabsTrigger value="cursor" className="px-2 py-1 text-[10px]">Cursor</TabsTrigger>
                  <TabsTrigger value="codex" className="px-2 py-1 text-[10px]">Codex</TabsTrigger>
                  <TabsTrigger value="vscode" className="px-2 py-1 text-[10px]">VS Code</TabsTrigger>
                  <TabsTrigger value="openclaw" className="px-2 py-1 text-[10px]">OpenClaw</TabsTrigger>
                </TabsList>
                <TabsContent value={connectTab}>
                  {connectMode === "oauth" ? (
                    oauthSnippetsMap[connectTab] !== undefined ? (
                      <CodeSnippet
                        code={oauthSnippetsMap[connectTab] as string}
                        filename={MCP_AGENT_FILES[connectTab]}
                      />
                    ) : (
                      <div className="rounded-[var(--radius-md)] border border-border px-3 py-2 text-xs text-foreground-muted">
                        This client uses stdio. Switch to PAT for its config.
                      </div>
                    )
                  ) : (
                    <CodeSnippet code={snippets[connectTab]} filename={MCP_AGENT_FILES[connectTab]} />
                  )}
                </TabsContent>
              </Tabs>
            </div>

            <div className="p-4">
              <div className="mb-2 text-xs font-semibold text-foreground">Active tokens</div>
              {pats.length === 0 ? (
                <div className="text-xs text-foreground-muted">No active tokens</div>
              ) : (
                <ul className="overflow-hidden rounded-[var(--radius-md)] border border-border divide-y divide-border">
                  {pats.slice(0, 4).map((token) => (
                    <li
                      key={token.token_id}
                      className="flex items-center justify-between gap-2 px-2 py-1.5 text-xs"
                    >
                      <span title={token.name} className="truncate font-medium text-foreground">
                        {token.name}
                      </span>
                      <div className="flex shrink-0 items-center gap-2">
                        <span className="tabular-nums text-[10px] text-foreground-muted">
                          {token.last_used_at ? timeAgo(token.last_used_at) : "Never used"}
                        </span>
                        <button
                          type="button"
                          onClick={() => setPendingRevoke(token)}
                          aria-label={`Revoke token ${token.name}`}
                          className="cursor-pointer rounded-[var(--radius-sm)] text-foreground-muted transition-token hover:text-destructive focus:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2 focus-visible:ring-offset-surface"
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
                  className="mt-2 inline-flex items-center gap-1 rounded-[var(--radius-sm)] text-xs text-link hover:text-link-hover focus:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2 focus-visible:ring-offset-surface"
                >
                  +{pats.length - 4} more
                  <ArrowRight className="h-3 w-3" aria-hidden />
                </Link>
              )}
            </div>
          </Panel>
        </aside>

        <section
          id="recent"
          className="order-2 scroll-mt-24 xl:order-none xl:col-start-1"
          aria-busy={recentLoading}
        >
        <header className="flex flex-wrap items-baseline justify-between gap-4 border-b border-border pb-3">
          <div className="flex items-baseline gap-3">
            <h2 className="text-xl font-semibold tracking-tight">Recent activity</h2>
            {!recentLoading && !recentError && (
              <Badge variant="default" className="tabular-nums">
                {recent.length}{recentCapped ? "+" : ""}
              </Badge>
            )}
          </div>
          <span className="text-xs text-foreground-muted">Across every accessible vault</span>
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
                <li key={i} className="grid grid-cols-[36px_minmax(0,1fr)_56px] items-center gap-3 px-4 py-3.5">
                  <span className="h-9 w-9 rounded-[var(--radius-md)] bg-surface-muted" />
                  <span className="min-w-0 space-y-2">
                    <span className="block h-3 w-3/5 rounded bg-surface-muted" />
                    <span className="block h-2.5 w-4/5 rounded bg-surface-muted" />
                  </span>
                  <span className="h-3 w-12 justify-self-end rounded bg-surface-muted" />
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
            action={<Button variant="outline" size="sm" onClick={() => loadRecent()}>Retry</Button>}
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
          <Panel className="mt-3">
            <ol className="divide-y divide-border stagger">
              {recent.map((change, index) => {
                const Icon = recentIcon(change.type);
                const tone = recentTone(change.type);
                return (
                  <li key={`${change.doc_id}:${change.changed_at ?? ""}:${index}`}>
                    <Link
                      to={`/vault/${change.vault}/doc/${change.doc_id}`}
                      className="home-activity-row group grid grid-cols-[36px_minmax(0,1fr)_auto] items-center gap-3 bg-surface px-4 py-3.5 transition-token hover:bg-surface-hover focus:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-inset"
                    >
                      <span
                        className="inline-flex h-9 w-9 shrink-0 items-center justify-center rounded-[var(--radius-md)] border border-border"
                        style={{
                          color: tone,
                          backgroundColor: `color-mix(in srgb, ${tone} 12%, transparent)`,
                        }}
                        aria-hidden
                      >
                        <Icon className="h-4 w-4" aria-hidden />
                      </span>

                      <span className="min-w-0">
                        <span
                          className="block truncate text-sm font-semibold tracking-tight text-foreground transition-colors group-hover:text-link"
                          title={change.title}
                        >
                          {change.title}
                        </span>
                        <span className="mt-1 flex min-w-0 items-center gap-1.5 text-xs text-foreground-muted">
                          <TooltipText className="max-w-32 shrink-0 truncate font-medium text-link">
                            {change.vault}
                          </TooltipText>
                          <ChevronRight className="h-3 w-3 shrink-0 text-subtle" aria-hidden />
                          <span className="min-w-0 truncate" title={change.path}>{change.path}</span>
                          {change.commit && (
                            <code className="coord ml-1 hidden shrink-0 rounded-[var(--radius-sm)] border border-border bg-surface-muted px-1.5 py-0.5 font-mono sm:inline">
                              {change.commit.slice(0, 7)}
                            </code>
                          )}
                        </span>
                      </span>

                      <span className="flex min-w-14 shrink-0 flex-col items-end gap-1.5">
                        <RelativeTime iso={change.changed_at} className="justify-end text-right" />
                        <ArrowUpRight
                          className="h-3.5 w-3.5 text-subtle transition-token group-hover:text-link"
                          aria-hidden
                        />
                      </span>
                    </Link>
                  </li>
                );
              })}
            </ol>
            {canLoadMore && (
              <div className="border-t border-border p-2">
                <Button
                  variant="ghost"
                  size="sm"
                  onClick={loadMoreRecent}
                  loading={recentLoadingMore}
                  aria-label="Show more recent activity"
                  className="w-full"
                >
                  {recentLoadingMore ? "Loading…" : "Show more"}
                </Button>
              </div>
            )}
          </Panel>
        )}
        </section>
      </div>

      <ConfirmDialog
        open={pendingRevoke !== null}
        onOpenChange={(open) => !open && setPendingRevoke(null)}
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
        mcpOauthEnabled={oauthEnabled}
      />
    </div>
  );
}

function RecentChangesSummary({
  recent,
  loading,
  error,
  capped,
  onRetry,
}: {
  recent: RecentRow[];
  loading: boolean;
  error: boolean;
  capped: boolean;
  onRetry: () => void;
}) {
  const latest = recent[0];

  return (
    <Panel aria-labelledby="recent-changes-summary-heading" aria-busy={loading}>
      <div className="flex items-center justify-between gap-3 border-b border-border px-4 py-3">
        <div>
          <h2
            id="recent-changes-summary-heading"
            className="text-sm font-semibold text-foreground"
          >
            Recent changes
          </h2>
          <p className="mt-0.5 text-xs text-foreground-muted">Across every accessible vault</p>
        </div>
        <span
          className="inline-grid h-8 w-8 shrink-0 place-items-center rounded-[var(--radius-md)] border border-border bg-surface-muted text-link"
          aria-hidden
        >
          <FileClock className="h-4 w-4" aria-hidden />
        </span>
      </div>

      {loading ? (
        <>
          <div className="px-4 py-4" aria-hidden>
            <span className="block h-8 w-14 animate-pulse rounded bg-surface-muted" />
            <span className="mt-2 block h-3 w-24 animate-pulse rounded bg-surface-muted" />
          </div>
          <div className="border-t border-border px-4 py-3" aria-hidden>
            <span className="block h-3 w-20 animate-pulse rounded bg-surface-muted" />
          </div>
        </>
      ) : error ? (
        <div className="p-4">
          <p className="text-sm font-medium text-foreground">Activity is unavailable</p>
          <p className="mt-1 text-xs text-foreground-muted">Try loading the recent changes again.</p>
          <Button variant="outline" size="sm" onClick={onRetry} className="mt-3">
            Retry
          </Button>
        </div>
      ) : latest ? (
        <>
          <div className="grid grid-cols-[minmax(0,1fr)_auto] items-end gap-4 px-4 py-4">
            <div>
              <div className="text-xl font-semibold tracking-tight tabular-nums text-foreground">
                {recent.length.toLocaleString()}{capped ? "+" : ""}
              </div>
              <p className="mt-1 text-xs text-foreground-muted">Recent entries</p>
            </div>
            <div className="text-right">
              <p className="text-xs font-medium text-foreground-muted">Latest</p>
              <RelativeTime iso={latest.changed_at} className="mt-1 justify-end text-right" />
            </div>
          </div>
          <Link
            to="/#recent"
            className="flex min-h-10 items-center justify-between border-t border-border px-4 py-2 text-xs font-medium text-link transition-token hover:bg-surface-hover hover:text-link-hover focus:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-inset"
          >
            View activity
            <ArrowRight className="h-3.5 w-3.5" aria-hidden />
          </Link>
        </>
      ) : (
        <div className="p-4">
          <p className="text-sm font-medium text-foreground">No recent changes</p>
          <p className="mt-1 text-xs leading-relaxed text-foreground-muted">
            New document activity will appear here.
          </p>
        </div>
      )}
    </Panel>
  );
}

function HomeVaultCard({
  vault,
  metrics,
  favorite,
  onToggleFavorite,
}: {
  vault: VaultRow;
  metrics?: VaultMetrics;
  favorite: boolean;
  onToggleFavorite: () => void;
}) {
  const contentMetrics = metrics
    ? [
        { label: "Documents", shortLabel: "Docs", type: "document", value: metrics.document_count ?? 0 },
        { label: "Tables", shortLabel: "Tables", type: "table", value: metrics.table_count ?? 0 },
        { label: "Files", shortLabel: "Files", type: "file", value: metrics.file_count ?? 0 },
      ]
    : null;

  return (
    <Panel inset={false} className="home-vault-card card-hover relative h-full overflow-hidden">
      <Link
        to={`/vault/${vault.name}`}
        className="group flex h-full min-h-40 flex-col p-3 pr-11 focus:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-inset"
      >
        <div className="flex min-w-0 items-center gap-2">
          <h3
            className="min-w-0 truncate text-sm font-semibold tracking-tight text-foreground transition-colors group-hover:text-link"
            title={vault.name}
          >
            {vault.name}
          </h3>
          {vault.status === "archived" && <Badge variant="archived">archived</Badge>}
        </div>

        <div className="mt-1.5 flex flex-wrap items-center gap-1.5">
          {vault.role && <RoleBadge role={vault.role} />}
          <RelativeTime iso={metrics?.last_activity} fallback="No activity" />
        </div>

        <p className="mt-2.5 line-clamp-2 min-h-9 text-xs leading-relaxed text-foreground-muted">
          {vault.description || "A shared knowledge space for your team and connected agents."}
        </p>

        <dl className="mt-auto grid grid-cols-3 divide-x divide-border border-t border-border pt-2">
          {contentMetrics ? (
            contentMetrics.map((metric, index) => {
              const Icon = recentIcon(metric.type);
              const tone = recentTone(metric.type);
              return (
                <div key={metric.label} className={index === 0 ? "pr-1.5" : "px-1.5 last:pr-0"}>
                  <dt
                    className="flex min-w-0 items-center gap-1 text-xs text-foreground-muted"
                    title={metric.label}
                  >
                    <span
                      className="inline-flex h-4 w-4 shrink-0 items-center justify-center rounded-[var(--radius-sm)]"
                      style={{
                        color: tone,
                        backgroundColor: `color-mix(in srgb, ${tone} 12%, transparent)`,
                      }}
                      aria-hidden
                    >
                      <Icon className="h-2.5 w-2.5" aria-hidden />
                    </span>
                    <span className="truncate">{metric.shortLabel}</span>
                  </dt>
                  <dd className="mt-0.5 text-sm font-semibold tabular-nums text-foreground">
                    {metric.value.toLocaleString()}
                  </dd>
                </div>
              );
            })
          ) : (
            Array.from({ length: 3 }).map((_, index) => (
              <div
                key={index}
                className={index === 0 ? "pr-1.5" : "px-1.5 last:pr-0"}
                aria-hidden
              >
                <span className="block h-3 w-12 animate-pulse rounded bg-surface-muted" />
                <span className="mt-2 block h-5 w-8 animate-pulse rounded bg-surface-muted" />
              </div>
            ))
          )}
        </dl>
      </Link>
      <button
        type="button"
        onClick={onToggleFavorite}
        aria-pressed={favorite}
        aria-label={favorite ? `Remove ${vault.name} from favorites` : `Add ${vault.name} to favorites`}
        className={`absolute right-2 top-2 grid h-9 w-9 place-items-center rounded-[var(--radius-sm)] transition-token focus:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2 focus-visible:ring-offset-surface ${
          favorite
            ? "bg-surface-selected text-surface-selected-foreground"
            : "text-foreground-muted hover:bg-surface-hover hover:text-foreground"
        }`}
      >
        <Star className={`h-3.5 w-3.5 ${favorite ? "fill-current" : ""}`} aria-hidden />
      </button>
    </Panel>
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
