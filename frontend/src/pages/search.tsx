import { useEffect, useRef, useState } from "react";
import { Link, useParams, useSearchParams } from "react-router-dom";
import { ExternalLink, File, FileText, Search as SearchIcon, Sparkles, Table } from "lucide-react";
import { searchDocs, grepDocs, listVaults, type GrepDoc } from "@/lib/api";
import { Alert } from "@/components/ui/alert";
import { Button } from "@/components/ui/button";
import { Skeleton } from "@/components/ui/skeleton";
import { Badge } from "@/components/ui/badge";
import { SelectMenu } from "@/components/ui/select-menu";
import { TooltipText } from "@/components/ui/tooltip-text";
import { EmptyState } from "@/components/empty-state";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { parseUri } from "@/lib/uri";
import { cn } from "@/lib/utils";

type Mode = "dense" | "literal";
type SourceType = "document" | "table" | "file";

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

interface DenseResult {
  source_type?: SourceType;
  // Canonical handle. As of backend 0.3.0 the form is
  // `akb://{vault}[/coll/{coll_path}]/{doc|table|file}/{identifier}` —
  // routing decisions here parse the URI tail.
  uri: string;
  vault: string;
  path: string;
  title: string;
  // Containing-collection path (null at vault root). Surfaced
  // explicitly by backend 0.3.0 so clients group/filter hits
  // without parsing the URI themselves.
  collection?: string | null;
  doc_type?: string;
  summary?: string;
  matched_section?: string;
  score: number;
}

function resultHref(r: DenseResult): string {
  const type = r.source_type || "document";
  if (type === "table") {
    // 0.3.0+: backend emits `path` as the bare table name (the
    // pre-0.3.0 synthetic `_tables/<name>` prefix was removed
    // because the `type`+`uri` fields already disambiguate kind).
    // Fall back to `r.title` for ancient cached responses that
    // might still carry the legacy shape.
    const name = r.path || r.title;
    return `/vault/${r.vault}/table/${encodeURIComponent(name)}`;
  }
  const parsed = parseUri(r.uri);
  if (type === "file") {
    return `/vault/${r.vault}/file/${parsed?.id ?? ""}`;
  }
  // URL-encode the doc path so a hierarchical id like `incidents/foo.md`
  // survives as a single React Router param.
  const docPath = parsed?.id ?? r.path;
  return `/vault/${r.vault}/doc/${encodeURIComponent(docPath)}`;
}

const TYPE_META: Record<
  SourceType,
  { label: string; icon: React.ComponentType<{ className?: string; "aria-hidden"?: boolean }> }
> = {
  document: { label: "Document", icon: FileText },
  table: { label: "Table", icon: Table },
  file: { label: "File", icon: File },
};

export default function SearchPage() {
  const [searchParams, setSearchParams] = useSearchParams();
  // `/vault/:name/search` routes the vault in via URL params — that
  // scope is implicit and cannot be changed from the scope picker
  // (you'd navigate to /search for cross-vault). The legacy `?v=`
  // query param is honored only on the global `/search` route.
  const { name: scopedVault } = useParams<{ name: string }>();
  const q = searchParams.get("q") || "";
  // Sanitize instead of a bare cast: an unknown ?mode= must fall back to dense,
  // not slip through as a truthy non-dense value that routes to grep with
  // neither toggle highlighted.
  const mode: Mode = searchParams.get("mode") === "literal" ? "literal" : "dense";
  const queryVault = searchParams.get("v") || "";
  const vault = scopedVault || queryVault;

  const [denseResults, setDenseResults] = useState<DenseResult[]>([]);
  const [literalResults, setLiteralResults] = useState<GrepDoc[]>([]);
  const [total, setTotal] = useState(0);
  const [totalMatches, setTotalMatches] = useState(0);
  // grep returns both doc-count and line-count; track the "what was
  // shown" side separately so the header can honestly say "N of M
  // docs · K of L matches" instead of pretending the response is the
  // whole population. Defaults to total* when the backend doesn't
  // distinguish (older grep, or pre-0.2.4 servers).
  const [returnedDocs, setReturnedDocs] = useState(0);
  const [returnedMatches, setReturnedMatches] = useState(0);
  const [truncated, setTruncated] = useState(false);
  const [degraded, setDegraded] = useState(false);
  const [loading, setLoading] = useState(false);
  const [searched, setSearched] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [vaults, setVaults] = useState<{ name: string }[]>([]);
  const [activeTypes, setActiveTypes] = useState<Set<DocTypeFilter>>(new Set(ALL_TYPES));
  // Epoch guard: a superseded (slower) response must not clobber a newer one.
  const reqId = useRef(0);

  function toggleType(t: DocTypeFilter) {
    setActiveTypes((prev) => {
      const next = new Set(prev);
      if (next.has(t)) next.delete(t);
      else next.add(t);
      return next;
    });
  }

  // Input-local query draft — separate from the committed URL `q` so
  // keystrokes don't fire a search per character. Submit (Enter / click)
  // pushes it into the URL, which drives the search via the useEffect.
  const [draft, setDraft] = useState(q);
  useEffect(() => { setDraft(q); }, [q]);

  useEffect(() => {
    if (!scopedVault) {
      listVaults().then((d) => setVaults(d.vaults || [])).catch(() => {});
    }
  }, [scopedVault]);

  useEffect(() => {
    if (q) doSearch(q, mode, vault);
    // Bump the epoch on cleanup so an in-flight resolve after a param change
    // or unmount is ignored (reqId is a request counter, not a DOM ref).
    // eslint-disable-next-line react-hooks/exhaustive-deps
    return () => { reqId.current++; };
  }, [q, mode, vault]);

  async function doSearch(s: string, m: Mode, v: string) {
    if (!s.trim()) return;
    const id = ++reqId.current;
    setLoading(true);
    setSearched(true);
    setError(null);
    try {
      if (m === "dense") {
        // Web shows a fuller page than the agent default (10). 25 stays under
        // the server-side ceiling (search_limit_max, 50).
        const d = await searchDocs(s, v || undefined, 25);
        if (id !== reqId.current) return; // superseded
        setDenseResults(d.results);
        setLiteralResults([]);
        setTotal(d.total ?? d.results.length);
        setTotalMatches(d.total_matches);
        setReturnedDocs(d.returned);
        setReturnedMatches(0);
        setTruncated(Boolean(d.truncated));
        setDegraded(Boolean(d.degraded));
      } else {
        const d = await grepDocs(s, v || undefined);
        if (id !== reqId.current) return;
        setLiteralResults(d.results);
        setDenseResults([]);
        setTotal(d.total_docs ?? d.results.length);
        setTotalMatches(d.total_matches);
        // Older servers (< 0.2.4) don't ship returned_*; fall back to
        // total_* so the header still renders a sensible single count.
        setReturnedDocs(d.returned_docs ?? d.total_docs);
        setReturnedMatches(d.returned_matches ?? d.total_matches);
        setTruncated(Boolean(d.truncated));
        setDegraded(false); // literal/grep uses SQL, not the vector store
      }
    } catch (e) {
      if (id !== reqId.current) return;
      // Surface the failure as a distinct error state instead of masking it
      // as "no results".
      setError(e instanceof Error ? e.message : "Search failed");
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
  }

  // The mode/vault toggles must work even before a query is committed: persist
  // the choice (so a later submit uses it) and, if the input already has text,
  // commit that draft too so results update immediately. Mirrors submitDraft's
  // param handling (preserve siblings; omit mode when dense, q when empty).
  function switchMode(m: Mode) {
    const next = new URLSearchParams(searchParams);
    const trimmed = draft.trim();
    if (trimmed) next.set("q", trimmed);
    if (m === "dense") next.delete("mode");
    else next.set("mode", m);
    setSearchParams(next, { replace: true });
  }

  function switchVault(v: string) {
    const next = new URLSearchParams(searchParams);
    const trimmed = draft.trim();
    if (trimmed) next.set("q", trimmed);
    if (v) next.set("v", v);
    else next.delete("v");
    setSearchParams(next, { replace: true });
  }

  const isShortQuery = q.trim().length > 0 && q.trim().length <= 6 && !/\s/.test(q.trim());
  const showLiteralHint = mode === "dense" && isShortQuery && searched && !loading;

  const allTypesActive = activeTypes.size === ALL_TYPES.length;
  const filteredDense = allTypesActive
    ? denseResults
    : denseResults.filter(
        (r) => !r.doc_type || activeTypes.has(r.doc_type as DocTypeFilter),
      );
  const groupedDense = groupByType(filteredDense);
  // Drive render gates off actual array length, not the count field (a falsy
  // `total` from a legacy response otherwise renders neither list nor empty).
  const resultCount = mode === "dense" ? filteredDense.length : literalResults.length;
  const hasResults = resultCount > 0;

  // Commit the draft into the URL. Empty queries clear everything.
  // Keep the current mode/scope — the mode toggle handles that separately.
  const submitDraft = () => {
    const trimmed = draft.trim();
    const next = new URLSearchParams(searchParams);
    if (trimmed) next.set("q", trimmed);
    else next.delete("q");
    setSearchParams(next, { replace: true });
  };

  return (
    <div className="fade-up max-w-[1280px] mx-auto">
      {/* Polite live region — announces searching / results / no-results / error. */}
      <p role="status" aria-live="polite" className="sr-only">
        {loading
          ? "Searching…"
          : error
            ? "Search failed"
            : !searched
              ? ""
              : !hasResults
                ? `No results for ${q}`
                : `${resultCount} results for ${q}`}
      </p>
      <div className="coord-spark mb-3">Search</div>
      <h1 className="font-display text-3xl tracking-tight text-foreground mb-6">
        {scopedVault ? scopedVault : "Query the base"}
      </h1>

      {/* Doc-type filter chips — client-side filter on doc_type field of
          dense results. ALL resets everything; individual chips toggle
          inclusion. Chips are only meaningful in dense (semantic) mode
          where doc_type is present, but we always render them so the
          user's filter survives a mode switch. */}
      <div role="group" aria-label="Filter by document type" className="flex flex-wrap gap-1 mb-4">
        <button
          type="button"
          aria-label="Show all types"
          aria-pressed={allTypesActive}
          onClick={() => setActiveTypes(new Set(ALL_TYPES))}
          className={cn(
            "px-2 h-7 rounded-[var(--radius-md)] border text-xs focus:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2 focus-visible:ring-offset-background",
            allTypesActive
              ? "border-transparent bg-surface-selected text-surface-selected-foreground"
              : "border-border bg-surface text-foreground-muted hover:bg-surface-hover",
          )}
        >
          All
        </button>
        {ALL_TYPES.map((t) => (
          <button
            key={t}
            type="button"
            aria-label={`Toggle ${t}`}
            aria-pressed={activeTypes.has(t)}
            onClick={() => toggleType(t)}
            className={cn(
              "inline-flex items-center gap-1 px-2 h-7 rounded-[var(--radius-md)] border text-xs focus:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2 focus-visible:ring-offset-background",
              activeTypes.has(t)
                ? "border-transparent bg-surface-selected text-surface-selected-foreground"
                : "border-border bg-surface text-foreground-muted hover:bg-surface-hover",
            )}
          >
            {t === "skill" && <Sparkles className="h-3 w-3" aria-hidden />}
            {t}
          </button>
        ))}
      </div>

      {/* Inline search form — mode toggle + editable input. The mode
          buttons update the URL immediately (no need to press Enter);
          the text input commits on submit. */}
      <form
        className="flex h-11 mb-4"
        onSubmit={(e) => {
          e.preventDefault();
          submitDraft();
        }}
        role="search"
        aria-label={scopedVault ? `Search within ${scopedVault}` : "Search all vaults"}
      >
        <div className="flex border border-border border-r-0 h-full shrink-0 rounded-l-[var(--radius-md)] overflow-hidden">
          <button
            type="button"
            onClick={() => switchMode("dense")}
            aria-pressed={mode === "dense"}
            title="Semantic hybrid search (dense + BM25 + cross-encoder rerank)"
            className={`px-3 h-full font-medium text-xs transition-token cursor-pointer focus:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-inset ${
              mode === "dense"
                ? "bg-surface-selected text-surface-selected-foreground"
                : "text-foreground hover:bg-surface-hover"
            }`}
          >
            Semantic
          </button>
          <button
            type="button"
            onClick={() => switchMode("literal")}
            aria-pressed={mode === "literal"}
            title="Literal substring / regex search"
            className={`px-3 h-full font-medium text-xs transition-token cursor-pointer focus:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-inset ${
              mode === "literal"
                ? "bg-surface-selected text-surface-selected-foreground"
                : "text-foreground hover:bg-surface-hover"
            }`}
          >
            Literal
          </button>
        </div>
        <div className="relative flex-1 min-w-0 flex items-center border border-border h-full px-3 focus-within:border-primary focus-within:ring-2 focus-within:ring-ring transition-token bg-surface">
          <SearchIcon
            className="h-4 w-4 text-foreground-muted mr-2 pointer-events-none shrink-0"
            aria-hidden
          />
          <label className="sr-only" htmlFor="vault-search">Query</label>
          <input
            id="vault-search"
            type="search"
            autoFocus
            placeholder={
              scopedVault
                ? `Search in ${scopedVault}…`
                : mode === "dense"
                  ? "Search all vaults (semantic)…"
                  : "Search all vaults (literal)…"
            }
            value={draft}
            onChange={(e) => setDraft(e.target.value)}
            className="flex-1 min-w-0 bg-transparent text-sm text-foreground placeholder:text-foreground-muted focus:outline-none"
          />
        </div>
        <Button
          type="submit"
          variant="accent"
          loading={loading}
          className="h-full shrink-0 rounded-l-none text-xs"
        >
          Search
        </Button>
      </form>

      {scopedVault && (
        <div className="flex items-center gap-3 text-xs mb-6">
          <span className="coord">Scope</span>
          <span className="text-foreground">{scopedVault}</span>
          <Link
            to={`/search${q ? `?q=${encodeURIComponent(q)}${mode !== "dense" ? `&mode=${mode}` : ""}` : ""}`}
            className="ml-auto inline-flex items-center gap-1 text-foreground-muted hover:text-link transition-colors rounded-[var(--radius-sm)] focus:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2 focus-visible:ring-offset-background"
          >
            <ExternalLink className="h-3 w-3" aria-hidden />
            Search all vaults
          </Link>
        </div>
      )}

      {!scopedVault && vaults.length > 0 && (
        <div className="flex items-center gap-3 mb-6">
          <span className="coord shrink-0">Scope</span>
          <SelectMenu
            value={vault}
            onValueChange={switchVault}
            aria-label="Search scope — limit to a vault"
            className="h-9 w-auto min-w-[220px] max-w-sm"
            options={[
              { value: "", label: `All vaults (${vaults.length})` },
              ...vaults.map((v) => ({ value: v.name, label: v.name })),
            ]}
          />
          {vault && (
            <button
              onClick={() => switchVault("")}
              className="coord hover:text-link transition-token cursor-pointer rounded-[var(--radius-sm)] focus:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2 focus-visible:ring-offset-background"
            >
              clear
            </button>
          )}
        </div>
      )}

      {degraded && (
        <Alert variant="warning" className="mb-4">
          Search is degraded — the retrieval index hit a transient issue, so these
          results may be incomplete. This isn't a "no matches" result; try again
          shortly, or switch to{" "}
          <button
            onClick={() => switchMode("literal")}
            className="underline font-medium hover:text-link cursor-pointer rounded-[var(--radius-sm)] focus:outline-none focus-visible:ring-2 focus-visible:ring-ring"
          >
            Literal
          </button>{" "}
          search.
        </Alert>
      )}

      {showLiteralHint && (
        <Alert variant="info" className="mb-4">
          Short single-token queries often work better in{" "}
          <button
            onClick={() => switchMode("literal")}
            className="underline font-medium hover:text-link cursor-pointer rounded-[var(--radius-sm)] focus:outline-none focus-visible:ring-2 focus-visible:ring-ring"
          >
            Literal
          </button>{" "}
          mode.
        </Alert>
      )}

      {loading && (
        <div className="rounded-[var(--radius-lg)] border border-border p-6 bg-surface shadow-sm space-y-3" aria-busy="true">
          <Skeleton className="h-4 w-48" />
          <Skeleton className="h-16" />
          <Skeleton className="h-16" />
          <Skeleton className="h-16" />
          <div className="coord text-center pt-2">Reranking…</div>
        </div>
      )}

      {error && !loading && (
        <Alert variant="destructive" className="mt-6">
          Search failed — {error}.{" "}
          <button
            onClick={() => doSearch(q, mode, vault)}
            className="underline font-medium hover:text-link cursor-pointer rounded-[var(--radius-sm)] focus:outline-none focus-visible:ring-2 focus-visible:ring-ring"
          >
            Retry
          </button>
        </Alert>
      )}

      {searched && !loading && !error && !hasResults && total === 0 && (
        <EmptyState
          title="No results"
          description={
            mode === "dense"
              ? "Try Literal mode for exact substring matching."
              : "Try Semantic mode for meaning-based search."
          }
          action={
            <button
              onClick={() => switchMode(mode === "dense" ? "literal" : "dense")}
              className="underline text-sm hover:text-link cursor-pointer rounded-[var(--radius-sm)] focus:outline-none focus-visible:ring-2 focus-visible:ring-ring"
            >
              Switch to {mode === "dense" ? "Literal" : "Semantic"}
            </button>
          }
        />
      )}

      {/* Type filter narrowed the dense results to zero (but the query did match). */}
      {!loading && !error && mode === "dense" && total > 0 && filteredDense.length === 0 && (
        <Alert variant="info" className="mt-6">
          No results match the active type filters.{" "}
          <button
            onClick={() => setActiveTypes(new Set(ALL_TYPES))}
            className="underline font-medium hover:text-link cursor-pointer rounded-[var(--radius-sm)] focus:outline-none focus-visible:ring-2 focus-visible:ring-ring"
          >
            Reset filters
          </button>
        </Alert>
      )}

      {/* Truncated = the prefetch pool was capped (semantic search is top-K,
          not an exhaustive scan). The backend `hint` is written for agents
          (suggests akb_grep count_only); on the web a short, calm note reads
          better than the full tooling sentence. */}
      {truncated && hasResults && !loading && (
        <Alert variant="info" title="Showing the most relevant matches" className="mt-6">
          Semantic search returns the top matches, not an exhaustive list — there
          may be more. Refine your query, or use{" "}
          <button
            onClick={() => switchMode("literal")}
            className="underline font-medium hover:text-link cursor-pointer rounded-[var(--radius-sm)] focus:outline-none focus-visible:ring-2 focus-visible:ring-ring"
          >
            Literal
          </button>{" "}
          search for an exact count.
        </Alert>
      )}

      {mode === "dense" && filteredDense.length > 0 && (
        <Tabs defaultValue="all" className="mt-6">
          <TabsList>
            <TabsTrigger value="all" className="gap-1.5">
              All
              <span className="coord tabular-nums">[{filteredDense.length}]</span>
            </TabsTrigger>
            {(["document", "table", "file"] as const).map((type) => {
              const group = groupedDense[type] || [];
              if (group.length === 0) return null;
              return (
                <TabsTrigger key={type} value={type} className="gap-1.5">
                  {TYPE_META[type].label}s
                  <span className="coord tabular-nums">[{group.length}]</span>
                </TabsTrigger>
              );
            })}
          </TabsList>

          {/* All — every hit in score order, types interleaved. */}
          <TabsContent value="all" className="pt-4">
            <DenseResultList items={filteredDense} />
          </TabsContent>

          {/* Per-type — same row template, filtered. */}
          {(["document", "table", "file"] as const).map((type) => {
            const group = groupedDense[type] || [];
            if (group.length === 0) return null;
            return (
              <TabsContent key={type} value={type} className="pt-4">
                <DenseResultList items={group} />
              </TabsContent>
            );
          })}
        </Tabs>
      )}

      {mode === "literal" && literalResults.length > 0 && (
        <section className="rounded-[var(--radius-lg)] overflow-hidden border border-border bg-surface shadow-sm mt-6" aria-label="Literal results">
          <header className="border-b border-border px-4 py-2 flex items-baseline justify-between gap-3 flex-wrap">
            <span className="coord-ink">Results · Literal</span>
            <span className="coord tabular-nums">
              {returnedDocs !== total || returnedMatches !== totalMatches
                ? `[${returnedDocs} of ${total} docs · ${returnedMatches} of ${totalMatches} matches]`
                : `[${total} docs · ${totalMatches} matches]`}
            </span>
          </header>
          <ol className="divide-y divide-border">
            {literalResults.map((r, i) => (
              <li key={r.uri}>
                <Link
                  to={`/vault/${r.vault}/doc/${encodeURIComponent(parseUri(r.uri)?.id ?? r.path)}`}
                  className="block px-5 py-4 group hover:bg-surface-hover transition-colors focus:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2 focus-visible:ring-offset-surface"
                >
                  <div className="grid grid-cols-[32px_1fr_60px] gap-4 items-baseline">
                    <span className="coord tabular-nums">
                      {String(i + 1).padStart(2, "0")}
                    </span>
                    <div className="min-w-0">
                      <TooltipText as="div" className="text-base font-medium tracking-tight text-foreground group-hover:text-link truncate">
                        {r.title}
                      </TooltipText>
                      <TooltipText as="div" className="coord mt-0.5 truncate" tip={`${r.vault} / ${r.path}`}>
                        {r.vault} / {r.path}
                      </TooltipText>
                      {r.matches.length > 0 && (
                        <div className="mt-2 space-y-1">
                          {r.matches.slice(0, 3).map((m, j) => (
                            <pre
                              key={j}
                              className="text-xs font-mono whitespace-pre-wrap text-foreground-muted border-l-2 border-link pl-3 line-clamp-2"
                            >
                              {m.text}
                            </pre>
                          ))}
                          {r.matches.length > 3 && (
                            <div className="coord tabular-nums">
                              +{r.matches.length - 3} more
                            </div>
                          )}
                        </div>
                      )}
                    </div>
                    <span className="coord-spark text-right tabular-nums">
                      ×{r.matches.length}
                    </span>
                  </div>
                </Link>
              </li>
            ))}
          </ol>
        </section>
      )}
    </div>
  );
}

function DenseResultList({ items }: { items: DenseResult[] }) {
  return (
    <ol className="rounded-[var(--radius-lg)] overflow-hidden border border-border bg-surface shadow-sm divide-y divide-border">
      {items.map((r, i) => (
        <li key={r.uri}>
          <Link
            to={resultHref(r)}
            className="block px-5 py-4 group hover:bg-surface-hover transition-colors focus:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2 focus-visible:ring-offset-surface"
          >
            <div className="grid grid-cols-[32px_1fr_60px] gap-4 items-baseline">
              <span className="coord tabular-nums">
                {String(i + 1).padStart(2, "0")}
              </span>
              <div className="min-w-0">
                <div className="flex items-baseline gap-2 flex-wrap">
                  <span className="text-base font-medium tracking-tight text-foreground group-hover:text-link">
                    {r.title}
                  </span>
                  {r.doc_type && <Badge variant="outline">{r.doc_type}</Badge>}
                </div>
                <div className="coord mt-0.5">
                  {r.vault}
                  {r.collection && <> · <span className="text-foreground-muted">{r.collection}</span></>}
                  {" / "}{r.path}
                </div>
                {r.summary && (
                  <p className="text-sm text-foreground-muted mt-2 line-clamp-2">
                    {r.summary}
                  </p>
                )}
                {r.matched_section && (
                  <pre className="mt-2 text-xs font-mono whitespace-pre-wrap text-foreground-muted line-clamp-3 border-l-2 border-link pl-3">
                    {r.matched_section}
                  </pre>
                )}
              </div>
              <span className="coord-spark text-right tabular-nums">
                {(r.score * 100).toFixed(0)}%
              </span>
            </div>
          </Link>
        </li>
      ))}
    </ol>
  );
}

function groupByType(results: DenseResult[]): Record<SourceType, DenseResult[]> {
  const groups: Record<SourceType, DenseResult[]> = {
    document: [],
    table: [],
    file: [],
  };
  for (const r of results) {
    const t = (r.source_type || "document") as SourceType;
    if (groups[t]) groups[t].push(r);
    else groups.document.push(r);
  }
  return groups;
}
