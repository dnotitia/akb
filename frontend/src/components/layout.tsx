import { Link, Outlet, useNavigate, Navigate, useLocation, useSearchParams, matchPath } from "react-router-dom";
import { useEffect, useState } from "react";
import { Search as SearchIcon } from "lucide-react";
import { getToken } from "@/lib/api";
import { useHealth } from "@/hooks/use-health";
import { UserMenu } from "@/components/user-menu";
import { ThemeToggle } from "@/components/theme-toggle";
import { Logo } from "@/components/logo";
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

  const wide = isVaultShellRoute(location.pathname);
  const { data: health } = useHealth(!!getToken());

  if (!getToken()) {
    return <Navigate to="/auth" replace />;
  }
  const upsert = health?.vector_store?.backfill?.upsert;
  const indexingPending: number | null = upsert
    ? Math.max(0, (upsert.pending || 0) - (upsert.abandoned || 0))
    : null;
  const indexingAbandoned: number = upsert?.abandoned || 0;

  // Vault workspace routes lock to viewport height (own internal scroll). Other
  // routes keep natural document scroll with the footer at the bottom.
  const rootClass = wide
    ? "h-screen flex flex-col overflow-hidden bg-background text-foreground"
    : "min-h-screen flex flex-col bg-background text-foreground";

  return (
    <div className={rootClass}>
      {/* ── Glass app header ───────────────────────────────────────── */}
      <header className="app-header sticky top-0 z-40 shrink-0">
        <div className="mx-auto flex max-w-[1600px] items-center gap-4 px-5 h-16">
          {/* Brand */}
          <Link
            to="/"
            aria-label="AKB home"
            className="shrink-0 rounded-[var(--radius-md)] focus:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2 focus-visible:ring-offset-background"
          >
            <Logo size={30} subtitle />
          </Link>

          {/* Global search */}
          <form
            className="flex-1 max-w-3xl hidden sm:flex h-10"
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
            <div className="flex w-full items-stretch rounded-[var(--radius-md)] border border-border bg-surface overflow-hidden focus-within:border-primary focus-within:ring-2 focus-within:ring-ring/30 transition-colors">
              <div className="flex shrink-0 p-1 gap-0.5">
                {(["dense", "literal"] as const).map((mode) => (
                  <button
                    key={mode}
                    type="button"
                    onClick={() => setSearchMode(mode)}
                    title={
                      mode === "dense"
                        ? "Semantic hybrid search (dense + BM25 + rerank)"
                        : "Literal substring / regex search"
                    }
                    aria-pressed={searchMode === mode}
                    className={`px-3 rounded-[var(--radius-sm)] text-xs font-medium transition-token cursor-pointer ${
                      searchMode === mode
                        ? "bg-primary text-primary-foreground"
                        : "text-foreground-muted hover:bg-surface-muted"
                    }`}
                  >
                    {mode === "dense" ? "Semantic" : "Literal"}
                  </button>
                ))}
              </div>
              <div className="relative flex flex-1 items-center pr-3">
                <SearchIcon
                  className="h-4 w-4 text-foreground-muted mr-2 pointer-events-none"
                  aria-hidden
                />
                <label className="sr-only" htmlFor="header-search">Search</label>
                <input
                  id="header-search"
                  type="search"
                  placeholder={searchMode === "dense" ? "Search knowledge…" : "Literal search…"}
                  value={searchQuery}
                  onChange={(e) => setSearchQuery(e.target.value)}
                  className="flex-1 bg-transparent text-sm text-foreground placeholder:text-foreground-muted focus:outline-none"
                />
              </div>
            </div>
          </form>

          {/* Nav + actions */}
          <nav aria-label="Primary" className="flex items-center gap-1 ml-auto">
            <NavLink to="/" active={location.pathname === "/"} name="Home" />
            <NavLink
              to="/vault"
              active={location.pathname.startsWith("/vault") && location.pathname !== "/vault/new"}
              name="Vaults"
            />
            <div className="mx-1.5 h-6 w-px bg-border" aria-hidden />
            <IndexingBadge pending={indexingPending} abandoned={indexingAbandoned} />
            <ThemeToggle />
            <UserMenu />
          </nav>
        </div>
      </header>

      {/* Content */}
      <main className={wide ? "flex-1 min-h-0 animate-in" : "flex-1 animate-in"}>
        {wide ? (
          <ErrorBoundary resetKeys={[location.pathname]}>
            <Outlet />
          </ErrorBoundary>
        ) : (
          <div className="mx-auto max-w-[1400px] px-6 py-8">
            <ErrorBoundary resetKeys={[location.pathname]}>
              <Outlet />
            </ErrorBoundary>
          </div>
        )}
      </main>

      {/* Footer — hidden on vault workspace routes (viewport-locked) */}
      {!wide && (
        <footer className="border-t border-border">
          <div className="mx-auto flex max-w-[1400px] items-center justify-between px-6 py-3">
            <div className="coord">© Dnotitia · Seahorse</div>
            <div className="coord hidden md:block">Agent Knowledgebase</div>
            <div className="coord">v1.0</div>
          </div>
        </footer>
      )}
    </div>
  );
}

function NavLink({ to, active, name }: { to: string; active: boolean; name: string }) {
  return (
    <Link
      to={to}
      aria-current={active ? "page" : undefined}
      className={`rounded-[var(--radius-md)] px-3 py-1.5 text-sm font-medium transition-token focus:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2 focus-visible:ring-offset-background ${
        active
          ? "bg-surface-muted text-foreground"
          : "text-foreground-muted hover:text-foreground hover:bg-surface-muted/60"
      }`}
    >
      {name}
    </Link>
  );
}
