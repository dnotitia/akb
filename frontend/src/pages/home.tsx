import { useEffect, useMemo, useRef, useState, type ReactNode } from "react";
import { Link, useLocation, useOutletContext } from "react-router-dom";
import {
  AlertTriangle,
  ArrowRight,
  ArrowUpRight,
  Box,
  Check,
  ChevronRight,
  Circle,
  Clock3,
  Database,
  FileClock,
  FolderPlus,
  PlugZap,
  Plus,
  Star,
} from "lucide-react";
import { Button } from "@/components/ui/button";
import { Panel } from "@/components/ui/panel";
import { Badge } from "@/components/ui/badge";
import { TonalIcon } from "@/components/ui/tonal-icon";
import { EmptyState } from "@/components/empty-state";
import type { VaultRow } from "@/components/vault-list";
import { useVaultFavorites } from "@/hooks/use-vault-favorites";
import { RelativeTime } from "@/components/ui/relative-time";
import { TooltipText } from "@/components/ui/tooltip-text";
import { RoleBadge } from "@/components/status-badge";
import { QuickstartDialog } from "@/components/quickstart-dialog";
import {
  listVaults,
  getRecent,
  getVaultInfo,
  listPATs,
  getAuthConfig,
} from "@/lib/api";
import { recentIcon, recentTone } from "@/lib/recent";
import type { HealthSnapshot } from "@/hooks/use-health";
import { useCurrentUser } from "@/contexts/current-user-context";
import {
  readRecentDocumentViews,
  type RecentDocumentView,
} from "@/lib/recent-document-views";

// Recent-activity fetch size. The list starts with this many; "Show more"
// grows it (doubling — "this many again") up to RECENT_MAX. When a fetch comes
// back full we render the count as "N+" rather than implying it's the total.
const RECENT_LIMIT = 6;
// Backend /recent caps `limit` at 100, so that's the ceiling for "Show more".
const RECENT_MAX = 100;
// How many vaults the Home preview shows before linking out to /vault.
const VAULT_PREVIEW_LIMIT = 4;
const HOME_SETUP_DISMISS_KEY = "akb.homeSetupDismissed";

interface RecentRow {
  doc_id: string;
  vault: string;
  path: string;
  title: string;
  type?: string;
  commit?: string;
  changed_at?: string;
  /** Forward-compatible enrichment. Older backends omit these fields and the
   *  row simply keeps its compact title/location shape. */
  updated_by_name?: string;
  author_name?: string;
  created_by_name?: string;
  action?: string;
  summary?: string;
  excerpt?: string;
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
  const currentUser = useCurrentUser();
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
  const [patsLoading, setPatsLoading] = useState(true);
  const [vaultMetrics, setVaultMetrics] = useState<Record<string, VaultMetrics>>({});
  const metricsRequested = useRef<Set<string>>(new Set());
  const [quickstartOpen, setQuickstartOpen] = useState(false);
  const [setupDismissed, setSetupDismissed] = useState(
    () => localStorage.getItem(HOME_SETUP_DISMISS_KEY) === "1",
  );
  const location = useLocation();
  const [recentViews, setRecentViews] = useState<RecentDocumentView[]>([]);
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

  useEffect(() => {
    setRecentViews(
      currentUser ? readRecentDocumentViews(currentUser.user_id, 8) : [],
    );
  }, [currentUser, location.key]);

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
    setPatsLoading(true);
    try {
      const d = await listPATs();
      setPats(d.tokens || []);
    } catch {
      /* non-fatal: leave pats empty */
    } finally {
      setPatsLoading(false);
    }
  }

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
  const continueWorking = useMemo(() => {
    if (vaultsLoading || vaultsError) return [];
    const accessible = new Set(vaults.map((vault) => vault.name));
    return recentViews
      .filter((view) => accessible.has(view.vault))
      .slice(0, 4);
  }, [recentViews, vaults, vaultsError, vaultsLoading]);

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

  const indexUpsert = health?.vector_store?.backfill?.upsert;
  const indexedCount = indexUpsert?.indexed ?? null;
  const indexingAbandoned = indexUpsert?.abandoned ?? 0;
  const indexingPending = indexUpsert
    ? Math.max(0, (indexUpsert.pending ?? 0) - indexingAbandoned)
    : 0;
  const hasVault = vaults.length > 0;
  const writableVault = vaults.find(
    (vault) => vault.status !== "archived" && vault.role !== "reader",
  );
  const hasKnowledge =
    recent.some((change) => change.path !== "overview/vault-skill.md") ||
    Object.values(vaultMetrics).some(
      (metrics) =>
        (metrics.document_count ?? 0) > 1 ||
        (metrics.table_count ?? 0) > 0 ||
        (metrics.file_count ?? 0) > 0,
    );
  // A read-only member should not be held in an onboarding step they cannot
  // complete. Their job is to explore the Vaults already shared with them.
  const knowledgeReady = hasKnowledge || (hasVault && !writableVault);
  const hasConnectedAgent = pats.some((token) => Boolean(token.last_used_at));
  const setupCompleted = [hasVault, knowledgeReady, hasConnectedAgent].filter(Boolean).length;
  const setupLoading = vaultsLoading || recentLoading || patsLoading;
  const setupComplete = setupCompleted === 3;
  const showAgentConnect = !setupLoading && !hasConnectedAgent;

  function dismissSetup() {
    localStorage.setItem(HOME_SETUP_DISMISS_KEY, "1");
    setSetupDismissed(true);
  }

  function showSetup() {
    localStorage.removeItem(HOME_SETUP_DISMISS_KEY);
    setSetupDismissed(false);
  }

  // Home is a working set, not a second search page. The primary column keeps
  // Vaults and change history close; the rail only carries setup and compact
  // workspace context that is not repeated in the ledgers.
  return (
    <div className="fade-up w-full space-y-6">
      <HomeWorkspaceHeader
        vaultCount={vaults.length}
        loading={vaultsLoading}
        indexedCount={indexedCount}
        indexingPending={indexingPending}
        indexingAbandoned={indexingAbandoned}
      />

      <div className="grid grid-cols-1 items-start gap-5 2xl:grid-cols-[minmax(0,1fr)_320px]">
        {continueWorking.length > 0 && (
          <div className="order-2 min-w-0 2xl:col-start-1 2xl:row-start-1">
            <ContinueWorkingSection items={continueWorking} />
          </div>
        )}

        <section
          id="vaults"
          className={`order-1 min-w-0 scroll-mt-24 2xl:col-start-1 ${
            continueWorking.length > 0 ? "2xl:row-start-2" : "2xl:row-start-1"
          }`}
          aria-busy={vaultsLoading}
        >
          <header className="flex min-h-10 flex-wrap items-center justify-between gap-3 border-b border-border pb-2.5">
            <div className="flex min-w-0 items-center gap-2.5">
              <TonalIcon tone="knowledge" size="sm">
                <Box aria-hidden />
              </TonalIcon>
              <h2 className="text-base font-semibold tracking-tight">Your vaults</h2>
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
            <div className="mt-3 grid gap-2 sm:grid-cols-2 lg:grid-cols-4" aria-hidden>
              {Array.from({ length: 4 }).map((_, index) => (
                <Panel key={index} className="min-h-32 p-3">
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
                  <Link to="/vault">
                    <Plus className="h-4 w-4" aria-hidden />
                    Open Vaults
                  </Link>
                </Button>
              }
            />
          ) : (
            <div className="mt-3 grid gap-2 sm:grid-cols-2 lg:grid-cols-4 stagger">
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
          className={`order-3 space-y-4 2xl:col-start-2 2xl:row-start-1 ${
            continueWorking.length > 0 ? "2xl:row-span-3" : "2xl:row-span-2"
          }`}
          aria-label="Workspace setup and summary"
        >
          {!setupComplete && !setupDismissed && (
            showAgentConnect ? (
              <HomeAgentConnectPanel
                completed={setupCompleted}
                tokenReady={pats.length > 0}
                tokenCount={pats.length}
                workspaceReady={hasVault && knowledgeReady}
                onConnect={() => setQuickstartOpen(true)}
                onDismiss={dismissSetup}
              />
            ) : (
              <HomeSetupPanel
                loading={setupLoading}
                completed={setupCompleted}
                hasVault={hasVault}
                hasKnowledge={knowledgeReady}
                hasConnectedAgent={hasConnectedAgent}
                tokenReady={pats.length > 0}
                writableVault={writableVault}
                onConnect={() => setQuickstartOpen(true)}
                onDismiss={dismissSetup}
              />
            )
          )}

          <WorkspaceSummary
            loading={vaultsLoading || patsLoading}
            vaultCount={vaults.length}
            ownedCount={vaultAccess.owned}
            editableCount={vaultAccess.editable}
            readOnlyCount={vaultAccess.readOnly}
            favoriteCount={liveFavCount}
            archivedCount={archivedVaults}
            tokenCount={pats.length}
            hasConnectedAgent={hasConnectedAgent}
            showAgentStatus={!showAgentConnect || setupDismissed}
            indexedCount={indexedCount}
            indexingPending={indexingPending}
            indexingAbandoned={indexingAbandoned}
            showSetupLink={!setupComplete && setupDismissed}
            onShowSetup={showSetup}
          />

        </aside>

        <section
          id="recent"
          className={`order-4 scroll-mt-24 2xl:col-start-1 ${
            continueWorking.length > 0 ? "2xl:row-start-3" : "2xl:row-start-2"
          }`}
          aria-busy={recentLoading}
        >
        <header className="flex min-h-10 flex-wrap items-center justify-between gap-3 border-b border-border pb-2.5">
          <div className="flex min-w-0 items-center gap-2.5">
            <TonalIcon tone="neutral" size="sm">
              <FileClock aria-hidden />
            </TonalIcon>
            <h2 className="text-base font-semibold tracking-tight">Recent updates</h2>
            {!recentLoading && !recentError && (
              <Badge variant="default" className="tabular-nums">
                {recent.length}{recentCapped ? "+" : ""}
              </Badge>
            )}
          </div>
          <span className="text-xs text-foreground-muted">Latest document changes across your Vaults</span>
        </header>
        <span className="sr-only" role="status" aria-live="polite">
          {recentLoading
            ? "Loading recent activity"
            : recentError
              ? "Could not load recent activity"
              : `${recent.length} recent update${recent.length === 1 ? "" : "s"}`}
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
            title="Couldn't load recent updates"
            description="The latest document changes are temporarily unavailable."
            action={<Button variant="outline" size="sm" onClick={() => loadRecent()}>Retry</Button>}
          />
        ) : recent.length === 0 ? (
          <EmptyState
            icon={
              <span className="feature-tile feat-knowledge h-14 w-14">
                <FileClock className="h-6 w-6" aria-hidden />
              </span>
            }
            title="Nothing updated yet"
            description="Document changes across your Vaults will appear here."
          />
        ) : (
          <Panel className="mt-3">
            <ol className="divide-y divide-border stagger">
              {recent.map((change) => {
                const Icon = recentIcon(change.type);
                const tone = recentTone(change.type);
                const updateAuthor = change.updated_by_name || change.author_name;
                const creator = updateAuthor ? null : change.created_by_name;
                const preview = change.excerpt?.trim() || change.summary?.trim();
                return (
                  <li key={`${change.doc_id}:${change.changed_at ?? change.commit ?? change.path}`}>
                    <Link
                      to={`/vault/${change.vault}/doc/${change.doc_id}`}
                      className="home-activity-row group grid grid-cols-[36px_minmax(0,1fr)_auto] items-start gap-3 bg-surface px-4 py-3.5 transition-token hover:bg-surface-hover focus:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-inset"
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
                        {updateAuthor && (
                          <span className="mb-1 block truncate text-xs text-foreground-muted">
                            <span className="font-medium text-foreground">{updateAuthor}</span>{" "}
                            {recentActionLabel(change.action)}
                          </span>
                        )}
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
                        {creator && (
                          <span className="mt-1 block truncate text-xs text-foreground-muted">
                            Created by <span className="font-medium text-foreground">{creator}</span>
                          </span>
                        )}
                        {preview && (
                          <span className="mt-2 line-clamp-2 block max-w-4xl text-xs leading-relaxed text-foreground-muted">
                            {preview}
                          </span>
                        )}
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
                  aria-label="Show more recent updates"
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

      <QuickstartDialog
        open={quickstartOpen}
        onOpenChange={setQuickstartOpen}
        onTokenCreated={loadPATs}
        mcpOauthEnabled={oauthEnabled}
      />
    </div>
  );
}

function collectionFromPath(path: string): string {
  const parts = path.split("/").filter(Boolean);
  return parts.length > 1 ? parts.slice(0, -1).join(" / ") : "Vault root";
}

function ContinueWorkingSection({ items }: { items: RecentDocumentView[] }) {
  return (
    <section aria-labelledby="continue-working-heading">
      <header className="flex min-h-10 flex-wrap items-center justify-between gap-3 border-b border-border pb-2.5">
        <div className="flex min-w-0 items-center gap-2.5">
          <TonalIcon tone="knowledge" size="sm">
            <Clock3 aria-hidden />
          </TonalIcon>
          <h2 id="continue-working-heading" className="text-base font-semibold tracking-tight">
            Continue working
          </h2>
          <Badge variant="default" className="tabular-nums">{items.length}</Badge>
        </div>
        <span className="text-xs text-foreground-muted">Recent on this browser</span>
      </header>

      <ul className="mt-3 grid gap-2 sm:grid-cols-2 lg:grid-cols-4">
        {items.map((item) => (
          <li key={`${item.vault}:${item.path}`} className="min-w-0">
            <Panel className="h-full">
              <Link
                to={`/vault/${encodeURIComponent(item.vault)}/doc/${encodeURIComponent(item.path)}`}
                className="group grid min-h-20 grid-cols-[1.75rem_minmax(0,1fr)] items-center gap-2.5 px-3 py-3 transition-token hover:bg-surface-hover focus:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-inset"
              >
                <span className="inline-flex h-7 w-7 shrink-0 items-center justify-center rounded-[var(--radius-sm)] border border-border bg-surface-2 text-link" aria-hidden>
                  <FileClock className="h-3.5 w-3.5" aria-hidden />
                </span>
                <span className="min-w-0">
                  <span className="flex min-w-0 items-center gap-2">
                    <span className="min-w-0 flex-1 truncate text-sm font-semibold tracking-tight text-foreground transition-colors group-hover:text-link">
                      {item.title}
                    </span>
                    <ArrowUpRight className="h-3.5 w-3.5 shrink-0 text-subtle transition-token group-hover:text-link" aria-hidden />
                  </span>
                  <span className="mt-1 flex min-w-0 items-center gap-1.5 text-xs text-foreground-muted">
                    <TooltipText className="max-w-24 shrink-0 truncate font-medium text-link">
                      {item.vault}
                    </TooltipText>
                    <ChevronRight className="h-3 w-3 shrink-0 text-subtle" aria-hidden />
                    <span className="min-w-0 flex-1 truncate" title={collectionFromPath(item.path)}>
                      {collectionFromPath(item.path)}
                    </span>
                    <span aria-hidden>·</span>
                    <span className="sr-only">Viewed </span>
                    <RelativeTime iso={item.viewedAt} className="shrink-0" />
                  </span>
                </span>
              </Link>
            </Panel>
          </li>
        ))}
      </ul>
    </section>
  );
}

function recentActionLabel(action?: string): string {
  if (action === "create" || action === "created") return "created this document";
  if (action === "move" || action === "moved") return "moved this document";
  return "updated this document";
}

function HomeWorkspaceHeader({
  vaultCount,
  loading,
  indexedCount,
  indexingPending,
  indexingAbandoned,
}: {
  vaultCount: number;
  loading: boolean;
  indexedCount: number | null;
  indexingPending: number;
  indexingAbandoned: number;
}) {
  const indexLabel = indexingAbandoned > 0
    ? `${indexingAbandoned.toLocaleString()} need attention`
    : indexingPending > 0
      ? `${indexingPending.toLocaleString()} indexing`
      : indexedCount !== null
        ? `${indexedCount.toLocaleString()} indexed`
        : null;
  const indexTone = indexingAbandoned > 0
    ? "bg-warning"
    : indexingPending > 0
      ? "bg-info"
      : "bg-success";

  return (
    <header
      className="border-b border-border pb-5 pt-2"
      aria-labelledby="home-workspace-heading"
    >
      <div className="flex flex-col gap-5 sm:flex-row sm:items-end sm:justify-between">
        <div className="min-w-0">
          <h1
            id="home-workspace-heading"
            className="text-2xl font-semibold tracking-tight text-foreground sm:text-3xl"
          >
            Your <span className="brand-gradient">workspace</span>
          </h1>
          <p className="mt-1.5 max-w-2xl text-sm leading-relaxed text-foreground-muted">
            Open a Vault, catch up on recent changes, or complete the remaining setup.
          </p>
        </div>

        <dl
          className="flex min-h-10 flex-wrap items-end gap-x-4 gap-y-2 text-sm sm:justify-end"
          role="status"
          aria-live="polite"
        >
          <div className="min-w-20">
            <dt className="text-xs text-foreground-muted">Vault access</dt>
            <dd className="mt-0.5 font-semibold tabular-nums text-foreground">
              {loading ? "Loading…" : `${vaultCount.toLocaleString()} available`}
            </dd>
          </div>
          {indexLabel && (
            <div className="min-w-28 border-l border-border pl-4">
              <dt className="text-xs text-foreground-muted">Knowledge index</dt>
              <dd className="mt-0.5 flex items-center gap-2 font-semibold tabular-nums text-foreground">
                <span className={`h-1.5 w-1.5 shrink-0 rounded-full ${indexTone}`} aria-hidden />
                {indexLabel}
              </dd>
            </div>
          )}
        </dl>
      </div>
    </header>
  );
}

function HomeAgentConnectPanel({
  completed,
  tokenReady,
  tokenCount,
  workspaceReady,
  onConnect,
  onDismiss,
}: {
  completed: number;
  tokenReady: boolean;
  tokenCount: number;
  workspaceReady: boolean;
  onConnect: () => void;
  onDismiss: () => void;
}) {
  return (
    <Panel variant="workspace" aria-labelledby="home-agent-connect-heading">
      <div className="flex items-start justify-between gap-3 border-b border-border bg-surface-2/60 px-4 py-3">
        <div className="flex min-w-0 items-center gap-2.5">
          <TonalIcon tone="info" size="sm">
            <PlugZap aria-hidden />
          </TonalIcon>
          <div className="min-w-0">
            <h2 id="home-agent-connect-heading" className="text-sm font-semibold text-foreground">
              Connect an agent
            </h2>
            <p className="mt-0.5 text-xs text-foreground-muted">
              {completed} of 3 complete
            </p>
          </div>
        </div>
        <div className="flex shrink-0 items-center gap-1">
          <Badge variant="pending">needs setup</Badge>
          <button
            type="button"
            onClick={onDismiss}
            className="min-h-8 cursor-pointer rounded-[var(--radius-sm)] px-2 text-xs text-foreground-muted transition-token hover:bg-surface-hover hover:text-foreground focus:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2 focus-visible:ring-offset-surface"
          >
            Hide
          </button>
        </div>
      </div>

      <div className="p-4">
        <p className="text-sm leading-relaxed text-foreground">
          {tokenReady
            ? `${tokenCount.toLocaleString()} access token${tokenCount === 1 ? " is" : "s are"} ready. Add AKB to an agent and use it once to complete setup.`
            : "Create access, choose your coding agent, and copy a ready-to-use connection command."}
        </p>
        <p className="mt-1.5 text-xs leading-relaxed text-foreground-muted">
          {workspaceReady
            ? "Your Vaults and knowledge are ready. The connection becomes active after the agent reaches AKB once."
            : "You can connect now and add Vaults or knowledge alongside the rest of setup."}
        </p>
        <div className="mt-4 flex flex-wrap items-center gap-3">
          {tokenReady ? (
            <Button asChild variant="accent" size="sm">
              <Link to="/settings?tab=tokens">
                Finish connection
                <ArrowRight className="h-3.5 w-3.5" aria-hidden />
              </Link>
            </Button>
          ) : (
            <Button variant="accent" size="sm" onClick={onConnect}>
              <PlugZap className="h-3.5 w-3.5" aria-hidden />
              Connect agent
            </Button>
          )}
          <Link
            to="/settings?tab=tokens"
            className="inline-flex min-h-9 items-center rounded-[var(--radius-sm)] text-xs text-link transition-token hover:text-link-hover focus:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2 focus-visible:ring-offset-surface"
          >
            Manage connections
          </Link>
        </div>
      </div>
    </Panel>
  );
}

function HomeSetupPanel({
  loading,
  completed,
  hasVault,
  hasKnowledge,
  hasConnectedAgent,
  tokenReady,
  writableVault,
  onConnect,
  onDismiss,
}: {
  loading: boolean;
  completed: number;
  hasVault: boolean;
  hasKnowledge: boolean;
  hasConnectedAgent: boolean;
  tokenReady: boolean;
  writableVault?: VaultRow;
  onConnect: () => void;
  onDismiss: () => void;
}) {
  const nextStep = !hasVault ? "vault" : !hasKnowledge ? "knowledge" : "agent";

  return (
    <Panel variant="workspace" aria-labelledby="home-setup-heading" aria-busy={loading}>
      <div className="border-b border-border bg-surface-2/60 px-4 py-3">
        <div className="flex items-start justify-between gap-3">
          <div className="flex min-w-0 items-center gap-2.5">
            <TonalIcon tone="info" size="sm">
              <PlugZap aria-hidden />
            </TonalIcon>
            <div>
              <h2 id="home-setup-heading" className="text-sm font-semibold text-foreground">
                Finish setup
              </h2>
              <p className="mt-0.5 text-xs text-foreground-muted">
                {loading ? "Checking workspace…" : `${completed} of 3 complete`}
              </p>
            </div>
          </div>
          <button
            type="button"
            onClick={onDismiss}
            className="min-h-8 shrink-0 cursor-pointer rounded-[var(--radius-sm)] px-2 text-xs text-foreground-muted transition-token hover:bg-surface-hover hover:text-foreground focus:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2 focus-visible:ring-offset-surface"
          >
            Hide
          </button>
        </div>
        <div
          className="mt-3 h-1.5 overflow-hidden rounded-full bg-surface"
          role="progressbar"
          aria-label="Workspace setup progress"
          aria-valuemin={0}
          aria-valuemax={3}
          aria-valuenow={loading ? undefined : completed}
        >
          <span
            className="block h-full rounded-full bg-primary transition-token"
            style={{ width: `${loading ? 0 : (completed / 3) * 100}%` }}
          />
        </div>
      </div>

      {loading ? (
        <div className="space-y-3 p-4" aria-hidden>
          {Array.from({ length: 3 }).map((_, index) => (
            <div key={index} className="flex items-center gap-3">
              <span className="h-7 w-7 rounded-[var(--radius-sm)] bg-surface-muted" />
              <span className="h-3 flex-1 rounded bg-surface-muted" />
            </div>
          ))}
        </div>
      ) : (
        <ol className="divide-y divide-border">
          <SetupRow
            title="Open a vault"
            description="Choose a knowledge space to work in."
            complete={hasVault}
            current={nextStep === "vault"}
            action={
              nextStep === "vault" ? (
                <Button asChild variant="accent" size="sm">
                  <Link to="/vault">Open Vaults</Link>
                </Button>
              ) : undefined
            }
          />
          <SetupRow
            title="Add knowledge"
            description="Add a document, table, file, or bundle."
            complete={hasKnowledge}
            current={nextStep === "knowledge"}
            action={
              nextStep === "knowledge" ? (
                <Button asChild variant="accent" size="sm">
                  <Link to={writableVault ? `/vault/${writableVault.name}` : "/vault"}>
                    {writableVault ? "Add knowledge" : "Open Vaults"}
                  </Link>
                </Button>
              ) : undefined
            }
          />
          <SetupRow
            title="Connect an agent"
            description={
              tokenReady
                ? "Your token is ready; use it from an agent once."
                : "Create access and add AKB to your coding agent."
            }
            complete={hasConnectedAgent}
            current={nextStep === "agent"}
            action={
              nextStep === "agent" ? (
                tokenReady ? (
                  <Button asChild variant="accent" size="sm">
                    <Link to="/settings?tab=tokens">Finish connection</Link>
                  </Button>
                ) : (
                  <Button variant="accent" size="sm" onClick={onConnect}>
                    Connect agent
                  </Button>
                )
              ) : undefined
            }
          />
        </ol>
      )}
    </Panel>
  );
}

function SetupRow({
  title,
  description,
  complete,
  current,
  action,
}: {
  title: string;
  description: string;
  complete: boolean;
  current: boolean;
  action?: ReactNode;
}) {
  return (
    <li
      className="grid grid-cols-[auto_minmax(0,1fr)] items-start gap-3 px-4 py-3"
      aria-current={current ? "step" : undefined}
    >
      <TonalIcon tone={complete ? "success" : current ? "info" : "neutral"} size="sm">
        {complete ? <Check aria-hidden /> : <Circle aria-hidden />}
      </TonalIcon>
      <div className="min-w-0">
        <div className="flex min-h-7 flex-wrap items-center justify-between gap-2">
          <span className="text-sm font-medium text-foreground">{title}</span>
          {complete && <Badge variant="success">Complete</Badge>}
        </div>
        <p className="mt-0.5 text-xs leading-relaxed text-foreground-muted">{description}</p>
        {action && <div className="mt-3">{action}</div>}
      </div>
    </li>
  );
}

function WorkspaceSummary({
  loading,
  vaultCount,
  ownedCount,
  editableCount,
  readOnlyCount,
  favoriteCount,
  archivedCount,
  tokenCount,
  hasConnectedAgent,
  showAgentStatus,
  indexedCount,
  indexingPending,
  indexingAbandoned,
  showSetupLink,
  onShowSetup,
}: {
  loading: boolean;
  vaultCount: number;
  ownedCount: number;
  editableCount: number;
  readOnlyCount: number;
  favoriteCount: number;
  archivedCount: number;
  tokenCount: number;
  hasConnectedAgent: boolean;
  showAgentStatus: boolean;
  indexedCount: number | null;
  indexingPending: number;
  indexingAbandoned: number;
  showSetupLink: boolean;
  onShowSetup: () => void;
}) {
  const value = (count: number) => loading ? "—" : count.toLocaleString();
  const indexText = indexingAbandoned > 0
    ? `${indexingAbandoned.toLocaleString()} items need attention`
    : indexingPending > 0
      ? `${indexingPending.toLocaleString()} items are indexing`
      : indexedCount !== null
        ? `${indexedCount.toLocaleString()} chunks indexed`
        : "Index status unavailable";
  const agentText = hasConnectedAgent
    ? "Agent connection active"
    : tokenCount > 0
      ? `${tokenCount.toLocaleString()} token${tokenCount === 1 ? "" : "s"} ready`
      : "No agent connection yet";

  return (
    <Panel variant="workspace" aria-labelledby="workspace-summary-heading" aria-busy={loading}>
      <div className="flex items-center justify-between gap-3 border-b border-border bg-surface-2/60 px-4 py-3">
        <div className="flex min-w-0 items-center gap-2.5">
          <TonalIcon tone="data" size="sm">
            <Database aria-hidden />
          </TonalIcon>
          <div>
            <h2 id="workspace-summary-heading" className="text-sm font-semibold text-foreground">
              Workspace
            </h2>
            <p className="mt-0.5 text-xs text-foreground-muted">Access and connection status</p>
          </div>
        </div>
        <Badge variant="default" className="tabular-nums">{value(vaultCount)} total</Badge>
      </div>

      <dl className="grid grid-cols-3 divide-x divide-border border-b border-border">
        <CompactStat label="Owned" value={value(ownedCount)} />
        <CompactStat label="Shared edit" value={value(editableCount)} />
        <CompactStat label="Read only" value={value(readOnlyCount)} />
      </dl>

      <div className="divide-y divide-border">
        <StatusLine
          icon={<Database aria-hidden />}
          tone={indexingAbandoned > 0 ? "warning" : indexingPending > 0 ? "info" : "success"}
          label="Knowledge index"
          value={indexText}
        />
        {showAgentStatus && (
          <StatusLine
            icon={<PlugZap aria-hidden />}
            tone={hasConnectedAgent ? "success" : tokenCount > 0 ? "info" : "neutral"}
            label="Agent access"
            value={agentText}
          />
        )}
      </div>

      <div className="flex flex-wrap items-center justify-between gap-3 border-t border-border px-4 py-2.5 text-xs">
        <span className="text-foreground-muted">
          {favoriteCount.toLocaleString()} favorites
          {archivedCount > 0 ? ` · ${archivedCount.toLocaleString()} archived` : ""}
        </span>
        <div className="flex items-center gap-3">
          {showSetupLink && (
            <button
              type="button"
              onClick={onShowSetup}
              className="cursor-pointer rounded-[var(--radius-sm)] text-link transition-token hover:text-link-hover focus:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2 focus-visible:ring-offset-surface"
            >
              Show setup
            </button>
          )}
          <Link
            to="/vault"
            className="rounded-[var(--radius-sm)] text-link transition-token hover:text-link-hover focus:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2 focus-visible:ring-offset-surface"
          >
            Open Vaults
          </Link>
          <Link
            to="/settings?tab=tokens"
            className="rounded-[var(--radius-sm)] text-link transition-token hover:text-link-hover focus:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2 focus-visible:ring-offset-surface"
          >
            Connections
          </Link>
        </div>
      </div>
    </Panel>
  );
}

function CompactStat({ label, value }: { label: string; value: string }) {
  return (
    <div className="min-w-0 px-3 py-3 text-center">
      <dt className="truncate text-xs text-foreground-muted" title={label}>{label}</dt>
      <dd className="mt-1 text-base font-semibold tabular-nums text-foreground">{value}</dd>
    </div>
  );
}

function StatusLine({
  icon,
  tone,
  label,
  value,
}: {
  icon: ReactNode;
  tone: "neutral" | "info" | "success" | "warning";
  label: string;
  value: string;
}) {
  return (
    <div className="flex items-center gap-3 px-4 py-3">
      <TonalIcon tone={tone} size="sm">{icon}</TonalIcon>
      <div className="min-w-0">
        <div className="text-xs font-medium text-foreground">{label}</div>
        <div className="mt-0.5 truncate text-xs text-foreground-muted" title={value}>{value}</div>
      </div>
    </div>
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
        className="group flex h-full min-h-32 flex-col p-3 pr-11 focus:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-inset"
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

        <p className="mt-2 line-clamp-1 text-xs leading-relaxed text-foreground-muted">
          {vault.description || "A shared knowledge space for your team and connected agents."}
        </p>

        <dl className="mt-auto flex items-center gap-3 border-t border-border pt-2">
          {contentMetrics ? (
            contentMetrics.map((metric) => {
              const Icon = recentIcon(metric.type);
              const tone = recentTone(metric.type);
              return (
                <div
                  key={metric.label}
                  className="flex min-w-0 items-center gap-1"
                  title={`${metric.label}: ${metric.value.toLocaleString()}`}
                >
                  <dt className="sr-only">{metric.shortLabel}</dt>
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
                  <dd className="text-xs font-semibold tabular-nums text-foreground">
                    {metric.value.toLocaleString()}
                  </dd>
                </div>
              );
            })
          ) : (
            Array.from({ length: 3 }).map((_, index) => (
              <div
                key={index}
                className="flex items-center gap-1"
                aria-hidden
              >
                <span className="block h-4 w-4 animate-pulse rounded bg-surface-muted" />
                <span className="block h-3 w-6 animate-pulse rounded bg-surface-muted" />
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
