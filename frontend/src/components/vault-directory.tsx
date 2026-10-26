import { useId, useLayoutEffect, useMemo, useRef, useState } from "react";
import { Link } from "react-router-dom";
import { Box, LayoutGrid, List, Plus, Search, Star } from "lucide-react";
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
import { WorkspaceSectionHeader } from "@/components/ui/workspace-section-header";
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

  return <section aria-labelledby="vault-directory-heading" className="@container/directory mx-auto w-full max-w-6xl p-3 sm:p-4 lg:p-6">
    <h1 className="sr-only">Vaults</h1>
    <WorkspaceSectionHeader id="vault-directory-heading" title="Your vaults" icon={Box} tone="collection"
      right={<Button variant="outline" size="sm" onClick={create}><Plus className="h-4 w-4" aria-hidden />New vault</Button>} />
    <div className="mt-4 flex flex-wrap items-end gap-3">
      <div className="min-w-0 flex-1 basis-64">
        <Label htmlFor="vault-directory-query" className="mb-1.5 block text-xs">Find a vault</Label>
        <div className="relative"><Search className="pointer-events-none absolute left-3 top-1/2 h-4 w-4 -translate-y-1/2 text-foreground-muted" aria-hidden />
          <Input id="vault-directory-query" value={query} placeholder="Filter by name or description" className="pl-9"
            onChange={event => { setQuery(event.target.value); setVisible(PAGE); }} />
        </div>
      </div>
      <div role="group" aria-label="Vault list filter" className="flex flex-wrap gap-1 rounded-[var(--radius-md)] border border-border p-1">
        {(["all", "favorites", "owned"] as const).map(value => <Button key={value} ref={value === "favorites" ? favoritesFilter : undefined} variant="ghost" size="sm" aria-pressed={filter === value}
          onClick={() => { setFilter(value); setVisible(PAGE); }}
          className={filter === value ? "bg-surface-selected text-surface-selected-foreground" : ""}>
          {value === "all" ? "All" : value === "favorites" ? "Favorites" : "Owned"}
        </Button>)}
      </div>
    </div>
    <div className="my-3 flex flex-wrap items-center justify-between gap-2">
    <p aria-label={vaults.length + (vaults.length === 1 ? " vault available" : " vaults available")} className="text-xs text-foreground-muted">
      {matches.length.toLocaleString()} {matches.length === 1 ? "vault" : "vaults"}{query || filter !== "all" ? " matching your filters" : " available"}
    </p>
    <TooltipProvider>
      <div role="group" aria-label="Vault view" className="flex rounded-[var(--radius-md)] border border-border p-0.5">
        {([{ value: "grid", label: "Grid view", icon: LayoutGrid }, { value: "list", label: "List view", icon: List }] as const).map(({ value, label, icon: Icon }) =>
          <Tooltip key={value}><TooltipTrigger asChild>
            <Button variant="ghost" size="icon" aria-label={label} aria-pressed={view === value} aria-controls={`${directoryId}-list`}
              onClick={() => changeView(value)} className={cn("max-sm:h-11 max-sm:w-11 [@media(pointer:coarse)]:h-11 [@media(pointer:coarse)]:w-11", view === value && "bg-surface-selected text-surface-selected-foreground")}>
              <Icon className="h-4 w-4" aria-hidden />
            </Button>
          </TooltipTrigger><TooltipContent>{label}</TooltipContent></Tooltip>,
        )}
      </div>
    </TooltipProvider>
    </div>
    <ul id={`${directoryId}-list`} aria-label="Available vaults" data-slot="vault-directory-grid" data-view={view} className={view === "grid"
      ? "grid auto-rows-fr grid-cols-1 gap-3 @min-[30rem]/directory:grid-cols-2 @min-[48rem]/directory:grid-cols-3 @min-[64rem]/directory:grid-cols-4"
      : "divide-y divide-border overflow-hidden rounded-[var(--radius-md)] border border-border bg-surface"}>
      {matches.slice(0, visible).map(vault => {
        const nameId = `${directoryId}-${vault.id}-name`;
        const descriptionId = `${directoryId}-${vault.id}-description`;
        const accessId = `${directoryId}-${vault.id}-access`;
        const description = vault.description?.trim();
        const readOnly = vault.role === "reader";
        const archived = vault.status === "archived";
        const describedBy = [description && descriptionId, (readOnly || archived) && accessId].filter(Boolean).join(" ");
        return <li key={vault.id} className="min-w-0">
        <Panel data-slot="vault-directory-card" variant="workspace" inset={false} flush className={cn("relative flex h-full min-w-0 transition-token", view === "grid"
          ? "vault-box min-h-32 rounded-none border-0 bg-transparent"
          : "min-h-16 rounded-none border-0 hover:bg-surface-hover")}>
        <Link to={"/vault/" + encodeURIComponent(vault.name)} aria-labelledby={nameId} aria-describedby={describedBy || undefined}
          className={cn("group flex min-w-0 flex-1 p-3 focus:outline-none focus-visible:ring-2 focus-visible:ring-inset focus-visible:ring-ring", view === "grid"
            ? "vault-box-link flex-col rounded-[var(--radius-xs)]"
            : "items-center gap-3 pr-14")}>
          {view === "grid" ? <div data-slot="vault-card-mark" className="mb-2 flex h-8 shrink-0 items-center pr-11"><Box className="h-4 w-4 shrink-0 text-link" aria-hidden /></div>
            : <Box className="h-4 w-4 shrink-0 text-link" aria-hidden />}
          <div className={cn("min-w-0 flex-1", view === "grid" && "flex flex-col")}>
            <h2 id={nameId} className="min-w-0 break-words text-sm font-semibold leading-5 text-foreground [overflow-wrap:anywhere] group-hover:text-link">{vault.name}</h2>
            {description && <p id={descriptionId} title={description} className={cn("line-clamp-1 break-words text-xs leading-4 text-foreground-muted", view === "grid" ? "mt-auto pt-2" : "mt-1")}>{description}</p>}
            {(readOnly || archived) && <div id={accessId} data-slot="vault-card-access" className="mt-2 flex flex-wrap items-center gap-2">
              {archived && <Badge variant="archived">Archived</Badge>}
              {readOnly && <Badge variant="outline">Read only</Badge>}
            </div>}
          </div>
        </Link>
        <Button ref={node => { if (node) favoriteButtons.current.set(vault.id, node); else favoriteButtons.current.delete(vault.id); }}
          variant="ghost" size="icon" disabled={!user?.user_id} aria-pressed={isFavorite(vault.id)}
          aria-label={(isFavorite(vault.id) ? "Remove " : "Add ") + vault.name + (isFavorite(vault.id) ? " from favorites" : " to favorites")}
          className={cn("absolute h-11 w-11 shrink-0", view === "grid" ? "right-[calc(var(--spacing-vault-box-depth)+.25rem)] top-[calc(var(--spacing-vault-box-depth)+.25rem)]" : "right-1 top-1/2 -translate-y-1/2")} onClick={event => {
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
