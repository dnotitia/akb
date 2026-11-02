import { useId, useLayoutEffect, useMemo, useRef, useState } from "react";
import { Link } from "react-router-dom";
import { ArrowUpRight, Box, LayoutGrid, List, Plus, Search, Star } from "lucide-react";
import { useVaultFavorites } from "@/hooks/use-vault-favorites";
import { useVaultDirectoryView } from "@/hooks/use-vault-directory-view";
import type { VaultSummary } from "@/hooks/use-vaults";
import { useCurrentUser } from "@/contexts/current-user-context";
import { useOpenVaultCreateDialog } from "@/contexts/vault-create-dialog-context";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { AutoLoadMore } from "@/components/ui/auto-load-more";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Panel } from "@/components/ui/panel";
import { Tooltip, TooltipContent, TooltipProvider, TooltipTrigger } from "@/components/ui/tooltip";
import { cn } from "@/lib/utils";

type DirectoryVault = VaultSummary & { description?: string; status?: string };
type Filter = "all" | "favorites" | "owned";
const PAGE = 30;

/** An actual directory is the primary entry point even with the navigator hidden. */
export function VaultDirectory({ vaults }: { vaults: DirectoryVault[] }) {
  const directoryId = useId();
  const create = useOpenVaultCreateDialog();
  const user = useCurrentUser();
  const { isFavorite, toggleFavorite, favOrder } = useVaultFavorites();
  const { view, changeView } = useVaultDirectoryView();
  const [query, setQuery] = useState("");
  const [filter, setFilter] = useState<Filter>("all");
  const [visible, setVisible] = useState(PAGE);
  const favoriteButtons = useRef(new Map<string, HTMLButtonElement>());
  const favoritesFilter = useRef<HTMLButtonElement>(null);
  const pendingFavoriteFocus = useRef<{ source: HTMLButtonElement; index: number } | null>(null);
  const matches = useMemo(() => vaults.filter(vault =>
    (filter !== "owned" || vault.role === "owner") &&
    (filter !== "favorites" || isFavorite(vault.id)) &&
    (vault.name + " " + (vault.description ?? "")).toLocaleLowerCase().includes(query.trim().toLocaleLowerCase()),
  ).sort((a, b) => {
    const aFav = isFavorite(a.id), bFav = isFavorite(b.id);
    return aFav !== bFav ? (aFav ? -1 : 1) : aFav ? favOrder(a.id) - favOrder(b.id) : a.name.localeCompare(b.name);
  }), [vaults, filter, query, isFavorite, favOrder]);

  useLayoutEffect(() => {
    const pending = pendingFavoriteFocus.current;
    pendingFavoriteFocus.current = null;
    if (!pending || pending.source.isConnected ||
      (document.activeElement !== document.body && document.activeElement !== pending.source)) return;

    const remaining = matches.slice(0, visible);
    const next = remaining[Math.min(pending.index, remaining.length - 1)];
    const target = next && favoriteButtons.current.get(next.id);
    if (target && !target.disabled) target.focus();
    else favoritesFilter.current?.focus();
  }, [matches, visible]);

  return <section aria-labelledby="vault-directory-heading" className="vault-page vault-directory @container/directory">
    <header className="vault-directory-heading">
      <div className="min-w-0">
        <h1 id="vault-directory-heading">Your vaults</h1>
        <p className="mt-2 text-sm text-foreground-muted">A home for your documents, data, and ideas.</p>
      </div>
      <Button variant="accent" className="vault-primary-action shadow-none" onClick={create}><Plus className="h-4 w-4" aria-hidden />New vault</Button>
    </header>
    <div className="vault-directory-tools">
      <div className="vault-directory-search">
        <Label htmlFor="vault-directory-query" className="sr-only">Find a vault</Label>
        <Search className="pointer-events-none absolute left-3 top-1/2 h-4 w-4 -translate-y-1/2 text-foreground-muted" aria-hidden />
        <Input id="vault-directory-query" value={query} placeholder="Find a vault…" className="pl-10"
          onChange={event => { setQuery(event.target.value); setVisible(PAGE); }} />
      </div>
    </div>
    <div className="vault-directory-filter-row">
      <div role="group" aria-label="Vault list filter" className="vault-directory-filters">
        {(["all", "favorites", "owned"] as const).map(value => <Button key={value} ref={value === "favorites" ? favoritesFilter : undefined} variant="ghost" size="sm" aria-pressed={filter === value}
          onClick={() => { setFilter(value); setVisible(PAGE); }}>
          {value === "all" ? "All" : value === "favorites" ? "Favorites" : "Owned"}
        </Button>)}
      </div>
      <p aria-label={vaults.length + (vaults.length === 1 ? " vault available" : " vaults available")} className="text-xs text-foreground-muted">
        {matches.length.toLocaleString()} {matches.length === 1 ? "vault" : "vaults"}{query || filter !== "all" ? " matching your filters" : " available"}
      </p>
      <TooltipProvider>
        <div role="group" aria-label="Vault view" className="vault-view-switch">
          {([{ value: "grid", label: "Grid view", icon: LayoutGrid }, { value: "list", label: "List view", icon: List }] as const).map(({ value, label, icon: Icon }) =>
            <Tooltip key={value}><TooltipTrigger asChild>
              <Button variant="ghost" size="icon" aria-label={label} aria-pressed={view === value} aria-controls={`${directoryId}-list`}
                onClick={() => changeView(value)}><Icon className="h-4 w-4" aria-hidden /></Button>
            </TooltipTrigger><TooltipContent>{label}</TooltipContent></Tooltip>,
          )}
        </div>
      </TooltipProvider>
    </div>
    <ul id={`${directoryId}-list`} aria-label="Available vaults" data-slot="vault-directory-grid" data-view={view}
      className={view === "grid" ? "vault-library-grid" : "vault-library-list"}>
      {matches.slice(0, visible).map(vault => {
        const nameId = `${directoryId}-${vault.id}-name`;
        const descriptionId = `${directoryId}-${vault.id}-description`;
        const accessId = `${directoryId}-${vault.id}-access`;
        const description = vault.description?.trim();
        const readOnly = vault.role === "reader";
        const archived = vault.status === "archived";
        const describedBy = [description && descriptionId, (readOnly || archived) && accessId].filter(Boolean).join(" ");
        return <li key={vault.id} className="min-w-0">
        <Panel data-slot="vault-directory-card" variant="workspace" inset={false} flush className={cn("vault-library-card relative flex h-full min-w-0", isFavorite(vault.id) && "vault-library-card-favorite")}>
        <Link to={"/vault/" + encodeURIComponent(vault.name)} aria-labelledby={nameId} aria-describedby={describedBy || undefined}
          className="vault-library-card-link group min-w-0 flex-1 focus:outline-none focus-visible:ring-2 focus-visible:ring-inset focus-visible:ring-ring">
          <div data-slot="vault-card-mark" className="vault-library-mark"><Box className="h-5 w-5" aria-hidden /></div>
          <div className="vault-library-card-copy min-w-0 flex-1">
            <h2 id={nameId} className="min-w-0 break-words text-ui font-semibold leading-5 text-foreground [overflow-wrap:anywhere] group-hover:text-link">{vault.name}</h2>
            {description && <p id={descriptionId} title={description} className="vault-library-card-description line-clamp-1 break-words text-xs leading-4 text-foreground-muted">{description}</p>}
            {(readOnly || archived) && <div id={accessId} data-slot="vault-card-access" className="mt-1 flex flex-wrap items-center gap-2">
              {archived && <Badge variant="archived">Archived</Badge>}
              {readOnly && <Badge variant="outline">Read only</Badge>}
            </div>}
          </div>
          <ArrowUpRight className="vault-library-card-arrow h-4 w-4" aria-hidden />
        </Link>
        <Button ref={node => { if (node) favoriteButtons.current.set(vault.id, node); else favoriteButtons.current.delete(vault.id); }}
          variant="ghost" size="icon" disabled={!user?.user_id} aria-pressed={isFavorite(vault.id)}
          aria-label={(isFavorite(vault.id) ? "Remove " : "Add ") + vault.name + (isFavorite(vault.id) ? " from favorites" : " to favorites")}
          className="vault-library-favorite absolute h-11 w-11 shrink-0" onClick={event => {
            // A keyboard/assistive activation removes this focused card only in Favorites.
            if (filter === "favorites" && isFavorite(vault.id) && event.detail === 0 && document.activeElement === event.currentTarget) {
              pendingFavoriteFocus.current = { source: event.currentTarget, index: matches.findIndex(match => match.id === vault.id) };
            }
            toggleFavorite(vault.id);
          }}>
          <Star className={isFavorite(vault.id) ? "h-4 w-4 fill-current text-link" : "h-4 w-4 text-foreground-muted"} aria-hidden />
        </Button>
        </Panel>
      </li>; })}
    </ul>
    {matches.length === 0 && <div role="status" className="py-8 text-center">
      <p className="text-sm font-medium">{filter === "favorites" && !query ? "No favorite vaults yet" : "No vaults match these filters"}</p>
      <p className="mt-1 text-sm text-foreground-muted">{filter === "favorites" && !query ? "Use the star beside a vault to keep it close." : "Try another name or show all available vaults."}</p>
      <Button variant="link" className="mt-2" onClick={() => { setQuery(""); setFilter("all"); setVisible(PAGE); }}>Show all vaults</Button>
    </div>}
    <AutoLoadMore hasMore={matches.length > visible} requestKey={`${query}:${filter}:${visible}:${matches.length}`}
      label="Show more vaults" onLoadMore={() => setVisible(count => count + PAGE)} />
  </section>;
}
