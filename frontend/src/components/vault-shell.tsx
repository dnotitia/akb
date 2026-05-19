import { Outlet, useLocation, useParams } from "react-router-dom";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { PanelLeftClose, PanelLeftOpen } from "lucide-react";
import { VaultExplorer } from "@/components/vault-explorer";
import { VaultNav } from "@/components/vault-nav";
import { TitleBar, VaultActions, type VaultPageKind } from "@/components/title-bar";
import { ErrorBoundary } from "@/components/error-boundary";
import { VaultRefreshProvider } from "@/contexts/vault-refresh-context";

/**
 * 3-column vault workspace:
 *   [ 200px vault-nav | 260px tree | 1fr content ]
 * Above them a 36px TitleBar with breadcrumb + VaultActions.
 * Each page renders into the Outlet and owns its inner layout (e.g. the
 * document page still decides where its right-rail outline goes).
 */
export function VaultShell() {
  const { name } = useParams<{ name: string }>();
  const location = useLocation();
  const [visible, setVisible] = useState(true);

  // Tree defaults to open on every vault switch — clicking the active
  // vault row in the nav (or ⌘\) toggles it.
  useEffect(() => {
    setVisible(true);
  }, [name]);

  const toggleTree = useCallback(() => setVisible((v) => !v), []);

  // Refs hold the latest refetch functions reported up from VaultNav /
  // VaultExplorer via `onRefetchReady`. We can't lift `useVaultTree` to
  // the shell (the explorer needs the tree state internally for filter,
  // expand, etc.) and we can't put a provider *below* the children that
  // need it, so we adopt a callback-ref pattern: children publish their
  // refetch on mount, the shell stores it in a ref, and the context
  // exposes stable thunks that dereference the ref at call time.
  const refetchTreeRef = useRef<() => void>(() => {});
  const refetchVaultsRef = useRef<() => void>(() => {});
  const refetchTree = useCallback(() => refetchTreeRef.current(), []);
  const refetchVaults = useCallback(() => refetchVaultsRef.current(), []);
  const onTreeRefetchReady = useCallback((fn: () => void) => {
    refetchTreeRef.current = fn;
  }, []);
  const onVaultsRefetchReady = useCallback((fn: () => void) => {
    refetchVaultsRef.current = fn;
  }, []);

  // cmd+\ / ctrl+\ toggles the tree column.
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if ((e.metaKey || e.ctrlKey) && e.key === "\\") {
        e.preventDefault();
        setVisible((v) => !v);
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, []);

  const crumbs = useMemo(() => {
    if (!name) return [];
    const base = [{ label: name, to: `/vault/${name}` }];
    // Decode the path suffix when on document/table/file routes
    const docMatch = location.pathname.match(/^\/vault\/[^/]+\/doc\/(.+)$/);
    if (docMatch) {
      const raw = decodeURIComponent(docMatch[1]);
      const parts = raw.split("/");
      return [
        ...base,
        ...parts.slice(0, -1).map((p) => ({ label: p })),
        { label: parts[parts.length - 1] },
      ];
    }
    const tableMatch = location.pathname.match(/^\/vault\/[^/]+\/table\/(.+)$/);
    if (tableMatch) {
      return [...base, { label: `table · ${decodeURIComponent(tableMatch[1])}` }];
    }
    const fileMatch = location.pathname.match(/^\/vault\/[^/]+\/file\/(.+)$/);
    if (fileMatch) {
      return [...base, { label: `file · ${decodeURIComponent(fileMatch[1]).slice(0, 16)}` }];
    }
    return base;
  }, [name, location.pathname]);

  const isGraph = location.pathname.endsWith("/graph");
  const isPublications = location.pathname.endsWith("/publications");
  const isSearch = location.pathname.endsWith("/search");
  const isMembers = location.pathname.endsWith("/members");
  const isSettings = location.pathname.endsWith("/settings");
  const isActivity = location.pathname.endsWith("/activity");
  // Admin/management pages don't browse vault content — the tree explorer
  // is irrelevant there and just narrows the column. Hide it like /graph
  // does, but unlike graph these pages still scroll normally.
  const isAdminPage = isMembers || isSettings || isActivity;
  const page: VaultPageKind = isGraph
    ? "graph"
    : isPublications
      ? "publish"
      : isSearch
        ? "search"
        : "overview";

  // /vault (no :name) — simplified shell: left nav picker + content,
  // no tree explorer, no vault actions. Keeps shell chrome consistent
  // so hopping between vault and the index feels like the same place.
  if (!name) {
    return (
      <VaultRefreshProvider refetchTree={refetchTree} refetchVaults={refetchVaults}>
        <div className="flex flex-col h-full min-h-0">
          <TitleBar crumbs={[{ label: "Vaults" }]} />
          <div className="grid grid-cols-[200px_1fr] flex-1 min-h-0">
            <VaultNav
              current=""
              onRefetchReady={onVaultsRefetchReady}
            />
            <div className="min-w-0 min-h-0 overflow-y-auto bg-background">
              <ErrorBoundary resetKeys={[location.pathname]}>
                <Outlet />
              </ErrorBoundary>
            </div>
          </div>
        </div>
      </VaultRefreshProvider>
    );
  }

  // Graph is its own navigation (nodes/edges), so the tree explorer is
  // redundant there and eats canvas width. Keep the user's preference
  // for other routes; force-hide the tree on /graph so the canvas gets
  // room by default. Members/settings/activity follow the same rule —
  // those are vault-level admin pages, not content browsing.
  const showTree = !isGraph && !isAdminPage && visible;
  const gridCols = showTree ? "200px 260px 1fr" : "200px 1fr";

  return (
    <VaultRefreshProvider refetchTree={refetchTree} refetchVaults={refetchVaults}>
      <div className="flex flex-col h-full min-h-0">
        <TitleBar
          crumbs={crumbs}
          right={<VaultActions vault={name} page={page} />}
        />
        <div
          className="grid grid-cols-[var(--cols)] flex-1 min-h-0"
          style={{ ["--cols" as any]: gridCols }}
        >
          <VaultNav
            current={name}
            onRefetchReady={onVaultsRefetchReady}
            onCurrentVaultClick={toggleTree}
          />
          {showTree && (
            <VaultExplorer
              vault={name}
              onRefetchReady={onTreeRefetchReady}
            />
          )}
          {/* Content column. The tree toggle lives here so it naturally
              sits on the outlet's left edge — next to the explorer when
              it's expanded, and at VaultNav's right edge when collapsed.
              Graph renders full-bleed (no padding, no inner scroll);
              other pages scroll internally with uniform padding. */}
          {isGraph ? (
            // Graph owns the full column — no tree toggle (tree is forced
            // hidden on /graph) and no inner scroll.
            <div className="min-w-0 min-h-0 relative bg-background overflow-hidden">
              <ErrorBoundary resetKeys={[location.pathname]}>
                <Outlet />
              </ErrorBoundary>
            </div>
          ) : (
            // Button lives in the non-scrolling outer wrapper so it stays
            // pinned to the column's top-left regardless of how far the
            // reader has scrolled the article.
            <div className="min-w-0 min-h-0 relative bg-background flex flex-col overflow-hidden">
              {!isAdminPage && (
                <button
                  onClick={() => setVisible((v) => !v)}
                  title={`${visible ? "Hide" : "Show"} tree (⌘\\)`}
                  aria-label={`${visible ? "Hide" : "Show"} vault tree`}
                  aria-expanded={visible}
                  className="absolute top-3 left-3 z-10 h-9 w-9 inline-flex items-center justify-center bg-surface border border-border text-foreground-muted hover:text-foreground hover:bg-surface-muted focus:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2 focus-visible:ring-offset-background transition-colors cursor-pointer"
                >
                  {visible ? (
                    <PanelLeftClose className="h-4 w-4" aria-hidden />
                  ) : (
                    <PanelLeftOpen className="h-4 w-4" aria-hidden />
                  )}
                </button>
              )}
              <div className="flex-1 min-h-0 overflow-y-auto">
                <div className="px-8 py-8 lg:px-10 lg:py-12">
                  <ErrorBoundary resetKeys={[location.pathname]}>
                    <Outlet />
                  </ErrorBoundary>
                </div>
              </div>
            </div>
          )}
        </div>
      </div>
    </VaultRefreshProvider>
  );
}
