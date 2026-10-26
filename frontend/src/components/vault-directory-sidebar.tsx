import { useEffect, useId, useMemo, useRef, useState, type MouseEvent } from "react";
import { Link } from "react-router-dom";
import { Box, Plus, Star } from "lucide-react";
import { Button } from "@/components/ui/button";
import { AutoLoadMore } from "@/components/ui/auto-load-more";
import { Skeleton } from "@/components/ui/skeleton";
import { InlineLoadingState } from "@/components/ui/loading-state";
import { RailFilterField, RailFilterToggle, RailIdentity, RailManagement, RailSearchToolbar } from "@/components/navigation-rail-controls";
import { VaultNavigationToggle, type VaultNavigationControl } from "@/components/vault-navigation-controls";
import { Tooltip, TooltipContent, TooltipProvider, TooltipTrigger } from "@/components/ui/tooltip";
import { useResourceNavigation } from "@/contexts/resource-navigation-context";
import { useVaultFavorites } from "@/hooks/use-vault-favorites";
import { useVaults } from "@/hooks/use-vaults";
import { SCOPES, inScope, readScope, writeScope, type RoleScope } from "@/lib/vault-scope";
import { cn } from "@/lib/utils";

/** The directory and collection explorer occupy the same context panel. */
export function VaultDirectorySidebar({ onCreateVault, onNavigate, onRefetchReady, navigationControl }: {
  onCreateVault: () => void;
  onNavigate?: () => void;
  onRefetchReady?: (refetch: () => void) => void;
  navigationControl?: VaultNavigationControl;
}) {
  const { vaults, loading, error, refetch } = useVaults();
  const { isFavorite, favOrder } = useVaultFavorites();
  const { requestNavigation } = useResourceNavigation();
  const [query, setQuery] = useState("");
  const [scope, setScope] = useState<RoleScope>(readScope);
  const [showScope, setShowScope] = useState(false);
  const [visible, setVisible] = useState(30);
  const input = useRef<HTMLInputElement>(null);
  const filterId = useId();
  useEffect(() => { onRefetchReady?.(refetch); }, [onRefetchReady, refetch]);
  const matches = useMemo(() => vaults.filter(vault => inScope(vault.role, scope)
    && vault.name.toLocaleLowerCase().includes(query.trim().toLocaleLowerCase()))
    .sort((a, b) => {
      const aFavorite = isFavorite(a.id), bFavorite = isFavorite(b.id);
      return aFavorite !== bFavorite ? (aFavorite ? -1 : 1)
        : aFavorite ? favOrder(a.id) - favOrder(b.id) : a.name.localeCompare(b.name);
    }), [vaults, query, scope, isFavorite, favOrder]);
  const shown = matches.slice(0, visible);
  const favorites = shown.filter(vault => isFavorite(vault.id));
  const others = shown.filter(vault => !isFavorite(vault.id));
  function follow(event: MouseEvent<HTMLAnchorElement>, href: string) {
    if (event.defaultPrevented || event.button !== 0 || event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return;
    if (!requestNavigation(href, { state: { vaultPageNavigation: href } })) { event.preventDefault(); return; }
    onNavigate?.();
  }
  function changeScope(next: RoleScope) { setScope(next); writeScope(next); setVisible(30); }
  const row = (vault: typeof vaults[number]) => {
    const href = `/vault/${encodeURIComponent(vault.name)}`;
    return <li key={vault.id}><Link to={href} state={{ vaultPageNavigation: href }} onClick={event => follow(event, href)}
      className="flex min-h-9 items-center gap-2 rounded-[var(--radius-sm)] px-2 py-1.5 text-ui text-foreground hover:bg-surface-hover hover:text-link focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-inset focus-visible:ring-ring max-sm:min-h-11 pointer-coarse:min-h-11">
      <Box className="h-4 w-4 shrink-0 text-link" aria-hidden /><span className="min-w-0 flex-1 break-words [overflow-wrap:anywhere]">{vault.name}</span>
      {isFavorite(vault.id) && <Star className="h-3 w-3 shrink-0 fill-current text-link" aria-hidden />}
    </Link></li>;
  };
  return <div className="flex min-h-0 flex-1 flex-col" data-slot="vault-directory-sidebar">
    <RailIdentity slot="vault-identity-header" workspace>
      <h2 className="min-w-0 flex-1 px-2 text-sm font-semibold">Knowledge</h2>
      {navigationControl && <VaultNavigationToggle id="vault-navigation-collapse" {...navigationControl} />}
    </RailIdentity>
    <RailManagement label="Vaults" slot="vault-management-row" workspace>
      <RailFilterToggle compact label="Filter sidebar vaults by role" open={showScope} count={Number(scope !== "all")} controls={filterId} onClick={() => setShowScope(value => !value)} />
      <TooltipProvider><Tooltip><TooltipTrigger asChild><Button variant="ghost" size="icon" onClick={onCreateVault} aria-label="Create vault" className="h-9 w-9 shrink-0 rounded-[var(--radius-sm)] text-foreground-muted transition-token hover:bg-surface-hover hover:text-link focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-inset max-sm:h-11 max-sm:w-11 pointer-coarse:h-11 pointer-coarse:w-11"><Plus className="h-3.5 w-3.5" aria-hidden /></Button></TooltipTrigger><TooltipContent>Create vault</TooltipContent></Tooltip></TooltipProvider>
    </RailManagement>
    <RailSearchToolbar search={<RailFilterField compact inputRef={input} label="Find a vault in sidebar" placeholder="Find vault" value={query} onChange={value => { setQuery(value); setVisible(30); }} />} />
    <div id={filterId} hidden={!showScope} className="shrink-0 px-2 pb-2"><div role="group" aria-label="Filter sidebar vaults by your role" className="flex gap-1 rounded-[var(--radius-md)] bg-background p-1">
      {SCOPES.map(option => <button key={option.key} type="button" aria-pressed={scope === option.key} onClick={() => changeScope(option.key)} className={cn("min-h-9 flex-1 rounded-[var(--radius-sm)] px-2 text-xs focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring max-sm:min-h-11 pointer-coarse:min-h-11", scope === option.key ? "bg-surface-selected text-surface-selected-foreground" : "text-foreground-muted hover:bg-surface-hover")}>{option.label}</button>)}
    </div></div>
    {(scope !== "all" || query) && <div className="flex shrink-0 items-center justify-between gap-2 px-3 pb-1 text-xs text-foreground-muted"><span>{scope !== "all" ? SCOPES.find(option => option.key === scope)?.label : "Name filter"}</span><Button variant="link" size="sm" className="h-9 px-0 max-sm:h-11 pointer-coarse:h-11" onClick={() => { setQuery(""); changeScope("all"); input.current?.focus(); }}>Reset filters</Button></div>}
    <div className="min-h-0 flex-1 overflow-y-auto overscroll-contain px-1.5 pb-3 rail-scroll">
      {loading && <InlineLoadingState label={vaults.length ? "Updating vaults" : "Loading vaults"} className="px-2 py-2" />}
      {loading && vaults.length === 0 && <div aria-hidden>{Array.from({ length: 6 }, (_, index) => <div key={index} className="flex h-9 items-center gap-2 px-2 max-sm:h-11 pointer-coarse:h-11"><Skeleton className="h-4 w-4" /><Skeleton className="h-3 flex-1" /></div>)}</div>}
      {error && <div role="status" className="px-2 py-2 text-xs text-foreground-muted">{vaults.length ? "Couldn't update vaults. Previous list shown." : "Vaults unavailable."}<Button variant="link" size="sm" className="ml-2 h-9 px-1 max-sm:h-11 pointer-coarse:h-11" onClick={refetch} disabled={loading}>Retry</Button></div>}
      <nav aria-label="Vaults" aria-busy={loading || undefined}>
        {favorites.length > 0 && <section aria-label="Favorite vaults"><h3 className="px-2 pb-1 pt-1 text-xs font-medium text-foreground-muted">Favorites</h3><ul>{favorites.map(row)}</ul></section>}
        {others.length > 0 && <section aria-label="Other vaults">{favorites.length > 0 && <h3 className="px-2 pb-1 pt-2 text-xs font-medium text-foreground-muted">Other vaults</h3>}<ul>{others.map(row)}</ul></section>}
        {!loading && !error && vaults.length === 0 && <p className="px-2 py-3 text-xs text-foreground-muted">No vaults yet.</p>}
        {vaults.length > 0 && matches.length === 0 && <p role="status" className="px-2 py-3 text-xs text-foreground-muted">No vaults match these filters.</p>}
        <AutoLoadMore hasMore={matches.length > visible} paused={loading} requestKey={`${query}:${scope}:${visible}:${matches.length}`}
          label="Show more vaults" onLoadMore={() => setVisible(value => value + 30)} />
      </nav>
    </div>
  </div>;
}
