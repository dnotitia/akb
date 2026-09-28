import { useLayoutEffect, useRef, useState, type MouseEvent } from "react";
import * as DropdownMenu from "@radix-ui/react-dropdown-menu";
import { ChevronDown } from "lucide-react";
import { Link, matchRoutes, useLocation } from "react-router-dom";
import { appRouteContract } from "@/app-route-contract";
import { getVaultPageActions } from "@/components/title-bar";
import { Button } from "@/components/ui/button";
import { useResourceNavigation } from "@/contexts/resource-navigation-context";
import { visibleNavigationItems } from "@/lib/navigation-overflow";
import { cn } from "@/lib/utils";

const sectionPaths = {
  overview: "/vault/:name",
  search: "/vault/:name/search",
  graph: "/vault/:name/graph",
  publish: "/vault/:name/publications",
  members: "/vault/:name/members",
  settings: "/vault/:name/settings",
} as const;

/** One persistent Vault scope, distinct from resource-local viewing controls. */
export function VaultSectionNavigation({ vault }: { vault: string }) {
  const { pathname } = useLocation();
  const match = matchRoutes([...appRouteContract], pathname)?.at(-1);
  if (
    match?.route.boundary !== "vault-shell" || match.params.name !== vault
    || !["/vault/:name/activity", "/vault/:name/doc/:id", "/vault/:name/file/:id", "/vault/:name/table/:table", ...Object.values(sectionPaths)].includes(match.route.path)
  ) return null;
  return <VaultNavigationLinks key={vault} vault={vault} route={match.route.path} />;
}

const linkLayout = "inline-flex h-11 items-center gap-1.5 whitespace-nowrap px-2 text-sm lg:h-10";
// Padding includes the management divider, so overflow measures its real width.
const managementStartLayout = "pl-5 before:absolute before:left-1 before:top-1/2 before:h-4 before:w-px before:-translate-y-1/2 before:bg-border-strong";

function VaultNavigationLinks({ vault, route }: { vault: string; route: string }) {
  const actions = getVaultPageActions(vault).filter(action => action.key !== "search");
  const { requestNavigation } = useResourceNavigation();
  const navRef = useRef<HTMLElement>(null);
  const measureRef = useRef<HTMLDivElement>(null);
  const moreRef = useRef<HTMLButtonElement>(null);
  const focusedKeyRef = useRef<string | null>(null);
  const followedLinkRef = useRef(false);
  const [open, setOpen] = useState(false);
  const [metrics, setMetrics] = useState({ widths: [] as number[], available: 0, more: 0, gap: 4 });
  const activeIndex = actions.findIndex(action => sectionPaths[action.key] === route);
  const visible = metrics.widths.length ? visibleNavigationItems(metrics.widths, metrics.available, metrics.more, metrics.gap, activeIndex) : actions.map((_, index) => index);
  const overflow = actions.filter((_, index) => !visible.includes(index));

  useLayoutEffect(() => {
    const nav = navRef.current;
    const measure = measureRef.current;
    if (!nav || !measure) return;
    const update = () => {
      const style = getComputedStyle(nav);
      const items = Array.from(measure.children).map(item => Math.ceil(item.getBoundingClientRect().width));
      const widths = items.slice(0, -1);
      const available = Math.floor(nav.getBoundingClientRect().width - parseFloat(style.paddingLeft) - parseFloat(style.paddingRight));
      const next = { widths, available, more: items.at(-1) ?? 0, gap: parseFloat(style.columnGap) || 0 };
      const focused = document.activeElement as HTMLElement | null;
      if (focused?.dataset.vaultDestination) focusedKeyRef.current = focused.dataset.vaultDestination;
      else if (focused === moreRef.current) focusedKeyRef.current = "more";
      setMetrics(previous => JSON.stringify(previous) === JSON.stringify(next) ? previous : next);
    };
    update();
    if (typeof ResizeObserver === "undefined") return;
    const observer = new ResizeObserver(update);
    observer.observe(nav);
    observer.observe(measure);
    return () => observer.disconnect();
  }, []);

  const visibleKeys = visible.map(index => actions[index].key).join(",");
  useLayoutEffect(() => {
    // Do not reopen an old disclosure if resizing restores More later.
    setOpen(false);
    // A resize must not strand keyboard focus on a removed destination/trigger.
    const key = focusedKeyRef.current;
    focusedKeyRef.current = null;
    if (!key || (document.activeElement !== document.body && document.activeElement?.isConnected)) return;
    const link = navRef.current?.querySelector<HTMLElement>(`[data-vault-destination="${key}"]`);
    (link ?? moreRef.current ?? navRef.current?.querySelector<HTMLElement>("a"))?.focus();
  }, [visibleKeys]);

  const labelFor = (action: (typeof actions)[number]) => action.key === "publish" ? "Public links" : action.label;
  const beforeNavigate = (event: MouseEvent<HTMLAnchorElement>, href: string) => {
    if (event.button !== 0 || event.metaKey || event.ctrlKey || event.shiftKey || event.altKey || event.defaultPrevented) return;
    if (!requestNavigation(href)) event.preventDefault();
    // Route focus or a confirmation dialog owns focus after a selection, not More.
    followedLinkRef.current = true;
  };

  const renderLink = (action: (typeof actions)[number], inMenu = false) => {
    const active = route === sectionPaths[action.key];
    const label = labelFor(action);
    const Icon = action.icon;
    return (
      <Link
        key={action.key}
        to={action.href}
        onClick={event => beforeNavigate(event, action.href)}
        onKeyDownCapture={inMenu ? event => {
          // Radix activates items with element.click(), which drops modifiers.
          // Let the native anchor handle modified Enter (new tab/window).
          if (event.key === "Enter" && (event.metaKey || event.ctrlKey || event.shiftKey || event.altKey)) event.stopPropagation();
        } : undefined}
        data-vault-destination={action.key}
        aria-label={label}
        aria-current={active ? "page" : undefined}
        className={cn(
          inMenu ? "flex min-h-11 items-center gap-2 rounded-[var(--radius-sm)] px-3 text-sm outline-none data-[highlighted]:bg-surface-hover" : cn(linkLayout, "relative min-w-0 shrink transition-token hover:bg-surface-hover focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-inset focus-visible:ring-ring", action.key === "members" && managementStartLayout),
          active
            ? "font-semibold text-link after:absolute after:inset-x-2 after:bottom-0 after:h-0.5 after:bg-link"
            : "font-medium text-foreground-muted hover:text-link",
        )}
      >
        <Icon className="h-4 w-4 shrink-0" aria-hidden />
        <span className="truncate" title={label}>{label}</span>
      </Link>
    );
  };

  return (
    <div className={cn("@container/vault-navigation relative flex h-11 min-w-0 shrink-0 items-center gap-2 px-3 lg:h-10", route === sectionPaths.search ? "bg-surface" : "bg-background")}>
    <div aria-hidden className="pointer-events-none absolute inset-x-0 bottom-0 border-b border-border" />
    <nav
      ref={navRef}
      aria-label="Vault sections"
      data-slot="vault-section-navigation"
      className="relative flex h-full min-w-0 flex-1 items-center gap-1"
    >
      {actions.filter((_, index) => visible.includes(index)).map(action => renderLink(action))}
      {!!overflow.length && <DropdownMenu.Root modal={false} open={open} onOpenChange={value => { if (value) followedLinkRef.current = false; setOpen(value); }}>
        <DropdownMenu.Trigger asChild>
          <Button ref={moreRef} type="button" variant="ghost" aria-label="More vault pages"
            className={cn(linkLayout, "shrink-0 rounded-[var(--radius-sm)] text-foreground-muted data-[state=open]:bg-surface-selected data-[state=open]:text-surface-selected-foreground")}>
            More<ChevronDown className="h-4 w-4" aria-hidden />
          </Button>
        </DropdownMenu.Trigger>
        <DropdownMenu.Portal>
          <DropdownMenu.Content align="end" sideOffset={4} collisionPadding={8} aria-label="More vault pages"
            onCloseAutoFocus={event => { if (followedLinkRef.current || !moreRef.current) event.preventDefault(); }}
            className="z-[var(--z-popover)] max-h-[var(--radix-dropdown-menu-content-available-height)] min-w-48 max-w-[calc(100vw-1rem)] overflow-y-auto rounded-[var(--radius-md)] border border-border bg-surface p-1 shadow-md">
            {overflow.map(action => <DropdownMenu.Item key={action.key} asChild>{renderLink(action, true)}</DropdownMenu.Item>)}
          </DropdownMenu.Content>
        </DropdownMenu.Portal>
      </DropdownMenu.Root>}
      {/* Non-interactive max-weight copies reserve real font/icon widths, including
          after font loading and text scaling. They never enter the tab order. */}
      <div aria-hidden inert className="pointer-events-none invisible absolute inset-x-0 top-0 h-0 overflow-hidden">
      <div ref={measureRef} className="flex w-max gap-1">
        {actions.map(action => <span key={action.key} className={cn(linkLayout, "relative shrink-0 font-semibold", action.key === "members" && managementStartLayout)}><action.icon className="h-4 w-4 shrink-0" />{labelFor(action)}</span>)}
        <span className={cn(linkLayout, "shrink-0 font-medium")}>More<ChevronDown className="h-4 w-4" /></span>
      </div>
      </div>
    </nav>
    </div>
  );
}
