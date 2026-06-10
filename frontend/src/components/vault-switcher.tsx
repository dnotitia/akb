import { useEffect } from "react";
import { Link } from "react-router-dom";
import * as DropdownMenu from "@radix-ui/react-dropdown-menu";
import { ArrowRight, Check, ChevronDown, Plus } from "lucide-react";
import { useVaults } from "@/hooks/use-vaults";
import { VaultChip } from "@/components/ui/vault-chip";
import { cn } from "@/lib/utils";

/**
 * The vault switcher — the single place to jump between vaults, lifted out of
 * the sidebar into the title bar (Model A). The current vault's monogram + name
 * is the trigger; opening it reveals all vaults, "New vault", and a link to the
 * full directory. Reachable from every vault sub-route including /graph, where
 * the sidebar is unmounted.
 *
 * Built on the already-installed Radix DropdownMenu (no new dependency). Quick
 * filtering is the menu's built-in typeahead (each item's `textValue` is the
 * vault name); large vault counts fall back to "All vaults" → the directory,
 * which has a real filter field.
 */
export function VaultSwitcher({
  current,
  onRefetchReady,
}: {
  /** The open vault. Absent on /vault (no vault selected) → "Select a vault". */
  current?: string;
  onRefetchReady?: (refetch: () => void) => void;
}) {
  const { vaults, loading, refetch } = useVaults();

  // Publish refetch upward so the shell's VaultRefreshProvider can drive it
  // after a vault mutation (create/delete) — the role VaultNav used to fill.
  useEffect(() => {
    onRefetchReady?.(refetch);
  }, [onRefetchReady, refetch]);

  const itemClass =
    "flex items-center gap-2 px-2 py-1.5 rounded-[var(--radius-sm)] text-sm outline-none cursor-pointer data-[highlighted]:bg-surface-hover";

  return (
    <DropdownMenu.Root onOpenChange={(open) => open && refetch()}>
      <DropdownMenu.Trigger asChild>
        <button
          type="button"
          aria-label={current ? `Current vault: ${current}. Switch vault` : "Select a vault"}
          className="inline-flex items-center gap-1.5 max-w-[240px] h-7 px-1.5 rounded-[var(--radius-md)] text-foreground hover:bg-surface-hover transition-token focus:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2 focus-visible:ring-offset-surface cursor-pointer"
        >
          {current ? (
            <>
              <VaultChip name={current} size="sm" />
              <span className="font-mono text-[13px] font-medium truncate" title={current}>
                {current}
              </span>
            </>
          ) : (
            <span className="text-[13px] font-medium text-foreground-muted">Select a vault</span>
          )}
          <ChevronDown className="h-3.5 w-3.5 shrink-0 text-foreground-muted" aria-hidden />
        </button>
      </DropdownMenu.Trigger>
      <DropdownMenu.Portal>
        <DropdownMenu.Content
          align="start"
          sideOffset={6}
          className="z-[var(--z-popover)] min-w-[240px] max-w-[340px] rounded-[var(--radius-md)] border border-border bg-surface p-1 shadow-md"
        >
          <div className="max-h-[60vh] overflow-y-auto">
            {vaults.length === 0 ? (
              <div className="px-3 py-2 coord">No vaults yet</div>
            ) : (
              vaults.map((v) => {
                const active = v.name === current;
                return (
                  <DropdownMenu.Item key={v.id} textValue={v.name} asChild>
                    <Link
                      to={`/vault/${v.name}`}
                      aria-current={active ? "page" : undefined}
                      className={cn(
                        itemClass,
                        active && "bg-surface-selected text-surface-selected-foreground",
                      )}
                    >
                      <VaultChip name={v.name} size="sm" />
                      <span className="font-mono truncate flex-1 min-w-0" title={v.name}>
                        {v.name}
                      </span>
                      {active && <Check className="h-3.5 w-3.5 shrink-0 text-primary" aria-hidden />}
                    </Link>
                  </DropdownMenu.Item>
                );
              })
            )}
          </div>

          <DropdownMenu.Separator className="my-1 h-px bg-border" />

          <DropdownMenu.Item textValue="New vault" asChild>
            <Link to="/vault/new" className={cn(itemClass, "text-foreground")}>
              <Plus className="h-3.5 w-3.5 shrink-0 text-foreground-muted" aria-hidden />
              New vault
            </Link>
          </DropdownMenu.Item>
          <DropdownMenu.Item textValue="All vaults" asChild>
            <Link to="/vault" className={cn(itemClass, "text-foreground-muted")}>
              <ArrowRight className="h-3.5 w-3.5 shrink-0" aria-hidden />
              All vaults
              {!loading && vaults.length > 0 && (
                <span className="ml-auto coord tabular-nums">{vaults.length}</span>
              )}
            </Link>
          </DropdownMenu.Item>
        </DropdownMenu.Content>
      </DropdownMenu.Portal>
    </DropdownMenu.Root>
  );
}
