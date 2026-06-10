import { Outlet, useLocation, useParams } from "react-router-dom";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { PanelLeftClose, PanelLeftOpen } from "lucide-react";
import { VaultExplorer } from "@/components/vault-explorer";
import { VaultRail } from "@/components/vault-rail";
import { TitleBar, VaultActions, type Crumb, type VaultPageKind } from "@/components/title-bar";
import { ErrorBoundary } from "@/components/error-boundary";
import { VaultRefreshProvider } from "@/contexts/vault-refresh-context";
import { useColumnResize } from "@/hooks/use-column-resize";

const TREE_VISIBLE_KEY = "akb.treeVisible";
const VAULT_COLLAPSED_KEY = "akb.vaultRailCollapsed";

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

  // The vault column can simplify to a thin icon rail (persisted) when the user
  // wants the space back; the tree column collapses independently via ⌘\.
  const [vaultCollapsed, setVaultCollapsed] = useState<boolean>(() => {
    if (typeof window === "undefined") return false;
    return window.localStorage.getItem(VAULT_COLLAPSED_KEY) === "1";
  });
  const toggleVaultCollapsed = useCallback(() => {
    setVaultCollapsed((c) => {
      const next = !c;
      window.localStorage.setItem(VAULT_COLLAPSED_KEY, next ? "1" : "0");
      return next;
    });
  }, []);

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

  // The sidebar vault list carries vault identity; the breadcrumb anchors the
  // current vault (link to overview) + the sub-path within it.
  const crumbs = useMemo<Crumb[]>(() => {
    if (!name) return [];
    const base: Crumb[] = [{ label: name, to: `/vault/${name}` }];
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
    // Named section sub-routes — label the current location so the breadcrumb's
    // last (aria-current) crumb isn't the vault name itself on these pages.
    const SECTION_LABELS: Record<string, string> = {
      settings: "Settings",
      members: "Members",
      activity: "Activity",
      search: "Search",
      publications: "Publications",
    };
    const tail = location.pathname.split("/").pop() || "";
    if (SECTION_LABELS[tail]) return [...base, { label: SECTION_LABELS[tail] }];
    return base;
  }, [name, location.pathname]);

  const isGraph = location.pathname.endsWith("/graph");
  const isPublications = location.pathname.endsWith("/publications");
  const isSearch = location.pathname.endsWith("/search");
  const isMembers = location.pathname.endsWith("/members");
  const isSettings = location.pathname.endsWith("/settings");
  const isActivity = location.pathname.endsWith("/activity");
  const page: VaultPageKind = isGraph
    ? "graph"
    : isPublications
      ? "publish"
      : isSearch
        ? "search"
        : isMembers
          ? "members"
          : isSettings
            ? "settings"
            : isActivity
              ? "activity"
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
          {/* Sidebar shell card — TWO columns: the vault RAIL is always mounted
              (incl. /graph, so switching never disappears) on its own scroll
              axis; the collection-TREE column sits to its right, hidden on
              /graph and collapsible via ⌘\ (width animates to 0). Separating
              the two navs onto different axes is what stops them cramping each
              other / nesting two scrolls in one column. */}
          <div className="shrink-0 h-full py-2 pl-2">
            <div className="h-full flex min-h-0 rounded-[var(--radius-lg)] border border-border bg-surface shadow-md overflow-hidden">
              <VaultRail
                current={name || ""}
                onRefetchReady={onVaultsRefetchReady}
                collapsed={vaultCollapsed}
                onToggleCollapsed={toggleVaultCollapsed}
              />
              {/* Collection tree column — expanded: a "Collections" header (with
                  a « collapse toggle that mirrors the vault column's) over the
                  tree. Collapsed: it doesn't vanish — it leaves a thin strip
                  with just the » expand toggle at the top, exactly like the
                  vault rail, so both columns minimize the same way. */}
              {!isGraph && visible && (
                <div className="shrink-0 h-full flex flex-col min-h-0" style={{ width: tree.width }}>
                  <div className="flex items-center justify-between h-9 px-3 shrink-0 border-b border-border">
                    <span className="coord-ink">Collections</span>
                    <button
                      type="button"
                      onClick={() => setTreeVisible(false)}
                      title="Collapse tree (⌘\\)"
                      aria-label="Collapse collection tree"
                      aria-expanded={true}
                      className="text-foreground-muted hover:text-link transition-colors cursor-pointer focus:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2 focus-visible:ring-offset-surface"
                    >
                      <PanelLeftClose className="h-4 w-4" aria-hidden />
                    </button>
                  </div>
                  {name ? (
                    <div className="flex-1 min-h-0">
                      <VaultExplorer vault={name} onRefetchReady={onTreeRefetchReady} />
                    </div>
                  ) : (
                    <div className="flex-1 min-h-0 flex items-center justify-center px-6 text-center">
                      <p className="coord leading-relaxed">
                        No vault open.
                        <br />
                        Select one on the left to see its collections.
                      </p>
                    </div>
                  )}
                </div>
              )}
              {!isGraph && !visible && (
                <nav
                  aria-label="Collections (collapsed)"
                  className="shrink-0 h-full w-10 flex flex-col items-center py-2"
                >
                  <button
                    type="button"
                    onClick={() => setTreeVisible(true)}
                    title="Show tree (⌘\\)"
                    aria-label="Show collection tree"
                    aria-expanded={false}
                    className="flex h-9 w-9 items-center justify-center rounded-[var(--radius-md)] text-foreground-muted hover:text-foreground hover:bg-surface-hover transition-token focus:outline-none focus-visible:ring-2 focus-visible:ring-ring cursor-pointer"
                  >
                    <PanelLeftOpen className="h-4 w-4" aria-hidden />
                  </button>
                </nav>
              )}
            </div>
          </div>
          {/* resize handle — resizes the tree column; only when the tree shows.
              Delta-based, so the fixed rail offset doesn't affect it. */}
          {showTree && (
            <div
              role="separator"
              aria-orientation="vertical"
              aria-label="Resize tree panel"
              title="Drag to resize · double-click to reset"
              {...tree.handlers}
              className="group relative z-10 w-2 shrink-0 cursor-col-resize touch-none"
            >
              <div className="mx-auto h-full w-px bg-border transition-colors group-hover:bg-primary group-active:bg-primary" />
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
