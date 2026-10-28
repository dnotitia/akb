import { Link, matchPath, useLocation, useMatch } from "react-router-dom";
import { ChevronLeft, ChevronsLeft, ChevronsRight } from "lucide-react";
import { useResourceNavigation } from "@/contexts/resource-navigation-context";
import { Button } from "@/components/ui/button";
import { Tooltip, TooltipContent, TooltipProvider, TooltipTrigger } from "@/components/ui/tooltip";
import { cn } from "@/lib/utils";

export interface VaultNavigationControl {
  open: boolean;
  onToggle: () => void;
}

/** The context boundary owns disclosure; the page title owns parent navigation. */
export function VaultNavigationToggle({ open, onToggle, id, hidden = false }: VaultNavigationControl & { id: string; hidden?: boolean }) {
  const label = open ? "Close vault navigation" : "Open vault navigation";
  const Icon = open ? ChevronsLeft : ChevronsRight;
  return <TooltipProvider delayDuration={250}><Tooltip><TooltipTrigger asChild>
    <Button id={id} variant="ghost" size="icon" hidden={hidden} data-vault-navigation-toggle=""
      aria-label={label} aria-expanded={open} aria-controls="vault-workspace-navigation" onClick={onToggle}
      className={cn("h-9 w-9 shrink-0 rounded-[var(--radius-sm)] text-foreground-muted hover:text-link max-sm:h-11 max-sm:w-11 pointer-coarse:h-11 pointer-coarse:w-11", hidden && "hidden")}>
      <Icon className="h-4 w-4" aria-hidden />
    </Button>
  </TooltipTrigger><TooltipContent>{label}</TooltipContent></Tooltip></TooltipProvider>;
}

/** A stable parent destination, including on direct visits without local history. */
export function VaultParentBackLink({ className }: { className?: string }) {
  const { pathname } = useLocation();
  const vault = useMatch({ path: "/vault/:name", end: false })?.params.name;
  if (!vault) return null;
  const overview = Boolean(matchPath("/vault/:name", pathname));
  const href = overview ? "/vault" : `/vault/${encodeURIComponent(vault)}`;
  const label = overview ? "Back to all vaults" : `Back to vault ${vault}`;
  return <VaultBackLink href={href} label={label} className={className} />;
}

/** Return to the Vault directory with its context panel ready for selection. */
export function VaultSidebarBackLink() {
  return <VaultBackLink href="/vault" label="Show vault list" sidebar />;
}

function VaultBackLink({ href, label, className, sidebar = false }: { href: string; label: string; className?: string; sidebar?: boolean }) {
  const { requestNavigation } = useResourceNavigation();
  const state = sidebar ? { vaultSidebarNavigation: true } : { vaultPageNavigation: href };
  return <TooltipProvider delayDuration={250}><Tooltip><TooltipTrigger asChild>
    <Link to={href} state={state} data-vault-parent-back={sidebar ? undefined : ""} data-vault-sidebar-return={sidebar ? "" : undefined} aria-label={label}
      onClick={event => {
        if (event.defaultPrevented || event.button !== 0 || event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return;
        if (!requestNavigation(href, { state })) event.preventDefault();
      }}
      className={cn("inline-flex h-9 w-9 shrink-0 items-center justify-center rounded-[var(--radius-sm)] text-foreground-muted hover:bg-surface-hover hover:text-link focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-inset focus-visible:ring-ring max-sm:h-11 max-sm:w-11 pointer-coarse:h-11 pointer-coarse:w-11", className)}>
      <ChevronLeft className="h-4 w-4" aria-hidden />
    </Link>
  </TooltipTrigger><TooltipContent>{label}</TooltipContent></Tooltip></TooltipProvider>;
}
