import { Link, Outlet, Navigate, useLocation } from "react-router-dom";
import { useEffect, useLayoutEffect, useState } from "react";
import { useQueryClient } from "@tanstack/react-query";
import {
  clearPrivateAssetCache,
  getAuthConfig,
  getMe,
  getToken,
  type CurrentUser,
} from "@/lib/api";
import { useHealth } from "@/hooks/use-health";
import { UserMenu } from "@/components/user-menu";
import { Logo } from "@/components/logo";
import { ErrorBoundary } from "@/components/error-boundary";
import { GlobalSearchDialog } from "@/components/global-search-dialog";
import { appRouteBoundaryForPath } from "@/app-route-contract";
import { CurrentUserProvider } from "@/contexts/current-user-context";

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

  useEffect(() => {
    let cancelled = false;
    void (async () => {
      const config = await getAuthConfig();
      if (
        config.available !== true ||
        config.auth_mode === null ||
        (config.auth_mode === "local" && !getToken())
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
        const identityChanged = identityFingerprint(verified) !== activeFingerprint;
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
  const { data: health } = useHealth(session.status === "authenticated");

  useLayoutEffect(() => {
    const root = document.documentElement;
    root.classList.toggle("vault-workspace-scroll-lock", wide);
    return () => root.classList.remove("vault-workspace-scroll-lock");
  }, [wide]);

  if (session.status === "checking" || revalidating) {
    return (
      <div className="flex min-h-screen items-center justify-center bg-background text-foreground">
        <div className="coord" role="status" aria-live="polite">Verifying session…</div>
      </div>
    );
  }

  if (session.status === "unauthenticated") {
    // Preserve where the user was headed so /auth can return them there after
    // signing in (deep-linked / shared URLs don't dump everyone on home).
    const dest = location.pathname + location.search;
    const to = dest && dest !== "/" ? `/auth?next=${encodeURIComponent(dest)}` : "/auth";
    return <Navigate to={to} replace />;
  }
  // Vault workspace routes lock to viewport height (own internal scroll). Other
  // routes keep natural document scroll with the footer at the bottom.
  const rootClass = wide
    ? "h-screen flex flex-col overflow-hidden bg-background text-foreground"
    : "min-h-screen flex flex-col bg-background text-foreground";

  return (
    <div className={rootClass}>
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
        {/* One responsive horizontal system across Home and task routes. */}
        <div className="flex h-14 w-full items-center px-3">
          {/* The brand stays visually independent. Search-corpus status belongs
              with the Home search affordance rather than the identity lockup. */}
          <div className="flex min-w-0 items-center">
            <Link
              to="/"
              aria-label="AKB home"
              className="shrink-0 rounded-[var(--radius-md)] focus:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2 focus-visible:ring-offset-background"
            >
              <Logo size={28} subtitle variant="header" />
            </Link>
          </div>

          {/* This is a real global-search surface, not a shortcut to /search.
              Advanced mode and vault/type filters remain on the full page. */}
          <CurrentUserProvider user={session.user}>
            <GlobalSearchDialog />
          </CurrentUserProvider>

          {/* Nav + actions */}
          <nav aria-label="Primary" className="ml-4 flex items-center gap-1">
            <NavLink to="/" active={location.pathname === "/"} name="Home" />
            <NavLink
              to="/vault"
              active={location.pathname.startsWith("/vault") && location.pathname !== "/vault/new"}
              name="Vaults"
            />
            <div className="mx-1.5 h-6 w-px bg-border" aria-hidden />
            <UserMenu initialUser={session.user} />
          </nav>
        </div>
      </header>

      {/* Content */}
      <main id="main" tabIndex={-1} className={wide ? "flex-1 min-h-0 animate-in focus:outline-none" : "flex-1 animate-in focus:outline-none"}>
        {wide ? (
          <CurrentUserProvider user={session.user}>
            <ErrorBoundary resetKeys={[location.pathname, location.search]}>
              <Outlet context={{ health }} />
            </ErrorBoundary>
          </CurrentUserProvider>
        ) : (
          <div className="w-full px-4 py-8 sm:px-6 lg:px-8 xl:px-12 2xl:px-16">
            <CurrentUserProvider user={session.user}>
              <ErrorBoundary resetKeys={[location.pathname, location.search]}>
                <Outlet context={{ health }} />
              </ErrorBoundary>
            </CurrentUserProvider>
          </div>
        )}
      </main>

      {/* Footer — hidden on vault workspace routes (viewport-locked) */}
      {!wide && (
        <footer className="border-t border-border">
          <div className="flex w-full items-center justify-between px-4 py-3 sm:px-6 lg:px-8 xl:px-12 2xl:px-16">
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
          ? "bg-surface-selected text-surface-selected-foreground"
          : "text-foreground-muted hover:text-foreground hover:bg-surface-hover"
      }`}
    >
      {name}
    </Link>
  );
}
