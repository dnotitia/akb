import { Link, Outlet, useNavigate, Navigate, useLocation, useSearchParams, matchPath } from "react-router-dom";
import { useEffect, useState } from "react";
import { Search as SearchIcon } from "lucide-react";
import { getToken } from "@/lib/api";
import { useMeasuredHeight } from "@/hooks/use-measured-height";
import { useHealth } from "@/hooks/use-health";
import { UserMenu } from "@/components/user-menu";
import { IndexingBadge } from "@/components/status-badge";
import { ErrorBoundary } from "@/components/error-boundary";

const VAULT_SHELL_PATTERNS = [
  "/vault",
  "/vault/:name",
  "/vault/:name/doc/:id",
  "/vault/:name/table/:table",
  "/vault/:name/file/:id",
  "/vault/:name/graph",
  "/vault/:name/publications",
  "/vault/:name/search",
  "/vault/:name/members",
  "/vault/:name/settings",
  "/vault/:name/activity",
];

function isVaultShellRoute(pathname: string): boolean {
  if (pathname === "/vault/new") return false;
  return VAULT_SHELL_PATTERNS.some((p) => !!matchPath({ path: p, end: true }, pathname));
}

type SearchMode = "dense" | "literal";

export function Layout() {
  const navigate = useNavigate();
  const location = useLocation();
  const [searchParams] = useSearchParams();
  const onSearchPage = location.pathname === "/search";
  const [searchQuery, setSearchQuery] = useState(() =>
    onSearchPage ? searchParams.get("q") || "" : "",
  );
  const [searchMode, setSearchMode] = useState<SearchMode>(() =>
    (onSearchPage && (searchParams.get("mode") as SearchMode)) || "dense",
  );

  useEffect(() => {
    if (onSearchPage) {
      setSearchQuery(searchParams.get("q") || "");
      setSearchMode((searchParams.get("mode") as SearchMode) || "dense");
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [location.pathname, searchParams]);

  if (!getToken()) {
    return <Navigate to="/auth" replace />;
  }

  const wide = isVaultShellRoute(location.pathname);
  const [headerRef, headerHeight] = useMeasuredHeight();

  // System-wide indexing pressure: aggregate of pending embeddings + pending
  // Vector-store backfill across every vault. Lives here (not on
  // per-vault pages) because /health is global — surfacing it on a
  // vault page made users think it was vault-scoped.
  const { data: health } = useHealth(!!getToken());
  const indexingPending: number | null = health
    ? (health.vector_store?.backfill?.upsert?.pending || 0)
    : null;

  // Vault workspace routes lock to viewport height and supply their own
  // internal scroll regions (tree / content / etc.). Non-vault routes keep
  // natural document scroll with the footer at the bottom.
  const rootClass = wide
    ? "h-screen flex flex-col overflow-hidden bg-background text-foreground"
    : "min-h-screen bg-background text-foreground";

  return (
    <div
      className={rootClass}
      style={{ ["--header-h" as any]: headerHeight ? `${headerHeight}px` : "96px" }}
    >
      {/* ── Header ─────────────────────────────────────────────── */}
      <header
        ref={headerRef}
        className={
          wide
            ? "shrink-0 bg-background border-b border-border"
            : "sticky top-0 z-40 bg-background/95 backdrop-blur border-b border-border"
        }
      >
        {/* Coordinate strip */}
        <div className="border-b border-border">
          <div className="mx-auto flex max-w-[1400px] items-center justify-between gap-3 px-6 py-1">
            <div className="coord">§ AKB · Agent Knowledgebase</div>
            <div className="flex items-center gap-3">
              <IndexingBadge pending={indexingPending} />
              <div className="coord hidden md:block">
                {new Date().toUTCString().slice(0, 22).toUpperCase()}
              </div>
              <div className="coord">v1.0</div>
            </div>
          </div>
        </div>

        {/* Main bar */}
        <div className="mx-auto flex max-w-[1400px] items-center gap-4 px-6 h-14">
          {/* Logo */}
          <Link
            to="/"
            className="flex items-baseline gap-2 group focus:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2 focus-visible:ring-offset-background"
          >
            <span className="font-mono text-base font-semibold tracking-tight group-hover:text-accent transition-colors">
              AKB
            </span>
            <span className="coord hidden sm:inline">/ v1.0</span>
          </Link>

          {/* Search */}
          <form
            className="flex-1 max-w-xl flex h-9"
            onSubmit={(e) => {
              e.preventDefault();
              if (!searchQuery.trim()) return;
              const p = new URLSearchParams({ q: searchQuery });
              if (searchMode !== "dense") p.set("mode", searchMode);
              navigate(`/search?${p.toString()}`);
            }}
            role="search"
            aria-label="Search knowledge base"
          >
            <div className="flex border border-border border-r-0 h-full">
              <button
                type="button"
                onClick={() => setSearchMode("dense")}
                title="Semantic hybrid search (dense + BM25 + cross-encoder rerank)"
                aria-pressed={searchMode === "dense"}
                className={`px-2.5 h-full font-mono text-[10px] tracking-wider transition-colors cursor-pointer ${
                  searchMode === "dense"
                    ? "bg-foreground text-background"
                    : "text-foreground hover:bg-surface-muted"
                }`}
              >
                SEMANTIC
              </button>
              <button
                type="button"
                onClick={() => setSearchMode("literal")}
                title="Literal substring / regex search"
                aria-pressed={searchMode === "literal"}
                className={`px-2.5 h-full font-mono text-[10px] tracking-wider border-l border-border transition-colors cursor-pointer ${
                  searchMode === "literal"
                    ? "bg-foreground text-background"
                    : "text-foreground hover:bg-surface-muted"
                }`}
              >
                LITERAL
              </button>
            </div>
            <label className="sr-only" htmlFor="header-search">
              Search
            </label>
            <div className="relative flex-1 flex items-center border border-border h-full px-3 focus-within:border-accent transition-colors bg-surface">
              <SearchIcon
                className="h-4 w-4 text-foreground-muted mr-2 pointer-events-none"
                aria-hidden
              />
              <input
                id="header-search"
                type="search"
                placeholder={searchMode === "dense" ? "Search (semantic)" : "Search (literal)"}
                value={searchQuery}
                onChange={(e) => setSearchQuery(e.target.value)}
                className="flex-1 bg-transparent text-sm text-foreground placeholder:text-foreground-muted focus:outline-none"
              />
            </div>
          </form>

          {/* Nav */}
          <nav
            aria-label="Primary"
            className="flex items-center gap-1 ml-auto"
          >
            <NavLink to="/" active={location.pathname === "/"} label="01" name="Home" />
            <NavLink
              to="/vault"
              active={location.pathname.startsWith("/vault") && location.pathname !== "/vault/new"}
              label="02"
              name="Vaults"
            />
            <div className="mx-1 h-6 w-px bg-border" aria-hidden />
            <UserMenu />
          </nav>
        </div>
      </header>

      {/* Content */}
      <main
        className={
          wide
            ? "flex-1 min-h-0 fade-in"
            : "mx-auto max-w-[1400px] px-6 py-8 fade-in"
        }
      >
        <ErrorBoundary resetKeys={[location.pathname]}>
          <Outlet />
        </ErrorBoundary>
      </main>

      {/* Footer — hidden on vault workspace routes (viewport-locked) */}
      {!wide && (
        <footer className="border-t border-border mt-16">
          <div className="mx-auto flex max-w-[1400px] items-center justify-between px-6 py-2">
            <div className="coord">© Dnotitia / Seahorse</div>
            <div className="coord hidden md:block">AGENT KNOWLEDGEBASE</div>
            <div className="coord">v1.0</div>
          </div>
        </footer>
      )}
    </div>
  );
}

function NavLink({
  to,
  active,
  label,
  name,
}: {
  to: string;
  active: boolean;
  label: string;
  name: string;
}) {
  return (
    <Link
      to={to}
      aria-current={active ? "page" : undefined}
      className={`group flex items-baseline gap-1.5 px-2 py-1 transition-colors focus:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2 focus-visible:ring-offset-background ${
        active ? "text-accent" : "text-foreground hover:text-accent"
      }`}
    >
      <span className={`coord ${active ? "text-accent" : "group-hover:text-accent"}`}>
        {label}
      </span>
      <span className="text-sm font-medium">{name}</span>
    </Link>
  );
}
