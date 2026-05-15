import { useEffect, useState } from "react";
import { Link, useParams, useSearchParams } from "react-router-dom";
import { ExternalLink, File, FileText, Lightbulb, Search as SearchIcon, Table } from "lucide-react";
import { searchDocs, grepDocs, listVaults, type GrepDoc } from "@/lib/api";
import { Skeleton } from "@/components/ui/skeleton";
import { Badge } from "@/components/ui/badge";
import { EmptyState } from "@/components/empty-state";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { parseUri } from "@/lib/uri";

type Mode = "dense" | "literal";
type SourceType = "document" | "table" | "file";

interface DenseResult {
  source_type?: SourceType;
  // Canonical handle — `akb://{vault}/{doc|table|file}/{identifier}`.
  // Backend dropped the legacy `source_id`/`doc_id`/`file_id` shape;
  // routing decisions here parse the URI tail.
  uri: string;
  vault: string;
  path: string;
  title: string;
  doc_type?: string;
  summary?: string;
  matched_section?: string;
  score: number;
}

function resultHref(r: DenseResult): string {
  const type = r.source_type || "document";
  if (type === "table") {
    const name = (r.path || "").replace(/^_tables\//, "") || r.title;
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
  const mode = (searchParams.get("mode") as Mode) || "dense";
  const queryVault = searchParams.get("v") || "";
  const vault = scopedVault || queryVault;

  const [denseResults, setDenseResults] = useState<DenseResult[]>([]);
  const [literalResults, setLiteralResults] = useState<GrepDoc[]>([]);
  const [total, setTotal] = useState(0);
  const [totalMatches, setTotalMatches] = useState(0);
  const [loading, setLoading] = useState(false);
  const [searched, setSearched] = useState(false);
  const [vaults, setVaults] = useState<{ name: string }[]>([]);

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
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [q, mode, vault]);

  async function doSearch(s: string, m: Mode, v: string) {
    if (!s.trim()) return;
    setLoading(true);
    setSearched(true);
    try {
      if (m === "dense") {
        const d = await searchDocs(s, v || undefined);
        setDenseResults(d.results);
        setLiteralResults([]);
        setTotal(d.total);
        setTotalMatches(0);
      } else {
        const d = await grepDocs(s, v || undefined);
        setLiteralResults(d.results);
        setDenseResults([]);
        setTotal(d.total_docs);
        setTotalMatches(d.total_matches);
      }
    } catch {
      setDenseResults([]);
      setLiteralResults([]);
      setTotal(0);
      setTotalMatches(0);
    }
    setLoading(false);
  }

  function buildParams(next: { q?: string; mode?: Mode; v?: string }) {
    const p = new URLSearchParams();
    p.set("q", next.q ?? q);
    const m = next.mode ?? mode;
    if (m !== "dense") p.set("mode", m);
    const v = next.v ?? vault;
    if (v) p.set("v", v);
    return p;
  }

  function switchMode(m: Mode) {
    if (!q) return;
    setSearchParams(buildParams({ mode: m }));
  }

  function switchVault(v: string) {
    if (!q) return;
    setSearchParams(buildParams({ v }));
  }

  const isShortQuery = q.trim().length > 0 && q.trim().length <= 6 && !/\s/.test(q.trim());
  const showLiteralHint = mode === "dense" && isShortQuery && searched && !loading;

  const groupedDense = groupByType(denseResults);

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
      <div className="coord-spark mb-3">§ SEARCH</div>
      <h1 className="font-serif text-[42px] leading-[1] tracking-[-0.02em] text-foreground mb-6">
        {scopedVault ? scopedVault : "Query the base"}
        <span className="text-foreground-muted">.</span>
      </h1>

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
        <div className="flex border border-border border-r-0 h-full shrink-0">
          <button
            type="button"
            onClick={() => switchMode("dense")}
            aria-pressed={mode === "dense"}
            title="Semantic hybrid search (dense + BM25 + cross-encoder rerank)"
            className={`px-3 h-full font-mono text-[11px] tracking-wider transition-colors cursor-pointer ${
              mode === "dense"
                ? "bg-foreground text-background"
                : "text-foreground hover:bg-surface-muted"
            }`}
          >
            SEMANTIC
          </button>
          <button
            type="button"
            onClick={() => switchMode("literal")}
            aria-pressed={mode === "literal"}
            title="Literal substring / regex search"
            className={`px-3 h-full font-mono text-[11px] tracking-wider border-l border-border transition-colors cursor-pointer ${
              mode === "literal"
                ? "bg-foreground text-background"
                : "text-foreground hover:bg-surface-muted"
            }`}
          >
            LITERAL
          </button>
        </div>
        <div className="relative flex-1 flex items-center border border-border h-full px-3 focus-within:border-accent transition-colors bg-surface">
          <SearchIcon
            className="h-4 w-4 text-foreground-muted mr-2 pointer-events-none"
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
            className="flex-1 bg-transparent text-sm text-foreground placeholder:text-foreground-muted focus:outline-none"
          />
        </div>
        <button
          type="submit"
          className="px-4 h-full font-mono text-[11px] tracking-wider bg-accent text-accent-foreground hover:bg-accent/90 transition-colors cursor-pointer focus:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2 focus-visible:ring-offset-background"
        >
          SEARCH
        </button>
      </form>

      {scopedVault && (
        <div className="flex items-center gap-3 text-xs mb-6">
          <span className="coord">SCOPE</span>
          <span className="font-mono text-foreground">{scopedVault}</span>
          <Link
            to={`/search${q ? `?q=${encodeURIComponent(q)}${mode !== "dense" ? `&mode=${mode}` : ""}` : ""}`}
            className="ml-auto inline-flex items-center gap-1 font-mono uppercase tracking-wider text-foreground-muted hover:text-accent transition-colors focus:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2 focus-visible:ring-offset-background"
          >
            <ExternalLink className="h-3 w-3" aria-hidden />
            Search all vaults
          </Link>
        </div>
      )}

      {!scopedVault && vaults.length > 0 && (
        <div className="border border-border flex items-stretch flex-wrap bg-surface mb-6">
          <div className="coord px-4 py-2 border-r border-border flex items-center">
            SCOPE
          </div>
          <button
            onClick={() => switchVault("")}
            aria-pressed={vault === ""}
            className={`px-3 py-2 font-mono text-xs tracking-wider border-r border-border transition-colors cursor-pointer ${
              vault === ""
                ? "bg-foreground text-background"
                : "text-foreground hover:bg-surface-muted"
            }`}
          >
            ALL VAULTS
          </button>
          {vaults.map((v) => (
            <button
              key={v.name}
              onClick={() => switchVault(v.name)}
              aria-pressed={vault === v.name}
              className={`px-3 py-2 font-mono text-xs tracking-wider border-r border-border transition-colors cursor-pointer ${
                vault === v.name
                  ? "bg-foreground text-background"
                  : "text-foreground hover:bg-surface-muted"
              }`}
            >
              {v.name}
            </button>
          ))}
        </div>
      )}

      {showLiteralHint && (
        <div
          role="note"
          className="border border-border px-6 py-3 text-sm flex items-center gap-3 bg-surface-muted mb-4"
        >
          <Lightbulb className="h-4 w-4 text-accent shrink-0" aria-hidden />
          <span className="text-foreground">
            Short single-token queries often work better in{" "}
            <button
              onClick={() => switchMode("literal")}
              className="underline font-medium hover:text-accent cursor-pointer"
            >
              LITERAL
            </button>{" "}
            mode.
          </span>
        </div>
      )}

      {loading && (
        <div className="border border-border p-6 bg-surface space-y-3">
          <Skeleton className="h-4 w-48" />
          <Skeleton className="h-16" />
          <Skeleton className="h-16" />
          <Skeleton className="h-16" />
          <div className="coord text-center pt-2">— Reranking…</div>
        </div>
      )}

      {searched && !loading && total === 0 && (
        <EmptyState
          title="No results"
          description={
            mode === "dense"
              ? "Try LITERAL mode for exact substring matching."
              : "Try SEMANTIC mode for meaning-based search."
          }
          action={
            <button
              onClick={() => switchMode(mode === "dense" ? "literal" : "dense")}
              className="underline text-sm hover:text-accent cursor-pointer"
            >
              Switch to {mode === "dense" ? "LITERAL" : "SEMANTIC"}
            </button>
          }
        />
      )}

      {total > 0 && mode === "dense" && (
        <Tabs defaultValue="all" className="mt-6">
          <TabsList>
            <TabsTrigger value="all" className="gap-1.5">
              All
              <span className="coord tabular-nums">[{denseResults.length}]</span>
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
            <DenseResultList items={denseResults} />
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

      {total > 0 && mode === "literal" && (
        <section className="border border-border bg-surface mt-6" aria-label="Literal results">
          <header className="border-b border-border px-4 py-2 flex items-baseline justify-between">
            <span className="coord-ink">§ RESULTS · LITERAL</span>
            <span className="coord tabular-nums">
              [{total} docs · {totalMatches} matches]
            </span>
          </header>
          <ol className="divide-y divide-border">
            {literalResults.map((r, i) => (
              <li key={r.uri}>
                <Link
                  to={`/vault/${r.vault}/doc/${encodeURIComponent(parseUri(r.uri)?.id ?? r.path)}`}
                  className="block px-5 py-4 group hover:bg-surface-muted transition-colors focus:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2 focus-visible:ring-offset-surface"
                >
                  <div className="grid grid-cols-[32px_1fr_60px] gap-4 items-baseline">
                    <span className="coord tabular-nums">
                      {String(i + 1).padStart(2, "0")}
                    </span>
                    <div className="min-w-0">
                      <div className="text-base font-medium tracking-tight text-foreground group-hover:text-accent">
                        {r.title}
                      </div>
                      <div className="coord mt-0.5">
                        {r.vault} / {r.path}
                      </div>
                      {r.matches.length > 0 && (
                        <div className="mt-2 space-y-1">
                          {r.matches.slice(0, 3).map((m, j) => (
                            <pre
                              key={j}
                              className="text-xs font-mono whitespace-pre-wrap text-foreground-muted border-l-2 border-accent pl-3 line-clamp-2"
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
    <ol className="border border-border bg-surface divide-y divide-border">
      {items.map((r, i) => (
        <li key={r.uri}>
          <Link
            to={resultHref(r)}
            className="block px-5 py-4 group hover:bg-surface-muted transition-colors focus:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2 focus-visible:ring-offset-surface"
          >
            <div className="grid grid-cols-[32px_1fr_60px] gap-4 items-baseline">
              <span className="coord tabular-nums">
                {String(i + 1).padStart(2, "0")}
              </span>
              <div className="min-w-0">
                <div className="flex items-baseline gap-2 flex-wrap">
                  <span className="text-base font-medium tracking-tight text-foreground group-hover:text-accent">
                    {r.title}
                  </span>
                  {r.doc_type && <Badge variant="outline">{r.doc_type}</Badge>}
                </div>
                <div className="coord mt-0.5">
                  {r.vault} / {r.path}
                </div>
                {r.summary && (
                  <p className="text-sm text-foreground-muted mt-2 line-clamp-2">
                    {r.summary}
                  </p>
                )}
                {r.matched_section && (
                  <pre className="mt-2 text-xs font-mono whitespace-pre-wrap text-foreground-muted line-clamp-3 border-l-2 border-accent pl-3">
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
