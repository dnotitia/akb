import * as DropdownMenu from "@radix-ui/react-dropdown-menu";
import { Check, ChevronDown } from "lucide-react";
import { useMemo, useState } from "react";
import { cn } from "@/lib/utils";
import { TooltipText } from "@/components/ui/tooltip-text";
import { MenuFilter } from "@/components/ui/menu-filter";

export interface SelectOption {
  value: string;
  label: string;
  /** Optional secondary line (e.g. a technical id under a human label). */
  hint?: string;
  disabled?: boolean;
}

interface SelectMenuProps {
  value: string;
  onValueChange: (value: string) => void;
  options: SelectOption[];
  placeholder?: string;
  id?: string;
  className?: string;
  disabled?: boolean;
  "aria-label"?: string;
  "aria-invalid"?: boolean;
  "aria-describedby"?: string;
  /** Mono-format the trigger value (for identifier-shaped values). */
  mono?: boolean;
  /**
   * Show a live filter box atop the open list once the option count crosses
   * `searchThreshold` (default 8). Filters options by their label.
   */
  searchable?: boolean;
  searchThreshold?: number;
  searchPlaceholder?: string;
}

/**
 * Design-system Select: a fully themed dropdown built on Radix DropdownMenu
 * (RadioGroup semantics) so the open list matches the app — rounded surface,
 * tokens, light/dark — instead of the browser's native popup, which renders
 * unstyled and ignores the theme. Keeps the trigger labelable (id/aria-label)
 * and form-safe (Radix triggers are type="button", so they never submit).
 */
export function SelectMenu({
  value,
  onValueChange,
  options,
  placeholder = "Select…",
  id,
  className,
  disabled,
  mono,
  searchable = false,
  searchThreshold = 8,
  searchPlaceholder = "Filter…",
  "aria-label": ariaLabel,
  "aria-invalid": ariaInvalid,
  "aria-describedby": ariaDescribedby,
}: SelectMenuProps) {
  const current = options.find((o) => o.value === value);
  const [filter, setFilter] = useState("");
  const showFilter = searchable && options.length > searchThreshold;
  const q = filter.trim().toLowerCase();
  const filtered = useMemo(
    () => (showFilter && q ? options.filter((o) => o.label.toLowerCase().includes(q)) : options),
    [options, q, showFilter],
  );
  return (
    <DropdownMenu.Root>
      <DropdownMenu.Trigger
        id={id}
        disabled={disabled}
        aria-label={ariaLabel}
        aria-invalid={ariaInvalid || undefined}
        aria-describedby={ariaDescribedby}
        className={cn(
          "flex h-10 w-full items-center justify-between gap-2 rounded-[var(--radius-md)] border border-border bg-surface px-3 py-2 text-left text-sm text-foreground",
          "focus:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2 focus-visible:ring-offset-background",
          "data-[state=open]:border-border-strong",
          "aria-[invalid=true]:border-destructive aria-[invalid=true]:focus-visible:ring-destructive",
          "disabled:opacity-50 disabled:cursor-not-allowed",
          "cursor-pointer transition-colors duration-150",
          className,
        )}
      >
        <TooltipText
          className={cn(
            "truncate",
            mono && "font-mono",
            !current && "text-foreground-muted",
          )}
          tip={current?.label}
        >
          {current ? current.label : placeholder}
        </TooltipText>
        <ChevronDown className="h-4 w-4 shrink-0 text-foreground-muted" aria-hidden />
      </DropdownMenu.Trigger>
      <DropdownMenu.Portal>
        <DropdownMenu.Content
          align="start"
          sideOffset={6}
          onCloseAutoFocus={showFilter ? () => setFilter("") : undefined}
          className="z-[var(--z-popover)] max-h-[min(60vh,18rem)] min-w-[var(--radix-dropdown-menu-trigger-width)] overflow-y-auto rounded-[var(--radius-md)] border border-border bg-surface p-1 shadow-md"
        >
          {showFilter && (
            <div className="sticky -top-1 z-10 -mx-1 -mt-1 mb-0.5 border-b border-border bg-surface px-1 pt-1">
              <MenuFilter
                value={filter}
                onChange={setFilter}
                placeholder={searchPlaceholder}
              />
            </div>
          )}
          {showFilter && filtered.length === 0 && (
            <div className="px-3 py-2 text-xs text-foreground-muted">No matches</div>
          )}
          <DropdownMenu.RadioGroup value={value} onValueChange={onValueChange}>
            {filtered.map((o) => (
              <DropdownMenu.RadioItem
                key={o.value}
                value={o.value}
                disabled={o.disabled}
                className={cn(
                  "relative flex cursor-pointer select-none items-start gap-2 rounded-[var(--radius-sm)] py-1.5 pl-7 pr-3 text-sm text-foreground outline-none",
                  "data-[highlighted]:bg-surface-hover data-[state=checked]:text-link",
                  "data-[disabled]:cursor-not-allowed data-[disabled]:opacity-50",
                )}
              >
                <DropdownMenu.ItemIndicator className="absolute left-2 top-1.5 inline-flex">
                  <Check className="h-3.5 w-3.5" aria-hidden />
                </DropdownMenu.ItemIndicator>
                <span className="min-w-0">
                  <TooltipText
                    className={cn("block truncate", mono && "font-mono")}
                    tip={o.label}
                  >
                    {o.label}
                  </TooltipText>
                  {o.hint && (
                    <TooltipText
                      className="block truncate text-xs text-foreground-muted"
                      tip={o.hint}
                    >
                      {o.hint}
                    </TooltipText>
                  )}
                </span>
              </DropdownMenu.RadioItem>
            ))}
          </DropdownMenu.RadioGroup>
        </DropdownMenu.Content>
      </DropdownMenu.Portal>
    </DropdownMenu.Root>
  );
}
