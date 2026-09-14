import { useCallback, useEffect, useMemo, useRef, useState, type ReactNode } from "react";
import { Link, useLocation, useNavigate } from "react-router-dom";
import { ArrowRight, Box, FileText, FolderPlus, PlugZap, Plus, Star } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Panel } from "@/components/ui/panel";
import { Badge } from "@/components/ui/badge";
import { Alert } from "@/components/ui/alert";
import { LoadingState } from "@/components/ui/loading-state";
import { RelativeTime } from "@/components/ui/relative-time";
import { QuickstartDialog } from "@/components/quickstart-dialog";
import { VaultCreateDialog } from "@/components/vault-create-dialog";
import { HomeRecentUpdates } from "@/components/home-recent-updates";
import type { VaultRow } from "@/components/vault-list";
import { useVaultFavorites } from "@/hooks/use-vault-favorites";
import { useCurrentUser } from "@/contexts/current-user-context";
import { listVaults, getVaultInfo, listPATs, getAuthConfig } from "@/lib/api";
import { readRecentDocumentViews, type RecentDocumentView } from "@/lib/recent-document-views";

const PREVIEW_LIMIT = 4;
const GUIDE_KEY = "akb.homeConnectionGuideDismissed";
const focus = "focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2 focus-visible:ring-offset-background";
const cardGrid = "grid grid-cols-1 gap-3 sm:grid-cols-2 xl:grid-cols-4";

interface VaultMetrics {
  document_count?: number;
  table_count?: number;
  file_count?: number;
}

function guideHidden(userId: string) {
  try { return localStorage.getItem(`${GUIDE_KEY}:${userId}`) === "1"; }
  catch { return false; }
}

export default function HomePage() {
  const user = useCurrentUser();
  // Personal history and pending requests must never survive an account switch.
  return <HomeWorkspace key={user?.user_id ?? "anonymous"} userId={user?.user_id ?? ""} />;
}

function HomeWorkspace({ userId }: { userId: string }) {
  const location = useLocation();
  const navigate = useNavigate();
  const [vaults, setVaults] = useState<VaultRow[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(false);
  const [metrics, setMetrics] = useState<Record<string, VaultMetrics>>({});
  const [metricsDone, setMetricsDone] = useState<Set<string>>(new Set());
  const [recentViews, setRecentViews] = useState<RecentDocumentView[]>([]);
  const { isFavorite, toggleFavorite, favOrder } = useVaultFavorites();
  const [connection, setConnection] = useState<"loading" | "unused" | "used" | "unknown">("loading");
  const [oauthEnabled, setOauthEnabled] = useState(false);
  const [guideDismissed, setGuideDismissed] = useState(() => guideHidden(userId));
  const [quickstartOpen, setQuickstartOpen] = useState(false);
  const [createOpen, setCreateOpen] = useState(false);
  const createTrigger = useRef<HTMLButtonElement | null>(null);
  const generation = useRef(0);

  const loadVaults = useCallback(async () => {
    const request = ++generation.current;
    setLoading(true); setError(false);
    try {
      const data = await listVaults();
      if (request === generation.current) setVaults(data.vaults || []);
    } catch {
      if (request === generation.current) setError(true);
    } finally {
      if (request === generation.current) setLoading(false);
    }
  }, []);

  useEffect(() => {
    const requests = generation;
    void loadVaults();
    return () => { requests.current++; };
  }, [loadVaults]);

  useEffect(() => {
    let cancelled = false;
    // Token presence suppresses the optional nudge; it is not agent-health proof.
    listPATs().then(data => {
      if (cancelled) return;
      const tokens = data.tokens || [];
      setConnection(tokens.length > 0 ? "used" : "unused");
    }).catch(() => { if (!cancelled) setConnection("unknown"); });
    getAuthConfig().then(config => {
      if (!cancelled) setOauthEnabled(config.available && config.mcp_oauth.enabled);
    }).catch(() => { /* Optional: token connection remains available. */ });
    return () => { cancelled = true; };
  }, []);

  useEffect(() => {
    setRecentViews(readRecentDocumentViews(userId, 12));
  }, [userId, location.key]);

  const previewVaults = useMemo(() => {
    const favorites = vaults.filter(vault => isFavorite(vault.id))
      .sort((a, b) => favOrder(a.id) - favOrder(b.id));
    const others = vaults.filter(vault => !isFavorite(vault.id))
      .sort((a, b) => Number(a.status === "archived") - Number(b.status === "archived"));
    return [...favorites, ...others].slice(0, PREVIEW_LIMIT);
  }, [vaults, isFavorite, favOrder]);

  useEffect(() => {
    let cancelled = false;
    // At most four cards are enriched; favorites cannot expand Home into a directory.
    const missing = previewVaults.filter(vault => !metricsDone.has(vault.name));
    if (!missing.length) return;
    void Promise.all(missing.map(async vault => {
      try {
        const info = await getVaultInfo(vault.name);
        return { name: vault.name, value: { document_count: info?.document_count, table_count: info?.table_count, file_count: info?.file_count } };
      } catch { return { name: vault.name, value: {} }; }
    })).then(results => {
      if (cancelled) return;
      setMetrics(current => ({ ...current, ...Object.fromEntries(results.map(item => [item.name, item.value])) }));
      setMetricsDone(current => new Set([...current, ...results.map(item => item.name)]));
    });
    return () => { cancelled = true; };
  }, [previewVaults, metricsDone]);

  const recentlyViewed = useMemo(() => {
    if (loading || error) return [];
    const accessible = new Set(vaults.map(vault => vault.name));
    return recentViews.filter(view => accessible.has(view.vault)).slice(0, PREVIEW_LIMIT);
  }, [recentViews, vaults, loading, error]);

  useEffect(() => {
    const target = location.hash.slice(1);
    if (!["vaults", "recent"].includes(target)) return;
    const frame = requestAnimationFrame(() => {
      document.getElementById(target)?.scrollIntoView({
        behavior: window.matchMedia("(prefers-reduced-motion: reduce)").matches ? "auto" : "smooth",
        block: "start",
      });
    });
    return () => cancelAnimationFrame(frame);
  }, [location.hash, location.key]);

  function dismissGuide(hidden: boolean) {
    setGuideDismissed(hidden);
    try {
      if (hidden) localStorage.setItem(`${GUIDE_KEY}:${userId}`, "1");
      else localStorage.removeItem(`${GUIDE_KEY}:${userId}`);
    } catch { /* The guide remains usable when browser storage is blocked. */ }
    requestAnimationFrame(() => document.getElementById("home-show-guide")?.focus());
  }

  function toggleVaultFavorite(vault: VaultRow) {
    toggleFavorite(vault.id);
    requestAnimationFrame(() => {
      (document.getElementById(`home-favorite-${vault.id}`) ?? document.getElementById("home-vaults-link"))?.focus();
    });
  }

  const showGuide = connection === "unused" && !guideDismissed;
  const noVaults = !loading && !error && vaults.length === 0;
  const connectionControl = !showGuide && <Button id="home-show-guide" variant="ghost" size="sm" aria-expanded={connection === "unused" ? false : undefined} onClick={() => {
    if (connection === "unused") dismissGuide(false);
    else setQuickstartOpen(true);
  }}><PlugZap className="h-4 w-4" aria-hidden />{connection === "unused" ? "Show connection guide" : "Connect an agent"}</Button>;

  return <><h1 className="sr-only">Home</h1><div className="w-full space-y-7 lg:space-y-8">
      {showGuide && <div id="home-connection-guide" className="flex flex-wrap items-center justify-between gap-x-6 gap-y-2 border-y border-border py-3 text-sm">
        <p className="text-foreground-muted">Use your vaults in an AI tool. Optional — you can keep working here without a connection.</p>
        <div className="flex shrink-0 items-center gap-3">
          <Button variant="link" size="sm" className="px-0" onClick={() => setQuickstartOpen(true)}>Set up a connection<ArrowRight className="h-4 w-4" aria-hidden /></Button>
          <Button id="home-show-guide" variant="ghost" size="sm" aria-expanded={true} aria-controls="home-connection-guide" onClick={() => dismissGuide(true)}>Hide connection guide</Button>
        </div>
      </div>}

    {recentlyViewed.length > 0 && <section aria-labelledby="home-viewed-heading">
      <SectionHeader id="home-viewed-heading" title="Recently viewed" accessory={<div className="flex flex-wrap items-center gap-3"><span className="text-xs text-foreground-muted">On this browser</span>{connectionControl}</div>} />
      <ul className={recentlyViewed.length === 1 ? "grid grid-cols-1" : cardGrid}>
        {recentlyViewed.map(item => <li key={`${item.vault}:${item.path}`} className="min-w-0"><RecentDocumentCard item={item} compact={recentlyViewed.length === 1} /></li>)}
      </ul>
    </section>}

    <section id="vaults" className="scroll-mt-24" aria-labelledby="home-vaults-heading" aria-busy={loading}>
      <SectionHeader id="home-vaults-heading" title="Your vaults" accessory={<div className="flex flex-wrap items-center gap-3">{recentlyViewed.length === 0 && connectionControl}<Link id="home-vaults-link" to="/vault" className={`inline-flex min-h-9 items-center gap-2 rounded-[var(--radius-sm)] text-sm text-link hover:text-link-hover ${focus}`}>
        View all vaults{!loading && !error && vaults.length > 0 ? ` (${vaults.length})` : ""}<ArrowRight className="h-4 w-4" aria-hidden />
      </Link></div>} />
      {loading && vaults.length === 0 ? <LoadingState label="Loading your vaults"><div className={cardGrid}>
        {Array.from({ length: 4 }, (_, i) => <Panel key={i} className="min-h-36 space-y-4 p-4"><div className="h-5 w-2/3 rounded bg-surface-2" /><div className="h-9 rounded bg-surface-2" /><div className="h-4 w-1/2 rounded bg-surface-2" /></Panel>)}
      </div></LoadingState> : error && vaults.length === 0 ? <Alert><p>Could not load your vaults.</p><Button variant="link" onClick={loadVaults}>Retry</Button></Alert> : noVaults ? <Panel className="p-6 sm:p-8">
        <FolderPlus className="mb-4 h-6 w-6 text-link" aria-hidden />
        <h3 className="text-lg font-semibold">Create your first vault</h3>
        <p className="mt-2 max-w-xl text-sm leading-relaxed text-foreground-muted">A vault keeps related documents, tables, and files together. You can invite your team after creating it.</p>
        <Button ref={createTrigger} variant="accent" className="mt-5" onClick={() => setCreateOpen(true)}><Plus className="h-4 w-4" aria-hidden />Create a vault</Button>
        <p className="mt-4 text-sm text-foreground-muted">Joining an existing team? Ask a vault owner to invite you.</p>
      </Panel> : <>
        {error && <Alert className="mb-4">Could not refresh your vaults. <Button variant="link" onClick={loadVaults}>Retry</Button></Alert>}
        <ul className={cardGrid}>{previewVaults.map(vault => <li key={vault.id} className="min-w-0">
          <HomeVaultCard vault={vault} metrics={metrics[vault.name]} metricsReady={metricsDone.has(vault.name)} favorite={isFavorite(vault.id)} onToggleFavorite={() => toggleVaultFavorite(vault)} />
        </li>)}</ul>
      </>}
    </section>

    <div className="grid items-start gap-8 xl:grid-cols-2 2xl:gap-10">
      <section id="recent" className="min-w-0 scroll-mt-24" aria-label="Recent document updates"><HomeRecentUpdates scope="all" /></section>
      <section className="min-w-0" aria-label="Watched document updates"><HomeRecentUpdates scope="watching" /></section>
    </div>

    <QuickstartDialog open={quickstartOpen} onOpenChange={setQuickstartOpen} onTokenCreated={() => setConnection("used")} mcpOauthEnabled={oauthEnabled} />
    <VaultCreateDialog open={createOpen} onOpenChange={setCreateOpen} returnFocusRef={createTrigger}
      onCreated={name => navigate(`/vault/${encodeURIComponent(name)}`)} onOpenExisting={name => navigate(`/vault/${encodeURIComponent(name)}`)} />
  </div></>;
}

function SectionHeader({ id, title, accessory }: { id: string; title: string; accessory?: ReactNode }) {
  return <header className="mb-3 flex min-h-9 flex-wrap items-center justify-between gap-x-4 gap-y-1 border-b border-border pb-2">
    <h2 id={id} tabIndex={-1} className={`rounded-[var(--radius-sm)] text-base font-semibold tracking-tight ${focus}`}>{title}</h2>{accessory}
  </header>;
}

function RecentDocumentCard({ item, compact = false }: { item: RecentDocumentView; compact?: boolean }) {
  const parts = item.path.split("/").filter(Boolean);
  const collection = parts.length > 1 ? parts.slice(0, -1).join(" / ") : "";
  return <Panel className="h-full">
    <Link to={`/vault/${encodeURIComponent(item.vault)}/doc/${encodeURIComponent(item.path)}`} className={`group flex h-full gap-3 p-4 transition-token hover:bg-surface-hover ${focus} focus-visible:ring-inset`}>
      <FileText className="mt-0.5 h-4 w-4 shrink-0 text-link" aria-hidden />
      <div className={compact ? "min-w-0 flex-1 sm:flex sm:items-center sm:justify-between sm:gap-6" : "min-w-0 flex-1"}>
        <div className="min-w-0">
        <h3 className="line-clamp-2 break-words text-sm font-semibold leading-snug group-hover:text-link" title={item.title}>{item.title}</h3>
        <p className="mt-2 truncate text-xs text-foreground-muted" title={`${item.vault}${collection ? ` / ${collection}` : ""}`}>{item.vault}{collection ? ` / ${collection}` : ""}</p>
        </div>
        <p className={`text-xs text-subtle ${compact ? "mt-2 shrink-0 sm:mt-0" : "mt-1"}`}>Viewed <RelativeTime iso={item.viewedAt} /></p>
      </div>
    </Link>
  </Panel>;
}

function HomeVaultCard({ vault, metrics, metricsReady, favorite, onToggleFavorite }: {
  vault: VaultRow; metrics?: VaultMetrics; metricsReady: boolean; favorite: boolean; onToggleFavorite: () => void;
}) {
  const counts = [
    { label: "Documents", value: metrics?.document_count },
    { label: "Tables", value: metrics?.table_count },
    { label: "Files", value: metrics?.file_count },
  ].filter((item): item is { label: string; value: number } => typeof item.value === "number" && Number.isFinite(item.value) && item.value >= 0);
  return <Panel inset={false} className="relative h-full overflow-hidden">
    <Link to={`/vault/${encodeURIComponent(vault.name)}`} className={`group flex h-full flex-col p-4 transition-token hover:bg-surface-hover ${focus} focus-visible:ring-inset`}>
      <div className="flex items-start gap-2 pr-7"><Box className="mt-0.5 h-4 w-4 shrink-0 text-link" aria-hidden /><h3 className="line-clamp-2 break-words text-base font-semibold leading-snug group-hover:text-link" title={vault.name}>{vault.name}</h3></div>
      {vault.description?.trim() && <p className="mt-2 line-clamp-2 text-sm leading-relaxed text-foreground-muted" title={vault.description}>{vault.description}</p>}
      {(vault.role === "reader" || vault.status === "archived") && <div className="mt-2 flex flex-wrap gap-2">{vault.status === "archived" && <Badge variant="archived">Archived</Badge>}{vault.role === "reader" && <Badge>Read only</Badge>}</div>}
      <div className="mt-auto pt-4">
        {counts.length > 0 ? <dl className="flex flex-wrap gap-x-3 gap-y-1 text-xs text-foreground-muted">{counts.map(item => <div key={item.label} className="flex items-baseline gap-1"><dt>{item.label}</dt><dd className="font-medium tabular-nums text-foreground">{item.value.toLocaleString()}</dd></div>)}</dl> : !metricsReady ? <span className="text-xs text-subtle">Loading details…</span> : <span className="inline-flex items-center gap-1 text-sm text-link">Open vault<ArrowRight className="h-3.5 w-3.5" aria-hidden /></span>}
      </div>
    </Link>
    <Button id={`home-favorite-${vault.id}`} variant="ghost" size="icon" aria-label={`${favorite ? "Remove" : "Add"} ${vault.name} ${favorite ? "from" : "to"} favorites`} aria-pressed={favorite} onClick={onToggleFavorite} className="absolute right-2 top-2 h-9 w-9">
      <Star className={`h-4 w-4 ${favorite ? "fill-current text-link" : "text-foreground-muted"}`} aria-hidden />
    </Button>
  </Panel>;
}
