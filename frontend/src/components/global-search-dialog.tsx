import { useEffect, useRef, useState, type KeyboardEvent } from "react";
import { matchRoutes, useLocation, useNavigate } from "react-router-dom";
import {
  ArrowRight,
  Clock3,
  File,
  FileText,
  LoaderCircle,
  Search,
  Table2,
  X,
  type LucideIcon,
} from "lucide-react";
import { listVaults, searchDocs } from "@/lib/api";
import { parseUri } from "@/lib/uri";
import {
  Dialog,
  DialogClose,
  DialogContent,
  DialogDescription,
  DialogTitle,
  DialogTrigger,
} from "@/components/ui/dialog";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { SearchVaultPicker } from "@/components/search-vault-picker";
import { appRouteContract } from "@/app-route-contract";
import { cn } from "@/lib/utils";
import { documentPreviewState } from "@/lib/document-preview-navigation";
import { useCurrentUser } from "@/contexts/current-user-context";
import { useResourceNavigation } from "@/contexts/resource-navigation-context";
import {
  clearRecentSearches,
  readRecentSearches,
  recordRecentSearch,
  type RecentSearch,
} from "@/lib/recent-searches";
import { cleanSearchContext, safeSearchTags } from "@/lib/search-display";
import {
  readRecentDocumentViews,
  type RecentDocumentView,
} from "@/lib/recent-document-views";
import { RelativeTime } from "@/components/ui/relative-time";

type SearchSource = "document" | "table" | "file";
type SearchSourceFilter = "all" | SearchSource;

interface GlobalSearchResult {
  source_type?: SearchSource;
  uri: string;
  vault: string;
  path: string;
  title: string;
  collection?: string | null;
  doc_type?: string;
  summary?: string;
  matched_section?: string;
  tags?: string[];
  score: number;
}

const SOURCE_META: Record<
  SearchSource,
  { label: string; icon: LucideIcon }
> = {
  document: { label: "Document", icon: FileText },
  table: { label: "Table", icon: Table2 },
  file: { label: "File", icon: File },
};

const SOURCE_FILTERS: Array<{
  key: SearchSourceFilter;
  label: string;
  icon: LucideIcon;
}> = [
  { key: "all", label: "All", icon: Search },
  { key: "document", label: "Documents", icon: FileText },
  { key: "table", label: "Tables", icon: Table2 },
  { key: "file", label: "Files", icon: File },
];

const SUGGESTIONS = [
  "deployment guide",
  "authentication decisions",
  "onboarding checklist",
] as const;

function matchesSearchScope(search: RecentSearch, vault?: string) {
  return search.surface === "global"
    && (vault ? search.vaults.length === 1 && search.vaults[0] === vault : search.vaults.length === 0);
}

function resultHref(result: GlobalSearchResult): string {
  const source = result.source_type || "document";
  if (source === "table") {
    return `/vault/${result.vault}/table/${encodeURIComponent(result.path || result.title)}`;
  }
  const parsed = parseUri(result.uri);
  if (source === "file") {
    return `/vault/${result.vault}/file/${encodeURIComponent(parsed?.id ?? "")}`;
  }
  return `/vault/${result.vault}/doc/${encodeURIComponent(parsed?.id ?? result.path)}`;
}

/** One stable entry point; named Vault routes supply the initial search scope. */
export function GlobalSearchDialog() {
  const currentUser = useCurrentUser();
  const { pathname } = useLocation();
  const match = matchRoutes([...appRouteContract], pathname)?.at(-1);
  const vault = match?.route.boundary === "vault-shell" ? match.params.name : undefined;
  return <KnowledgeSearchDialog key={JSON.stringify([currentUser?.user_id, vault])} contextVault={vault} />;
}

function KnowledgeSearchDialog({ contextVault }: { contextVault?: string }) {
  const currentUser = useCurrentUser();
  const currentUserId = currentUser?.user_id;
  const navigate = useNavigate();
  const location = useLocation();
  const { requestNavigation } = useResourceNavigation();
  const [vault, setVault] = useState<string | undefined>(contextVault);
  const [availableVaults, setAvailableVaults] = useState<string[] | null>(null);
  const [vaultsError, setVaultsError] = useState(false);
  const [vaultsRetry, setVaultsRetry] = useState(0);
  const id = "global";
  const triggerId = `${id}-search-trigger`;
  const scopeLabel = vault ? `Search in ${vault}` : "Search all accessible vaults";
  const inputRef = useRef<HTMLInputElement>(null);
  const resultsScrollRef = useRef<HTMLDivElement>(null);
  const requestId = useRef(0);
  const [open, setOpen] = useState(false);
  const [query, setQuery] = useState("");
  const [results, setResults] = useState<GlobalSearchResult[]>([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [activeIndex, setActiveIndex] = useState(-1);
  const [activeSource, setActiveSource] = useState<SearchSourceFilter>("all");
  const [recentSearches, setRecentSearches] = useState<RecentSearch[]>([]);
  const [recentDocuments, setRecentDocuments] = useState<RecentDocumentView[]>([]);
  const [retryKey, setRetryKey] = useState(0);
  const normalizedQuery = query.trim();
  const sourceCounts = results.reduce(
    (counts, result) => {
      const source = result.source_type || "document";
      counts[source] += 1;
      return counts;
    },
    { document: 0, table: 0, file: 0 } as Record<SearchSource, number>,
  );
  const visibleResults = loading || error ? [] : results;
  const hasRecentSearches = recentSearches.length > 0;
  const hasRecentDocuments = recentDocuments.length > 0;

  function changeScope(next?: string) {
    if (vault === next) return;
    // Invalidate immediately: an old response must never be actionable under a
    // newly selected scope, including before the request effect is re-run.
    ++requestId.current;
    setVault(next);
    setResults([]);
    setError(null);
    setActiveIndex(-1);
    setLoading(Boolean(normalizedQuery));
    setRecentSearches([]);
    setRecentDocuments([]);
    if (resultsScrollRef.current) resultsScrollRef.current.scrollTop = 0;
  }

  useEffect(() => {
    if (!open) return;
    let cancelled = false;
    setAvailableVaults(null);
    setVaultsError(false);
    void listVaults().then(response => {
      if (cancelled) return;
      setAvailableVaults([...new Set((response.vaults || [])
        .map((item: { name?: unknown }) => item.name)
        .filter((name): name is string => typeof name === "string" && name.length > 0))]);
    }).catch(() => {
      if (!cancelled) setVaultsError(true);
    });
    return () => { cancelled = true; };
  }, [open, vaultsRetry]);

  useEffect(() => {
    if (!open || !currentUserId) {
      if (!currentUserId) {
        setRecentSearches([]);
        setRecentDocuments([]);
      }
      return;
    }
    setRecentSearches(
      readRecentSearches(currentUserId)
        .filter(search => matchesSearchScope(search, vault))
        .slice(0, 6),
    );
    const accessible = new Set(availableVaults || []);
    setRecentDocuments(readRecentDocumentViews(currentUserId)
      .filter(document => (!vault || document.vault === vault) && accessible.has(document.vault))
      .slice(0, 4));
  }, [currentUserId, open, vault, availableVaults]);

  useEffect(() => {
    const currentRequest = ++requestId.current;
    if (!open || !normalizedQuery) {
      setResults([]);
      setLoading(false);
      setError(null);
      setActiveIndex(-1);
      return;
    }

    setResults([]);
    setError(null);
    setActiveIndex(-1);
    setLoading(true);
    const timer = window.setTimeout(() => {
      void searchDocs(normalizedQuery, vault ? [vault] : [], 12, { source_type: activeSource === "all" ? undefined : activeSource })
        .then((response) => {
          if (currentRequest !== requestId.current) return;
          const nextResults = (response.results || []) as GlobalSearchResult[];
          setResults(nextResults);
          setActiveIndex(nextResults.length > 0 ? 0 : -1);
          if (response.degraded) setError("Search is incomplete; the retrieval service is temporarily unavailable. Please retry.");
        })
        .catch((caught: unknown) => {
          if (currentRequest !== requestId.current) return;
          setError(caught instanceof Error ? caught.message : "Search failed");
          setResults([]);
          setActiveIndex(-1);
        })
        .finally(() => {
          if (currentRequest === requestId.current) setLoading(false);
        });
    }, 220);

    return () => { window.clearTimeout(timer); ++requestId.current; };
  }, [normalizedQuery, open, retryKey, activeSource, vault]);

  function rememberGlobalSearch() {
    if (!currentUserId || !normalizedQuery) return;
    recordRecentSearch(currentUserId, {
      query: normalizedQuery,
      mode: "semantic",
      vaults: vault ? [vault] : [],
      surface: "global",
    });
    setRecentSearches(
      readRecentSearches(currentUserId)
        .filter(search => matchesSearchScope(search, vault))
        .slice(0, 6),
    );
  }

  function openResult(result: GlobalSearchResult) {
    rememberGlobalSearch();
    setOpen(false);
    const href = resultHref(result);
    const source = result.source_type || "document";
    const options = {
      state:
        source === "document"
          ? documentPreviewState(location, triggerId)
          : undefined,
    };
    if (requestNavigation(href, options)) navigate(href, options);
  }

  function openRecentDocument(document: RecentDocumentView) {
    setOpen(false);
    const href = `/vault/${encodeURIComponent(document.vault)}/doc/${encodeURIComponent(document.path)}`;
    const options = { state: documentPreviewState(location, triggerId) };
    if (requestNavigation(href, options)) navigate(href, options);
  }

  function selectResultWithKeyboard(index: number) {
    setActiveIndex(index);
    const container = resultsScrollRef.current;
    const option = document.getElementById(`${id}-search-result-${index}`);
    if (!container || !option) return;
    // Scroll only the result ledger, never the launching workspace or dialog.
    const viewport = container.getBoundingClientRect();
    const bounds = option.getBoundingClientRect();
    if (bounds.top < viewport.top) container.scrollTop += bounds.top - viewport.top;
    else if (bounds.bottom > viewport.bottom) {
      container.scrollTop += Math.min(bounds.bottom - viewport.bottom, bounds.top - viewport.top);
    }
  }

  function handleInputKeyDown(event: KeyboardEvent<HTMLInputElement>) {
    if (event.nativeEvent.isComposing || visibleResults.length === 0) return;
    if (event.key === "ArrowDown") {
      event.preventDefault();
      selectResultWithKeyboard((activeIndex + 1) % visibleResults.length);
      return;
    }
    if (event.key === "ArrowUp") {
      event.preventDefault();
      selectResultWithKeyboard(activeIndex <= 0 ? visibleResults.length - 1 : activeIndex - 1);
      return;
    }
    if (event.key === "Enter" && activeIndex >= 0) {
      event.preventDefault();
      openResult(visibleResults[activeIndex]);
    }
  }

  const resultStatus = loading
    ? "Searching knowledge…"
    : error
      ? "Search failed"
      : normalizedQuery
        ? `${visibleResults.length} result${visibleResults.length === 1 ? "" : "s"}`
        : "";

  return (
    <Dialog open={open} onOpenChange={next => {
      if (next) {
        changeScope(contextVault);
        setAvailableVaults(null);
        setVaultsError(false);
      }
      setOpen(next);
    }}>
      <DialogTrigger asChild>
        <button
          id={triggerId}
          type="button"
          aria-label="Search knowledge"
          title="Search documents, tables, and files"
          className="ml-auto flex h-9 w-9 shrink-0 items-center justify-center gap-2 rounded-[var(--radius-sm)] border border-border-strong bg-surface text-left text-sm text-foreground-muted transition-token hover:border-primary hover:bg-surface-hover hover:text-foreground focus:outline-none focus-visible:ring-2 focus-visible:ring-ring sm:w-64 sm:min-w-9 sm:shrink sm:justify-start sm:px-3 lg:shrink-0"
        >
          <Search className="h-4 w-4 shrink-0" aria-hidden />
          <span className="hidden truncate sm:inline">Search knowledge…</span>
        </button>
      </DialogTrigger>

      <DialogContent
        hideClose
        data-testid={`${id}-search-dialog`}
        className="top-16 flex max-h-[calc(100dvh-5rem)] w-[calc(100%-1rem)] max-w-[96rem] -translate-y-0 flex-col gap-0 overflow-hidden rounded-[var(--radius-lg)] border-border-strong p-0 shadow-xl sm:w-[calc(100%-2rem)]"
        onOpenAutoFocus={(event) => {
          event.preventDefault();
          inputRef.current?.focus();
        }}
      >
        <DialogTitle className="sr-only">{vault ? scopeLabel : "Search knowledge"}</DialogTitle>
        <DialogDescription className="sr-only">
          {vault ? `Search documents, tables, and files in ${vault}.` : "Search documents, tables, and files across every accessible vault."}
        </DialogDescription>

        <div className="flex shrink-0 items-start gap-2 border-b border-border-strong bg-surface p-2.5 sm:items-center sm:p-3">
          <div className="flex min-w-0 flex-1 flex-col items-start gap-1 rounded-[var(--radius-md)] border border-border-strong bg-background p-1.5 transition-token focus-within:border-primary focus-within:ring-2 focus-within:ring-ring sm:flex-row sm:items-center sm:gap-2.5">
            <SearchVaultPicker value={vault} contextVault={contextVault} vaults={availableVaults}
              error={vaultsError} onRetry={() => setVaultsRetry(key => key + 1)} onChange={changeScope} />
            <div className="flex h-9 w-full min-w-0 flex-1 items-center gap-2.5 px-1.5 sm:w-auto">
            <Search className="h-4 w-4 shrink-0 text-foreground-muted" aria-hidden />
            <label htmlFor={`${id}-search-input`} className="sr-only">
              {scopeLabel}
            </label>
            <input
              ref={inputRef}
              id={`${id}-search-input`}
              type="search"
              role="combobox"
              aria-autocomplete="list"
              aria-expanded={visibleResults.length > 0}
              aria-controls={
                visibleResults.length > 0 ? `${id}-search-results` : undefined
              }
              aria-activedescendant={
                activeIndex >= 0 && activeIndex < visibleResults.length ? `${id}-search-result-${activeIndex}` : undefined
              }
              autoComplete="off"
              spellCheck={false}
              placeholder="Search documents, tables, and files…"
              value={query}
              onChange={(event) => setQuery(event.target.value)}
              onKeyDown={handleInputKeyDown}
              className="h-full min-w-0 flex-1 appearance-none bg-transparent text-base text-foreground placeholder:text-foreground-muted focus:outline-none [&::-webkit-search-cancel-button]:hidden"
            />
            {loading && (
              <LoaderCircle className="h-4 w-4 shrink-0 animate-spin text-foreground-muted" aria-hidden />
            )}
            {query && !loading && (
              <button
                type="button"
                aria-label={vault ? "Clear vault search" : "Clear global search"}
                onClick={() => setQuery("")}
                className="inline-flex h-7 shrink-0 cursor-pointer items-center justify-center rounded-[var(--radius-sm)] px-2 text-xs font-medium text-foreground-muted transition-token hover:bg-surface-hover hover:text-foreground focus:outline-none focus-visible:ring-2 focus-visible:ring-ring"
              >
                Clear
              </button>
            )}
            </div>
          </div>
          <DialogClose asChild>
            <button
              type="button"
              aria-label="Close search"
              className="inline-flex h-10 w-10 shrink-0 cursor-pointer items-center justify-center rounded-[var(--radius-md)] text-foreground-muted transition-token hover:bg-surface-hover hover:text-foreground focus:outline-none focus-visible:ring-2 focus-visible:ring-ring"
            >
              <X className="h-4 w-4" aria-hidden />
            </button>
          </DialogClose>
        </div>

        <p role="status" aria-live="polite" className="sr-only">
          {resultStatus}
        </p>

        <div className="flex min-h-11 shrink-0 items-center border-b border-border bg-surface-2/60 px-3 py-1.5 sm:px-4">
          <div
            role="group"
            aria-label={`Limit ${id} search by content kind`}
            className="flex min-w-0 flex-1 flex-wrap items-center gap-1.5"
          >
            {SOURCE_FILTERS.map(({ key, label, icon: FilterIcon }) => {
              const selected = activeSource === key;
              const count = key === "all" ? results.length : sourceCounts[key];
              return (
                <button
                  key={key}
                  type="button"
                  aria-pressed={selected}
                  aria-label={
                    normalizedQuery && !loading && !error
                      ? `${label}, ${count} result${count === 1 ? "" : "s"}`
                      : label
                  }
                  onClick={() => {
                    setActiveSource(key);
                    setActiveIndex(
                      key === "all"
                        ? results.length > 0
                          ? 0
                          : -1
                        : sourceCounts[key] > 0
                          ? 0
                          : -1,
                    );
                  }}
                  className={cn(
                    "inline-flex h-8 cursor-pointer items-center gap-1.5 rounded-[var(--radius-sm)] border px-2.5 text-xs font-medium transition-token focus:outline-none focus-visible:ring-2 focus-visible:ring-ring",
                    selected
                      ? "border-border-strong bg-surface-selected text-surface-selected-foreground"
                      : "border-border bg-surface text-foreground-muted hover:border-border-strong hover:bg-surface-hover hover:text-foreground",
                  )}
                >
                  <FilterIcon className="h-3.5 w-3.5" aria-hidden />
                  {label}
                  {normalizedQuery && !loading && !error && (
                    <span className="tabular-nums text-foreground-muted">
                      {count}
                    </span>
                  )}
                </button>
              );
            })}
          </div>
        </div>

        <div ref={resultsScrollRef} className="rail-scroll min-h-0 max-h-[min(34rem,calc(100dvh-10rem))] flex-1 overflow-y-auto bg-surface">
          {!normalizedQuery && (
            <>
              {(hasRecentSearches || hasRecentDocuments) && (
                <div
                  className={cn(
                    "grid border-b border-border",
                    hasRecentSearches && hasRecentDocuments && "lg:grid-cols-2",
                  )}
                >
                  {hasRecentSearches && (
                    <section
                      aria-labelledby={`${id}-recent-searches-heading`}
                      className={cn(
                        hasRecentDocuments && "lg:border-r lg:border-border",
                      )}
                    >
                  <div className="flex min-h-10 items-center justify-between gap-3 border-b border-border bg-surface-2/60 px-4 sm:px-5">
                    <div className="flex min-w-0 items-center gap-2">
                      <Clock3 className="h-3.5 w-3.5 shrink-0 text-foreground-muted" aria-hidden />
                      <h2
                        id={`${id}-recent-searches-heading`}
                        className="text-xs font-semibold text-foreground"
                      >
                        Recent searches
                      </h2>
                    </div>
                    <button
                      type="button"
                      onClick={() => {
                        if (!currentUserId) return;
                        clearRecentSearches(currentUserId, { surface: "global", vaults: vault ? [vault] : [] });
                        setRecentSearches([]);
                      }}
                      className="inline-flex h-8 cursor-pointer items-center rounded-[var(--radius-sm)] px-2 text-xs font-medium text-foreground-muted transition-token hover:bg-surface-hover hover:text-foreground focus:outline-none focus-visible:ring-2 focus-visible:ring-ring"
                    >
                      Clear
                    </button>
                  </div>
                  <div className="divide-y divide-border" aria-label={`Recent ${id} searches`}>
                    {recentSearches.slice(0, 4).map((search) => (
                      <button
                        key={`${search.mode}:${search.query}`}
                        type="button"
                        onClick={() => setQuery(search.query)}
                        className="group flex min-h-12 w-full cursor-pointer items-center gap-3 px-4 py-2 text-left transition-token hover:bg-surface-hover focus:outline-none focus-visible:ring-2 focus-visible:ring-inset focus-visible:ring-ring sm:px-5"
                      >
                        <Search className="h-4 w-4 shrink-0 text-foreground-muted" aria-hidden />
                        <span className="min-w-0 flex-1 truncate text-sm text-foreground">
                          {search.query}
                        </span>
                        <RelativeTime
                          iso={search.searchedAt}
                          className="hidden shrink-0 text-xs text-foreground-muted sm:inline-flex"
                        />
                        <ArrowRight className="h-3.5 w-3.5 shrink-0 text-foreground-muted transition-colors group-hover:text-link" aria-hidden />
                      </button>
                    ))}
                  </div>
                    </section>
                  )}

                  {hasRecentDocuments && (
                    <section aria-labelledby={`${id}-recent-documents-heading`}>
                  <div className="flex min-h-10 items-center justify-between gap-3 border-b border-border bg-surface-2/60 px-4 sm:px-5">
                    <div className="flex min-w-0 items-center gap-2">
                      <FileText className="h-3.5 w-3.5 shrink-0 text-foreground-muted" aria-hidden />
                      <h2
                        id={`${id}-recent-documents-heading`}
                        className="text-xs font-semibold text-foreground"
                      >
                        Recently viewed
                      </h2>
                    </div>
                    <span className="text-xs text-foreground-muted">This browser</span>
                  </div>
                  <div className="divide-y divide-border" aria-label="Recently viewed documents">
                    {recentDocuments.map((document) => (
                      <button
                        key={`${document.vault}:${document.path}`}
                        type="button"
                        onClick={() => openRecentDocument(document)}
                        className="group grid min-h-12 w-full grid-cols-[auto_minmax(0,1fr)_auto] items-center gap-3 px-4 py-2 text-left transition-token hover:bg-surface-hover focus:outline-none focus-visible:ring-2 focus-visible:ring-inset focus-visible:ring-ring sm:px-5"
                      >
                        <span className="flex h-8 w-8 shrink-0 items-center justify-center rounded-[var(--radius-md)] bg-surface-2 text-foreground-muted transition-colors group-hover:text-link">
                          <FileText className="h-4 w-4" aria-hidden />
                        </span>
                        <span className="min-w-0">
                          <span className="block truncate text-sm font-medium text-foreground transition-colors group-hover:text-link">
                            {document.title}
                          </span>
                          <span className="mt-0.5 block truncate text-xs text-foreground-muted">
                            {document.vault} · {document.path}
                          </span>
                        </span>
                        <span className="flex shrink-0 items-center gap-2">
                          <RelativeTime
                            iso={document.viewedAt}
                            className="hidden text-xs text-foreground-muted sm:inline-flex"
                          />
                          <ArrowRight className="h-3.5 w-3.5 text-foreground-muted transition-colors group-hover:text-link" aria-hidden />
                        </span>
                      </button>
                    ))}
                  </div>
                    </section>
                  )}
                </div>
              )}

              <section aria-labelledby={`${id}-search-suggestions-heading`}>
                <div className="flex min-h-10 items-center justify-between gap-3 border-b border-border bg-surface-2/60 px-4 sm:px-5">
                  <h2
                    id={`${id}-search-suggestions-heading`}
                    className="text-xs font-semibold text-foreground"
                  >
                    Suggested searches
                  </h2>
                  <span className="text-xs text-foreground-muted">
                    Try one to start
                  </span>
                </div>
                <div
                  aria-label={`Suggested ${id} searches`}
                  className="grid divide-y divide-border sm:grid-cols-3 sm:divide-x sm:divide-y-0"
                >
                  {SUGGESTIONS.map((suggestion) => (
                    <button
                      key={suggestion}
                      type="button"
                      onClick={() => setQuery(suggestion)}
                      className="group flex min-h-14 w-full cursor-pointer items-center gap-3 px-4 text-left transition-token hover:bg-surface-hover focus:outline-none focus-visible:ring-2 focus-visible:ring-inset focus-visible:ring-ring sm:px-5"
                    >
                      <Search className="h-4 w-4 shrink-0 text-foreground-muted" aria-hidden />
                      <span className="min-w-0 flex-1 truncate text-sm text-foreground">
                        {suggestion}
                      </span>
                      <ArrowRight className="hidden h-3.5 w-3.5 shrink-0 text-foreground-muted transition-colors group-hover:text-link lg:block" aria-hidden />
                    </button>
                  ))}
                </div>
              </section>
            </>
          )}

          {normalizedQuery && loading && (
            <div className="divide-y divide-border" aria-hidden>
              {[0, 1, 2].map((item) => (
                <div key={item} className="flex gap-3 px-4 py-3 sm:px-5">
                  <span className="h-8 w-8 shrink-0 animate-pulse rounded-[var(--radius-md)] bg-surface-2" />
                  <div className="min-w-0 flex-1 space-y-2 py-0.5">
                    <span className="block h-3.5 w-1/3 animate-pulse rounded-[var(--radius-sm)] bg-surface-2" />
                    <span className="block h-3 w-1/2 animate-pulse rounded-[var(--radius-sm)] bg-surface-2" />
                  </div>
                </div>
              ))}
            </div>
          )}

          {normalizedQuery && !loading && error && (
            <div className="flex min-h-56 flex-col items-center justify-center px-5 text-center">
              <p className="text-sm font-semibold text-foreground">Search is unavailable</p>
              <p className="mt-1 max-w-md text-xs leading-relaxed text-foreground-muted">{error}</p>
              <Button
                type="button"
                variant="outline"
                size="sm"
                className="mt-3"
                onClick={() => setRetryKey((current) => current + 1)}
              >
                Retry
              </Button>
            </div>
          )}

          {normalizedQuery && !loading && !error && results.length === 0 && (
            <div className="flex min-h-56 flex-col items-center justify-center px-5 text-center">
              <Search className="h-5 w-5 text-foreground-muted" aria-hidden />
              <p className="mt-3 text-sm font-semibold text-foreground">
                No results for “{normalizedQuery}”
              </p>
              <p className="mt-1 text-xs text-foreground-muted">
                {vault ? `No matches in ${vault}. Try fewer words or expand your search.` : "Try fewer words or continue in the search page for exact matching."}
              </p>
              {vault && <Button type="button" variant="outline" className="mt-3" onClick={() => { changeScope(undefined); inputRef.current?.focus(); }}>Search all vaults instead</Button>}
              {activeSource !== "all" && <Button type="button" variant="outline" className="mt-3" onClick={() => setActiveSource("all")}>Show all results</Button>}
            </div>
          )}

          {visibleResults.length > 0 && !loading && !error && (
            <section aria-labelledby={`${id}-search-results-heading`}>
              <div className="flex min-h-10 items-center justify-between gap-3 border-b border-border bg-surface-2/60 px-4 sm:px-5">
                <h2
                  id={`${id}-search-results-heading`}
                  className="text-xs font-semibold text-foreground"
                >
                  Top matches
                </h2>
                <span className="text-xs tabular-nums text-foreground-muted">
                  {activeSource === "all"
                    ? `${results.length} result${results.length === 1 ? "" : "s"}`
                    : `${visibleResults.length} of ${results.length}`}
                </span>
              </div>
              <ul id={`${id}-search-results`} role="listbox" aria-label="Knowledge search results">
                {visibleResults.map((result, index) => {
                  const source =
                    result.source_type && result.source_type in SOURCE_META
                      ? result.source_type
                      : "document";
                  const { icon: SourceIcon, label } = SOURCE_META[source];
                  const detail = cleanSearchContext(
                    result.matched_section || result.summary,
                  );
                  const tags = safeSearchTags(result.tags);
                  return (
                    <li key={result.uri} role="presentation">
                      <button
                        type="button"
                        id={`${id}-search-result-${index}`}
                        role="option"
                        aria-selected={index === activeIndex}
                        tabIndex={-1}
                        onMouseMove={() => setActiveIndex(index)}
                        onClick={() => openResult(result)}
                        className={cn(
                          "group grid w-full grid-cols-[auto_minmax(0,1fr)_auto] items-start gap-3 border-b border-border px-4 py-2.5 text-left transition-token last:border-b-0 focus:outline-none focus-visible:ring-2 focus-visible:ring-inset focus-visible:ring-ring sm:px-5",
                          index === activeIndex
                            ? "bg-surface-selected text-surface-selected-foreground"
                            : "bg-surface text-foreground hover:bg-surface-hover",
                        )}
                      >
                        <span className="flex h-8 w-8 shrink-0 items-center justify-center rounded-[var(--radius-md)] bg-surface-2 text-foreground-muted">
                          <SourceIcon className="h-4 w-4" aria-hidden />
                        </span>
                        <span className="min-w-0">
                          <span className="flex min-w-0 flex-wrap items-center gap-2">
                            <span className="truncate text-sm font-semibold">{result.title}</span>
                            {result.doc_type && <Badge variant="outline">{result.doc_type}</Badge>}
                            {tags.slice(0, 2).map((tag) => (
                              <Badge key={tag} variant="secondary">
                                {tag}
                              </Badge>
                            ))}
                          </span>
                          <span className="mt-1 flex min-w-0 flex-wrap items-center gap-x-2 gap-y-1 text-xs text-foreground-muted">
                            <span>{label}</span>
                            <span aria-hidden>·</span>
                            <span>{result.vault}</span>
                            <span aria-hidden>·</span>
                            <span className="min-w-0 truncate">{result.path}</span>
                          </span>
                          {detail && (
                            <span className="mt-1.5 block line-clamp-2 whitespace-pre-line text-xs leading-relaxed text-foreground-muted">
                              {detail}
                            </span>
                          )}
                        </span>
                        <span className="hidden items-center gap-1.5 pt-1 text-xs font-medium text-foreground-muted transition-colors group-hover:text-link sm:inline-flex">
                          {source === "document" ? "Preview" : "Open"}
                          <ArrowRight className="h-3.5 w-3.5" aria-hidden />
                        </span>
                      </button>
                    </li>
                  );
                })}
              </ul>
            </section>
          )}
        </div>

        <div className="flex min-h-11 shrink-0 items-center justify-between gap-3 border-t border-border bg-surface-2/60 px-3 py-2 sm:px-4">
          <span className="min-w-0 truncate text-xs text-foreground-muted">Select a result to open it</span>
          <button
            type="button"
            onClick={() => {
              rememberGlobalSearch();
              setOpen(false);
              const params = new URLSearchParams();
              if (normalizedQuery) params.set("q", normalizedQuery);
              if (activeSource !== "all") params.set("source", activeSource);
              const href = `${vault ? `/vault/${encodeURIComponent(vault)}/search` : "/search"}?${params}`;
              if (requestNavigation(href)) navigate(href);
            }}
            className="inline-flex h-8 shrink-0 items-center gap-1.5 rounded-[var(--radius-sm)] px-2 text-xs font-medium text-link transition-token hover:bg-surface-hover hover:text-link-hover focus:outline-none focus-visible:ring-2 focus-visible:ring-ring"
          >
            Continue in search page
            <ArrowRight className="h-3.5 w-3.5" aria-hidden />
          </button>
        </div>
      </DialogContent>
    </Dialog>
  );
}
