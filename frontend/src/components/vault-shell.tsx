import { Outlet, useLocation, useNavigate, useParams, useOutletContext } from "react-router-dom";
import { useCallback, useEffect, useLayoutEffect, useRef, useState } from "react";
import { AppPageLocation } from "@/components/app-page-location";
import { VaultExplorer } from "@/components/vault-explorer";
import { VaultCreateDialog } from "@/components/vault-create-dialog";
import { DocumentCreateDialog } from "@/components/document-create-dialog";
import { VaultNavigationHeader } from "@/components/vault-navigation-header";
import { VaultDirectorySidebar } from "@/components/vault-directory-sidebar";
import { VaultParentBackLink, VaultNavigationToggle } from "@/components/vault-navigation-controls";
import { WorkspacePersonalSections } from "@/components/workspace-personal-sections";
import { useCurrentUser } from "@/contexts/current-user-context";
import { useVaults } from "@/hooks/use-vaults";
import { useWorkspaceShortcuts } from "@/lib/workspace-shortcuts";
import { TitleBar } from "@/components/title-bar";
import { ErrorBoundary } from "@/components/error-boundary";
import { VaultRefreshProvider } from "@/contexts/vault-refresh-context";
import { VaultCreateDialogProvider } from "@/contexts/vault-create-dialog-context";
import {
  DocumentCreateDialogProvider,
  type DocumentCreateDialogOptions,
} from "@/contexts/document-create-dialog-context";
import { useColumnResize } from "@/hooks/use-column-resize";
import { cn } from "@/lib/utils";
import { focusedVaultPageForPath } from "@/lib/vault-workspace";

const TREE_VISIBLE_KEY = "akb.treeVisible";

export type { VaultNavigationControl } from "@/components/vault-navigation-controls";

/**
 * One context panel owns Vault selection and content exploration. The global
 * app rail remains separate; the same panel shows Vaults or their collections.
 * Panel width and visibility survive route changes and responsive overlays.
 */
export function VaultShell() {
  const { name } = useParams<{ name: string }>();
  const location = useLocation();
  const navigate = useNavigate();
  const layout = useOutletContext<{
    setVaultNavigationWidth?: (width: number) => void;
    minimumVaultWorkspaceWidth?: number;
  } | null>();
  const focusedPage = focusedVaultPageForPath(location.pathname);
  const isFocusedWorkspace = focusedPage !== null;
  const isGraph = focusedPage === "graph";
  const isDocument = location.pathname.includes("/doc/");
  const isTable = location.pathname.includes("/table/");
  const isFile = location.pathname.includes("/file/");
  const isResourceViewer = isDocument || isTable || isFile;
  const isPublications = focusedPage === "publish";
  const isSearch = location.pathname.endsWith("/search");
  const isMembers = focusedPage === "members";
  const isSettings = focusedPage === "settings";
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
  const [directoryVisible, setDirectoryVisible] = useState(true);
  const previousVault = useRef(name);
  const workspaceRef = useRef<HTMLDivElement>(null);
  const navigationStubRef = useRef<HTMLDivElement>(null);
  const [collapsedNavigationWidth, setCollapsedNavigationWidth] = useState(37);
  const navigationHadFocus = useRef(false);
  const [workspaceWidth, setWorkspaceWidth] = useState(0);
  const [minimumContentWidth, setMinimumContentWidth] = useState(656);
  const [visible, setVisible] = useState<boolean>(() => {
    if (typeof window === "undefined") return true;
    return window.localStorage.getItem(TREE_VISIBLE_KEY) !== "0";
  });
  useLayoutEffect(() => {
    if (previousVault.current === name) return;
    // Entering a Vault replaces a visible directory in-place. A fresh deep
    // link still honors its saved preference; returning restores the directory.
    if (!previousVault.current && name) setVisible(directoryVisible);
    if (previousVault.current && !name) setDirectoryVisible(true);
    previousVault.current = name;
  }, [name, directoryVisible]);
  const [routeTreeOverride, setRouteTreeOverride] = useState<{
    key: string;
    visible: boolean;
  } | null>(null);
  const panel = useColumnResize({
    storageKey: "akb.focusedNavigationWidth.v1",
    min: 224,
    max: 360,
    default: 256,
  });
  const navigationTrigger = useCallback(() => {
    const opener = document.getElementById("vault-navigation-trigger");
    if (opener && !opener.hidden) return opener;
    const closer = document.getElementById("vault-navigation-collapse");
    return closer && !closer.closest("[hidden]") ? closer : null;
  }, []);
  const focusNavigationTrigger = useCallback(() => {
    navigationTrigger()?.focus();
  }, [navigationTrigger]);

  useEffect(() => {
    const media = window.matchMedia("(min-width: 1024px)");
    const sync = () => setDesktopNav(media.matches);
    sync();
    media.addEventListener("change", sync);
    return () => media.removeEventListener("change", sync);
  }, []);

  useEffect(() => {
    const showDirectory = location.pathname === "/vault" && location.state?.vaultSidebarNavigation === true;
    setMobileNavOpen(showDirectory);
    if (!showDirectory) return;
    setDirectoryVisible(true);
    const frame = requestAnimationFrame(() => {
      document.querySelector<HTMLInputElement>('[data-slot="vault-directory-sidebar"] input[type="search"]')?.focus();
    });
    return () => cancelAnimationFrame(frame);
  }, [location.pathname, location.search, location.state?.vaultSidebarNavigation]);

  const setTreeVisible = useCallback((next: boolean) => {
    setVisible(next);
    try { window.localStorage.setItem(TREE_VISIBLE_KEY, next ? "1" : "0"); } catch { /* Keep the session choice when storage is unavailable. */ }
  }, []);

  // A Collection deep link may reveal its tree without changing the user's
  // saved rail preference. Ordinary section changes retain the same rail.
  const collectionTarget = location.pathname === `/vault/${encodeURIComponent(name ?? "")}`
    ? new URLSearchParams(location.search).get("collection") : null;
  // A card in the main Collection workspace already presents its destination;
  // a raw deep link or picker shortcut may still reveal the explorer for it.
  const revealCollectionTarget = collectionTarget && location.state?.collectionView !== "main";
  const routeTreeKey = revealCollectionTarget ? `collection:${location.key}` : "";
  const effectiveTreeVisible = !name ? directoryVisible : revealCollectionTarget
    ? routeTreeOverride?.key === routeTreeKey ? routeTreeOverride.visible : true
    : visible;
  const setEffectiveTreeVisible = useCallback(
    (next: boolean) => {
      if (!name) { setDirectoryVisible(next); return; }
      if (revealCollectionTarget) {
        setRouteTreeOverride({ key: routeTreeKey, visible: next });
        return;
      }
      setTreeVisible(next);
    },
    [routeTreeKey, setTreeVisible, revealCollectionTarget, name],
  );

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
      createVaultTriggerRef.current = mobileNavOpen ? document.getElementById("vault-navigation-trigger") : document.activeElement;
    }
    setMobileNavOpen(false);
    setCreateVaultOpen(true);
  }, [mobileNavOpen]);
  const handleVaultCreated = useCallback(
    (vaultName: string) => {
      setCreateVaultOpen(false);
      refetchVaults();
      navigate(`/vault/${encodeURIComponent(vaultName)}`);
    },
    [navigate, refetchVaults],
  );
  const openCreateDocument = useCallback(
    (options?: DocumentCreateDialogOptions) => {
      if (!name) return;
      if (document.activeElement instanceof HTMLElement) {
        createDocumentTriggerRef.current = mobileNavOpen ? document.getElementById("vault-navigation-trigger") : document.activeElement;
      }
      setMobileNavOpen(false);
      setCreateDocument((current) => ({
        open: true,
        collection: options?.collection?.trim() ?? "",
        session: current.session + 1,
      }));
    },
    [name, mobileNavOpen],
  );
  const handleDocumentCreated = useCallback(
    (path?: string) => {
      setCreateDocument((current) => ({ ...current, open: false }));
      if (!name) return;
      navigate(
        path
          ? `/vault/${encodeURIComponent(name)}/doc/${encodeURIComponent(path)}`
          : `/vault/${encodeURIComponent(name)}`,
      );
    },
    [name, navigate],
  );

  const workspaceNavigationWidth = panel.width + 1;
  const settledNavigationWidth = panel.settledWidth + 1;
  // Measure the workspace after the personal rail, not a viewport breakpoint.
  // A narrow reading surface temporarily uses the existing navigation drawer;
  // neither the saved widths nor the user's expanded/collapsed preference changes.
  useLayoutEffect(() => {
    const element = workspaceRef.current;
    if (!element) return;
    const measure = () => {
      // Rail widths and header requirements are layout CSS pixels. A visual
      // rect includes CSS zoom and would keep inline rails over a clipped header.
      setWorkspaceWidth(element.clientWidth);
      setMinimumContentWidth(41 * (parseFloat(getComputedStyle(document.documentElement).fontSize) || 16));
    };
    measure();
    const observer = new ResizeObserver(measure);
    observer.observe(element);
    return () => observer.disconnect();
  }, []);
  const navigationOverlay = desktopNav && workspaceWidth > 0 && workspaceWidth - settledNavigationWidth < (layout?.minimumVaultWorkspaceWidth ?? minimumContentWidth);
  const inlineNavigation = desktopNav && !navigationOverlay && effectiveTreeVisible;
  const navigationOpen = inlineNavigation || mobileNavOpen;
  useEffect(() => {
    if (!revealCollectionTarget) setRouteTreeOverride(null);
    if (revealCollectionTarget && (!desktopNav || navigationOverlay)) setMobileNavOpen(true);
  }, [revealCollectionTarget, routeTreeKey, desktopNav, navigationOverlay]);
  const toggleNavigation = useCallback(() => {
    if (desktopNav && !navigationOverlay) {
      setMobileNavOpen(false);
      setEffectiveTreeVisible(!effectiveTreeVisible);
    } else {
      setMobileNavOpen(open => !open);
    }
    requestAnimationFrame(() => {
      const target = navigationTrigger();
      if (target !== document.activeElement) target?.focus();
    });
  }, [desktopNav, navigationOverlay, effectiveTreeVisible, setEffectiveTreeVisible, navigationTrigger]);
  useLayoutEffect(() => {
    if (navigationOpen || !navigationHadFocus.current) return;
    const active = document.activeElement;
    const pane = document.getElementById("vault-workspace-navigation");
    // Responsive reflow can hide the focused control before this effect runs.
    // Keep a visible keyboard starting point without stealing dialog focus.
    if (active === document.body || pane?.contains(active)
      || active?.hasAttribute("data-vault-navigation-resizer")) {
      navigationHadFocus.current = false;
      focusNavigationTrigger();
    }
  }, [navigationOpen, desktopNav, focusNavigationTrigger]);
  const closeAfterNavigation = useCallback(() => {
    // Header links mark the destination for the persistent main-focus observer.
    setMobileNavOpen(false);
  }, []);
  useLayoutEffect(() => {
    const stub = navigationStubRef.current;
    if (!stub) return;
    const measure = () => setCollapsedNavigationWidth(stub.offsetWidth || (desktopNav ? 37 : 45));
    measure();
    const observer = new ResizeObserver(measure);
    observer.observe(stub);
    return () => observer.disconnect();
  }, [inlineNavigation, desktopNav]);

  useEffect(() => {
    if (inlineNavigation || !mobileNavOpen) return;
    const panel = document.getElementById("vault-workspace-navigation");
    const frame = window.requestAnimationFrame(() => {
      if (!panel?.contains(document.activeElement) && !document.activeElement?.hasAttribute("data-vault-navigation-resizer")) {
        const initialControl = panel?.querySelector<HTMLElement>("#vault-navigation-collapse")
          ?? panel?.querySelector<HTMLElement>('button:not([disabled]), a[href]');
        initialControl?.focus();
      }
    });
    const tooltipEscapes = new WeakSet<KeyboardEvent>();
    const rememberTooltipEscape = (event: KeyboardEvent) => {
      if (event.key !== "Escape" || !(event.target instanceof HTMLElement)
        || !panel?.contains(event.target)) return;
      const describesTooltip = event.target.getAttribute("aria-describedby")?.split(/\s+/)
        .some(id => document.getElementById(id)?.getAttribute("role") === "tooltip");
      if (!describesTooltip && !panel.querySelector('[role="tooltip"]')) return;
      // Radix dismisses layers at document capture. Remember interactive layers
      // before they disappear so one Escape never closes both them and the rail.
      const nestedLayerOpen = Array.from(document.querySelectorAll(
        '[role="dialog"], [role="alertdialog"], [role="menu"], [role="listbox"], dialog[open]',
      )).some(layer => !layer.closest('[hidden], [inert], [aria-hidden="true"], [data-state="closed"]'));
      if (!nestedLayerOpen) tooltipEscapes.add(event);
    };
    const close = (event: KeyboardEvent) => {
      // A tooltip is only a label: its consumed Escape also dismisses the rail.
      // Pickers, menus, dialogs and other cancelled events keep their priority.
      if (event.key !== "Escape" || event.defaultPrevented && !tooltipEscapes.has(event)) return;
      setMobileNavOpen(false);
    };
    // This is a navigation disclosure, not a modal. Let Tab leave naturally,
    // dismissing the covering panel before the next destination takes focus.
    // Portalled switcher/collection menus remain part of the disclosure.
    const belongsToNavigation = (target: EventTarget | null) => {
      return target instanceof HTMLElement && (panel?.contains(target)
        || target.id.startsWith("vault-navigation-trigger")
        || target.hasAttribute("data-vault-navigation-resizer")
        || target.closest('[data-radix-popper-content-wrapper], [role="dialog"]'));
    };
    let leaveFrame: number | undefined;
    const leave = (event: FocusEvent) => {
      if (belongsToNavigation(event.target)) return;
      if (leaveFrame !== undefined) window.cancelAnimationFrame(leaveFrame);
      // A cancelled confirmation can briefly return focus to the editor before
      // its portalled Vault picker restores it. Let that picker settle first;
      // an intentional departure closes it before this frame and dismisses both.
      leaveFrame = window.requestAnimationFrame(() => {
        if (belongsToNavigation(document.activeElement)
          || panel?.querySelector('[data-vault-switcher-trigger][aria-expanded="true"]')) return;
        setMobileNavOpen(false);
      });
    };
    window.addEventListener("keydown", rememberTooltipEscape, true);
    window.addEventListener("keydown", close);
    document.addEventListener("focusin", leave);
    return () => {
      window.cancelAnimationFrame(frame);
      if (leaveFrame !== undefined) window.cancelAnimationFrame(leaveFrame);
      window.removeEventListener("keydown", rememberTooltipEscape, true);
      window.removeEventListener("keydown", close);
      document.removeEventListener("focusin", leave);
    };
  }, [inlineNavigation, mobileNavOpen, location.key]);

  // The same shortcut toggles the same panel on every named Vault route.
  useEffect(() => {
    const onKey = (event: KeyboardEvent) => {
      if (!event.defaultPrevented && !event.altKey && (event.metaKey || event.ctrlKey) && event.key === "\\") {
        event.preventDefault();
        toggleNavigation();
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [name, toggleNavigation]);

  const publishWidth = layout?.setVaultNavigationWidth;
  useLayoutEffect(() => {
    publishWidth?.(inlineNavigation ? workspaceNavigationWidth : collapsedNavigationWidth);
  }, [inlineNavigation, workspaceNavigationWidth, collapsedNavigationWidth, publishWidth]);
  useLayoutEffect(() => () => publishWidth?.(0), [publishWidth]);
  const cancelResize = panel.cancel;
  useLayoutEffect(() => {
    if (inlineNavigation && !panel.isResizing) setMobileNavOpen(false);
  }, [inlineNavigation, panel.width, panel.isResizing]);
  useEffect(() => {
    if (!desktopNav || !navigationOpen) cancelResize();
  }, [desktopNav, navigationOpen, cancelResize]);

  return (
    <VaultCreateDialogProvider openCreateVault={openCreateVault}>
      <DocumentCreateDialogProvider openCreateDocument={openCreateDocument}>
        <VaultRefreshProvider
          refetchTree={refetchTree}
          refetchVaults={refetchVaults}
        >
        <div className="flex flex-col h-full min-h-0">
        <div ref={workspaceRef} onFocusCapture={event => {
          const target = event.target as HTMLElement;
          navigationHadFocus.current = Boolean(target.closest("#vault-workspace-navigation") || target.hasAttribute("data-vault-navigation-resizer"));
        }} className="relative flex flex-1 min-h-0 min-w-0" data-navigation-mode={inlineNavigation ? "inline" : "overlay"} data-workspace-mode={isFocusedWorkspace ? "focused" : "content"}>
          {!inlineNavigation && mobileNavOpen && <button type="button" tabIndex={-1} aria-label="Dismiss vault navigation"
            onClick={() => { setMobileNavOpen(false); focusNavigationTrigger(); }}
            className="absolute inset-0 z-40 cursor-default bg-foreground/20" />}
          {!inlineNavigation && <div ref={navigationStubRef} data-slot="vault-navigation-collapsed" aria-hidden={navigationOpen || undefined}
            className={cn("relative flex w-9 shrink-0 flex-col border-r border-border bg-navigation max-sm:w-11 pointer-coarse:w-11", desktopNav && "-mt-14 h-[calc(100%+3.5rem)]")}>
            <div className="flex h-14 shrink-0 items-center justify-center border-b border-border">
              <VaultNavigationToggle id="vault-navigation-trigger" open={false} onToggle={toggleNavigation} hidden={navigationOpen} />
            </div>
          </div>}
          <>
          <div
            id="vault-workspace-navigation"
            aria-label={name ? `${name} navigation` : "Vault directory navigation"}
            hidden={!navigationOpen}
            className={cn(
              "absolute bottom-0 left-0 z-[var(--z-overlay)] max-w-full shrink-0 min-h-0 flex-col border-r border-border bg-navigation shadow-lg",
              desktopNav ? "-top-14" : "top-0",
              inlineNavigation && "lg:static lg:z-auto lg:-mt-14 lg:h-[calc(100%+3.5rem)] lg:shadow-none",
              navigationOpen ? "flex" : "hidden",
            )}
            style={{ width: desktopNav ? panel.width : "min(20rem, calc(100vw - 1.5rem))" }}
          >
            {name ? <><VaultNavigationHeader current={name} onCreateVault={openCreateVault}
              onRefetchReady={onVaultsRefetchReady} onNavigate={closeAfterNavigation} navigationControl={{ open: navigationOpen, onToggle: toggleNavigation }} />
            <div id="vault-collections-sidebar" data-slot="vault-collections-sidebar" className="flex min-h-0 flex-1 flex-col">
              <VaultExplorer key={name} vault={name} presentation="focused" onRefetchReady={onTreeRefetchReady} />
            </div>
            <VaultScopedShortcuts vault={name} /></> : <VaultDirectorySidebar onCreateVault={openCreateVault} onNavigate={closeAfterNavigation} onRefetchReady={onVaultsRefetchReady} navigationControl={{ open: navigationOpen, onToggle: toggleNavigation }} />}
          </div>
          {desktopNav && navigationOpen && (
            <div
              role="separator"
              data-vault-navigation-resizer=""
              aria-orientation="vertical"
              aria-label="Resize vault navigation"
              aria-controls="vault-workspace-navigation"
              aria-valuemin={224}
              aria-valuemax={360}
              aria-valuenow={panel.width}
              aria-valuetext={`${panel.width}px`}
              tabIndex={0}
              title="Drag or use arrow keys to resize · double-click to reset"
              {...panel.handlers}
              onPointerUp={event => {
                if (workspaceWidth - panel.width - 1 < (layout?.minimumVaultWorkspaceWidth ?? minimumContentWidth)) setMobileNavOpen(true);
                panel.handlers.onPointerUp(event);
              }}
              onPointerCancel={event => {
                if (workspaceWidth - panel.width - 1 < (layout?.minimumVaultWorkspaceWidth ?? minimumContentWidth)) setMobileNavOpen(true);
                panel.handlers.onPointerCancel(event);
              }}
              onKeyDown={event => {
                if (["ArrowLeft", "ArrowRight", "Home", "End"].includes(event.key)) setMobileNavOpen(true);
                panel.handlers.onKeyDown(event);
              }}
              style={!inlineNavigation ? { position: "absolute", left: panel.width, top: 0, bottom: 0 } : undefined}
              className={cn(
                "group relative z-[var(--z-overlay)] w-px shrink-0 cursor-col-resize touch-none outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2 focus-visible:ring-offset-background before:absolute before:inset-y-0 before:-left-1.5 before:w-3",
                inlineNavigation && "lg:-mt-14 lg:h-[calc(100%+3.5rem)]",
              )}
            >
              <div className="mx-auto h-full w-px bg-border transition-colors group-hover:bg-primary group-focus-visible:bg-primary group-active:bg-primary" />
            </div>
          )}
          </>

          {/* Desktop location lives in the app header. Narrow screens keep the
              breadcrumb and navigation-drawer trigger in their own location row. */}
          <div className="@container/vault-content flex min-h-0 min-w-0 flex-1 flex-col">
            {!desktopNav && <TitleBar
              crumbs={[]}
              breadcrumb={<AppPageLocation mobile parentBack={Boolean(name)} />}
              left={name ? <VaultParentBackLink /> : undefined}
              className="h-14 shrink-0 overflow-visible bg-surface pr-2 backdrop-blur-none"
              showBack={false}
            />}

            {isGraph || isResourceViewer || isSearch ? (
              <div className="relative min-h-0 min-w-0 flex-1 overflow-hidden bg-background">
                <ErrorBoundary resetKeys={[location.pathname]}>
                  <Outlet context={{ vaultNavigationOpen: navigationOpen }} />
                </ErrorBoundary>
              </div>
            ) : (
              <div
                data-slot="vault-route-viewport"
                className={cn(
                  "min-h-0 min-w-0 flex-1 bg-background",
                  "overflow-y-auto",
                )}
              >
                <div
                  className={cn(
                    "w-full",
                    isOverview || isPublications || isMembers || isSettings
                        ? "min-h-full px-4 py-5 sm:px-6 xl:px-8"
                        : !name ? "p-0"
                          : isActivity ? "px-3 py-5 lg:px-4 lg:py-6 xl:px-5"
                            : "px-5 py-5 lg:px-7 lg:py-6 xl:px-8",
                  )}
                >
                  <ErrorBoundary resetKeys={[location.pathname]}>
                    <Outlet context={{ vaultNavigationOpen: navigationOpen }} />
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
          onOpenExisting={handleVaultCreated}
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
            desktopLeftOffset={(inlineNavigation ? workspaceNavigationWidth : collapsedNavigationWidth) + 16}
          />
        )}
        </VaultRefreshProvider>
      </DocumentCreateDialogProvider>
    </VaultCreateDialogProvider>
  );
}

/** Pins remain scoped to the current accessible Vault; no second directory. */
function VaultScopedShortcuts({ vault }: { vault: string }) {
  const user = useCurrentUser();
  const { vaults } = useVaults();
  const shortcuts = useWorkspaceShortcuts(user?.user_id);
  if (!user || !vaults.some(item => item.name === vault) || !shortcuts.some(item => item.vault === vault)) return null;
  return <div className="max-h-48 shrink-0 overflow-y-auto border-t border-border pt-2 rail-scroll">
    <WorkspacePersonalSections userId={user.user_id} vaults={vaults.filter(item => item.name === vault)} sections={["pinned"]} />
  </div>;
}
