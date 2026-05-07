import { Link, useLocation } from "react-router-dom";
import { useEffect, useMemo, useState } from "react";
import {
  Box,
  Plus,
  Search as SearchIcon,
  Share2,
} from "lucide-react";
import { listVaults } from "@/lib/api";
import { cn } from "@/lib/utils";

interface VaultSummary {
  id: string;
  name: string;
  role?: string;
  is_pinned?: boolean;
}

/**
 * Left vault-level navigation column — col 1 of the 3-col workspace.
 * Shows: all-vaults picker (with filter) · this-vault page list · account.
 */
export function VaultNav({ current }: { current: string }) {
  const [vaults, setVaults] = useState<VaultSummary[]>([]);
  const [filter, setFilter] = useState("");
  const { pathname } = useLocation();

  // Refetch on every route change. VaultNav is mounted by vault-shell and
  // doesn't unmount across vault sub-routes, so a one-shot fetch on mount
  // would leave the picker stale after a vault delete (or any external
  // mutation). The list endpoint is a cheap PG query — refetching on
  // navigation is the simplest way to keep it honest.
  useEffect(() => {
    listVaults()
      .then((d) => setVaults((d.vaults as VaultSummary[]) || []))
      .catch(() => setVaults([]));
  }, [pathname]);

  const filtered = useMemo(() => {
    const q = filter.trim().toLowerCase();
    if (!q) return vaults;
    return vaults.filter((v) => v.name?.toLowerCase().includes(q));
  }, [vaults, filter]);

  return (
    <aside
      className="flex flex-col h-full overflow-hidden border-r border-border bg-surface"
      aria-label="Vault navigation"
    >
      {/* VAULTS header + filter */}
      <div className="px-2 pt-3 pb-1 shrink-0">
        <div className="px-2 pb-1.5 flex items-center justify-between">
          <span className="coord">VAULTS</span>
          <Link
            to="/vault/new"
            aria-label="New vault"
            className="text-foreground-muted hover:text-accent transition-colors cursor-pointer focus:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2 focus-visible:ring-offset-surface"
          >
            <Plus className="h-3 w-3" aria-hidden />
          </Link>
        </div>
        <div className="relative mb-1">
          <SearchIcon
            className="absolute left-2 top-1/2 -translate-y-1/2 h-3 w-3 text-foreground-muted pointer-events-none"
            aria-hidden
          />
          <input
            type="search"
            value={filter}
            onChange={(e) => setFilter(e.target.value)}
            placeholder="Filter"
            aria-label="Filter vaults"
            className="w-full h-9 pl-6 pr-2 bg-background border border-border text-[11px] text-foreground placeholder:text-foreground-muted focus:outline-none focus:border-accent transition-colors"
          />
        </div>
      </div>

      {/* VAULTS list (scrollable) */}
      <div className="px-2 pb-2 overflow-y-auto flex-1 min-h-0">
        <div className="flex flex-col gap-px">
          {filtered.length === 0 && filter && (
            <div className="px-2 py-2 coord">— NO MATCHES —</div>
          )}
          {filtered.map((v) => (
            <NavItem
              key={v.id}
              to={`/vault/${v.name}`}
              label={v.name}
              icon={Box}
              active={v.name === current}
            />
          ))}
        </div>
      </div>

    </aside>
  );
}

function NavItem({
  to,
  label,
  icon: Icon,
  active,
  accent,
}: {
  to: string;
  label: string;
  icon: React.ComponentType<{ className?: string; "aria-hidden"?: boolean }>;
  active?: boolean;
  accent?: boolean;
}) {
  return (
    <Link
      to={to}
      aria-current={active ? "page" : undefined}
      className={cn(
        "flex items-center gap-2 px-2 h-7 text-sm transition-colors",
        "focus:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2 focus-visible:ring-offset-surface",
        active && accent && "bg-accent/10 text-accent",
        active && !accent && "bg-surface-muted text-foreground border-l-2 border-accent -ml-[2px]",
        !active && "text-foreground-muted hover:text-foreground hover:bg-surface-muted",
      )}
    >
      <Icon className="h-3.5 w-3.5 shrink-0" aria-hidden />
      <span className="truncate">{label}</span>
    </Link>
  );
}

export { Share2 };
