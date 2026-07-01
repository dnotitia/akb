import { Link, useLocation } from "react-router-dom";
import { useEffect, useMemo, useState } from "react";
import {
  Box,
  ChevronsLeft,
  ChevronsRight,
  Filter,
  Plus,
  RefreshCw,
  Search as SearchIcon,
  Star,
} from "lucide-react";
import { useVaults, type VaultSummary } from "@/hooks/use-vaults";
import { useVaultFavorites } from "@/hooks/use-vault-favorites";
import { VaultChip } from "@/components/ui/vault-chip";
import { TooltipText } from "@/components/ui/tooltip-text";
import { roleIcon } from "@/lib/roles";
import { SCOPES, type RoleScope, inScope, readScope, writeScope } from "@/lib/vault-scope";
import {
  Tooltip,
  TooltipContent,
  TooltipProvider,
  TooltipTrigger,
} from "@/components/ui/tooltip";
import { cn } from "@/lib/utils";

/**
 * The vault column — an always-visible, favoritable, role-aware list of vaults
 * in its OWN column / scroll axis, separate from the collection tree to its
 * right. Two modes the user toggles:
 *
 *   • expanded (drag-resizable, default 240px): VaultChip + NAME + a hover star
 *     (pin) + a trailing role glyph, grouped Favorites-first, with a name filter
 *     + a role-scope filter;
 *   • collapsed (w-14): an icon RAIL of monograms, favorites first, a teal
 *     corner dot on pinned vaults, role + favorite surfaced via the tooltip.
 *
 * Favorites persist per-browser (localStorage, keyed by vault id); the role
 * scope persists too. Both modes read the same partition so they never disagree.
 */

export function VaultRail({
  current,
  onRefetchReady,
  collapsed,
  onToggleCollapsed,
  width,
}: {
  current: string;
  onRefetchReady?: (refetch: () => void) => void;
  collapsed: boolean;
  onToggleCollapsed: () => void;
  /** Requested rail width (px) from VaultShell's drag-resize; applied only in
   *  expanded mode — the collapsed branch is a fixed w-14 icon rail and ignores
   *  it. The right divider is drawn by the shell's resize handle, not this nav. */
  width?: number;
}) {
  const { vaults, loading, refetch } = useVaults();
  const { isFavorite, toggleFavorite, favOrder } = useVaultFavorites();
  const { pathname } = useLocation();
  const [filter, setFilter] = useState("");
  const [scope, setScope] = useState<RoleScope>(readScope);
  const [showScope, setShowScope] = useState(() => readScope() !== "all");

  useEffect(() => {
    refetch();
  }, [pathname, refetch]);

  useEffect(() => {
    onRefetchReady?.(refetch);
  }, [onRefetchReady, refetch]);

  const changeScope = (s: RoleScope) => {
    setScope(s);
    writeScope(s);
  };

  const q = filter.trim().toLowerCase();
  // Role scope applies to BOTH modes; the name filter is expanded-only.
  const scoped = useMemo(() => vaults.filter((v) => inScope(v.role, scope)), [vaults, scope]);
  const filtered = useMemo(
    () => (q ? scoped.filter((v) => v.name?.toLowerCase().includes(q)) : scoped),
    [scoped, q],
  );

  // Partition into favorites (in favorite order) + the rest (backend A-Z order).
  // Deriving from the LIVE list means a favorited-but-deleted/revoked vault id
  // simply never matches — no ghost rows.
  const partition = (list: VaultSummary[]) => {
    const favs = list.filter((v) => isFavorite(v.id)).sort((a, b) => favOrder(a.id) - favOrder(b.id));
    const others = list.filter((v) => !isFavorite(v.id));
    return { favs, others };
  };

  const headBtn =
    "text-foreground-muted hover:text-link transition-colors cursor-pointer focus:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2 focus-visible:ring-offset-surface disabled:cursor-default disabled:opacity-50";
  const railBtn =
    "flex h-9 w-9 items-center justify-center rounded-[var(--radius-md)] text-foreground-muted hover:text-foreground hover:bg-surface-hover transition-token focus:outline-none focus-visible:ring-2 focus-visible:ring-ring cursor-pointer disabled:cursor-default disabled:opacity-50";

  // ── Collapsed: a thin icon rail (favorites first) ───────────────────
  if (collapsed) {
    const { favs, others } = partition(scoped);
    const ordered = [...favs, ...others];
    return (
      <TooltipProvider delayDuration={250}>
        <nav
          aria-label="Vaults"
          className="w-14 shrink-0 h-full flex flex-col bg-surface border-r border-border"
        >
          <div className="shrink-0 flex flex-col items-center gap-1 py-2 border-b border-border">
            <Tooltip>
              <TooltipTrigger asChild>
                <button
                  type="button"
                  onClick={onToggleCollapsed}
                  aria-label="Expand vault list"
                  aria-expanded={false}
                  className={railBtn}
                >
                  <ChevronsRight className="h-4 w-4" aria-hidden />
                </button>
              </TooltipTrigger>
              <TooltipContent side="right">Expand vault list</TooltipContent>
            </Tooltip>
            <Tooltip>
              <TooltipTrigger asChild>
                <Link to="/vault/new" aria-label="New vault" className={railBtn}>
                  <Plus className="h-4 w-4" aria-hidden />
                </Link>
              </TooltipTrigger>
              <TooltipContent side="right">New vault</TooltipContent>
            </Tooltip>
          </div>
          <ul className="flex-1 overflow-y-auto rail-scroll py-2 flex flex-col items-center gap-1">
            {ordered.length === 0 ? (
              <li className="coord py-2" aria-live="polite">
                {loading ? "…" : "—"}
              </li>
            ) : (
              ordered.map((v) => {
                const active = v.name === current;
                const fav = isFavorite(v.id);
                const roleLabel = v.role ? ` · ${v.role}` : "";
                return (
                  <li key={v.id}>
                    <Tooltip>
                      <TooltipTrigger asChild>
                        <Link
                          to={`/vault/${v.name}`}
                          aria-label={`${v.name}${roleLabel}${fav ? " · favorite" : ""}`}
                          aria-current={active ? "page" : undefined}
                          onKeyDown={(e) => {
                            if (e.key === "p") {
                              e.preventDefault();
                              toggleFavorite(v.id);
                            }
                          }}
                          className={cn(
                            "relative flex h-11 w-11 items-center justify-center rounded-[var(--radius-md)] transition-token",
                            "focus:outline-none focus-visible:ring-2 focus-visible:ring-ring",
                            active ? "bg-surface-selected ring-2 ring-primary" : "hover:bg-surface-hover",
                          )}
                        >
                          <VaultChip name={v.name} size="md" />
                          {fav && (
                            <span
                              className="absolute -top-0.5 -right-0.5 h-2 w-2 rounded-full bg-primary ring-2 ring-surface"
                              aria-hidden
                            />
                          )}
                        </Link>
                      </TooltipTrigger>
                      <TooltipContent side="right">
                        {v.name}
                        {roleLabel}
                        {fav ? " · favorite" : ""}
                      </TooltipContent>
                    </Tooltip>
                  </li>
                );
              })
            )}
          </ul>
        </nav>
      </TooltipProvider>
    );
  }

  // ── Expanded: a named, grouped, filterable list ─────────────────────
  const { favs, others } = partition(filtered);

  const renderRow = (v: VaultSummary) => {
    const active = v.name === current;
    const fav = isFavorite(v.id);
    const { Icon: RoleIcon, label: roleLabel } = v.role
      ? roleIcon(v.role)
      : { Icon: null, label: "" };
    return (
      <li
        key={v.id}
        className={cn(
          "group relative flex items-center rounded-[var(--radius-sm)]",
          active ? "bg-surface-selected" : "hover:bg-surface-hover",
        )}
      >
        {active && (
          <span className="absolute left-0 top-1.5 bottom-1.5 w-0.5 rounded-full bg-primary" aria-hidden />
        )}
        <Link
          to={`/vault/${v.name}`}
          aria-current={active ? "page" : undefined}
          aria-label={v.role ? `${v.name}, ${roleLabel}` : v.name}
          onKeyDown={(e) => {
            if (e.key === "p") {
              e.preventDefault();
              toggleFavorite(v.id);
            }
          }}
          className={cn(
            "flex min-w-0 flex-1 items-center gap-2 pl-3 h-9 text-sm transition-colors rounded-[var(--radius-sm)]",
            "focus:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-inset",
            active ? "text-surface-selected-foreground" : "text-foreground",
          )}
        >
          <Box className="h-3.5 w-3.5 shrink-0" aria-hidden />
          <TooltipText tip={v.name} side="right" className="truncate">
            {v.name}
          </TooltipText>
        </Link>
        <button
          type="button"
          onClick={() => toggleFavorite(v.id)}
          aria-label={fav ? `Unpin ${v.name}` : `Pin ${v.name}`}
          aria-pressed={fav}
          className={cn(
            "shrink-0 rounded-[var(--radius-sm)] p-1 transition-colors focus:outline-none focus-visible:ring-2 focus-visible:ring-ring",
            fav
              ? "text-primary"
              : "text-foreground-muted opacity-0 hover:text-foreground focus-visible:opacity-100 group-hover:opacity-100",
          )}
        >
          <Star className={cn("h-3.5 w-3.5", fav && "fill-current")} aria-hidden />
        </button>
        {RoleIcon && (
          <span title={roleLabel} className="shrink-0 pr-2 text-foreground-muted" aria-hidden>
            <RoleIcon className="h-3.5 w-3.5" aria-hidden />
          </span>
        )}
      </li>
    );
  };

  return (
    <nav
      aria-label="Vaults"
      style={{ width }}
      className="shrink-0 h-full flex flex-col bg-surface"
    >
      <div className="flex items-center justify-between h-9 px-3 shrink-0 border-b border-border">
        <span className="coord-ink">Vaults</span>
        <div className="flex items-center gap-2.5">
          <button
            type="button"
            onClick={refetch}
            disabled={loading}
            title="Refresh vaults"
            aria-label="Refresh vaults"
            className={headBtn}
          >
            <RefreshCw className={cn("h-3 w-3", loading && "animate-spin")} aria-hidden />
          </button>
          <button
            type="button"
            onClick={() => setShowScope((s) => !s)}
            aria-pressed={showScope}
            title="Filter by role"
            aria-label="Filter by role"
            className={cn(headBtn, scope !== "all" && "text-primary hover:text-primary")}
          >
            <Filter className="h-3.5 w-3.5" aria-hidden />
          </button>
          <Link to="/vault/new" title="New vault" aria-label="New vault" className={headBtn}>
            <Plus className="h-3.5 w-3.5" aria-hidden />
          </Link>
          <button
            type="button"
            onClick={onToggleCollapsed}
            title="Minimize to rail"
            aria-label="Minimize vault list to a rail"
            aria-expanded={true}
            className={headBtn}
          >
            <ChevronsLeft className="h-4 w-4" aria-hidden />
          </button>
        </div>
      </div>

      {vaults.length > 0 && (
        <div className="px-2 py-1.5 shrink-0 border-b border-border space-y-1.5">
          <div className="relative">
            <SearchIcon
              className="absolute left-2 top-1/2 -translate-y-1/2 h-3 w-3 text-foreground-muted pointer-events-none"
              aria-hidden
            />
            <input
              type="search"
              value={filter}
              onChange={(e) => setFilter(e.target.value)}
              placeholder="Filter vaults"
              aria-label="Filter vaults"
              className="w-full h-8 pl-6 pr-2 rounded-[var(--radius-md)] bg-background border border-border text-xs text-foreground placeholder:text-foreground-muted focus:outline-none focus:border-primary focus-visible:ring-2 focus-visible:ring-ring transition-colors"
            />
          </div>
          {showScope && (
            <div
              role="group"
              aria-label="Filter vaults by your role"
              className="flex gap-0.5 rounded-[var(--radius-md)] bg-background p-0.5"
            >
              {SCOPES.map((s) => (
                <button
                  key={s.key}
                  type="button"
                  onClick={() => changeScope(s.key)}
                  aria-pressed={scope === s.key}
                  className={cn(
                    "flex-1 rounded-[var(--radius-sm)] px-1 py-1 text-[11px] transition-colors focus:outline-none focus-visible:ring-2 focus-visible:ring-ring",
                    scope === s.key
                      ? "bg-surface-selected text-surface-selected-foreground font-medium"
                      : "text-foreground-muted hover:text-foreground hover:bg-surface-hover",
                  )}
                >
                  {s.label}
                </button>
              ))}
            </div>
          )}
        </div>
      )}

      <div className="flex-1 overflow-y-auto rail-scroll py-1">
        {vaults.length === 0 ? (
          <p className="px-3 py-2 coord" aria-live="polite">
            {loading ? "Loading…" : "No vaults yet"}
          </p>
        ) : filtered.length === 0 ? (
          <div className="px-3 py-2">
            <p className="coord">
              {scope !== "all" ? "No vaults match this role filter" : "No matches"}
            </p>
            {scope !== "all" && (
              <button
                type="button"
                onClick={() => changeScope("all")}
                className="mt-1 rounded-[var(--radius-sm)] text-xs text-link hover:text-link-hover hover:underline focus:outline-none focus-visible:ring-2 focus-visible:ring-ring"
              >
                Show all roles
              </button>
            )}
          </div>
        ) : (
          <>
            {favs.length > 0 && (
              <section aria-label="Favorite vaults">
                <h3 className="px-3 pt-1 pb-0.5 coord">Favorites</h3>
                <ul>{favs.map(renderRow)}</ul>
              </section>
            )}
            {others.length > 0 && (
              <section aria-label="All vaults">
                {favs.length > 0 && (
                  <h3 className="mt-1 border-t border-border px-3 pt-2 pb-0.5 coord">All vaults</h3>
                )}
                <ul>{others.map(renderRow)}</ul>
              </section>
            )}
          </>
        )}
      </div>
    </nav>
  );
}
