import {
  Box,
  ChevronDown,
  MoreHorizontal,
  StarOff,
  Boxes,
  House,
  CircleHelp,
  PanelLeftClose,
  PanelLeftOpen,
  Search,
  Settings,
  type LucideIcon,
} from "lucide-react";
import { useRef, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import * as DropdownMenu from "@radix-ui/react-dropdown-menu";
import { useCurrentUser } from "@/contexts/current-user-context";
import { readLegacyVaultFavorites, useVaultFavorites } from "@/hooks/use-vault-favorites";
import { WorkspacePersonalSections } from "@/components/workspace-personal-sections";
import { ConfirmDialog } from "@/components/ui/confirm-dialog";
import { Dialog, DialogContent, DialogDescription, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { listVaults } from "@/lib/api";
import { TooltipText } from "@/components/ui/tooltip-text";
import { Link, useLocation } from "react-router-dom";
import {
  Tooltip,
  TooltipContent,
  TooltipProvider,
  TooltipTrigger,
} from "@/components/ui/tooltip";
import { cn } from "@/lib/utils";
import { Logo } from "@/components/logo";

const PRIMARY_ITEMS: Array<{
  to: string;
  label: string;
  icon: LucideIcon;
  active: (pathname: string) => boolean;
}> = [
  {
    to: "/",
    label: "Home",
    icon: House,
    active: (pathname) => pathname === "/",
  },
  {
    to: "/search",
    label: "Search",
    icon: Search,
    active: (pathname) => pathname === "/search",
  },
  {
    to: "/vault",
    label: "Vaults",
    icon: Boxes,
    active: (pathname) => pathname.startsWith("/vault"),
  },
];

export function AppSidebar({
  compact,
  collapsible = false,
  onCompactChange,
}: {
  compact: boolean;
  collapsible?: boolean;
  onCompactChange?: (compact: boolean) => void;
}) {
  const { pathname, search } = useLocation();
  const user = useCurrentUser();
  const { favorites, toggleFavorite, importFavorites } = useVaultFavorites();
  const [importOpen, setImportOpen] = useState(false);
  const [helpOpen, setHelpOpen] = useState(false);
  const importTrigger = useRef<HTMLButtonElement>(null);
  const helpTrigger = useRef<HTMLButtonElement>(null);
  const [favoritesOpen, setFavoritesOpen] = useState(true);
  const [showAll, setShowAll] = useState(false);
  const disclosure = useRef<HTMLButtonElement>(null);
  const vaultQuery = useQuery({
    queryKey: ["workspace-favorite-vaults", user?.user_id, pathname],
    queryFn: () => listVaults(),
    enabled: !!user,
    retry: false,
  });
  // Resolve saved IDs against accessible Vaults; never persist display names.
  const vaults = vaultQuery.isError ? [] : (vaultQuery.data?.vaults ?? []);
  const pinned = favorites.flatMap(id => {
    const vault = vaults.find(v => v.id === id);
    return vault ? [vault] : [];
  });
  const hasFavorites = pinned.length > 0;
  const legacyIds = readLegacyVaultFavorites();
  const importable = vaults.filter(vault => legacyIds.includes(vault.id) && !favorites.includes(vault.id));
  const visiblePinned = showAll ? pinned : pinned.slice(0, 5);
  const currentFavorite = visiblePinned.some(v => pathname === `/vault/${encodeURIComponent(v.name)}` || pathname.startsWith(`/vault/${encodeURIComponent(v.name)}/`));
  const focusFavorites = () => {
    const target = disclosure.current ?? document.getElementById("workspace-vaults-link");
    target?.focus();
  };
  const toggleLabel = compact ? "Expand sidebar" : "Collapse sidebar";

  return (
    <TooltipProvider delayDuration={250}>
      <aside
        data-testid="app-sidebar"
        data-compact={compact ? "true" : "false"}
        className={cn(
          "fixed inset-y-0 left-0 z-40 hidden h-dvh shrink-0 overflow-hidden border-r border-border bg-surface lg:flex lg:flex-col",
          compact ? "lg:w-14" : "lg:w-52",
        )}
      >
        <div className={cn("flex h-14 shrink-0 items-center border-b border-border", compact ? "justify-center" : "px-4")}>
          <Link to="/" aria-label="AKB home" className="rounded-[var(--radius-sm)] focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2 focus-visible:ring-offset-surface">
            <Logo size={28} wordmark={!compact} variant="header" />
          </Link>
        </div>
          <div
            data-slot="workspace-sidebar-heading"
            className={cn(
              "flex h-10 shrink-0 items-center border-b border-border px-2",
              compact ? "justify-center" : "justify-between",
            )}
          >
            {!compact && <span className="coord px-2">Workspace</span>}
            {collapsible && onCompactChange && (
              <Tooltip>
                <TooltipTrigger asChild>
                  <button
                    type="button"
                    aria-label={toggleLabel}
                    aria-expanded={!compact}
                    aria-controls="workspace-navigation"
                    onClick={() => onCompactChange(!compact)}
                    className="flex h-8 w-8 items-center justify-center rounded-[var(--radius-sm)] text-foreground-muted transition-token hover:bg-surface-hover hover:text-foreground focus:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-inset"
                  >
                    {compact ? (
                      <PanelLeftOpen className="h-4 w-4" aria-hidden />
                    ) : (
                      <PanelLeftClose className="h-4 w-4" aria-hidden />
                    )}
                  </button>
                </TooltipTrigger>
                <TooltipContent side="right">{toggleLabel}</TooltipContent>
              </Tooltip>
            )}
          </div>

        <nav
          id="workspace-navigation"
          aria-label="Workspace navigation"
          className={cn("flex shrink-0 flex-col gap-1 p-2", compact && "items-center")}
        >
          {PRIMARY_ITEMS.map((item) => (
            <div key={item.to} className="flex items-center">
            <AppSidebarLink
              {...item}
              compact={compact}
              selected={item.active(pathname) && (compact || item.to !== "/vault" || !currentFavorite || !favoritesOpen)}
            />
            </div>
          ))}
        </nav>
        <div className="min-h-0 flex-1 overflow-y-auto rail-scroll" data-slot="workspace-personal-scroll">
        {!compact && (hasFavorites || importable.length > 0) && (
          <div className="flex min-h-9 items-center justify-between pl-5 pr-2" data-slot="workspace-favorites-heading">
            <p className="text-xs font-medium text-foreground-muted">Favorites</p>
            {hasFavorites && (
              <button ref={disclosure} type="button" aria-label={favoritesOpen ? "Collapse favorite vaults" : "Expand favorite vaults"} aria-expanded={favoritesOpen} aria-controls="workspace-favorite-vaults" onClick={() => setFavoritesOpen(value => !value)} className="flex h-9 w-8 shrink-0 items-center justify-center rounded-[var(--radius-sm)] text-foreground-muted hover:bg-surface-hover focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-inset">
                <ChevronDown className={cn("h-3.5 w-3.5 transition-token", !favoritesOpen && "-rotate-90")} aria-hidden />
              </button>
            )}
          </div>
        )}
        {!compact && importable.length > 0 && <button ref={importTrigger} type="button" onClick={() => setImportOpen(true)} className="mx-3 mb-2 rounded-[var(--radius-sm)] px-2 py-1.5 text-left text-xs text-link hover:bg-surface-hover focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring">Import saved favorites</button>}
        {!compact && hasFavorites && favoritesOpen && (
          <nav id="workspace-favorite-vaults" aria-label="Favorite vaults" className="px-2 pb-3">
            <ul className="space-y-0.5">
              {visiblePinned.map(vault => {
                const to = `/vault/${encodeURIComponent(vault.name)}`;
                const selected = pathname === to || pathname.startsWith(`${to}/`);
                return <li key={vault.id} className={cn("group flex min-w-0 items-center rounded-[var(--radius-sm)]", selected ? "bg-surface-selected text-surface-selected-foreground" : "text-foreground-muted hover:bg-surface-hover")}>
                  <Link to={to} aria-current={selected ? "location" : undefined} className="flex h-9 min-w-0 flex-1 items-center gap-2 rounded-[var(--radius-sm)] pl-6 pr-1 text-sm focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-inset">
                    <Box className="h-3.5 w-3.5 shrink-0" aria-hidden />
                    <TooltipText className="min-w-0 truncate" tip={vault.name}>{vault.name}</TooltipText>
                  </Link>
                  <DropdownMenu.Root>
                    <DropdownMenu.Trigger aria-label={`Options for ${vault.name}`} className="mr-1 flex h-8 w-7 shrink-0 items-center justify-center rounded-[var(--radius-sm)] opacity-0 group-hover:opacity-100 group-focus-within:opacity-100 data-[state=open]:opacity-100 hover:bg-surface-active focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring">
                      <MoreHorizontal className="h-3.5 w-3.5" aria-hidden />
                    </DropdownMenu.Trigger>
                    <DropdownMenu.Portal><DropdownMenu.Content side="right" align="start" sideOffset={8} onCloseAutoFocus={event => { event.preventDefault(); focusFavorites(); }} className="z-[var(--z-popover)] rounded-[var(--radius-md)] border border-border bg-surface p-1 shadow-md">
                      <DropdownMenu.Item onSelect={() => { toggleFavorite(vault.id); requestAnimationFrame(focusFavorites); }} className="flex cursor-pointer items-center gap-2 rounded-[var(--radius-sm)] px-3 py-2 text-sm text-foreground outline-none data-[highlighted]:bg-surface-hover">
                        <StarOff className="h-4 w-4" aria-hidden />Remove from favorites
                      </DropdownMenu.Item>
                    </DropdownMenu.Content></DropdownMenu.Portal>
                  </DropdownMenu.Root>
                </li>;
              })}
            </ul>
            {pinned.length > 5 && <button type="button" onClick={() => setShowAll(value => !value)} className="mt-1 flex h-9 w-full items-center rounded-[var(--radius-sm)] pl-6 text-xs text-link hover:bg-surface-hover focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-inset">{showAll ? "Show less" : `Show ${pinned.length - 5} more`}</button>}
          </nav>
        )}
        {!compact && user && <WorkspacePersonalSections key={user.user_id} userId={user.user_id} vaults={vaults} />}
        {!compact && vaultQuery.isError && <div className="px-4 py-2 text-xs text-foreground-muted" role="status">Personal links unavailable. <button type="button" onClick={() => void vaultQuery.refetch()} className="rounded-[var(--radius-sm)] text-link underline focus-visible:ring-2 focus-visible:ring-ring">Retry</button></div>}
        </div>
        <nav aria-label="Workspace support" className={cn("flex shrink-0 flex-col gap-1 border-t border-border p-2", compact && "items-center")}>
          <Tooltip><TooltipTrigger asChild>
            <button ref={helpTrigger} type="button" onClick={() => setHelpOpen(true)} aria-label="Help" className={cn("flex h-10 items-center gap-3 rounded-[var(--radius-md)] text-sm text-foreground-muted hover:bg-surface-hover focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-inset", compact ? "w-10 justify-center" : "w-full px-3")}>
              <CircleHelp className="h-4 w-4 shrink-0" aria-hidden />{!compact && "Help"}
            </button>
          </TooltipTrigger>{compact && <TooltipContent side="right">Help</TooltipContent>}</Tooltip>
          <AppSidebarLink to={pathname === "/settings" ? `${pathname}${search}` : "/settings"} label="Settings" icon={Settings} compact={compact} selected={pathname === "/settings"} />
        </nav>
      </aside>
      <ConfirmDialog open={importOpen} onOpenChange={setImportOpen} title="Import saved favorites?" confirmLabel="Import favorites" returnFocusRef={importTrigger}
        description={<div className="space-y-3"><p>These favorites were saved in this browser before accounts were separated. Import them into your current account only if they are yours. Existing favorites stay unchanged.</p><ul className="max-h-48 list-disc overflow-y-auto pl-5">{importable.map(v => <li key={v.id}>{v.name}</li>)}</ul></div>}
        onConfirm={() => { importFavorites(importable.map(v => v.id)); setImportOpen(false); requestAnimationFrame(focusFavorites); }} />
      <Dialog open={helpOpen} onOpenChange={setHelpOpen}><DialogContent onCloseAutoFocus={event => { event.preventDefault(); helpTrigger.current?.focus(); }}>
        <DialogHeader><DialogTitle>Getting around AKB</DialogTitle><DialogDescription>Your workspace connects people, knowledge, and AI tools.</DialogDescription></DialogHeader>
        <div className="space-y-4 text-sm">
          <p><strong>Vaults</strong> are shared knowledge spaces. Open one to browse its Collections, documents, tables, and files.</p>
          <p><strong>Favorites and Pinned</strong> are personal shortcuts. Star a Vault, use Pin in the document toolbar, or pin a Collection from its actions menu. They do not change sharing permissions.</p>
          <p><strong>Recently viewed and Drafts</strong> help you return to work saved in this browser. Drafts are not published documents.</p>
          <p><strong>Watch</strong> a document to receive changes in your Inbox. Pinning does not subscribe you to notifications.</p>
          <p><strong>Search</strong> in the top bar for quick results, or use the Search page for advanced filters.</p>
          <Link to="/settings?tab=tokens" onClick={() => setHelpOpen(false)} className="inline-flex rounded-[var(--radius-sm)] text-link underline focus-visible:ring-2 focus-visible:ring-ring">Set up an AI tool connection</Link>
        </div>
      </DialogContent></Dialog>
    </TooltipProvider>
  );
}

function AppSidebarLink({
  to,
  label,
  icon: Icon,
  compact,
  selected,
}: {
  to: string;
  label: string;
  icon: LucideIcon;
  compact: boolean;
  selected: boolean;
}) {
  const link = (
    <Link
      to={to}
      id={to === "/vault" ? "workspace-vaults-link" : undefined}
      aria-label={compact ? label : undefined}
      aria-current={selected ? "page" : undefined}
      className={cn(
        "relative flex h-10 min-w-0 flex-auto items-center rounded-[var(--radius-md)] text-sm font-medium transition-token focus:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-inset",
        compact ? "w-10 justify-center" : "w-full gap-3 px-3",
        selected
          ? "bg-surface-selected text-surface-selected-foreground"
          : "text-foreground-muted hover:bg-surface-hover hover:text-foreground",
      )}
    >
      {selected && (
        <span
          className="absolute bottom-2 left-0 top-2 w-0.5 rounded-full bg-primary"
          aria-hidden
        />
      )}
      <Icon className="h-4 w-4 shrink-0" aria-hidden />
      {!compact && <span>{label}</span>}
    </Link>
  );

  if (!compact) return link;

  return (
    <Tooltip>
      <TooltipTrigger asChild>{link}</TooltipTrigger>
      <TooltipContent side="right">{label}</TooltipContent>
    </Tooltip>
  );
}
