import { Outlet, useLocation, useParams } from "react-router-dom";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { ChevronsLeft, ChevronsRight } from "lucide-react";
import { VaultExplorer } from "@/components/vault-explorer";
import { VaultNav } from "@/components/vault-nav";
import { TitleBar, VaultActions, type VaultPageKind } from "@/components/title-bar";
import { ErrorBoundary } from "@/components/error-boundary";
import { VaultRefreshProvider } from "@/contexts/vault-refresh-context";
import { useColumnResize } from "@/hooks/use-column-resize";

const TREE_VISIBLE_KEY = "akb.treeVisible";

/**
 * Vault workspace: a glass TitleBar over a horizontal split — a **persistent,
 * resizable left sidebar** (vault switcher + collection tree) and the content
 * column. The sidebar is the primary navigation surface; it stays pinned (no
 * slide-over) so jumping between docs/collections never costs an extra click.
 * Collapse it with the Tree button or ⌘\ (state persists); the tree is hidden
 * on /graph, which owns the full canvas.
 */
export function VaultShell() {
  const { name } = useParams<{ name: string }>();
  const location = useLocation();
  const [visible, setVisible] = useState<boolean>(() => {
    if (typeof window === "undefined") return true;
    return window.localStorage.getItem(TREE_VISIBLE_KEY) !== "0";
  });
  const tree = useColumnResize({ storageKey: "akb.treeWidth", min: 240, max: 640, default: 300 });

  const setTreeVisible = useCallback((next: boolean) => {
    setVisible(next);
    window.localStorage.setItem(TREE_VISIBLE_KEY, next ? "1" : "0");
  }, []);

  // Callback-ref pattern: children publish their refetch fns on mount; the
  // shell stores them in refs and exposes stable thunks via context.
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

  // ⌘\ / ctrl+\ toggles the sidebar.
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if ((e.metaKey || e.ctrlKey) && e.key === "\\") {
        e.preventDefault();
        setVisible((v) => {
          const next = !v;
          window.localStorage.setItem(TREE_VISIBLE_KEY, next ? "1" : "0");
          return next;
        });
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, []);

  const crumbs = useMemo(() => {
    if (!name) return [{ label: "Vaults" }];
    const base = [{ label: name, to: `/vault/${name}` }];
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
    if (tableMatch) return [...base, { label: `table · ${decodeURIComponent(tableMatch[1])}` }];
    const fileMatch = location.pathname.match(/^\/vault\/[^/]+\/file\/(.+)$/);
    if (fileMatch) return [...base, { label: `file · ${decodeURIComponent(fileMatch[1]).slice(0, 16)}` }];
    return base;
  }, [name, location.pathname]);

  const isGraph = location.pathname.endsWith("/graph");
  const isPublications = location.pathname.endsWith("/publications");
  const isSearch = location.pathname.endsWith("/search");
  const page: VaultPageKind = isGraph
    ? "graph"
    : isPublications
      ? "publish"
      : isSearch
        ? "search"
        : "overview";

  // Graph owns the full canvas — the tree is redundant there.
  const showTree = visible && !isGraph;

  return (
    <VaultRefreshProvider refetchTree={refetchTree} refetchVaults={refetchVaults}>
      <div className="flex flex-col h-full min-h-0">
        <TitleBar
          crumbs={crumbs}
          right={name ? <VaultActions vault={name} page={page} /> : undefined}
        />

        <div className="flex flex-1 min-h-0">
          {/* Collapsed — slim reopen affordance */}
          {!visible && !isGraph && (
            <button
              onClick={() => setTreeVisible(true)}
              title="Show tree (⌘\\)"
              aria-label="Show vault tree"
              className="my-2 ml-2 self-start inline-flex h-9 w-7 items-center justify-center rounded-[var(--radius-md)] border border-border bg-surface shadow-sm text-foreground-muted hover:text-foreground hover:bg-surface-muted transition-token focus:outline-none focus-visible:ring-2 focus-visible:ring-ring cursor-pointer"
            >
              <ChevronsRight className="h-4 w-4" aria-hidden />
            </button>
          )}

          {/* Floating, resizable sidebar — width animates on collapse/expand.
              Always mounted (when not on /graph) so the toggle slides smoothly
              instead of popping; the panel keeps a fixed width and slides under
              the overflow-hidden wrapper. */}
          {!isGraph && (
            <div
              className="shrink-0 overflow-hidden transition-[width] duration-300 ease-out"
              style={{ width: visible ? tree.width + 8 : 0 }}
              aria-hidden={!visible}
            >
              <div className="h-full py-2 pl-2" style={{ width: tree.width + 8 }}>
                <aside
                  style={{ width: tree.width }}
                  className="h-full flex flex-col min-h-0 rounded-[var(--radius-lg)] border border-border bg-surface shadow-md overflow-hidden"
                  aria-label="Vault navigation"
                >
                  <div className="flex items-center justify-between h-9 px-2.5 border-b border-border shrink-0">
                    <span className="coord-ink">Workspace</span>
                    <button
                      onClick={() => setTreeVisible(false)}
                      title="Collapse (⌘\\)"
                      aria-label="Collapse vault tree"
                      className="inline-flex h-6 w-6 items-center justify-center rounded-[var(--radius-sm)] text-foreground-muted hover:text-foreground hover:bg-surface-muted transition-token cursor-pointer"
                    >
                      <ChevronsLeft className="h-4 w-4" aria-hidden />
                    </button>
                  </div>
                  <div className="shrink-0 max-h-[42%] overflow-y-auto border-b border-border rail-scroll">
                    <VaultNav current={name || ""} onRefetchReady={onVaultsRefetchReady} />
                  </div>
                  {name && (
                    <div className="flex-1 min-h-0 overflow-y-auto rail-scroll">
                      <VaultExplorer vault={name} onRefetchReady={onTreeRefetchReady} />
                    </div>
                  )}
                </aside>
              </div>
            </div>
          )}
          {/* resize handle — only when expanded; line is invisible until hover
              so there's no hard seam between the panel and the content. */}
          {showTree && (
            <div
              role="separator"
              aria-orientation="vertical"
              aria-label="Resize tree panel"
              title="Drag to resize · double-click to reset"
              {...tree.handlers}
              className="group relative z-10 w-2 shrink-0 cursor-col-resize touch-none"
            >
              <div className="mx-auto h-full w-px bg-transparent transition-colors group-hover:bg-accent group-active:bg-accent" />
            </div>
          )}

          {/* Content column */}
          {isGraph ? (
            <div className="flex-1 min-w-0 min-h-0 relative bg-background overflow-hidden">
              <ErrorBoundary resetKeys={[location.pathname]}>
                <Outlet />
              </ErrorBoundary>
            </div>
          ) : (
            <div className="flex-1 min-w-0 min-h-0 overflow-y-auto bg-background">
              <div className="mx-auto max-w-[1100px] px-6 py-8 lg:px-10 lg:py-10">
                <ErrorBoundary resetKeys={[location.pathname]}>
                  <Outlet />
                </ErrorBoundary>
              </div>
            </div>
          )}
        </div>
      </div>
    </VaultRefreshProvider>
  );
}
