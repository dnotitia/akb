import { Link, useLocation } from "react-router-dom";
import { useEffect, useMemo, useState } from "react";
import { Box, ChevronsLeft, ChevronsRight, Plus, RefreshCw, Search as SearchIcon } from "lucide-react";
import { useVaults } from "@/hooks/use-vaults";
import { VaultChip } from "@/components/ui/vault-chip";
import {
  Tooltip,
  TooltipContent,
  TooltipProvider,
  TooltipTrigger,
} from "@/components/ui/tooltip";
import { cn } from "@/lib/utils";

/**
 * The vault column — an always-visible list of vaults in its OWN column / scroll
 * axis, separate from the collection tree to its right (the structural fix for
 * the old stacked, two-scroll sidebar). It has two modes the user toggles:
 *
 *   • expanded (w-44): monogram + NAME + a filter — identifiable at a glance;
 *   • collapsed (w-14): an icon RAIL of monograms (names on hover/aria) — when
 *     the user wants the space back, the list simplifies to a thin rail.
 *
 * Either way the switcher stays always-visible (incl. /graph + while the tree is
 * collapsed), so vault-switching never disappears.
 */
export function VaultRail({
  current,
  onRefetchReady,
  collapsed,
  onToggleCollapsed,
}: {
  current: string;
  onRefetchReady?: (refetch: () => void) => void;
  collapsed: boolean;
  onToggleCollapsed: () => void;
}) {
  const { vaults, loading, refetch } = useVaults();
  const { pathname } = useLocation();
  const [filter, setFilter] = useState("");

  useEffect(() => {
    refetch();
  }, [pathname, refetch]);

  useEffect(() => {
    onRefetchReady?.(refetch);
  }, [onRefetchReady, refetch]);

  const q = filter.trim().toLowerCase();
  const filtered = useMemo(
    () => (q ? vaults.filter((v) => v.name?.toLowerCase().includes(q)) : vaults),
    [vaults, q],
  );

  const headBtn =
    "text-foreground-muted hover:text-link transition-colors cursor-pointer focus:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2 focus-visible:ring-offset-surface disabled:cursor-default disabled:opacity-50";
  const railBtn =
    "flex h-9 w-9 items-center justify-center rounded-[var(--radius-md)] text-foreground-muted hover:text-foreground hover:bg-surface-hover transition-token focus:outline-none focus-visible:ring-2 focus-visible:ring-ring cursor-pointer disabled:cursor-default disabled:opacity-50";

  // ── Collapsed: a thin icon rail ─────────────────────────────────────
  if (collapsed) {
    return (
      <TooltipProvider delayDuration={250}>
        <nav
          aria-label="Vaults"
          className="w-14 shrink-0 h-full flex flex-col bg-surface border-r border-border"
        >
          {/* Controls pinned to the TOP in both modes (expanded header / this
              rail head) so the collapse/expand toggle never jumps to the foot. */}
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
            {vaults.length === 0 ? (
              <li className="coord py-2" aria-live="polite">
                {loading ? "…" : "—"}
              </li>
            ) : (
              vaults.map((v) => {
                const active = v.name === current;
                return (
                  <li key={v.id}>
                    <Tooltip>
                      <TooltipTrigger asChild>
                        <Link
                          to={`/vault/${v.name}`}
                          aria-label={v.name}
                          aria-current={active ? "page" : undefined}
                          className={cn(
                            "flex h-11 w-11 items-center justify-center rounded-[var(--radius-md)] transition-token",
                            "focus:outline-none focus-visible:ring-2 focus-visible:ring-ring",
                            active
                              ? "bg-surface-selected ring-2 ring-primary"
                              : "hover:bg-surface-hover",
                          )}
                        >
                          <VaultChip name={v.name} size="md" />
                        </Link>
                      </TooltipTrigger>
                      <TooltipContent side="right">{v.name}</TooltipContent>
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

  // ── Expanded: a named, filterable list ──────────────────────────────
  return (
    <nav
      aria-label="Vaults"
      className="w-44 shrink-0 h-full flex flex-col bg-surface border-r border-border"
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
        <div className="px-2 py-1.5 shrink-0 border-b border-border">
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
        </div>
      )}

      <ul className="flex-1 overflow-y-auto rail-scroll py-1">
        {vaults.length === 0 ? (
          <li className="px-3 py-2 coord" aria-live="polite">
            {loading ? "Loading…" : "No vaults yet"}
          </li>
        ) : filtered.length === 0 ? (
          <li className="px-3 py-2 coord">No matches</li>
        ) : (
          filtered.map((v) => {
            const active = v.name === current;
            return (
              <li key={v.id}>
                <Link
                  to={`/vault/${v.name}`}
                  aria-current={active ? "page" : undefined}
                  className={cn(
                    "flex items-center gap-2 px-3 h-9 text-sm transition-colors",
                    "focus:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2 focus-visible:ring-offset-surface",
                    active
                      ? "bg-surface-selected text-surface-selected-foreground border-l-2 border-primary -ml-[2px]"
                      : "text-foreground-muted hover:text-foreground hover:bg-surface-hover",
                  )}
                >
                  <Box className="h-3.5 w-3.5 shrink-0" aria-hidden />
                  <span title={v.name} className="font-mono truncate">
                    {v.name}
                  </span>
                </Link>
              </li>
            );
          })
        )}
      </ul>
    </nav>
  );
}
