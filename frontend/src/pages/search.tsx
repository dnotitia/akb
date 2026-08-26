import {
  type ComponentType,
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
} from "react";
import {
  Link,
  useLocation,
  useParams,
  useSearchParams,
} from "react-router-dom";
import {
  ArrowUpRight,
  Braces,
  Clock3,
  ExternalLink,
  File,
  FileText,
  FolderSearch,
  Layers3,
  Search as SearchIcon,
  SlidersHorizontal,
  Sparkles,
  Table,
  Tag,
} from "lucide-react";
import { searchDocs, grepDocs, listVaults, type GrepDoc } from "@/lib/api";
import { Alert } from "@/components/ui/alert";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Panel } from "@/components/ui/panel";
import { RelativeTime } from "@/components/ui/relative-time";
import { Skeleton } from "@/components/ui/skeleton";
import { VaultScopePicker } from "@/components/vault-scope-picker";
import { useCurrentUser } from "@/contexts/current-user-context";
import {
  clearRecentSearches,
  readRecentSearches,
  recordRecentSearch,
  type RecentSearch,
} from "@/lib/recent-searches";
import {
  readRecentDocumentViews,
  type RecentDocumentView,
} from "@/lib/recent-document-views";
import { cleanSearchContext, safeSearchTags } from "@/lib/search-display";
import { parseUri } from "@/lib/uri";
import { cn } from "@/lib/utils";
import { documentPreviewState } from "@/lib/document-preview-navigation";

type Mode = "dense" | "literal";
type SourceType = "document" | "table" | "file";
type SourceFilter = "all" | SourceType;

const ALL_TYPES = [
  "skill",
  "note",
  "report",
  "decision",
  "spec",
  "plan",
  "session",
  "task",
  "reference",
] as const;
type DocTypeFilter = (typeof ALL_TYPES)[number];

const SUGGESTED_QUERIES = [
  "deployment guide",
  "authentication decisions",
  "onboarding checklist",
] as const;

interface DenseResult {
  source_type?: SourceType;
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

const TYPE_META: Record<
  SourceType,
  {
    label: string;
    icon: ComponentType<{ className?: string; "aria-hidden"?: boolean }>;
  }
> = {
  document: { label: "Document", icon: FileText },
  table: { label: "Table", icon: Table },
  file: { label: "File", icon: File },
};

const SOURCE_FILTERS: Array<{
  key: SourceFilter;
  label: string;
  icon: ComponentType<{ className?: string; "aria-hidden"?: boolean }>;
}> = [
  { key: "all", label: "Everything", icon: Layers3 },
  { key: "document", label: "Documents", icon: FileText },
  { key: "table", label: "Tables", icon: Table },
  { key: "file", label: "Files", icon: File },
];

const inlineActionClass =
  "cursor-pointer rounded-[var(--radius-sm)] font-medium underline hover:text-link focus:outline-none focus-visible:ring-2 focus-visible:ring-ring";

function resultHref(result: DenseResult): string {
  const type = result.source_type || "document";
  if (type === "table") {
    const tableName = result.path || result.title;
    return `/vault/${result.vault}/table/${encodeURIComponent(tableName)}`;
  }
  const parsed = parseUri(result.uri);
  if (type === "file") {
    return `/vault/${result.vault}/file/${parsed?.id ?? ""}`;
  }
  const docPath = parsed?.id ?? result.path;
  return `/vault/${result.vault}/doc/${encodeURIComponent(docPath)}`;
}

export default function SearchPage() {
  const currentUser = useCurrentUser();
  const currentUserId = currentUser?.user_id;
  const [searchParams, setSearchParams] = useSearchParams();
  const { name: scopedVault } = useParams<{ name: string }>();
  const q = searchParams.get("q") || "";
  const mode: Mode =
    searchParams.get("mode") === "literal" ? "literal" : "dense";
  const vaultParam = searchParams.get("v") || "";
  const scopeVaults = useMemo(
    () =>
      scopedVault
        ? [scopedVault]
        : vaultParam
            .split(",")
            .map((vault) => vault.trim())
            .filter(Boolean),
    [scopedVault, vaultParam],
  );

  const [denseResults, setDenseResults] = useState<DenseResult[]>([]);
  const [literalResults, setLiteralResults] = useState<GrepDoc[]>([]);
  const [total, setTotal] = useState(0);
  const [totalMatches, setTotalMatches] = useState(0);
  const [returnedDocs, setReturnedDocs] = useState(0);
  const [returnedMatches, setReturnedMatches] = useState(0);
  const [truncated, setTruncated] = useState(false);
  const [degraded, setDegraded] = useState(false);
  const [loading, setLoading] = useState(false);
  const [searched, setSearched] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [vaults, setVaults] = useState<{ name: string }[]>([]);
  const [activeTypes, setActiveTypes] = useState<Set<DocTypeFilter>>(
    () => new Set(ALL_TYPES),
  );
  const [activeSource, setActiveSource] = useState<SourceFilter>("all");
  const [activeTags, setActiveTags] = useState<Set<string>>(() => new Set());
  const [filtersOpen, setFiltersOpen] = useState(false);
  const [draft, setDraft] = useState(q);
  const [recentSearches, setRecentSearches] = useState<RecentSearch[]>([]);
  const [recentDocuments, setRecentDocuments] = useState<
    RecentDocumentView[]
  >([]);
  const reqId = useRef(0);

  const doSearch = useCallback(
    async (
      searchQuery: string,
      searchMode: Mode,
      selectedVaults: string[],
    ) => {
      if (!searchQuery.trim()) return;
      const id = ++reqId.current;
      setLoading(true);
      setSearched(true);
      setError(null);

      try {
        if (searchMode === "dense") {
          const response = await searchDocs(searchQuery, selectedVaults, 25);
          if (id !== reqId.current) return;
          setDenseResults(response.results);
          setLiteralResults([]);
          setTotal(response.total ?? response.results.length);
          setTotalMatches(response.total_matches);
          setReturnedDocs(response.returned);
          setReturnedMatches(0);
          setTruncated(Boolean(response.truncated));
          setDegraded(Boolean(response.degraded));
        } else {
          const response = await grepDocs(searchQuery, selectedVaults);
          if (id !== reqId.current) return;
          setLiteralResults(response.results);
          setDenseResults([]);
          setTotal(response.total_docs ?? response.results.length);
          setTotalMatches(response.total_matches);
          setReturnedDocs(response.returned_docs ?? response.total_docs);
          setReturnedMatches(
            response.returned_matches ?? response.total_matches,
          );
          setTruncated(Boolean(response.truncated));
          setDegraded(false);
        }
        if (id === reqId.current && currentUserId) {
          recordRecentSearch(currentUserId, {
            query: searchQuery,
            mode: searchMode === "dense" ? "semantic" : "literal",
            vaults: selectedVaults,
            surface: "advanced",
          });
          setRecentSearches(readRecentSearches(currentUserId, 5));
        }
      } catch (caught) {
        if (id !== reqId.current) return;
        setError(caught instanceof Error ? caught.message : "Search failed");
        setDenseResults([]);
        setLiteralResults([]);
        setTotal(0);
        setTotalMatches(0);
        setReturnedDocs(0);
        setReturnedMatches(0);
        setTruncated(false);
        setDegraded(false);
      } finally {
        if (id === reqId.current) setLoading(false);
      }
    },
    [currentUserId],
  );

  useEffect(() => {
    if (!currentUserId) {
      setRecentSearches([]);
      setRecentDocuments([]);
      return;
    }
    setRecentSearches(readRecentSearches(currentUserId, 5));
    setRecentDocuments(readRecentDocumentViews(currentUserId, 8));
  }, [currentUserId]);

  useEffect(() => {
    setDraft(q);
  }, [q]);

  useEffect(() => {
    setActiveSource("all");
    setActiveTags(new Set());
  }, [q, mode]);

  useEffect(() => {
    if (!scopedVault) {
      listVaults()
        .then((response) => setVaults(response.vaults || []))
        .catch((caught) => {
          console.error("Failed to load vaults for the scope picker", caught);
        });
    }
  }, [scopedVault]);

  useEffect(() => {
    if (q) {
      void doSearch(q, mode, scopeVaults);
    } else {
      reqId.current += 1;
      setDenseResults([]);
      setLiteralResults([]);
      setTotal(0);
      setTotalMatches(0);
      setReturnedDocs(0);
      setReturnedMatches(0);
      setTruncated(false);
      setDegraded(false);
      setLoading(false);
      setSearched(false);
      setError(null);
    }

    return () => {
      reqId.current += 1;
    };
  }, [doSearch, q, mode, scopeVaults]);

  function switchMode(nextMode: Mode) {
    const next = new URLSearchParams(searchParams);
    const trimmed = draft.trim();
    if (trimmed) next.set("q", trimmed);
    if (nextMode === "dense") next.delete("mode");
    else next.set("mode", nextMode);
    setSearchParams(next, { replace: true });
  }

  function setScopeVaults(selectedVaults: string[]) {
    const next = new URLSearchParams(searchParams);
    const trimmed = draft.trim();
    if (trimmed) next.set("q", trimmed);
    if (selectedVaults.length) next.set("v", selectedVaults.join(","));
    else next.delete("v");
    setSearchParams(next, { replace: true });
  }

  function toggleType(type: DocTypeFilter) {
    setActiveTypes((current) => {
      const next = new Set(current);
      if (next.has(type)) next.delete(type);
      else next.add(type);
      return next;
    });
  }

  function toggleTag(tag: string) {
    setActiveTags((current) => {
      const next = new Set(current);
      if (next.has(tag)) next.delete(tag);
      else next.add(tag);
      return next;
    });
  }

  function commitQuery(value: string) {
    const trimmed = value.trim();
    const next = new URLSearchParams(searchParams);
    if (trimmed) next.set("q", trimmed);
    else next.delete("q");
    setDraft(trimmed);
    setSearchParams(next, { replace: true });
  }

  function repeatRecentSearch(item: RecentSearch) {
    const next = new URLSearchParams(searchParams);
    next.set("q", item.query);
    if (item.mode === "literal") next.set("mode", "literal");
    else next.delete("mode");
    if (!scopedVault) {
      if (item.vaults.length) next.set("v", item.vaults.join(","));
      else next.delete("v");
    }
    setDraft(item.query);
    setSearchParams(next, { replace: true });
  }

  function clearSearchHistory() {
    if (!currentUserId) return;
    clearRecentSearches(currentUserId);
    setRecentSearches([]);
  }

  const isShortQuery =
    q.trim().length > 0 && q.trim().length <= 6 && !/\s/.test(q.trim());
  const showLiteralHint =
    mode === "dense" && isShortQuery && searched && !loading;
  const allTypesActive = activeTypes.size === ALL_TYPES.length;
  const knownTypes = new Set<string>(ALL_TYPES);
  const typeFilteredDense = allTypesActive
    ? denseResults
    : denseResults.filter(
        (result) =>
          !result.doc_type ||
          !knownTypes.has(result.doc_type) ||
          activeTypes.has(result.doc_type as DocTypeFilter),
      );
  const filteredDense =
    activeTags.size === 0
      ? typeFilteredDense
      : typeFilteredDense.filter((result) =>
          safeSearchTags(result.tags).some((tag) => activeTags.has(tag)),
        );
  const groupedDense = groupByType(filteredDense);
  const sourceCounts = groupByType(denseResults);
  const tagCounts = new Map<string, number>();
  for (const result of denseResults) {
    for (const tag of safeSearchTags(result.tags)) {
      tagCounts.set(tag, (tagCounts.get(tag) ?? 0) + 1);
    }
  }
  const availableTags = [...tagCounts.entries()].sort(
    ([leftTag, leftCount], [rightTag, rightCount]) =>
      rightCount - leftCount || leftTag.localeCompare(rightTag),
  );
  const visibleDense =
    activeSource === "all" ? filteredDense : groupedDense[activeSource];
  const resultCount =
    mode === "dense" ? visibleDense.length : literalResults.length;
  const hasResults = resultCount > 0;
  const hasRawResults =
    mode === "dense" ? denseResults.length > 0 : literalResults.length > 0;

  const denseCountSummary =
    activeSource !== "all" || !allTypesActive
      ? `${visibleDense.length} visible · ${returnedDocs} loaded`
      : returnedDocs !== total
        ? `${returnedDocs} of ${total} top results loaded`
        : `${visibleDense.length} result${visibleDense.length === 1 ? "" : "s"}`;
  const literalCountSummary =
    returnedDocs !== total || returnedMatches !== totalMatches
      ? `${returnedDocs} of ${total} ${total === 1 ? "doc" : "docs"} · ${returnedMatches} of ${totalMatches} ${totalMatches === 1 ? "match" : "matches"}`
      : `${total} ${total === 1 ? "doc" : "docs"} · ${totalMatches} ${totalMatches === 1 ? "match" : "matches"}`;
  const allVaultsHref = `/search${
    q
      ? `?q=${encodeURIComponent(q)}${mode !== "dense" ? `&mode=${mode}` : ""}`
      : ""
  }`;

  const activeFilterCount =
    (activeSource === "all" ? 0 : 1) +
    (allTypesActive ? 0 : 1) +
    (activeTags.size === 0 ? 0 : 1);
  const accessibleVaultNames = new Set(
    scopedVault ? [scopedVault] : vaults.map((vault) => vault.name),
  );
  const visibleRecentDocuments = recentDocuments
    .filter((document) => accessibleVaultNames.has(document.vault))
    .slice(0, 4);
  const visibleRecentSearches = scopedVault
    ? recentSearches.filter((search) => search.vaults.includes(scopedVault))
    : recentSearches;

  return (
    <div data-testid="search-workspace" className="w-full max-w-none">
      <h1 className="sr-only">Search</h1>
      <p role="status" aria-live="polite" className="sr-only">
        {loading
          ? "Searching…"
          : error
            ? "Search failed"
            : !searched
              ? ""
              : !hasResults
                ? `No visible results for ${q}`
                : `${resultCount} results for ${q}`}
      </p>

      <Panel
        variant="workspace"
        inset={false}
        className="min-h-[30rem] w-full overflow-hidden border-border-strong shadow-sm"
      >
        <section aria-label="Search workspace">
          <form
            data-testid="search-command-header"
            className="border-b border-border-strong bg-surface p-2.5 sm:p-3"
            onSubmit={(event) => {
              event.preventDefault();
              commitQuery(draft);
            }}
            role="search"
            aria-label={
              scopedVault ? `Search within ${scopedVault}` : "Search all vaults"
            }
          >
            <span id="search-query-help" className="sr-only">
              {mode === "dense"
                ? "Semantic search matches meaning and keywords."
                : "Literal search matches exact text or a regular expression."}
            </span>
            <div className="flex flex-col gap-2 sm:flex-row">
              <div className="flex h-11 min-w-0 flex-1 items-center rounded-[var(--radius-md)] border border-border-strong bg-background px-3 transition-token focus-within:border-primary focus-within:ring-2 focus-within:ring-ring">
                <SearchIcon
                  className="mr-2.5 h-4 w-4 shrink-0 text-foreground-muted"
                  aria-hidden
                />
                <label htmlFor="vault-search" className="sr-only">
                  Search query
                </label>
                <input
                  id="vault-search"
                  type="search"
                  enterKeyHint="search"
                  aria-describedby="search-query-help"
                  placeholder="Search documents, tables, and files…"
                  value={draft}
                  onChange={(event) => setDraft(event.target.value)}
                  onKeyDown={(event) => {
                    if (event.key === "Escape" && draft) {
                      event.preventDefault();
                      commitQuery("");
                    }
                  }}
                  className="min-w-0 flex-1 appearance-none bg-transparent text-base text-foreground placeholder:text-foreground-muted focus:outline-none [&::-webkit-search-cancel-button]:hidden"
                />
                {draft && !loading && (
                  <button
                    type="button"
                    aria-label="Clear search query"
                    onClick={() => commitQuery("")}
                    className="ml-2 inline-flex h-8 shrink-0 cursor-pointer items-center justify-center rounded-[var(--radius-sm)] px-2 text-xs font-medium text-foreground-muted transition-token hover:bg-surface-hover hover:text-foreground focus:outline-none focus-visible:ring-2 focus-visible:ring-ring"
                  >
                    Clear
                  </button>
                )}
              </div>
              <Button
                type="submit"
                variant="accent"
                size="md"
                loading={loading}
                className="h-11 shrink-0 px-5 sm:min-w-24"
              >
                Search
              </Button>
            </div>
          </form>

          <div className="flex min-h-12 flex-wrap items-center gap-2 border-b border-border-strong bg-surface-2/60 px-3 py-1.5 sm:px-4">
            <div
              role="group"
              aria-label="Search mode"
              className="inline-flex h-9 items-center rounded-[var(--radius-md)] border border-border bg-surface p-0.5"
            >
              <button
                type="button"
                aria-label="Semantic"
                aria-pressed={mode === "dense"}
                onClick={() => switchMode("dense")}
                className={searchModeClass(mode === "dense")}
              >
                <Sparkles className="h-3.5 w-3.5" aria-hidden />
                Semantic
              </button>
              <button
                type="button"
                aria-label="Literal"
                aria-pressed={mode === "literal"}
                onClick={() => switchMode("literal")}
                className={searchModeClass(mode === "literal")}
              >
                <Braces className="h-3.5 w-3.5" aria-hidden />
                Literal
              </button>
            </div>

            <div className="hidden h-6 w-px bg-border sm:block" aria-hidden />

            <div className="flex min-w-0 flex-1 flex-wrap items-center gap-2">
              {scopedVault ? (
                <>
                  <div
                    aria-label={`Search scope: ${scopedVault}`}
                    className="inline-flex h-9 min-w-0 items-center gap-2 rounded-[var(--radius-md)] border border-border bg-surface px-2.5 text-xs text-foreground"
                  >
                    <FolderSearch
                      className="h-3.5 w-3.5 shrink-0 text-foreground-muted"
                      aria-hidden
                    />
                    <span className="truncate">{scopedVault}</span>
                    <Badge variant="outline">Fixed</Badge>
                  </div>
                  <Link
                    to={allVaultsHref}
                    className="inline-flex h-9 items-center gap-1.5 rounded-[var(--radius-md)] px-2.5 text-xs font-medium text-link transition-token hover:bg-surface-hover hover:text-link-hover focus:outline-none focus-visible:ring-2 focus-visible:ring-ring"
                  >
                    All vaults
                    <ExternalLink className="h-3.5 w-3.5" aria-hidden />
                  </Link>
                </>
              ) : vaults.length > 0 ? (
                <VaultScopePicker
                  vaults={vaults}
                  selected={scopeVaults}
                  onChange={setScopeVaults}
                  className="min-w-0 [&_button[aria-label^='Search_scope']]:h-9"
                />
              ) : (
                <div className="inline-flex h-9 items-center gap-2 rounded-[var(--radius-md)] border border-border bg-surface px-2.5 text-xs text-foreground-muted">
                  <FolderSearch className="h-3.5 w-3.5" aria-hidden />
                  All accessible vaults
                </div>
              )}
            </div>

            {mode === "dense" && (
              <button
                type="button"
                aria-label="Filter by document type"
                aria-expanded={filtersOpen}
                aria-controls="search-filter-tray"
                onClick={() => setFiltersOpen((open) => !open)}
                className={cn(
                  "inline-flex h-9 cursor-pointer items-center gap-2 rounded-[var(--radius-md)] border px-3 text-xs font-medium transition-token focus:outline-none focus-visible:ring-2 focus-visible:ring-ring",
                  filtersOpen || activeFilterCount > 0
                    ? "border-border-strong bg-surface-selected text-surface-selected-foreground"
                    : "border-border bg-surface text-foreground-muted hover:border-border-strong hover:bg-surface-hover hover:text-foreground",
                )}
              >
                <SlidersHorizontal className="h-3.5 w-3.5" aria-hidden />
                Filters
                {activeFilterCount > 0 && (
                  <Badge variant="secondary">{activeFilterCount}</Badge>
                )}
              </button>
            )}
          </div>

          {filtersOpen && mode === "dense" && (
            <aside
              id="search-filter-tray"
              aria-label="Search filters"
              className="border-b border-border-strong bg-surface px-3 py-3 sm:px-4"
            >
              {denseResults.length > 0 ? (
                <div
                  className={cn(
                    "grid gap-4",
                    availableTags.length > 0
                      ? "lg:grid-cols-2 xl:grid-cols-[minmax(16rem,0.8fr)_minmax(0,1.35fr)_minmax(15rem,1fr)]"
                      : "lg:grid-cols-[minmax(18rem,0.8fr)_minmax(0,1.6fr)]",
                  )}
                >
                  <fieldset>
                    <legend className="mb-2 text-xs font-semibold text-foreground">
                      Content
                    </legend>
                    <div
                      className="flex flex-wrap gap-1.5"
                      role="group"
                      aria-label="Filter by content kind"
                    >
                      {SOURCE_FILTERS.map(({ key, label, icon: Icon }) => {
                        const count =
                          key === "all"
                            ? denseResults.length
                            : sourceCounts[key].length;
                        return (
                          <button
                            key={key}
                            type="button"
                            aria-label={label}
                            aria-pressed={activeSource === key}
                            disabled={count === 0}
                            onClick={() => setActiveSource(key)}
                            className={sourceFilterClass(activeSource === key)}
                          >
                            <Icon className="h-3.5 w-3.5" aria-hidden />
                            {label}
                            <span className="tabular-nums text-foreground-muted">
                              {count}
                            </span>
                          </button>
                        );
                      })}
                    </div>
                  </fieldset>

                  <fieldset>
                    <legend className="mb-2 text-xs font-semibold text-foreground">
                      Document type
                    </legend>
                    <div
                      className="flex flex-wrap gap-1.5"
                      role="group"
                      aria-label="Filter by document type"
                    >
                      <button
                        type="button"
                        aria-label="Show all types"
                        aria-pressed={allTypesActive}
                        onClick={() => setActiveTypes(new Set(ALL_TYPES))}
                        className={filterButtonClass(allTypesActive)}
                      >
                        All types
                      </button>
                      {ALL_TYPES.map((type) => {
                        const count = denseResults.filter(
                          (result) => result.doc_type === type,
                        ).length;
                        return (
                          <button
                            key={type}
                            type="button"
                            aria-label={`Toggle ${type}`}
                            aria-pressed={activeTypes.has(type)}
                            disabled={count === 0}
                            onClick={() => toggleType(type)}
                            className={filterButtonClass(activeTypes.has(type))}
                          >
                            <span className="capitalize">{type}</span>
                            <span className="tabular-nums text-foreground-muted">
                              {count}
                            </span>
                          </button>
                        );
                      })}
                    </div>
                  </fieldset>

                  {availableTags.length > 0 && (
                    <fieldset>
                      <legend className="mb-2 flex items-center gap-1.5 text-xs font-semibold text-foreground">
                        <Tag className="h-3.5 w-3.5 text-foreground-muted" aria-hidden />
                        Tags
                      </legend>
                      <div
                        className="flex flex-wrap gap-1.5"
                        role="group"
                        aria-label="Filter by tag"
                      >
                        {availableTags.slice(0, 12).map(([tag, count]) => (
                          <button
                            key={tag}
                            type="button"
                            aria-label={`Toggle tag ${tag}`}
                            aria-pressed={activeTags.has(tag)}
                            onClick={() => toggleTag(tag)}
                            className={filterButtonClass(activeTags.has(tag))}
                          >
                            <span>{tag}</span>
                            <span className="tabular-nums text-foreground-muted">
                              {count}
                            </span>
                          </button>
                        ))}
                      </div>
                    </fieldset>
                  )}
                </div>
              ) : (
                <p className="text-xs text-foreground-muted">
                  Content and document-type filters appear after AKB finds
                  matches.
                </p>
              )}
            </aside>
          )}

          <div
            role="region"
            aria-label="Search results"
            data-testid="search-results-pane"
            className="min-h-[24rem] min-w-0 bg-surface"
          >
            {degraded && (
              <Alert
                variant="warning"
                className="rounded-none border-x-0 border-t-0"
              >
                Search is degraded, so these results may be incomplete. Try
                again shortly or switch to{" "}
                <button
                  type="button"
                  onClick={() => switchMode("literal")}
                  className={inlineActionClass}
                >
                  Literal
                </button>{" "}
                search.
              </Alert>
            )}

            {showLiteralHint && (
              <Alert
                variant="info"
                className="rounded-none border-x-0 border-t-0"
              >
                Short identifiers are usually easier to find with{" "}
                <button
                  type="button"
                  onClick={() => switchMode("literal")}
                  className={inlineActionClass}
                >
                  Literal
                </button>{" "}
                search.
              </Alert>
            )}

            {loading && <SearchLoadingState />}

            {error && !loading && (
              <Alert
                variant="destructive"
                title="Search unavailable"
                className="rounded-none border-x-0 border-t-0"
              >
                {error}.{" "}
                <button
                  type="button"
                  onClick={() => void doSearch(q, mode, scopeVaults)}
                  className={inlineActionClass}
                >
                  Retry
                </button>
              </Alert>
            )}

            {!searched && (
              <SearchStartState
                mode={mode}
                recentSearches={visibleRecentSearches}
                recentDocuments={visibleRecentDocuments}
                onRepeatSearch={repeatRecentSearch}
                onClearSearches={clearSearchHistory}
                onSuggestion={commitQuery}
              />
            )}

            {searched && !loading && !error && !hasRawResults && (
              <section aria-labelledby="no-search-results-heading">
                <div className="border-b border-border bg-surface-2/60 px-4 py-3 sm:px-5">
                  <h2
                    id="no-search-results-heading"
                    className="text-sm font-semibold text-foreground"
                  >
                    No results for “{q}”
                  </h2>
                  <p className="mt-1 text-xs leading-relaxed text-foreground-muted">
                    {mode === "dense"
                      ? "Try fewer terms, broaden the Vault scope, or look for the exact phrase."
                      : "Check the phrase or switch to meaning-based search."}
                  </p>
                </div>
                <div className="flex flex-wrap gap-2 px-4 py-4 sm:px-5">
                  <Button
                    type="button"
                    variant="outline"
                    size="sm"
                    onClick={() =>
                      switchMode(mode === "dense" ? "literal" : "dense")
                    }
                  >
                    Try {mode === "dense" ? "Literal" : "Semantic"}
                  </Button>
                  <Button
                    type="button"
                    variant="ghost"
                    size="sm"
                    onClick={() => commitQuery("")}
                  >
                    Clear query
                  </Button>
                </div>
              </section>
            )}

            {!loading && !error && hasRawResults && !hasResults && (
              <Alert
                variant="info"
                className="rounded-none border-x-0 border-t-0"
              >
                No results match the active filters.{" "}
                <button
                  type="button"
                  onClick={() => {
                    setActiveSource("all");
                    setActiveTypes(new Set(ALL_TYPES));
                    setActiveTags(new Set());
                  }}
                  className={inlineActionClass}
                >
                  Reset filters
                </button>
              </Alert>
            )}

            {truncated && hasResults && !loading && (
              <Alert
                variant="info"
                title="Showing the strongest matches"
                className="rounded-none border-x-0 border-t-0"
              >
                Semantic search ranks a focused result set. Refine the query, or
                use{" "}
                <button
                  type="button"
                  onClick={() => switchMode("literal")}
                  className={inlineActionClass}
                >
                  Literal
                </button>{" "}
                for an exact count.
              </Alert>
            )}

            {!loading && !error && hasResults && (
              <section aria-labelledby="search-results-heading">
                <div className="flex min-h-12 flex-wrap items-center justify-between gap-3 border-b border-border-strong bg-surface-2/60 px-4 py-2 sm:px-5">
                  <div className="flex min-w-0 flex-wrap items-baseline gap-x-2 gap-y-0.5">
                    <h2
                      id="search-results-heading"
                      className="text-sm font-semibold text-foreground"
                    >
                      Top matches
                    </h2>
                    <span className="max-w-full truncate text-sm text-foreground-muted">
                      for “{q}”
                    </span>
                  </div>
                  <div className="flex items-center gap-2">
                    <span className="text-xs tabular-nums text-foreground-muted">
                      {mode === "dense"
                        ? denseCountSummary
                        : literalCountSummary}
                    </span>
                    <Badge variant="outline">
                      {mode === "dense" ? (
                        <Sparkles className="h-3 w-3" aria-hidden />
                      ) : (
                        <Braces className="h-3 w-3" aria-hidden />
                      )}
                      {mode === "dense" ? "Semantic" : "Literal"}
                    </Badge>
                  </div>
                </div>
                {mode === "dense" ? (
                  <DenseResultList items={visibleDense} />
                ) : (
                  <LiteralResultList items={literalResults} />
                )}
              </section>
            )}
          </div>
        </section>
      </Panel>
    </div>
  );
}

function SearchLoadingState() {
  return (
    <div
      aria-busy="true"
      aria-label="Searching knowledge base"
      className="divide-y divide-border"
    >
      {[0, 1, 2].map((item) => (
        <div
          key={item}
          className="grid grid-cols-[2rem_minmax(0,1fr)] gap-3 px-4 py-4"
        >
          <Skeleton className="h-8 w-8 rounded-[var(--radius-md)]" />
          <div className="min-w-0 space-y-2">
            <Skeleton className="h-4 w-1/3" />
            <Skeleton className="h-3 w-3/5" />
            <Skeleton className="h-3 w-4/5" />
          </div>
        </div>
      ))}
    </div>
  );
}

function SearchStartState({
  mode,
  recentSearches,
  recentDocuments,
  onRepeatSearch,
  onClearSearches,
  onSuggestion,
}: {
  mode: Mode;
  recentSearches: RecentSearch[];
  recentDocuments: RecentDocumentView[];
  onRepeatSearch: (search: RecentSearch) => void;
  onClearSearches: () => void;
  onSuggestion: (query: string) => void;
}) {
  const location = useLocation();
  const visibleSearches = recentSearches.slice(0, 4);
  const visibleDocuments = recentDocuments.slice(0, 4);
  const hasSearches = visibleSearches.length > 0;
  const hasDocuments = visibleDocuments.length > 0;

  return (
    <div>
      {(hasSearches || hasDocuments) && (
        <div
          className={cn(
            "grid border-b border-border-strong bg-surface",
            hasSearches && hasDocuments && "lg:grid-cols-2",
          )}
        >
          {hasSearches && (
            <section aria-labelledby="recent-searches-heading">
              <div className="flex min-h-11 items-center justify-between gap-3 border-b border-border bg-surface-2/60 px-4 sm:px-5">
                <div className="flex min-w-0 items-center gap-2">
                  <Clock3 className="h-3.5 w-3.5 shrink-0 text-foreground-muted" aria-hidden />
                  <h2
                    id="recent-searches-heading"
                    className="text-xs font-semibold text-foreground"
                  >
                    Recent searches
                  </h2>
                </div>
                <button
                  type="button"
                  onClick={onClearSearches}
                  className="inline-flex h-8 cursor-pointer items-center rounded-[var(--radius-sm)] px-2 text-xs font-medium text-foreground-muted transition-token hover:bg-surface-hover hover:text-foreground focus:outline-none focus-visible:ring-2 focus-visible:ring-ring"
                >
                  Clear
                </button>
              </div>
              <div className="divide-y divide-border" aria-label="Recent searches">
                {visibleSearches.map((search) => (
                  <button
                    key={`${search.surface}:${search.mode}:${search.vaults.join(",")}:${search.query}`}
                    type="button"
                    onClick={() => onRepeatSearch(search)}
                    className="group flex min-h-14 w-full cursor-pointer items-center gap-3 px-4 py-2.5 text-left transition-token hover:bg-surface-hover focus:outline-none focus-visible:ring-2 focus-visible:ring-inset focus-visible:ring-ring sm:px-5"
                  >
                    <span className="flex h-8 w-8 shrink-0 items-center justify-center rounded-[var(--radius-md)] bg-surface-2 text-foreground-muted transition-colors group-hover:text-link">
                      <SearchIcon className="h-4 w-4" aria-hidden />
                    </span>
                    <span className="min-w-0 flex-1">
                      <span className="block truncate text-sm font-medium text-foreground">
                        {search.query}
                      </span>
                      <span className="mt-0.5 flex min-w-0 items-center gap-1.5 text-xs text-foreground-muted">
                        <span>{search.mode === "semantic" ? "Semantic" : "Literal"}</span>
                        <span aria-hidden>·</span>
                        <span className="truncate">
                          {search.vaults.length === 0
                            ? "All vaults"
                            : search.vaults.length === 1
                              ? search.vaults[0]
                              : `${search.vaults.length} vaults`}
                        </span>
                      </span>
                    </span>
                    <RelativeTime
                      iso={search.searchedAt}
                      className="hidden shrink-0 text-xs text-foreground-muted sm:block"
                    />
                  </button>
                ))}
              </div>
            </section>
          )}

          {hasDocuments && (
            <section
              aria-labelledby="recent-documents-heading"
              className={cn(hasSearches && "border-t border-border lg:border-l lg:border-t-0")}
            >
              <div className="flex min-h-11 items-center justify-between gap-3 border-b border-border bg-surface-2/60 px-4 sm:px-5">
                <div className="flex min-w-0 items-center gap-2">
                  <FileText className="h-3.5 w-3.5 shrink-0 text-foreground-muted" aria-hidden />
                  <h2
                    id="recent-documents-heading"
                    className="text-xs font-semibold text-foreground"
                  >
                    Recently viewed
                  </h2>
                </div>
                <span className="text-xs text-foreground-muted">This browser</span>
              </div>
              <div className="divide-y divide-border" aria-label="Recently viewed documents">
                {visibleDocuments.map((document, index) => {
                  const returnFocusId = `recent-search-document-${index}`;
                  return (
                    <Link
                      key={`${document.vault}:${document.path}`}
                      id={returnFocusId}
                      to={`/vault/${document.vault}/doc/${encodeURIComponent(document.path)}`}
                      state={documentPreviewState(location, returnFocusId)}
                      className="group flex min-h-14 items-center gap-3 px-4 py-2.5 transition-token hover:bg-surface-hover focus:outline-none focus-visible:ring-2 focus-visible:ring-inset focus-visible:ring-ring sm:px-5"
                    >
                      <span className="flex h-8 w-8 shrink-0 items-center justify-center rounded-[var(--radius-md)] bg-surface-2 text-foreground-muted transition-colors group-hover:text-link">
                        <FileText className="h-4 w-4" aria-hidden />
                      </span>
                      <span className="min-w-0 flex-1">
                        <span className="block truncate text-sm font-medium text-foreground transition-colors group-hover:text-link">
                          {document.title}
                        </span>
                        <span className="mt-0.5 block truncate text-xs text-foreground-muted">
                          {document.vault} · {document.path}
                        </span>
                      </span>
                      <RelativeTime
                        iso={document.viewedAt}
                        className="hidden shrink-0 text-xs text-foreground-muted sm:block"
                      />
                    </Link>
                  );
                })}
              </div>
            </section>
          )}
        </div>
      )}

      <section>
        <div className="flex min-h-11 items-center justify-between gap-3 border-b border-border bg-surface-2/60 px-4 sm:px-5">
          <div>
            <h2
              id="search-start-heading"
              className="text-xs font-semibold text-foreground"
            >
              Suggested searches
            </h2>
            <p className="mt-0.5 hidden text-xs text-foreground-muted sm:block">
              Start with an outcome, topic, or question.
            </p>
          </div>
          <span className="text-xs text-foreground-muted">
            {mode === "dense" ? "Meaning and context" : "Exact text or regex"}
          </span>
        </div>
        <div
          aria-label="Suggested searches"
          className="grid divide-y divide-border sm:grid-cols-3 sm:divide-x sm:divide-y-0"
        >
          {SUGGESTED_QUERIES.map((suggestion) => (
            <button
              key={suggestion}
              type="button"
              aria-label={suggestion}
              onClick={() => onSuggestion(suggestion)}
              className="group flex min-h-14 w-full cursor-pointer items-center gap-3 px-4 text-left transition-token hover:bg-surface-hover focus:outline-none focus-visible:ring-2 focus-visible:ring-inset focus-visible:ring-ring sm:px-5"
            >
              <SearchIcon
                className="h-4 w-4 shrink-0 text-foreground-muted"
                aria-hidden
              />
              <span className="min-w-0 flex-1 truncate text-sm text-foreground">
                {suggestion}
              </span>
              <ArrowUpRight
                className="hidden h-3.5 w-3.5 shrink-0 text-foreground-muted transition-colors group-hover:text-link lg:block"
                aria-hidden
              />
            </button>
          ))}
        </div>
      </section>
    </div>
  );
}

function DenseResultList({ items }: { items: DenseResult[] }) {
  const location = useLocation();

  return (
    <ol
      className="divide-y divide-border bg-surface"
      aria-label="Semantic search results"
    >
      {items.map((result, index) => {
        const sourceType = result.source_type || "document";
        const { icon: SourceIcon, label: sourceLabel } = TYPE_META[sourceType];
        const detail = cleanSearchContext(
          result.matched_section || result.summary,
        );
        const tags = safeSearchTags(result.tags);
        return (
          <li key={result.uri}>
            <Link
              id={`search-dense-result-${index}`}
              to={resultHref(result)}
              state={
                sourceType === "document"
                  ? documentPreviewState(
                      location,
                      `search-dense-result-${index}`,
                    )
                  : undefined
              }
              className="group grid grid-cols-[2rem_minmax(0,1fr)] gap-3 px-4 py-3 transition-token hover:bg-surface-hover focus:outline-none focus-visible:ring-2 focus-visible:ring-inset focus-visible:ring-ring sm:grid-cols-[2rem_minmax(0,1fr)_auto] sm:px-5"
            >
              <span className="flex h-8 w-8 items-center justify-center rounded-[var(--radius-md)] bg-surface-2 text-foreground-muted transition-colors group-hover:text-link">
                <SourceIcon className="h-4 w-4" aria-hidden />
              </span>

              <div className="min-w-0">
                <div className="flex min-w-0 flex-wrap items-center gap-2">
                  <span
                    className="text-xs tabular-nums text-subtle"
                    aria-label={`Result ${index + 1}`}
                  >
                    {index + 1}
                  </span>
                  <span className="text-sm font-semibold text-foreground transition-colors group-hover:text-link">
                    {result.title}
                  </span>
                  {result.doc_type && (
                    <Badge variant="outline">{result.doc_type}</Badge>
                  )}
                  {tags.slice(0, 2).map((tag) => (
                    <Badge key={tag} variant="secondary">
                      {tag}
                    </Badge>
                  ))}
                  {tags.length > 2 && (
                    <span className="text-xs tabular-nums text-foreground-muted">
                      +{tags.length - 2}
                    </span>
                  )}
                  {index === 0 && <Badge variant="secondary">Top match</Badge>}
                </div>
                <div className="mt-1 flex min-w-0 flex-wrap items-center gap-x-2 gap-y-1 text-xs text-foreground-muted">
                  <span>{sourceLabel}</span>
                  <span aria-hidden>·</span>
                  <span>{result.vault}</span>
                  <span aria-hidden>·</span>
                  <span className="min-w-0 [overflow-wrap:anywhere]">
                    {result.path}
                  </span>
                </div>
                {detail && (
                  <p className="mt-1.5 line-clamp-2 whitespace-pre-line text-xs leading-relaxed text-foreground-muted">
                    {detail}
                  </p>
                )}
              </div>

              <div className="hidden items-start gap-1.5 pt-0.5 text-xs font-medium text-foreground-muted transition-colors group-hover:text-link sm:flex">
                {sourceType === "document" ? "Preview" : "Open"}
                <ArrowUpRight className="h-3.5 w-3.5" aria-hidden />
              </div>
            </Link>
          </li>
        );
      })}
    </ol>
  );
}

function LiteralResultList({ items }: { items: GrepDoc[] }) {
  const location = useLocation();

  return (
    <ol
      className="divide-y divide-border bg-surface"
      aria-label="Literal search results"
    >
      {items.map((result, index) => (
        <li key={result.uri}>
          <Link
            id={`search-literal-result-${index}`}
            to={`/vault/${result.vault}/doc/${encodeURIComponent(
              parseUri(result.uri)?.id ?? result.path,
            )}`}
            state={documentPreviewState(
              location,
              `search-literal-result-${index}`,
            )}
            className="group grid grid-cols-[2rem_minmax(0,1fr)] gap-3 px-4 py-3 transition-token hover:bg-surface-hover focus:outline-none focus-visible:ring-2 focus-visible:ring-inset focus-visible:ring-ring sm:grid-cols-[2rem_minmax(0,1fr)_6.5rem] sm:px-5"
          >
            <span className="flex h-8 w-8 items-center justify-center rounded-[var(--radius-md)] bg-surface-2 text-foreground-muted transition-colors group-hover:text-link">
              <FileText className="h-4 w-4" aria-hidden />
            </span>

            <div className="min-w-0">
              <div className="flex min-w-0 flex-wrap items-center gap-2">
                <span
                  className="text-xs tabular-nums text-subtle"
                  aria-label={`Result ${index + 1}`}
                >
                  {index + 1}
                </span>
                <span className="text-sm font-semibold text-foreground transition-colors group-hover:text-link">
                  {result.title}
                </span>
                <span className="ml-auto text-xs font-semibold tabular-nums text-foreground sm:hidden">
                  {result.matches.length}
                </span>
              </div>
              <div className="mt-1 flex min-w-0 flex-wrap items-center gap-x-2 gap-y-1 text-xs text-foreground-muted">
                <span>Document</span>
                <span aria-hidden>·</span>
                <span>{result.vault}</span>
                <span aria-hidden>·</span>
                <span className="min-w-0 [overflow-wrap:anywhere]">
                  {result.path}
                </span>
              </div>

              {result.matches.length > 0 && (
                <div className="mt-2 space-y-1">
                  {result.matches.slice(0, 2).map((match, matchIndex) => (
                    <div
                      key={`${match.section || "match"}-${matchIndex}`}
                      className="rounded-[var(--radius-sm)] border border-border bg-surface-2 px-3 py-1.5"
                    >
                      {match.section && (
                        <div className="text-xs font-medium text-foreground">
                          {match.section}
                        </div>
                      )}
                      <pre className="mt-0.5 line-clamp-2 whitespace-pre-wrap font-mono text-xs leading-relaxed text-foreground-muted">
                        {match.text}
                      </pre>
                    </div>
                  ))}
                  {result.matches.length > 2 && (
                    <div className="text-xs tabular-nums text-foreground-muted">
                      +{result.matches.length - 2} more matches
                    </div>
                  )}
                </div>
              )}
            </div>

            <div className="hidden min-w-0 items-start justify-end gap-2 pt-0.5 text-xs text-foreground-muted sm:flex">
              <span className="tabular-nums">
                {result.matches.length}{" "}
                {result.matches.length === 1 ? "match" : "matches"}
              </span>
              <ArrowUpRight
                className="h-3.5 w-3.5 transition-colors group-hover:text-link"
                aria-hidden
              />
            </div>
          </Link>
        </li>
      ))}
    </ol>
  );
}

function searchModeClass(active: boolean) {
  return cn(
    "inline-flex h-8 cursor-pointer items-center gap-1.5 rounded-[var(--radius-sm)] px-2.5 text-xs font-medium transition-token focus:outline-none focus-visible:ring-2 focus-visible:ring-ring",
    active
      ? "bg-surface-selected text-surface-selected-foreground shadow-xs"
      : "text-foreground-muted hover:bg-surface-hover hover:text-foreground",
  );
}

function sourceFilterClass(active: boolean) {
  return cn(
    "inline-flex h-8 cursor-pointer items-center gap-2 rounded-[var(--radius-sm)] border px-2.5 text-xs font-medium transition-token focus:outline-none focus-visible:ring-2 focus-visible:ring-ring disabled:cursor-default disabled:opacity-50",
    active
      ? "border-border-strong bg-surface-selected text-surface-selected-foreground"
      : "border-border bg-surface text-foreground-muted hover:border-border-strong hover:bg-surface-hover hover:text-foreground",
  );
}

function filterButtonClass(active: boolean) {
  return cn(
    "inline-flex h-8 cursor-pointer items-center gap-2 rounded-[var(--radius-sm)] border px-2.5 text-xs font-medium transition-token focus:outline-none focus-visible:ring-2 focus-visible:ring-ring disabled:cursor-default disabled:opacity-50",
    active
      ? "border-border-strong bg-surface-selected text-surface-selected-foreground"
      : "border-border bg-surface text-foreground-muted hover:border-border-strong hover:bg-surface-hover hover:text-foreground",
  );
}

function groupByType(
  results: DenseResult[],
): Record<SourceType, DenseResult[]> {
  const groups: Record<SourceType, DenseResult[]> = {
    document: [],
    table: [],
    file: [],
  };
  for (const result of results) {
    const type = (result.source_type || "document") as SourceType;
    if (groups[type]) groups[type].push(result);
    else groups.document.push(result);
  }
  return groups;
}
