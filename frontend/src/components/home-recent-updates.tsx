import { useCallback, useEffect, useRef, useState } from "react";
import { Link, useLocation } from "react-router-dom";
import { ArrowUpRight, FileClock } from "lucide-react";
import { ApiError, getRecent } from "@/lib/api";
import { documentPreviewState } from "@/lib/document-preview-navigation";
import { recentIcon } from "@/lib/recent";
import { useCurrentUser } from "@/contexts/current-user-context";
import { Panel } from "@/components/ui/panel";
import { Button } from "@/components/ui/button";
import { Alert } from "@/components/ui/alert";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { TonalIcon } from "@/components/ui/tonal-icon";
import { RelativeTime } from "@/components/ui/relative-time";
import { LoadingState } from "@/components/ui/loading-state";
import { EmptyState } from "@/components/empty-state";

type Scope = "all" | "watching";
interface RecentRow {
  resource_id?: string;
  doc_id: string;
  vault: string;
  path: string;
  title: string;
  type?: string;
  changed_at?: string;
  updated_by_name?: string;
  author_name?: string;
  created_by_name?: string;
  excerpt?: string;
  summary?: string;
}
const PAGE_SIZE = 6;
function preference(userId: string): Scope {
  try { return localStorage.getItem(`akb.homeRecentScope:${userId}`) === "watching" ? "watching" : "all"; }
  catch { return "all"; }
}
class WatchingUnavailable extends Error {}

/** Account-keyed boundary prevents a previous account's rows flashing on switch. */
export function HomeRecentUpdates() {
  const user = useCurrentUser();
  return <RecentUpdates key={user?.user_id ?? "anonymous"} userId={user?.user_id ?? ""} />;
}

function RecentUpdates({ userId }: { userId: string }) {
  const location = useLocation();
  const [scope, setScope] = useState<Scope>(() => preference(userId));
  const [rows, setRows] = useState<RecentRow[]>([]);
  const [busy, setBusy] = useState(true);
  const [moreBusy, setMoreBusy] = useState(false);
  const [error, setError] = useState(false);
  const [failedMore, setFailedMore] = useState(false);
  const [unsupported, setUnsupported] = useState(false);
  const [cursor, setCursor] = useState<string | null>(null);
  const [legacyLimit, setLegacyLimit] = useState(PAGE_SIZE);
  const [legacyMore, setLegacyMore] = useState(false);
  const generation = useRef(0);

  const load = useCallback(async (more = false, next?: string, limit = PAGE_SIZE) => {
    const request = ++generation.current;
    setError(false);
    if (more) setMoreBusy(true); else setBusy(true);
    try {
      const data = await getRecent(undefined, limit, { scope, cursor: next });
      if (request !== generation.current) return;
      if (scope === "watching" && data.scope !== "watching") throw new WatchingUnavailable();
      if (!Array.isArray(data.changes)) throw new Error("Invalid recent response");
      const page: RecentRow[] = data.changes;
      setRows(previous => {
        const combined = more && next ? [...previous, ...page] : page;
        return Array.from(new Map(combined.map(row => [row.resource_id ?? `${row.vault}:${row.doc_id}`, row])).values());
      });
      setUnsupported(false);
      setCursor(data.next_cursor ?? null);
      setLegacyLimit(limit);
      setLegacyMore(data.scope === undefined && scope === "all" && page.length >= limit && limit < 100);
    } catch (failure) {
      if (request !== generation.current) return;
      const code = failure instanceof ApiError && typeof failure.detail === "object" && failure.detail !== null
        ? (failure.detail as { code?: string }).code : undefined;
      if (scope === "watching" && (failure instanceof WatchingUnavailable ||
        ["notifications_disabled", "notifications_session_required"].includes(code ?? "") ||
        (failure instanceof ApiError && [404, 405, 501].includes(failure.status)))) {
        setUnsupported(true); setRows([]); setCursor(null); setLegacyMore(false);
      } else { setError(true); setFailedMore(more); }
    } finally {
      if (request === generation.current) { setBusy(false); setMoreBusy(false); }
    }
  }, [scope]);

  useEffect(() => {
    setRows([]); setUnsupported(false); setCursor(null); setLegacyMore(false);
    void load();
    const refresh = () => void load();
    window.addEventListener("akb:watch-changed", refresh);
    window.addEventListener("akb:document-status-changed", refresh);
    window.addEventListener("focus", refresh);
    return () => {
      generation.current++;
      window.removeEventListener("akb:watch-changed", refresh);
      window.removeEventListener("akb:document-status-changed", refresh);
      window.removeEventListener("focus", refresh);
    };
  }, [load]);

  function select(value: string) {
    const selected = value === "watching" ? "watching" : "all";
    generation.current++;
    setRows([]); setBusy(true); setError(false); setUnsupported(false); setMoreBusy(false);
    setScope(selected);
    try { localStorage.setItem(`akb.homeRecentScope:${userId}`, selected); } catch { /* Storage is optional. */ }
  }
  const loadMore = () => void load(true, cursor ?? undefined, legacyMore ? Math.min(legacyLimit * 2, 100) : PAGE_SIZE);
  return <>
    <header className="flex min-h-10 flex-wrap items-center justify-between gap-3 border-b border-border pb-2.5">
      <div className="flex items-center gap-2.5"><TonalIcon tone="neutral" size="sm"><FileClock aria-hidden /></TonalIcon>
        <h2 className="text-base font-semibold tracking-tight">Recent updates</h2></div>
      {scope === "watching" && <Link className="text-sm text-link hover:underline focus-visible:ring-2 focus-visible:ring-ring" to="/settings?tab=notifications">Manage watches</Link>}
    </header>
    <Tabs value={scope} onValueChange={select} activationMode="manual" className="mt-3">
      <TabsList aria-label="Recent updates scope" className="bg-transparent p-0">
        <TabsTrigger id="home-updates-all" value="all">All</TabsTrigger><TabsTrigger id="home-updates-watching" value="watching">Watching</TabsTrigger>
      </TabsList>
      <TabsContent value={scope} aria-labelledby={`home-updates-${scope}`} className="pt-3" aria-busy={busy || moreBusy}>
        {busy && rows.length === 0 ? <LoadingState label="Loading recent updates"><Panel>
          {Array.from({ length: 3 }, (_, index) => <div key={index} className="space-y-2 border-b border-border p-4 last:border-0"><div className="h-4 w-2/3 rounded bg-surface-2" /><div className="h-3 w-1/3 rounded bg-surface-2" /></div>)}
        </Panel></LoadingState> : unsupported ? <Alert variant="info">Watching is not supported on this server yet. You can still view All updates.</Alert>
          : error && rows.length === 0 ? <Alert><div>Could not load recent updates.<Button variant="link" size="sm" onClick={() => void load()}>Retry</Button></div></Alert>
          : rows.length === 0 ? <EmptyState title={scope === "watching" ? "No watched documents yet" : "Nothing updated yet"}
            description={scope === "watching" ? "Turn on Watch in a document to find its latest changes here." : "Document changes across your Vaults will appear here."} />
          : <Panel>
            <ol className="divide-y divide-border">{rows.map(row => {
              const Icon = recentIcon(row.type);
              const collection = row.path.includes("/") ? row.path.slice(0, row.path.lastIndexOf("/")) : null;
              const preview = row.excerpt?.trim() || row.summary?.trim();
              const id = `home-recent-${row.resource_id ?? row.doc_id}`;
              return <li key={row.resource_id ?? `${row.vault}:${row.doc_id}`}><Link id={id}
                to={`/vault/${encodeURIComponent(row.vault)}/doc/${encodeURIComponent(row.doc_id)}`}
                state={documentPreviewState(location, id, `home-updates-${scope}`)}
                className="home-activity-row group grid grid-cols-[20px_minmax(0,1fr)_auto] items-start gap-3 bg-surface px-4 py-3 transition-token hover:bg-surface-hover focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-inset focus-visible:ring-ring">
                <Icon className="mt-0.5 h-4 w-4 text-link" aria-hidden />
                <span className="min-w-0"><span className="block truncate text-sm font-semibold text-foreground group-hover:text-link" title={row.title}>{row.title}</span>
                  <span className="mt-1 block truncate text-xs text-foreground-muted">{row.vault}{collection ? ` / ${collection}` : ""}{row.updated_by_name || row.author_name ? ` · ${row.updated_by_name || row.author_name}` : row.created_by_name ? ` · Created by ${row.created_by_name}` : ""}</span>
                  {preview && <span className="mt-1 block truncate text-xs text-foreground-muted">{preview}</span>}
                </span>
                <span className="flex items-center gap-2"><RelativeTime iso={row.changed_at} /><ArrowUpRight className="h-3.5 w-3.5 text-link" aria-hidden /></span>
              </Link></li>;
            })}</ol>
            {error && <Alert className="m-3"><div>Could not load updates. Your current list is still available.<Button variant="link" size="sm" onClick={failedMore ? loadMore : () => void load()}>Retry</Button></div></Alert>}
            {(cursor || legacyMore) && !error && <div className="border-t border-border p-2"><Button variant="ghost" size="sm" className="w-full" loading={moreBusy || busy} onClick={loadMore}>Show more</Button></div>}
          </Panel>}
      </TabsContent>
    </Tabs>
  </>;
}
