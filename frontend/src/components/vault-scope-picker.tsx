import * as DropdownMenu from "@radix-ui/react-dropdown-menu";
import { Check, ChevronDown, X } from "lucide-react";
import { useMemo, useState } from "react";
import { cn } from "@/lib/utils";
import { MenuFilter } from "@/components/ui/menu-filter";
import { MENU_FILTER_THRESHOLD, filterByText } from "@/components/ui/menu-filter-utils";

interface VaultScopePickerProps {
  vaults: { name: string }[];
  /** Selected vault names. Empty = "all accessible vaults" (the default). */
  selected: string[];
  onChange: (next: string[]) => void;
  className?: string;
}

/**
 * Multi-vault search scope. A checkbox dropdown (same Radix DropdownMenu +
 * MenuFilter pattern as SelectMenu, but CheckboxItem instead of RadioItem so
 * the menu stays open across toggles) plus removable chips for the current
 * selection. Empty selection means "search every accessible vault", so the
 * trigger reads "All vaults (N)" and there are no chips — the calm default.
 */
export function VaultScopePicker({ vaults, selected, onChange, className }: VaultScopePickerProps) {
  const [filter, setFilter] = useState("");
  const names = useMemo(() => vaults.map((v) => v.name), [vaults]);
  const showFilter = names.length > MENU_FILTER_THRESHOLD;
  const filtered = useMemo(
    () => (showFilter ? filterByText(names, filter, (n) => n) : names),
    [names, filter, showFilter],
  );
  const selectedSet = new Set(selected);

  function toggle(name: string) {
    onChange(selectedSet.has(name) ? selected.filter((s) => s !== name) : [...selected, name]);
  }

  const triggerLabel =
    selected.length === 0
      ? `All vaults (${vaults.length})`
      : `${selected.length} vault${selected.length === 1 ? "" : "s"}`;

  return (
    <div className={cn("flex flex-wrap items-center gap-2", className)}>
      <DropdownMenu.Root>
        <DropdownMenu.Trigger
          aria-label={`Search scope: ${triggerLabel}`}
          className={cn(
            "inline-flex h-9 shrink-0 items-center gap-2 rounded-[var(--radius-md)] border border-border bg-surface px-3 text-sm text-foreground",
            "focus:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2 focus-visible:ring-offset-background",
            "data-[state=open]:border-border-strong cursor-pointer transition-colors duration-150",
          )}
        >
          <span className={cn(selected.length === 0 && "text-foreground-muted")}>{triggerLabel}</span>
          <ChevronDown className="h-4 w-4 shrink-0 text-foreground-muted" aria-hidden />
        </DropdownMenu.Trigger>
        <DropdownMenu.Portal>
          <DropdownMenu.Content
            align="start"
            sideOffset={6}
            onCloseAutoFocus={showFilter ? () => setFilter("") : undefined}
            className="z-[var(--z-popover)] flex max-h-[min(60vh,20rem)] min-w-[15rem] flex-col overflow-hidden rounded-[var(--radius-md)] border border-border bg-surface p-1 shadow-md"
          >
            {showFilter && (
              <div className="-mx-1 -mt-1 mb-0.5 shrink-0 border-b border-border bg-surface px-1 pt-1">
                <MenuFilter value={filter} onChange={setFilter} placeholder="Filter vaults" />
              </div>
            )}
            <div className="min-h-0 flex-1 overflow-y-auto">
              {filtered.length === 0 && (
                <div className="px-3 py-2 text-xs text-foreground-muted">No matches</div>
              )}
              {filtered.map((name) => (
                <DropdownMenu.CheckboxItem
                  key={name}
                  checked={selectedSet.has(name)}
                  onCheckedChange={() => toggle(name)}
                  onSelect={(e) => e.preventDefault()}
                  className={cn(
                    "relative flex cursor-pointer select-none items-center gap-2 rounded-[var(--radius-sm)] py-1.5 pl-7 pr-3 text-sm text-foreground outline-none",
                    "data-[highlighted]:bg-surface-hover data-[state=checked]:text-link",
                  )}
                >
                  <DropdownMenu.ItemIndicator className="absolute left-2 inline-flex">
                    <Check className="h-3.5 w-3.5" aria-hidden />
                  </DropdownMenu.ItemIndicator>
                  <span className="truncate">{name}</span>
                </DropdownMenu.CheckboxItem>
              ))}
            </div>
            {selected.length > 0 && (
              <div className="-mx-1 -mb-1 mt-0.5 flex shrink-0 items-center justify-between border-t border-border bg-surface px-2 py-1.5">
                <span className="text-xs text-foreground-muted tabular-nums">{selected.length} selected</span>
                <DropdownMenu.Item
                  onSelect={(e) => {
                    e.preventDefault();
                    onChange([]);
                  }}
                  className="cursor-pointer rounded-[var(--radius-sm)] px-2 py-1 text-xs text-foreground-muted outline-none data-[highlighted]:bg-surface-hover data-[highlighted]:text-link"
                >
                  Clear
                </DropdownMenu.Item>
              </div>
            )}
          </DropdownMenu.Content>
        </DropdownMenu.Portal>
      </DropdownMenu.Root>

      {selected.map((name) => (
        <span
          key={name}
          className="inline-flex items-center gap-1 rounded-full border border-border bg-surface-muted px-2 py-0.5 text-xs text-foreground"
        >
          <span className="max-w-[12rem] truncate">{name}</span>
          <button
            type="button"
            aria-label={`Remove ${name} from search scope`}
            onClick={() => toggle(name)}
            className="inline-flex shrink-0 items-center rounded-full text-foreground-muted transition-colors hover:text-destructive focus:outline-none focus-visible:ring-2 focus-visible:ring-ring cursor-pointer"
          >
            <X className="h-3 w-3" aria-hidden />
          </button>
        </span>
      ))}
    </div>
  );
}
