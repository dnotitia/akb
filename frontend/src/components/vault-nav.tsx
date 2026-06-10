import { Link, useLocation } from "react-router-dom";
import { useEffect } from "react";
import { Plus, RefreshCw } from "lucide-react";
import { useVaults } from "@/hooks/use-vaults";
import { VaultChip } from "@/components/ui/vault-chip";
import { cn } from "@/lib/utils";

/**
 * The vault list — the top section of the workspace sidebar. Always visible
 * (discoverable, unlike a title-bar dropdown), with the current vault
 * highlighted. Clicking a row switches vaults. Refetches on navigation so the
 * picker stays honest after an external mutation, and publishes its refetch
 * upward for the VaultRefreshProvider.
 */
export function VaultNav({
  current,
  onRefetchReady,
}: {
  current: string;
  onRefetchReady?: (refetch: () => void) => void;
}) {
  const { vaults, loading, refetch } = useVaults();
  const { pathname } = useLocation();

  useEffect(() => {
    refetch();
  }, [pathname, refetch]);

  useEffect(() => {
    onRefetchReady?.(refetch);
  }, [onRefetchReady, refetch]);

  return (
    <div className="flex flex-col">
      <div className="flex items-center justify-between h-9 px-3 shrink-0 border-b border-border">
        <span className="coord-ink">Vaults</span>
        <div className="flex items-center gap-2.5">
          <button
            type="button"
            onClick={refetch}
            disabled={loading}
            title="Refresh vaults"
            aria-label="Refresh vaults"
            className="text-foreground-muted hover:text-link transition-colors cursor-pointer focus:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2 focus-visible:ring-offset-surface disabled:cursor-default disabled:opacity-50"
          >
            <RefreshCw className={cn("h-3 w-3", loading && "animate-spin")} aria-hidden />
          </button>
          <Link
            to="/vault/new"
            aria-label="New vault"
            className="text-foreground-muted hover:text-link transition-colors cursor-pointer focus:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2 focus-visible:ring-offset-surface"
          >
            <Plus className="h-3.5 w-3.5" aria-hidden />
          </Link>
        </div>
      </div>

      <ul className="py-1">
        {vaults.length === 0 ? (
          <li className="px-3 py-2 coord">{loading ? "Loading…" : "No vaults yet"}</li>
        ) : (
          vaults.map((v) => {
            const active = v.name === current;
            return (
              <li key={v.id}>
                <Link
                  to={`/vault/${v.name}`}
                  aria-current={active ? "page" : undefined}
                  className={cn(
                    "flex items-center gap-2 px-3 h-8 text-sm transition-colors",
                    "focus:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2 focus-visible:ring-offset-surface",
                    active
                      ? "bg-surface-selected text-surface-selected-foreground border-l-2 border-primary -ml-[2px]"
                      : "text-foreground-muted hover:text-foreground hover:bg-surface-hover",
                  )}
                >
                  <VaultChip name={v.name} size="sm" />
                  <span title={v.name} className="font-mono truncate">
                    {v.name}
                  </span>
                </Link>
              </li>
            );
          })
        )}
      </ul>
    </div>
  );
}
