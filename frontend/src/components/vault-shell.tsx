import { Outlet, useLocation, useNavigate, useParams } from "react-router-dom";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { PanelLeftClose, PanelLeftOpen } from "lucide-react";
import { VaultExplorer } from "@/components/vault-explorer";
import { VaultCreateDialog } from "@/components/vault-create-dialog";
import { DocumentCreateDialog } from "@/components/document-create-dialog";
import { VaultRail } from "@/components/vault-rail";
import {
  TitleBar,
  VaultActions,
  type Crumb,
  type VaultPageKind,
} from "@/components/title-bar";
import { ErrorBoundary } from "@/components/error-boundary";
import { VaultRefreshProvider } from "@/contexts/vault-refresh-context";
import { VaultCreateDialogProvider } from "@/contexts/vault-create-dialog-context";
import {
  DocumentCreateDialogProvider,
  type DocumentCreateDialogOptions,
} from "@/contexts/document-create-dialog-context";
import { useColumnResize } from "@/hooks/use-column-resize";
import { cn } from "@/lib/utils";

const TREE_VISIBLE_KEY = "akb.treeVisible";
const VAULT_COLLAPSED_KEY = "akb.vaultRailCollapsed";

/**
 * Vault workspace: one shared command row — Vaults | Collections | TitleBar —
 * over three aligned columns. The **persistent, resizable left sidebar** (vault
 * switcher + collection tree) is the primary navigation surface; it stays
 * pinned so jumping between docs/collections never costs an extra click.
 * Collapse it with the Tree button or ⌘\ (state persists); the tree is hidden
 * on /graph, which owns the full canvas.
 */
export function VaultShell() {
  const { name } = useParams<{ name: string }>();
  const location = useLocation();
  const navigate = useNavigate();
  const isGraph = location.pathname.endsWith("/graph");
  const isDocument = location.pathname.includes("/doc/");
  const isTable = location.pathname.includes("/table/");
  const isFile = location.pathname.includes("/file/");
  const isResourceViewer = isDocument || isTable || isFile;
  const isPublications = location.pathname.endsWith("/publications");
  const isSearch = location.pathname.endsWith("/search");
  const isMembers = location.pathname.endsWith("/members");
  const isSettings = location.pathname.endsWith("/settings");
  const isActivity = location.pathname.endsWith("/activity");
  const overviewPath = name ? `/vault/${encodeURIComponent(name)}` : "";
  const isOverview =
    !!overviewPath && location.pathname.replace(/\/+$/, "") === overviewPath;
  const [createVaultOpen, setCreateVaultOpen] = useState(false);
  const createVaultTriggerRef = useRef<HTMLElement | null>(null);
  const [createDocument, setCreateDocument] = useState({
    open: false,
    collection: "",
    session: 0,
  });
  const createDocumentTriggerRef = useRef<HTMLElement | null>(null);
  const [desktopNav, setDesktopNav] = useState(() =>
    typeof window !== "undefined"
      ? window.matchMedia("(min-width: 1024px)").matches
      : true,
  );
  const [mobileNavOpen, setMobileNavOpen] = useState(false);
  const [mobileRailCollapsed, setMobileRailCollapsed] = useState(false);
  const [visible, setVisible] = useState<boolean>(() => {
    if (typeof window === "undefined") return true;
    return window.localStorage.getItem(TREE_VISIBLE_KEY) !== "0";
  });
  const [routeTreeOverride, setRouteTreeOverride] = useState<{
    key: string;
    visible: boolean;
  } | null>(null);
  const tree = useColumnResize({
    storageKey: "akb.treeWidth.v2",
    min: 220,
    max: 480,
    default: 258,
  });
  // Vault switcher rail (expanded mode) — drag-resizable like the tree, its own
  // persisted width. Collapsed mode stays a fixed w-14 icon rail.
  const rail = useColumnResize({
    storageKey: "akb.vaultRailWidth.v2",
    min: 192,
    max: 320,
    default: 218,
  });

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

  useEffect(() => {
    const media = window.matchMedia("(min-width: 1024px)");
    const sync = () => setDesktopNav(media.matches);
    sync();
    media.addEventListener("change", sync);
    return () => media.removeEventListener("change", sync);
  }, []);

  useEffect(() => {
    setMobileNavOpen(false);
  }, [location.pathname]);

  const setTreeVisible = useCallback((next: boolean) => {
    setVisible(next);
    window.localStorage.setItem(TREE_VISIBLE_KEY, next ? "1" : "0");
  }, []);

  // Collection navigation is contextual to content browsing. Tool/admin views
  // start with it folded so their table, form, or result workspace owns the
  // horizontal space without overwriting the user's browsing preference.
  const toolView = isSearch || isMembers || isPublications || isSettings || isActivity;
  const routeTreeKey = toolView ? `${name ?? ""}:${location.pathname}` : "";
  const effectiveTreeVisible = toolView
    ? routeTreeOverride?.key === routeTreeKey && routeTreeOverride.visible
    : visible;
  const setEffectiveTreeVisible = useCallback(
    (next: boolean) => {
      if (toolView) {
        setRouteTreeOverride({ key: routeTreeKey, visible: next });
        return;
      }
      setTreeVisible(next);
    },
    [routeTreeKey, setTreeVisible, toolView],
  );

  useEffect(() => {
    if (!toolView) setRouteTreeOverride(null);
  }, [toolView]);

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
  const openCreateVault = useCallback(() => {
    if (document.activeElement instanceof HTMLElement) {
      createVaultTriggerRef.current = document.activeElement;
    }
    setMobileNavOpen(false);
    setCreateVaultOpen(true);
  }, []);
  const handleVaultCreated = useCallback(
    (vaultName: string) => {
      setCreateVaultOpen(false);
      refetchVaults();
      navigate(`/vault/${vaultName}`);
    },
    [navigate, refetchVaults],
  );
  const openCreateDocument = useCallback(
    (options?: DocumentCreateDialogOptions) => {
      if (!name) return;
      if (document.activeElement instanceof HTMLElement) {
        createDocumentTriggerRef.current = document.activeElement;
      }
      setMobileNavOpen(false);
      setCreateDocument((current) => ({
        open: true,
        collection: options?.collection?.trim() ?? "",
        session: current.session + 1,
      }));
    },
    [name],
  );
  const handleDocumentCreated = useCallback(
    (path?: string) => {
      setCreateDocument((current) => ({ ...current, open: false }));
      if (!name) return;
      navigate(
        path
          ? `/vault/${name}/doc/${encodeURIComponent(path)}`
          : `/vault/${name}`,
      );
    },
    [name, navigate],
  );

  // ⌘\ / ctrl+\ toggles the collection tree on desktop and the complete
  // workspace navigator on compact screens.
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if ((e.metaKey || e.ctrlKey) && e.key === "\\") {
        e.preventDefault();
        if (!desktopNav) {
          setMobileNavOpen((open) => !open);
          return;
        }
        setEffectiveTreeVisible(!effectiveTreeVisible);
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [desktopNav, effectiveTreeVisible, setEffectiveTreeVisible]);

  // The sidebar vault list carries vault identity; the breadcrumb anchors the
  // current vault (link to overview) + the sub-path within it.
  const crumbs = useMemo<Crumb[]>(() => {
    if (!name) return [{ label: "Vaults" }];
    const base: Crumb[] = [
      { label: "Home", to: "/" },
      { label: name, to: `/vault/${name}` },
    ];
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
    if (tableMatch)
      return [
        ...base,
        { label: `table · ${decodeURIComponent(tableMatch[1])}` },
      ];
    const fileMatch = location.pathname.match(/^\/vault\/[^/]+\/file\/(.+)$/);
    if (fileMatch)
      return [
        ...base,
        { label: `file · ${decodeURIComponent(fileMatch[1]).slice(0, 16)}` },
      ];
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
  // Collections belong to an active vault. Keeping an empty or collapsed tree
  // strip mounted on `/vault` made the no-selection state look like a broken
  // three-column workspace, so the shell becomes a clean two-column chooser
  // until a vault is opened.
  const showTree = !!name && effectiveTreeVisible && !isGraph;
  const workspaceNavigationWidth =
    (vaultCollapsed ? 56 : rail.width) +
    (!vaultCollapsed && !isGraph && !!name ? 8 : 0) +
    (!isGraph && !!name ? (effectiveTreeVisible ? tree.width : 40) : 0) +
    (showTree ? 8 : 0);
  const effectiveVaultCollapsed = desktopNav
    ? vaultCollapsed
    : mobileRailCollapsed;
  const toggleEffectiveVaultCollapsed = desktopNav
    ? toggleVaultCollapsed
    : () => setMobileRailCollapsed((collapsed) => !collapsed);
  const mobileTreeWidth = effectiveVaultCollapsed
    ? "calc(100vw - 3.5rem)"
    : "calc(100vw - 10rem)";

  return (
    <VaultCreateDialogProvider openCreateVault={openCreateVault}>
      <DocumentCreateDialogProvider openCreateDocument={openCreateDocument}>
        <VaultRefreshProvider
          refetchTree={refetchTree}
          refetchVaults={refetchVaults}
        >
        <div className="flex flex-col h-full min-h-0">
        <div className="relative flex flex-1 min-h-0">
          {/* Workspace navigation — TWO columns: the vault RAIL is always mounted
              (incl. /graph, so switching never disappears) on its own scroll
              axis; the collection-TREE column sits to its right, hidden on
              /graph and collapsible via ⌘\ (width animates to 0). Separating
              the two navs onto different axes stops nested scrolls. The shell
              is intentionally flush with the workspace rather than floating as
              a card, matching the mockup's command-deck structure. */}
          <div
            id="vault-workspace-navigation"
            className={cn(
              "absolute top-10 bottom-0 left-0 z-[var(--z-overlay)] hidden max-w-full shrink-0 min-h-0 bg-surface shadow-lg",
              "lg:static lg:z-auto lg:flex lg:shadow-none",
              mobileNavOpen && "flex",
              !showTree && "border-r border-border",
            )}
          >
            <VaultRail
              current={name || ""}
              onRefetchReady={onVaultsRefetchReady}
              onCreateVault={openCreateVault}
              collapsed={effectiveVaultCollapsed}
              onToggleCollapsed={toggleEffectiveVaultCollapsed}
              width={desktopNav ? rail.width : 160}
            />
            {/* Rail resize handle — sits between the vault rail and the tree
                  column INSIDE the card, and is the divider for the two. Only
                  in expanded mode with a tree region to its right (off /graph);
                  the collapsed icon rail is a fixed strip. */}
            {desktopNav && !vaultCollapsed && !isGraph && !!name && (
              <div
                role="separator"
                aria-orientation="vertical"
                aria-label="Resize vault list"
                title="Drag to resize · double-click to reset"
                {...rail.handlers}
                className="group relative z-10 w-2 shrink-0 cursor-col-resize touch-none"
              >
                <div className="mx-auto h-full w-px bg-border transition-colors group-hover:bg-primary group-active:bg-primary" />
              </div>
            )}
            {/* Collection tree column — expanded: a "Collections" header (with
                  a « collapse toggle that mirrors the vault column's) over the
                  tree. Collapsed: it doesn't vanish — it leaves a thin strip
                  with just the » expand toggle at the top, exactly like the
                  vault rail, so both columns minimize the same way. */}
            {!isGraph && effectiveTreeVisible && !!name && (
              <div
                className="shrink-0 h-full flex flex-col min-h-0"
                style={{ width: desktopNav ? tree.width : mobileTreeWidth }}
              >
                <div className="flex-1 min-h-0">
                  <VaultExplorer
                    vault={name}
                    onRefetchReady={onTreeRefetchReady}
                    onCollapse={() => setEffectiveTreeVisible(false)}
                  />
                </div>
              </div>
            )}
            {!isGraph && !effectiveTreeVisible && !!name && (
              <nav
                aria-label="Collections (collapsed)"
                className="h-full w-10 shrink-0 border-r border-border"
              >
                <div className="flex h-10 items-center justify-center border-b border-border">
                  <button
                    type="button"
                    onClick={() => setEffectiveTreeVisible(true)}
                    title="Show tree (⌘\\)"
                    aria-label="Show collection tree"
                    aria-expanded={false}
                    className="flex h-8 w-8 items-center justify-center rounded-[var(--radius-md)] text-foreground-muted hover:text-foreground hover:bg-surface-hover transition-token focus:outline-none focus-visible:ring-2 focus-visible:ring-ring cursor-pointer"
                  >
                    <PanelLeftOpen className="h-4 w-4" aria-hidden />
                  </button>
                </div>
              </nav>
            )}
          </div>
          {/* resize handle — resizes the tree column; only when the tree shows.
              Delta-based, so the fixed rail offset doesn't affect it. */}
          {desktopNav && showTree && (
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

          {/* The right column owns the third segment of the shared command row.
              Keeping TitleBar here (rather than above the whole workspace)
              lets all three column headers align naturally at the same level. */}
          <div className="flex min-h-0 min-w-0 flex-1 flex-col">
            <TitleBar
              crumbs={crumbs}
              right={
                name ? <VaultActions vault={name} page={page} /> : undefined
              }
              left={
                <button
                  type="button"
                  onClick={() => setMobileNavOpen((open) => !open)}
                  aria-label={
                    mobileNavOpen
                      ? "Close vault navigation"
                      : "Open vault navigation"
                  }
                  aria-expanded={mobileNavOpen}
                  aria-controls="vault-workspace-navigation"
                  className="inline-flex h-8 w-8 shrink-0 items-center justify-center rounded-[var(--radius-md)] text-foreground-muted hover:bg-surface-hover hover:text-foreground focus:outline-none focus-visible:ring-2 focus-visible:ring-ring lg:hidden"
                >
                  {mobileNavOpen ? (
                    <PanelLeftClose className="h-4 w-4" aria-hidden />
                  ) : (
                    <PanelLeftOpen className="h-4 w-4" aria-hidden />
                  )}
                </button>
              }
              className="shrink-0"
              showBack={false}
            />

            {isGraph || isResourceViewer || isSearch ? (
              <div className="relative min-h-0 min-w-0 flex-1 overflow-hidden bg-background">
                <ErrorBoundary resetKeys={[location.pathname]}>
                  <Outlet />
                </ErrorBoundary>
              </div>
            ) : (
              <div
                data-slot="vault-route-viewport"
                className={cn(
                  "min-h-0 min-w-0 flex-1 bg-background",
                  isSettings || isMembers
                    ? "overflow-y-auto xl:overflow-hidden"
                    : "overflow-y-auto",
                )}
              >
                <div
                  className={cn(
                    "w-full",
                    isSettings || isMembers
                      ? "min-h-full xl:h-full xl:min-h-0"
                      : isOverview
                        ? "min-h-full px-2 py-3 lg:p-0"
                        : isActivity || isPublications
                          ? "px-3 py-5 lg:px-4 lg:py-6 xl:px-5"
                          : "px-5 py-5 lg:px-7 lg:py-6 xl:px-8",
                  )}
                >
                  <ErrorBoundary resetKeys={[location.pathname]}>
                    <Outlet />
                  </ErrorBoundary>
                </div>
              </div>
            )}
          </div>
        </div>
        </div>
        <VaultCreateDialog
          open={createVaultOpen}
          onOpenChange={setCreateVaultOpen}
          onCreated={handleVaultCreated}
          returnFocusRef={createVaultTriggerRef}
        />
        {name && (
          <DocumentCreateDialog
            key={`${name}:${createDocument.session}`}
            open={createDocument.open}
            vault={name}
            initialCollection={createDocument.collection}
            onOpenChange={(open) =>
              setCreateDocument((current) => ({ ...current, open }))
            }
            onCreated={handleDocumentCreated}
            returnFocusRef={createDocumentTriggerRef}
            desktopLeftOffset={workspaceNavigationWidth + 16}
          />
        )}
        </VaultRefreshProvider>
      </DocumentCreateDialogProvider>
    </VaultCreateDialogProvider>
  );
}
