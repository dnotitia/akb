import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Panel } from "@/components/ui/panel";

type SearchMode = "dense" | "literal";

interface SearchSuggestion {
  scope: string;
  value: string;
}

interface IndexStatus {
  vaultCount: number;
  indexedCount: number;
  pending: number;
  abandoned: number;
  toneClassName: string;
}

interface HomeSearchHeroProps {
  query: string;
  mode: SearchMode;
  suggestions: SearchSuggestion[];
  indexStatus?: IndexStatus;
  onQueryChange: (query: string) => void;
  onModeChange: (mode: SearchMode) => void;
  onSearch: (query?: string) => void;
}

/**
 * Home's search-first masthead. This is intentionally a dedicated composition
 * instead of PageHeader: the title, segmented mode control, search field, and
 * discovery shortcuts share one centered measure in the product mockup.
 */
export function HomeSearchHero({
  query,
  mode,
  suggestions,
  indexStatus,
  onQueryChange,
  onModeChange,
  onSearch,
}: HomeSearchHeroProps) {
  return (
    <section
      className="home-search-hero -mx-4 mb-10 px-4 sm:-mx-6 sm:px-6 lg:-mx-8 lg:px-8 xl:-mx-12 xl:px-12 2xl:-mx-16 2xl:px-16"
      aria-labelledby="home-title"
    >
      <div className="mx-auto w-full max-w-[800px]">
        <h1 id="home-title" className="home-search-title text-balance text-center">
          Find what the team <em>already knows.</em>
        </h1>

        <form
          role="search"
          aria-label="Search all workspace knowledge"
          onSubmit={(event) => {
            event.preventDefault();
            onSearch();
          }}
        >
          <Panel className="knowledge-search-shell flex flex-col gap-2 p-1.5 sm:flex-row sm:items-stretch sm:gap-0">
            <div
              className="flex shrink-0 items-center gap-1 sm:border-r sm:border-border sm:pr-1.5"
              role="group"
              aria-label="Search mode"
            >
              {(["dense", "literal"] as const).map((searchMode) => (
                <button
                  key={searchMode}
                  type="button"
                  onClick={() => onModeChange(searchMode)}
                  aria-pressed={mode === searchMode}
                  className={`h-11 flex-1 rounded-[var(--radius-sm)] px-3.5 text-xs font-medium transition-token focus:outline-none focus-visible:ring-2 focus-visible:ring-ring sm:flex-none ${
                    mode === searchMode
                      ? "bg-surface-selected text-surface-selected-foreground"
                      : "text-foreground-muted hover:bg-surface-hover hover:text-foreground"
                  }`}
                >
                  {searchMode === "dense" ? "Semantic" : "Literal"}
                </button>
              ))}
            </div>

            <div className="relative min-w-0 flex-1">
              <Label htmlFor="home-search" className="sr-only">Search knowledge</Label>
              <Input
                id="home-search"
                type="search"
                value={query}
                onChange={(event) => onQueryChange(event.target.value)}
                placeholder={
                  mode === "dense"
                    ? "Search decisions, runbooks, specs…"
                    : "Find an exact phrase or pattern…"
                }
                className="h-11 rounded-none border-0 bg-transparent px-4 shadow-none focus-visible:ring-0 focus-visible:ring-offset-0"
              />
            </div>

            <Button
              type="submit"
              size="md"
              className="knowledge-search-action h-11 px-7 sm:ml-1.5"
            >
              Search
            </Button>
          </Panel>

          <div className="mt-4 flex flex-wrap items-center justify-center gap-x-4 gap-y-2">
            {indexStatus && (
              <>
                <span
                  className="inline-flex min-h-8 items-center gap-2 whitespace-nowrap px-1 text-xs text-foreground-muted"
                  role="status"
                  aria-live="polite"
                  title={`${indexStatus.vaultCount.toLocaleString()} accessible vaults · ${indexStatus.indexedCount.toLocaleString()} chunks indexed`}
                >
                  <span
                    className={`h-1.5 w-1.5 rounded-full ${indexStatus.toneClassName}`}
                    aria-hidden
                  />
                  <span className="tabular-nums">
                    {indexStatus.vaultCount.toLocaleString()} vaults · {indexStatus.indexedCount.toLocaleString()} indexed
                    {indexStatus.pending > 0 && ` · ${indexStatus.pending.toLocaleString()} indexing`}
                    {indexStatus.abandoned > 0 && ` · ${indexStatus.abandoned.toLocaleString()} stalled`}
                  </span>
                </span>
                <span className="hidden h-4 w-px bg-border sm:block" aria-hidden />
              </>
            )}

            <div
              className="flex flex-wrap items-center justify-center gap-2"
              aria-label="Suggested searches"
            >
              {suggestions.map((suggestion) => (
                <button
                  key={`${suggestion.scope}${suggestion.value}`}
                  type="button"
                  onClick={() => onSearch(`${suggestion.scope} ${suggestion.value}`)}
                  className="min-h-8 rounded-[var(--radius-full)] border border-border bg-surface px-3 text-xs text-foreground-muted transition-token hover:border-border-strong hover:bg-surface-hover hover:text-link focus:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2 focus-visible:ring-offset-background"
                >
                  <span className="text-link">{suggestion.scope}</span> {suggestion.value}
                </button>
              ))}
            </div>
          </div>
        </form>
      </div>
    </section>
  );
}
