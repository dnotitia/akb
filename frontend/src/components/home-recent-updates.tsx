import { useCallback, useEffect, useRef, useState } from "react";
import { Link, useLocation } from "react-router-dom";
import { ArrowUpRight } from "lucide-react";
import { ApiError, getRecent } from "@/lib/api";
import { documentPreviewState } from "@/lib/document-preview-navigation";
import { recentIcon } from "@/lib/recent";
import { useCurrentUser } from "@/contexts/current-user-context";
import { Panel } from "@/components/ui/panel";
import { Button } from "@/components/ui/button";
import { Alert } from "@/components/ui/alert";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
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
export function HomeRecentUpdates({ scope }: { scope?: Scope } = {}) {
  const user = useCurrentUser();
  return <RecentUpdates key={`${user?.user_id ?? "anonymous"}:${scope ?? "tabs"}`} userId={user?.user_id ?? ""} fixedScope={scope} />;
}

function RecentUpdates({ userId, fixedScope }: { userId: string; fixedScope?: Scope }) {
  const location = useLocation();
  const [scope, setScope] = useState<Scope>(() => fixedScope ?? preference(userId));
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
      <h2 id={`home-updates-heading-${scope}`} tabIndex={-1} className="text-base font-semibold tracking-tight">{fixedScope === "watching" ? "Watched documents" : "Recent updates"}</h2>
      {scope === "watching" && <Link className="text-sm text-link hover:underline focus-visible:ring-2 focus-visible:ring-ring" to="/settings?tab=notifications">Manage watches</Link>}
    </header>
    <Tabs value={scope} onValueChange={select} activationMode="manual" className="mt-3">
      {!fixedScope && <TabsList aria-label="Recent updates scope" className="bg-transparent p-0">
        <TabsTrigger id="home-updates-all" value="all">All documents</TabsTrigger><TabsTrigger id="home-updates-watching" value="watching">Watched documents</TabsTrigger>
      </TabsList>}
      <TabsContent value={scope} role={fixedScope ? "region" : "tabpanel"} aria-labelledby={fixedScope ? `home-updates-heading-${scope}` : `home-updates-${scope}`} className={fixedScope ? "pt-0" : "pt-3"} aria-busy={busy || moreBusy}>
        {busy && rows.length === 0 ? <LoadingState label="Loading recent updates"><Panel>
          {Array.from({ length: 3 }, (_, index) => <div key={index} className="space-y-2 border-b border-border p-4 last:border-0"><div className="h-4 w-2/3 rounded bg-surface-2" /><div className="h-3 w-1/3 rounded bg-surface-2" /></div>)}
        </Panel></LoadingState> : unsupported ? <Alert variant="info">Watching is not supported on this server yet. {fixedScope ? "Recent updates remains available separately." : "Select All documents to see recent updates."}</Alert>
          : error && rows.length === 0 ? <Alert><div>Could not load recent updates.<Button variant="link" size="sm" onClick={() => void load()}>Retry</Button></div></Alert>
          : rows.length === 0 ? <EmptyState title={scope === "watching" ? (fixedScope ? "No watched updates yet" : "No watched documents yet") : "Nothing updated yet"}
            description={scope === "watching" ? "Turn on Watch in a document to find its latest changes here." : "Document changes across your Vaults will appear here."} />
          : <Panel>
            <ol className="divide-y divide-border">{rows.map(row => {
              const Icon = recentIcon(row.type);
              const collection = row.path.includes("/") ? row.path.slice(0, row.path.lastIndexOf("/")) : null;
              const preview = row.excerpt?.trim() || row.summary?.trim();
              const id = `home-recent-${scope}-${row.resource_id ?? row.doc_id}`;
              const href = `/vault/${encodeURIComponent(row.vault)}/doc/${encodeURIComponent(row.doc_id)}`;
              return <li key={row.resource_id ?? `${row.vault}:${row.doc_id}`} className="group relative flex flex-col bg-surface transition-token hover:bg-surface-hover focus-within:bg-surface-hover"><Link id={id}
                to={href}
                aria-label={`Preview ${row.title}`}
                state={documentPreviewState(location, id, fixedScope ? `home-updates-heading-${scope}` : `home-updates-${scope}`)}
                className="home-activity-row grid min-w-0 flex-1 grid-cols-[20px_minmax(0,1fr)] items-start gap-3 px-4 py-3 after:absolute after:inset-0 after:content-[''] focus-visible:outline-none focus-visible:after:ring-2 focus-visible:after:ring-inset focus-visible:after:ring-ring">
                <Icon className="mt-0.5 h-4 w-4 text-link" aria-hidden />
                <span className="min-w-0"><span className="block sm:min-h-8 sm:pr-32"><span className="line-clamp-2 break-words text-sm font-semibold text-foreground group-hover:text-link" title={row.title}>{row.title}</span></span>
                  <span className="mt-1 flex flex-col gap-1 text-xs text-foreground-muted sm:flex-row sm:flex-wrap sm:items-baseline sm:gap-x-3"><span className="min-w-0 break-words">{row.vault}{collection ? ` / ${collection}` : ""}</span><span className="flex flex-wrap gap-x-2 gap-y-1 sm:border-l sm:border-border sm:pl-3"><span>Updated <RelativeTime iso={row.changed_at} fallback="time unavailable" /></span>{(row.updated_by_name || row.author_name || row.created_by_name) && <span>{row.updated_by_name || row.author_name ? `by ${row.updated_by_name || row.author_name}` : `Created by ${row.created_by_name}`}</span>}</span></span>
                  {preview && <span className="mt-2 block line-clamp-2 text-sm leading-relaxed text-foreground-muted">{preview}</span>}
                </span>
              </Link><Button asChild variant="outline" size="sm" className="relative z-10 mb-3 mr-4 min-h-9 self-end sm:absolute sm:right-4 sm:top-2 sm:m-0"><Link to={href} aria-label={`Open ${row.title} in ${row.vault} vault`}>
                Open in vault<ArrowUpRight className="h-3.5 w-3.5" aria-hidden />
              </Link></Button></li>;
            })}</ol>
            {error && <Alert className="m-3"><div>Could not load updates. Your current list is still available.<Button variant="link" size="sm" onClick={failedMore ? loadMore : () => void load()}>Retry</Button></div></Alert>}
            {(cursor || legacyMore) && !error && <div className="border-t border-border p-2"><Button variant="ghost" size="sm" className="w-full" loading={moreBusy || busy} onClick={loadMore}>Show more</Button></div>}
          </Panel>}
      </TabsContent>
    </Tabs>
  </>;
}
