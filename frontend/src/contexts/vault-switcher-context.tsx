import { createContext, useCallback, useContext, useEffect, useId, useLayoutEffect, useMemo, useRef, useState, type MouseEvent, type ReactNode } from "react";
import { Link, useLocation, useNavigate } from "react-router-dom";
import { Box, Check, ChevronDown, FolderOpen, Plus, Search, X } from "lucide-react";
import { useCurrentUser } from "@/contexts/current-user-context";
import { useResourceNavigation } from "@/contexts/resource-navigation-context";
import { useVaults, type VaultSummary } from "@/hooks/use-vaults";
import { useVaultFavorites } from "@/hooks/use-vault-favorites";
import { VaultCreateDialog } from "@/components/vault-create-dialog";
import { Button } from "@/components/ui/button";
import { Popover, PopoverAnchor, PopoverClose, PopoverContent } from "@/components/ui/popover";
import { RailFilterToggle } from "@/components/navigation-rail-controls";
import { Input } from "@/components/ui/input";
import { InlineLoadingState } from "@/components/ui/loading-state";
import { Tooltip, TooltipContent, TooltipProvider, TooltipTrigger } from "@/components/ui/tooltip";
import { cn } from "@/lib/utils";
import { SCOPES, inScope, readScope, writeScope, type RoleScope } from "@/lib/vault-scope";
import { isModalOpen, useModalOpen } from "@/lib/modal-visibility";
import { useRecentCollections } from "@/lib/recent-collections";

interface VaultSwitcherOptions { onNavigate?: () => void; side?: "right" | "bottom"; align?: "start" | "end" }
interface VaultSwitcherControls {
  openVaultSwitcher: (trigger?: HTMLElement, options?: VaultSwitcherOptions) => void;
  isOpen?: boolean;
  trigger?: HTMLElement | null;
}
const VaultSwitcherContext = createContext<VaultSwitcherControls | null>(null);

// eslint-disable-next-line react-refresh/only-export-components
export function useVaultSwitcher() { return useContext(VaultSwitcherContext); }

/** An anchored directory switches Vaults from the current Vault's context header. */
export function VaultSwitcherProvider({ children }: { children: ReactNode }) {
  const user = useCurrentUser();
  const location = useLocation();
  const navigate = useNavigate();
  const { requestNavigation } = useResourceNavigation();
  const { vaults, loading, error, refetch } = useVaults();
  const { isFavorite, favOrder } = useVaultFavorites();
  const recentCollections = useRecentCollections(user?.user_id);
  const modalOpen = useModalOpen();
  const [request, setRequest] = useState<{ routeKey: string; identity: string | undefined; trigger: HTMLElement | null; side: "right" | "bottom"; align: "start" | "end" } | null>(null);
  const [filter, setFilter] = useState("");
  const [scope, setScope] = useState<RoleScope>(readScope);
  const [showScope, setShowScope] = useState(false);
  const [showRecent, setShowRecent] = useState(false);
  const [recentCapacity, setRecentCapacity] = useState(3);
  const [scrollWholePicker, setScrollWholePicker] = useState(false);
  const [popupElement, setPopupElement] = useState<HTMLDivElement | null>(null);
  const [createOpen, setCreateOpen] = useState(false);
  const returnFocus = useRef<HTMLElement | null>(null);
  const createReturnFocus = useRef<HTMLElement | null>(null);
  const input = useRef<HTMLInputElement>(null);
  const navigationAccepted = useRef(false);
  const creating = useRef(false);
  const notified = useRef(false);
  const onNavigate = useRef<(() => void) | undefined>(undefined);
  const pendingGuard = useRef(false);
  const outsideInteraction = useRef(false);
  const liveRequest = useRef(request);
  liveRequest.current = request;
  const filtersId = useId();
  const titleId = useId();
  const descriptionId = useId();
  const recentId = useId();
  const suspendedByModal = useRef(false);
  const mounted = useRef(true);
  useEffect(() => {
    mounted.current = true;
    return () => { mounted.current = false; };
  }, []);

  const openVaultSwitcher = useCallback((trigger?: HTMLElement, options?: VaultSwitcherOptions) => {
    const target = trigger ?? (document.activeElement instanceof HTMLElement ? document.activeElement : null);
    const previous = liveRequest.current;
    if (previous?.trigger === target && previous.identity === user?.user_id && !creating.current) {
      setRequest(null);
      return;
    }
    returnFocus.current = target;
    onNavigate.current = options?.onNavigate;
    navigationAccepted.current = false;
    creating.current = false;
    notified.current = false;
    pendingGuard.current = false;
    outsideInteraction.current = false;
    setCreateOpen(false);
    if (!previous) {
      setFilter("");
      setScope(readScope());
      setShowScope(false);
      setShowRecent(false);
    }
    const fromRail = target?.id === "workspace-vaults-link";
    setRequest({ routeKey: location.key, identity: user?.user_id, trigger: target,
      side: options?.side ?? (fromRail ? "right" : "bottom"), align: options?.align ?? (fromRail ? "start" : "end") });
    // Reanchoring keeps the same content mounted, so Radix's mount autofocus
    // does not run again. There is no closing focus restoration to race it.
    if (previous) requestAnimationFrame(() => input.current?.focus());
  }, [location.key, user?.user_id]);
  const open = request !== null && request.identity === user?.user_id && !createOpen;
  const virtualAnchor = useMemo(() => ({ current: {
    getBoundingClientRect: () => request?.trigger?.getBoundingClientRect() ?? new DOMRect(),
    contextElement: request?.trigger ?? undefined,
  } }), [request?.trigger]);
  const controls = useMemo(() => ({ openVaultSwitcher, isOpen: open, trigger: open ? request?.trigger ?? null : null }), [openVaultSwitcher, open, request?.trigger]);
  useLayoutEffect(() => {
    if (!popupElement || !open) return;
    let frame = 0;
    function measure() {
      if (!popupElement) return;
      const available = parseFloat(getComputedStyle(popupElement).getPropertyValue("--radix-popover-content-available-height"));
      if (!Number.isFinite(available) || available <= 0) return;
      const rem = parseFloat(getComputedStyle(document.documentElement).fontSize) || 16;
      const touch = window.matchMedia("(max-width: 639px), (pointer: coarse)").matches;
      const list = popupElement.querySelector<HTMLElement>('[data-slot="vault-switcher-list"]');
      const recentRegion = popupElement.querySelector<HTMLElement>('[data-slot="vault-switcher-recent"]');
      const toggle = recentRegion?.querySelector("button");
      const recentLink = recentRegion?.querySelector("nav a");
      if (!list) return;
      const vaultRows = Array.from(list.querySelectorAll("a"));
      const chrome = Array.from(popupElement.children).filter(child => child !== list && child !== recentRegion)
        .reduce((height, child) => height + child.getBoundingClientRect().height, 0);
      const viewport = Math.min(28 * rem, available);
      const firstRowHeight = vaultRows[0]?.getBoundingClientRect().height || (touch ? 2.75 : 2.25) * rem;
      // If even the fixed controls and one Vault cannot fit (e.g. landscape
      // plus open role filters), give the entire picker one accessible scroll.
      const whole = viewport < chrome + firstRowHeight + .125 * rem;
      if (whole !== scrollWholePicker) popupElement.scrollTop = 0;
      setScrollWholePicker(whole);
      if (!recentRegion || !toggle || !recentLink) return;
      const lastReservedRow = vaultRows[Math.min(vaultRows.length, touch ? 2 : 3) - 1];
      // Measure the real prefix, including group labels and wrapping Vault
      // names. Scroll offset keeps this budget stable while browsing the list.
      const vaultSpace = lastReservedRow ? lastReservedRow.getBoundingClientRect().bottom - list.getBoundingClientRect().top + list.scrollTop : 0;
      const recentStyle = getComputedStyle(recentLink);
      const rowHeight = Math.max(parseFloat(recentStyle.minHeight) || 0,
        2 * (parseFloat(recentStyle.lineHeight) || 1.25 * rem) + (parseFloat(recentStyle.paddingTop) || 0) + (parseFloat(recentStyle.paddingBottom) || 0));
      const toggleHeight = Math.max(toggle.getBoundingClientRect().height, parseFloat(getComputedStyle(toggle).minHeight) || 0);
      const reserved = chrome + vaultSpace + toggleHeight + .5 * rem;
      const capacity = whole ? 0 : Math.max(0, Math.min(3, Math.floor((viewport - reserved) / rowHeight)));
      const focusedRecent = Array.from(recentRegion?.querySelectorAll("nav a") ?? []).findIndex(link => link.contains(document.activeElement));
      if (focusedRecent >= capacity || (capacity === 0 && toggle === document.activeElement)) (capacity ? toggle : input.current)?.focus({ preventScroll: true });
      if (capacity === 0) setShowRecent(false);
      setRecentCapacity(capacity);
    }
    function schedule() { cancelAnimationFrame(frame); frame = requestAnimationFrame(measure); }
    measure();
    const observer = typeof ResizeObserver === "undefined" ? null : new ResizeObserver(schedule);
    observer?.observe(popupElement);
    window.addEventListener("resize", schedule);
    return () => { cancelAnimationFrame(frame); observer?.disconnect(); window.removeEventListener("resize", schedule); };
  }, [popupElement, open, filter, scope, showScope, recentCollections.length, scrollWholePicker]);
  useEffect(() => {
    if (modalOpen) { suspendedByModal.current = true; return; }
    if (suspendedByModal.current && open) {
      suspendedByModal.current = false;
      requestAnimationFrame(() => input.current?.focus());
    }
  }, [modalOpen, open]);
  const notifyNavigation = useCallback(() => {
    if (notified.current) return;
    notified.current = true;
    onNavigate.current?.();
  }, []);
  const acceptNavigation = useCallback(() => {
    navigationAccepted.current = true;
    createReturnFocus.current = document.getElementById("main");
    setRequest(null);
    setCreateOpen(false);
    notifyNavigation();
  }, [notifyNavigation]);

  // Confirmation dialogs replay the guarded href/options later. Observe that
  // committed route too, rather than dismissing the directory on rejection.
  useEffect(() => {
    if (!request) return;
    if (request.identity !== user?.user_id) {
      setRequest(null);
      setCreateOpen(false);
    } else if (request.routeKey !== location.key) acceptNavigation();
  }, [request, location.key, user?.user_id, acceptNavigation]);

  const matching = useMemo(() => {
    const query = filter.trim().toLocaleLowerCase();
    return vaults.filter(vault => inScope(vault.role, scope) && (!query || vault.name.toLocaleLowerCase().includes(query)));
  }, [vaults, filter, scope]);
  const favorites = matching.filter(vault => isFavorite(vault.id)).sort((a, b) => favOrder(a.id) - favOrder(b.id));
  const others = matching.filter(vault => !isFavorite(vault.id));
  const currentVault = vaults.find(vault => {
    const base = `/vault/${encodeURIComponent(vault.name)}`;
    return location.pathname === base || location.pathname.startsWith(`${base}/`);
  });
  const recent = !filter.trim() && scope === "all" && !loading && !error && currentVault
    ? recentCollections.filter(item => item.vault === currentVault.name).slice(0, 3)
    : [];

  function follow(event: MouseEvent<HTMLAnchorElement>, href: string) {
    if (event.defaultPrevented || event.button !== 0 || event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return;
    if (!requestNavigation(href, { state: { vaultPageNavigation: href } })) { pendingGuard.current = true; event.preventDefault(); return; }
    acceptNavigation();
  }
  function restoreFocus() {
    requestAnimationFrame(() => {
      if (!mounted.current || isModalOpen() || liveRequest.current) return;
      const target = navigationAccepted.current ? document.getElementById("main") : returnFocus.current;
      // Responsive CSS can hide a still-connected desktop/mobile invoker while
      // the picker is open. Check its ancestors too before restoring focus.
      function visible(element: HTMLElement | null): element is HTMLElement {
        if (!element?.isConnected || element.closest("[hidden], [inert]")) return false;
        for (let ancestor: HTMLElement | null = element; ancestor; ancestor = ancestor.parentElement) {
          const style = getComputedStyle(ancestor);
          if (style.display === "none" || style.visibility === "hidden" || style.visibility === "collapse") return false;
        }
        return true;
      }
      const destination = visible(target) ? target : document.getElementById("main");
      if (visible(destination)) destination.focus({ preventScroll: true });
    });
  }
  function row(vault: VaultSummary) {
    const href = `/vault/${encodeURIComponent(vault.name)}`;
    const selected = location.pathname.replace(/\/+$/, "") === href || location.pathname.startsWith(`${href}/`);
    return <li key={vault.id}>
      <Link to={href} state={{ vaultPageNavigation: href }} onClick={event => follow(event, href)}
        aria-label={selected ? `${vault.name}, current vault` : undefined} aria-current={selected ? "location" : undefined}
        className={cn("flex min-h-9 items-center gap-2 rounded-[var(--radius-sm)] px-2 py-1.5 text-ui focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-inset max-sm:min-h-11 pointer-coarse:min-h-11", selected ? "bg-surface-selected text-surface-selected-foreground" : "text-foreground hover:bg-surface-hover")}>
        <Box className="h-4 w-4 shrink-0" aria-hidden /><span className="min-w-0 flex-1 break-words [overflow-wrap:anywhere]">{vault.name}</span>
        {selected && <><Check className="h-4 w-4 shrink-0" aria-hidden /><span className="sr-only">Current vault</span></>}
      </Link>
    </li>;
  }
  function changeScope(next: RoleScope) { setScope(next); writeScope(next); setShowRecent(false); }

  return <VaultSwitcherContext.Provider value={controls}>
    {children}
    <Popover open={open} modal={false} onOpenChange={next => { if (!next) setRequest(null); }}>
      <PopoverAnchor virtualRef={virtualAnchor} />
      <PopoverContent ref={setPopupElement} id="vault-switcher-panel" aria-labelledby={titleId} aria-describedby={descriptionId}
        side={request?.side ?? "bottom"} align={request?.align ?? "end"} collisionPadding={8}
        aria-hidden={modalOpen || undefined} inert={modalOpen || undefined}
        data-scroll-mode={scrollWholePicker ? "whole" : "list"}
        className={cn("flex max-h-[min(28rem,var(--radix-popover-content-available-height))] w-80 flex-col gap-0 p-0", scrollWholePicker ? "overflow-y-auto" : "overflow-hidden", modalOpen && "invisible pointer-events-none")}
        onKeyDownCapture={event => {
          // Tab is an explicit request to continue outside this nonmodal
          // picker. Delayed dialog autofocus is not a keyboard departure.
          if (event.key === "Tab" && !isModalOpen()) pendingGuard.current = false;
        }}
        onInteractOutside={event => {
          const target = event.detail.originalEvent.target;
          if (target instanceof Element && target.closest("[data-vault-switcher-trigger], [role=dialog]")) { event.preventDefault(); return; }
          if (pendingGuard.current && event.detail.originalEvent.type === "focusin") {
            // Dialog exit and editor effects can restore focus on different
            // frames. Preserve the rejected picker through every such return
            // until an explicit Tab or pointer departure supersedes it.
            event.preventDefault();
            requestAnimationFrame(() => input.current?.focus());
            return;
          }
          pendingGuard.current = false;
          outsideInteraction.current = true;
        }}
        onOpenAutoFocus={event => { event.preventDefault(); input.current?.focus(); }} onCloseAutoFocus={event => {
        event.preventDefault();
        // The creation dialog takes focus during this handoff.
        if (!creating.current && (navigationAccepted.current || !outsideInteraction.current)) restoreFocus();
      }}>
        <div className="relative min-h-9 shrink-0 px-3 py-2 pr-12 max-sm:min-h-11 pointer-coarse:min-h-11">
          <h2 id={titleId} className="text-sm font-semibold">Vaults</h2>
          <p id={descriptionId} className="sr-only">Choose a knowledge space.</p>
          <PopoverClose aria-label="Close vaults" className="absolute right-1 top-0 inline-flex h-9 w-9 items-center justify-center rounded-[var(--radius-sm)] text-foreground-muted hover:bg-surface-hover hover:text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring max-sm:h-11 max-sm:w-11 pointer-coarse:h-11 pointer-coarse:w-11"><X className="h-4 w-4" aria-hidden /></PopoverClose>
        </div>
        <div className="flex shrink-0 items-center gap-1 px-3 pb-2">
          <div className="relative min-w-0 flex-1">
            <Search className="pointer-events-none absolute left-3 top-1/2 h-4 w-4 -translate-y-1/2 text-foreground-muted" aria-hidden />
            <Input ref={input} type="search" aria-label="Find a vault" placeholder="Find a vault" value={filter} onChange={event => { setFilter(event.target.value); setShowRecent(false); }} className="h-9 pl-9 text-ui max-sm:h-11 max-sm:text-base pointer-coarse:h-11 pointer-coarse:text-base" />
          </div>
          <div className="[&>button]:h-9 [&>button]:w-9 max-sm:[&>button]:h-11 max-sm:[&>button]:w-11 pointer-coarse:[&>button]:h-11 pointer-coarse:[&>button]:w-11"><RailFilterToggle label="Filter by role" open={showScope} count={Number(scope !== "all")} controls={filtersId} onClick={() => setShowScope(value => !value)} /></div>
        </div>
        <div id={filtersId} hidden={!showScope} className="shrink-0 px-3 pb-2">
          <div role="group" aria-label="Filter vaults by your role" className="flex gap-1 rounded-[var(--radius-md)] bg-background p-1">
            {SCOPES.map(option => <button key={option.key} type="button" aria-pressed={scope === option.key} onClick={() => changeScope(option.key)}
              className={cn("min-h-9 flex-1 rounded-[var(--radius-sm)] px-2 text-xs focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring max-sm:min-h-11 pointer-coarse:min-h-11", scope === option.key ? "bg-surface-selected font-medium text-surface-selected-foreground" : "text-foreground-muted hover:bg-surface-hover hover:text-foreground")}>{option.label}</button>)}
          </div>
        </div>
        {(scope !== "all" || filter) && <div className="flex shrink-0 items-center justify-between gap-2 px-3 pb-2 text-xs text-foreground-muted">
          <span>{scope !== "all" ? SCOPES.find(option => option.key === scope)?.label : "Name filter"}</span>
          <button type="button" onClick={() => { setFilter(""); changeScope("all"); input.current?.focus(); }} className="inline-flex min-h-9 items-center rounded-[var(--radius-sm)] text-link focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring max-sm:min-h-11 pointer-coarse:min-h-11">Reset filters</button>
        </div>}
        {loading && <InlineLoadingState label={vaults.length ? "Updating vaults" : "Loading vaults"} className="shrink-0 px-5 py-2" />}
        {error && <div role="status" className="shrink-0 px-5 py-2 text-sm text-foreground-muted">
          {vaults.length ? "Couldn't update vaults. Previous list shown." : "Vaults unavailable."}{" "}
          <button type="button" onClick={refetch} disabled={loading} className="inline-flex min-h-9 items-center rounded-[var(--radius-sm)] text-link underline focus-visible:ring-2 focus-visible:ring-ring disabled:opacity-50 max-sm:min-h-11 pointer-coarse:min-h-11">Retry</button>
        </div>}
        <div data-slot="vault-switcher-list" className={cn("overscroll-contain", scrollWholePicker ? "shrink-0" : "min-h-0 flex-1 overflow-y-auto")}>
        <nav aria-label="Accessible vaults" aria-busy={loading || undefined} className="px-1.5 pb-2">
          {favorites.length > 0 && <section aria-label="Favorite vaults"><h3 className="px-2 pb-1 pt-1 text-xs font-medium text-foreground-muted">Favorites</h3><ul>{favorites.map(row)}</ul></section>}
          {others.length > 0 && <section aria-label="Other vaults">{favorites.length > 0 && <h3 className="px-2 pb-1 pt-2 text-xs font-medium text-foreground-muted">Other vaults</h3>}<ul>{others.map(row)}</ul></section>}
          {!loading && !error && vaults.length === 0 && <p className="px-3 py-3 text-sm text-foreground-muted">No vaults yet.</p>}
          {vaults.length > 0 && matching.length === 0 && <p role="status" className="px-3 py-3 text-sm text-foreground-muted">{scope === "all" ? "No vaults match this name." : "No vaults match these filters."}</p>}
        </nav>
        </div>
        {recent.length > 0 && currentVault && <div hidden={recentCapacity === 0} className="shrink-0 border-t border-border" data-slot="vault-switcher-recent">
          <button type="button" aria-label={`Recent collections in ${currentVault.name}`} aria-expanded={showRecent} aria-controls={recentId} onClick={() => setShowRecent(value => !value)}
            className="flex min-h-9 w-full items-center gap-2 px-3 py-1.5 text-left text-xs font-medium text-foreground-muted hover:bg-surface-hover hover:text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-inset max-sm:min-h-11 pointer-coarse:min-h-11">
            <ChevronDown className={cn("h-3.5 w-3.5 shrink-0", !showRecent && "-rotate-90")} aria-hidden />Recent collections
            <span className="ml-auto tabular-nums" aria-hidden>{Math.min(recent.length, recentCapacity)}</span>
          </button>
          <TooltipProvider><nav id={recentId} hidden={!showRecent} aria-label={`Recent collections in ${currentVault.name}`} className="px-1.5 pb-1.5">
            <ul>{recent.map((item, index) => {
              const href = `/vault/${encodeURIComponent(item.vault)}?collection=${encodeURIComponent(item.path)}`;
              const path = item.path.split("/").join(" / ");
              return <li key={item.path} hidden={index >= recentCapacity}><Tooltip><TooltipTrigger asChild><Link to={href} aria-label={path} state={{ vaultPageNavigation: href }} onClick={event => follow(event, href)}
                className="flex min-h-9 items-center gap-2 rounded-[var(--radius-sm)] px-2 py-1.5 text-ui text-foreground hover:bg-surface-hover focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-inset max-sm:min-h-11 pointer-coarse:min-h-11">
                <FolderOpen className="h-4 w-4 shrink-0 text-foreground-muted" aria-hidden /><span className="line-clamp-2 min-w-0 break-words [overflow-wrap:anywhere]">{path}</span>
              </Link></TooltipTrigger><TooltipContent className="max-w-80 break-words [overflow-wrap:anywhere]">{path}</TooltipContent></Tooltip></li>;
            })}</ul>
          </nav></TooltipProvider>
        </div>}
        <div className="flex shrink-0 flex-wrap items-center justify-between gap-2 border-t border-border px-3 py-2">
          <Link to="/vault" state={{ vaultPageNavigation: "/vault" }} onClick={event => follow(event, "/vault")} className="inline-flex min-h-9 items-center rounded-[var(--radius-sm)] text-ui text-link hover:text-link-hover focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring max-sm:min-h-11 pointer-coarse:min-h-11">View all vaults</Link>
          <Button variant="outline" size="sm" className="h-9 px-2 text-ui max-sm:h-11 pointer-coarse:h-11" onClick={() => { creating.current = true; createReturnFocus.current = returnFocus.current; setCreateOpen(true); }}><Plus className="h-3.5 w-3.5" aria-hidden />Create vault</Button>
        </div>
      </PopoverContent>
    </Popover>
    <VaultCreateDialog open={createOpen} onOpenChange={next => {
      setCreateOpen(next);
      if (!next) { creating.current = false; setRequest(null); }
    }} returnFocusRef={createReturnFocus} onCreated={vaultName => {
      refetch();
      const href = `/vault/${encodeURIComponent(vaultName)}`;
      const options = { state: { vaultPageNavigation: href } };
      if (requestNavigation(href, options)) { acceptNavigation(); navigate(href, options); }
    }} onOpenExisting={vaultName => {
      const href = `/vault/${encodeURIComponent(vaultName)}`;
      const options = { state: { vaultPageNavigation: href } };
      if (requestNavigation(href, options)) { acceptNavigation(); navigate(href, options); }
    }} />
  </VaultSwitcherContext.Provider>;
}
