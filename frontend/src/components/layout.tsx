import { Link, Outlet, Navigate, useLocation } from "react-router-dom";
import { useEffect, useLayoutEffect, useState } from "react";
import { useQueryClient } from "@tanstack/react-query";
import { Boxes, House, type LucideIcon } from "lucide-react";
import {
  clearPrivateAssetCache,
  getAuthConfig,
  getMe,
  getToken,
  type CurrentUser,
} from "@/lib/api";
import { UserMenu } from "@/components/user-menu";
import { Logo } from "@/components/logo";
import { ErrorBoundary } from "@/components/error-boundary";
import { GlobalSearchDialog } from "@/components/global-search-dialog";
import { HeaderIndexingStatus } from "@/components/header-indexing-status";
import { AppSidebar } from "@/components/app-sidebar";
import { appRouteBoundaryForPath } from "@/app-route-contract";
import { CurrentUserProvider } from "@/contexts/current-user-context";
import { useAccessibleIndexingHealth } from "@/hooks/use-accessible-indexing-health";
import { InlineLoadingState, LoadingState } from "@/components/ui/loading-state";
import { Skeleton } from "@/components/ui/skeleton";

const APP_SIDEBAR_COMPACT_KEY = "akb_app_sidebar_compact";

function identityFingerprint(user: CurrentUser): string {
  return JSON.stringify([
    user.user_id,
    user.username,
    user.email,
    user.display_name,
    user.is_admin,
    user.auth_method,
    user.key_class,
  ]);
}

export function Layout() {
  const queryClient = useQueryClient();
  const location = useLocation();
  const [session, setSession] = useState<
    | { status: "checking"; user: null }
    | { status: "authenticated"; user: CurrentUser }
    | { status: "unauthenticated"; user: null }
  >({ status: "checking", user: null });
  const [revalidating, setRevalidating] = useState(false);
  const [sidebarCollapsed, setSidebarCollapsed] = useState(() => {
    try {
      return localStorage.getItem(APP_SIDEBAR_COMPACT_KEY) === "true";
    } catch {
      return false;
    }
  });

  useEffect(() => {
    let cancelled = false;
    void (async () => {
      const config = await getAuthConfig();
      if (
        config.available !== true ||
        config.auth_mode === null ||
        ((config.auth_mode === "local" || config.auth_mode === "hybrid") &&
          !getToken())
      ) {
        if (!cancelled) setSession({ status: "unauthenticated", user: null });
        return;
      }
      try {
        const user = await getMe({ redirectOnUnauthorized: false });
        if (!cancelled) setSession({ status: "authenticated", user });
      } catch {
        if (!cancelled) setSession({ status: "unauthenticated", user: null });
      }
    })();
    return () => {
      cancelled = true;
    };
  }, []);

  const activeUser = session.status === "authenticated" ? session.user : null;
  const activeFingerprint = activeUser ? identityFingerprint(activeUser) : null;

  useEffect(() => {
    if (!activeUser || activeFingerprint === null) return;
    let disposed = false;
    let running = false;

    const revalidateForegroundIdentity = async () => {
      if (running || document.visibilityState === "hidden") return;
      running = true;
      setRevalidating(true);
      clearPrivateAssetCache();
      try {
        const verified = await getMe({ redirectOnUnauthorized: false });
        if (disposed) return;
        const identityChanged =
          identityFingerprint(verified) !== activeFingerprint;
        // SSO cookies are shared across tabs and are intentionally invisible
        // to JavaScript. After every foreground proof, clear query state so a
        // replaced identity or changed server-side ACL cannot inherit data
        // rendered under the prior cookie. Local mode only pays this cost when
        // its storage-backed identity actually changed.
        if (activeUser.auth_method === "browser_session" || identityChanged) {
          queryClient.clear();
        }
        setSession({ status: "authenticated", user: verified });
      } catch {
        if (disposed) return;
        queryClient.clear();
        clearPrivateAssetCache();
        setSession({ status: "unauthenticated", user: null });
      } finally {
        running = false;
        if (!disposed) setRevalidating(false);
      }
    };
    const onVisibilityChange = () => {
      if (document.visibilityState === "visible") {
        void revalidateForegroundIdentity();
      }
    };
    const onStorage = (event: StorageEvent) => {
      if (event.key === null || event.key === "akb_token") {
        void revalidateForegroundIdentity();
      }
    };

    window.addEventListener("focus", revalidateForegroundIdentity);
    window.addEventListener("storage", onStorage);
    document.addEventListener("visibilitychange", onVisibilityChange);
    return () => {
      disposed = true;
      window.removeEventListener("focus", revalidateForegroundIdentity);
      window.removeEventListener("storage", onStorage);
      document.removeEventListener("visibilitychange", onVisibilityChange);
    };
  }, [activeFingerprint, activeUser, queryClient]);

  const wide = appRouteBoundaryForPath(location.pathname) === "vault-shell";
  const isSearchWorkspace = location.pathname === "/search";
  const viewportLocked = wide || isSearchWorkspace;
  const sidebarCompact = wide || sidebarCollapsed;
  const { data: indexingStatus } = useAccessibleIndexingHealth(
    session.status === "authenticated",
    activeUser?.user_id,
  );

  function setSidebarCompact(compact: boolean) {
    setSidebarCollapsed(compact);
    try {
      localStorage.setItem(APP_SIDEBAR_COMPACT_KEY, String(compact));
    } catch {
      // Storage can be disabled. The current session still keeps the choice.
    }
  }

  useLayoutEffect(() => {
    const root = document.documentElement;
    root.classList.toggle("vault-workspace-scroll-lock", viewportLocked);
    return () => root.classList.remove("vault-workspace-scroll-lock");
  }, [viewportLocked]);

  if (session.status === "checking") {
    return <AppShellLoading />;
  }

  if (session.status === "unauthenticated") {
    // Preserve where the user was headed so /auth can return them there after
    // signing in (deep-linked / shared URLs don't dump everyone on home).
    const dest = location.pathname + location.search;
    const to =
      dest && dest !== "/" ? `/auth?next=${encodeURIComponent(dest)}` : "/auth";
    return <Navigate to={to} replace />;
  }
  // Canvas-style workspaces lock to viewport height and own their internal
  // scroll. Document-flow routes keep natural page scroll and the footer.
  const rootClass = viewportLocked
    ? "h-screen flex flex-col overflow-hidden bg-background text-foreground"
    : "min-h-screen flex flex-col bg-background text-foreground";

  return (
    <div className={rootClass} aria-busy={revalidating || undefined}>
      {revalidating && (
        <InlineLoadingState
          label="Refreshing access…"
          size="sm"
          className="fixed left-1/2 top-2 z-[var(--z-toast)] -translate-x-1/2 rounded-full border border-border bg-surface px-3 py-1.5 shadow-md"
        />
      )}
      {/* Skip link — first focusable element; jumps keyboard/SR users past the
          header chrome to the page content on every route. */}
      <a
        href="#main"
        className="sr-only focus:not-sr-only focus:absolute focus:left-4 focus:top-3 focus:z-[100] focus:rounded-[var(--radius-md)] focus:border focus:border-border focus:bg-surface focus:px-3 focus:py-2 focus:text-sm focus:shadow-md focus:outline-none focus-visible:ring-2 focus-visible:ring-ring"
      >
        Skip to content
      </a>
      {/* ── Glass app header ───────────────────────────────────────── */}
      <header className="app-header sticky top-0 z-40 shrink-0">
        <div className="flex h-14 w-full items-center">
          {/* Keep the brand lockup stable while the navigation rail changes
              density. Collapsing navigation must not remove product identity
              or shift the global-search entry point. */}
          <div className="flex shrink-0 items-center px-3 lg:w-52">
            <Link
              to="/"
              aria-label="AKB home"
              className="shrink-0 rounded-[var(--radius-md)] focus:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2 focus-visible:ring-offset-background"
            >
              <Logo
                size={28}
                wordmark
                subtitle
                variant="header"
              />
            </Link>
          </div>

          <div className="flex min-w-0 flex-1 items-center pr-3">
            <div className="ml-auto flex min-w-0 items-center gap-2">
              <HeaderIndexingStatus status={indexingStatus} />
              {/* This is a real global-search surface, not a shortcut to /search.
                  Advanced mode and vault/type filters remain on the full page. */}
              <CurrentUserProvider user={session.user}>
                <GlobalSearchDialog />
              </CurrentUserProvider>
            </div>

            {/* The persistent sidebar owns desktop entry points. Compact icon
                links remain in the header only where that rail is hidden. */}
            <nav
              aria-label="Primary mobile navigation"
              className="ml-2 flex items-center gap-1 lg:hidden"
            >
              <MobileNavLink
                to="/"
                active={location.pathname === "/"}
                name="Home"
                icon={House}
              />
              <MobileNavLink
                to="/vault"
                active={location.pathname.startsWith("/vault")}
                name="Vaults"
                icon={Boxes}
              />
            </nav>

            <div className="ml-3 flex shrink-0 items-center justify-end border-l border-border pl-3 lg:w-28">
              <UserMenu initialUser={session.user} />
            </div>
          </div>
        </div>
      </header>

      <div className={viewportLocked ? "flex min-h-0 flex-1" : "flex flex-1"}>
        <AppSidebar
          compact={sidebarCompact}
          collapsible={!wide}
          onCompactChange={setSidebarCompact}
        />

        <div
          className={
            viewportLocked
              ? "flex min-h-0 min-w-0 flex-1 flex-col"
              : "flex min-w-0 flex-1 flex-col"
          }
        >
          {/* Content */}
          <main
            id="main"
            tabIndex={-1}
            className={
              viewportLocked
                ? "min-h-0 flex-1 animate-in focus:outline-none"
                : "flex-1 animate-in focus:outline-none"
            }
          >
            {viewportLocked ? (
              <CurrentUserProvider user={session.user}>
                <ErrorBoundary resetKeys={[location.pathname, location.search]}>
                  <Outlet context={{ indexingStatus }} />
                </ErrorBoundary>
              </CurrentUserProvider>
            ) : (
              <div className="w-full px-4 py-8 sm:px-6 lg:px-8 xl:px-12 2xl:px-36">
                <CurrentUserProvider user={session.user}>
                  <ErrorBoundary
                    resetKeys={[location.pathname, location.search]}
                  >
                    <Outlet context={{ indexingStatus }} />
                  </ErrorBoundary>
                </CurrentUserProvider>
              </div>
            )}
          </main>

          {/* Footer — hidden while a viewport-locked workspace owns scrolling. */}
          {!viewportLocked && (
            <footer className="border-t border-border">
              <div className="flex w-full items-center justify-between px-4 py-3 sm:px-6 lg:px-8 xl:px-12 2xl:px-36">
                <div className="coord">© Dnotitia · Seahorse</div>
                <div className="coord hidden md:block">Agent Knowledgebase</div>
                <div className="coord">v1.0</div>
              </div>
            </footer>
          )}
        </div>
      </div>
    </div>
  );
}

function AppShellLoading() {
  return (
    <LoadingState label="Verifying session" className="min-h-screen bg-background text-foreground">
      <div className="flex min-h-screen flex-col">
        <header className="app-header shrink-0">
          <div className="flex h-14 w-full items-center">
            <div className="flex shrink-0 items-center px-3 lg:w-52">
              <Logo size={28} wordmark subtitle variant="header" />
            </div>
            <div className="ml-auto flex min-w-0 items-center gap-3 pr-3">
              <Skeleton className="hidden h-8 w-44 rounded-[var(--radius-md)] sm:block" />
              <Skeleton className="h-9 w-9 rounded-full" />
            </div>
          </div>
        </header>

        <div className="flex min-h-0 flex-1">
          <aside className="hidden w-52 shrink-0 border-r border-border bg-surface lg:block">
            <div className="space-y-2 p-3">
              {[0, 1, 2, 3].map((item) => (
                <div key={item} className="flex h-10 items-center gap-3 rounded-[var(--radius-md)] px-2">
                  <Skeleton className="h-8 w-8 shrink-0 rounded-[var(--radius-md)]" />
                  <Skeleton className="h-3.5 w-24 rounded-[var(--radius-sm)]" />
                </div>
              ))}
            </div>
          </aside>

          <main className="min-w-0 flex-1 px-4 py-8 sm:px-6 lg:px-8 xl:px-12 2xl:px-36">
            <div className="border-b border-border pb-6">
              <Skeleton className="h-3 w-28 rounded-[var(--radius-sm)]" />
              <Skeleton className="mt-4 h-9 w-64 max-w-full rounded-[var(--radius-md)]" />
              <Skeleton className="mt-3 h-4 w-full max-w-xl rounded-[var(--radius-sm)]" />
            </div>
            <div className="mt-8 grid gap-6 xl:grid-cols-[minmax(0,1fr)_21rem]">
              <div className="space-y-6">
                <section className="rounded-[var(--radius-lg)] border border-border bg-surface p-5 shadow-sm">
                  <Skeleton className="h-5 w-36 rounded-[var(--radius-sm)]" />
                  <div className="mt-5 grid gap-4 sm:grid-cols-2 lg:grid-cols-3">
                    {[0, 1, 2].map((item) => (
                      <Skeleton key={item} className="h-28 rounded-[var(--radius-lg)]" />
                    ))}
                  </div>
                </section>
                <Skeleton className="h-64 rounded-[var(--radius-lg)] border border-border" />
              </div>
              <Skeleton className="h-80 rounded-[var(--radius-lg)] border border-border" />
            </div>
          </main>
        </div>
      </div>
    </LoadingState>
  );
}

function MobileNavLink({
  to,
  active,
  name,
  icon: Icon,
}: {
  to: string;
  active: boolean;
  name: string;
  icon: LucideIcon;
}) {
  return (
    <Link
      to={to}
      aria-label={name}
      aria-current={active ? "page" : undefined}
      className={`flex h-9 w-9 items-center justify-center rounded-[var(--radius-md)] transition-token focus:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2 focus-visible:ring-offset-background ${
        active
          ? "bg-surface-selected text-surface-selected-foreground"
          : "text-foreground-muted hover:text-foreground hover:bg-surface-hover"
      }`}
    >
      <Icon className="h-4 w-4" aria-hidden />
    </Link>
  );
}
