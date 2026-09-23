import * as DropdownMenu from "@radix-ui/react-dropdown-menu";
import { Box, Check, ChevronDown, Globe, LoaderCircle, Search } from "lucide-react";
import { useEffect, useRef, useState } from "react";
import { Input } from "@/components/ui/input";
import { TooltipText } from "@/components/ui/tooltip-text";
import { cn } from "@/lib/utils";

interface SearchVaultPickerProps {
  value?: string;
  contextVault?: string;
  vaults: string[] | null;
  error: boolean;
  onRetry: () => void;
  onChange: (vault?: string) => void;
}

/** A search scope, not a workspace switch: selecting never navigates away. */
export function SearchVaultPicker({ value, contextVault, vaults, error, onRetry, onChange }: SearchVaultPickerProps) {
  const [open, setOpen] = useState(false);
  const [filter, setFilter] = useState("");
  const inputRef = useRef<HTMLInputElement>(null);
  const contentRef = useRef<HTMLDivElement>(null);
  const label = value || "All vaults";
  const Icon = value ? Box : Globe;
  const needle = filter.trim().toLocaleLowerCase();
  const matches = (vaults || []).filter(name => name.toLocaleLowerCase().includes(needle));
  const ordered = [...matches].sort((a, b) =>
    Number(b === contextVault) - Number(a === contextVault) || a.localeCompare(b),
  );

  useEffect(() => {
    if (!open) return;
    const frame = requestAnimationFrame(() => inputRef.current?.focus());
    return () => cancelAnimationFrame(frame);
  }, [open]);

  const itemClass = "relative flex min-h-11 cursor-pointer select-none items-center gap-2 rounded-[var(--radius-sm)] px-2 py-2 text-sm outline-none transition-token data-[highlighted]:bg-surface-hover data-[state=checked]:text-link sm:min-h-9";

  return <DropdownMenu.Root open={open} onOpenChange={next => { setOpen(next); setFilter(""); }}>
    <DropdownMenu.Trigger
      aria-label={`Search scope: ${label}`}
      className="flex h-9 max-w-full shrink-0 items-center gap-1.5 rounded-[var(--radius-sm)] border border-border-strong bg-surface-selected px-2.5 text-sm font-medium text-surface-selected-foreground transition-token hover:border-primary focus:outline-none focus-visible:ring-2 focus-visible:ring-ring sm:max-w-64"
    >
      <Icon className="h-4 w-4 shrink-0" aria-hidden />
      <TooltipText className="min-w-0 truncate" tip={label}>{label}</TooltipText>
      <ChevronDown className="h-4 w-4 shrink-0" aria-hidden />
    </DropdownMenu.Trigger>
    <DropdownMenu.Portal>
      <DropdownMenu.Content ref={contentRef} align="start" sideOffset={6} collisionPadding={8}
        aria-label="Choose search scope"
        className="z-[var(--z-popover)] flex max-h-[min(24rem,var(--radix-dropdown-menu-content-available-height))] w-80 max-w-[calc(100vw-1rem)] flex-col overflow-hidden rounded-[var(--radius-md)] border border-border-strong bg-surface text-foreground shadow-md"
      >
        <div className="shrink-0 space-y-2 border-b border-border p-3">
          <DropdownMenu.Label className="text-sm font-semibold">Search scope</DropdownMenu.Label>
          <div className="relative">
            <Search className="pointer-events-none absolute left-2.5 top-1/2 h-4 w-4 -translate-y-1/2 text-foreground-muted" aria-hidden />
            <Input ref={inputRef} type="search" aria-label="Filter vaults" placeholder="Filter vaults…"
              value={filter} onChange={event => setFilter(event.target.value)} className="h-9 pl-8"
              onKeyDown={event => {
                if (event.key === "Escape") return;
                event.stopPropagation();
                if (event.nativeEvent.isComposing) return;
                if (event.key === "ArrowDown" || event.key === "ArrowUp") {
                  event.preventDefault();
                  const items = contentRef.current?.querySelectorAll<HTMLElement>('[role="menuitemradio"]');
                  (event.key === "ArrowDown" ? items?.[0] : items?.[items.length - 1])?.focus();
                }
              }}
            />
          </div>
        </div>
        <div className="min-h-0 flex-1 overflow-y-auto overscroll-contain p-1">
          <DropdownMenu.RadioGroup value={value || ""} onValueChange={next => onChange(next || undefined)}>
            <DropdownMenu.RadioItem value="" className={itemClass}>
              <Globe className="h-4 w-4 shrink-0 text-foreground-muted" aria-hidden />
              <span className="min-w-0 flex-1">All vaults</span>
              <DropdownMenu.ItemIndicator><Check className="h-4 w-4" aria-hidden /></DropdownMenu.ItemIndicator>
            </DropdownMenu.RadioItem>
            <DropdownMenu.Separator className="my-1 h-px bg-border" />
            {ordered.map(name => <DropdownMenu.RadioItem key={name} value={name} className={itemClass}>
              <Box className="h-4 w-4 shrink-0 text-foreground-muted" aria-hidden />
              <span className="min-w-0 flex-1">
                <TooltipText className="block truncate" tip={name}>{name}</TooltipText>
                {name === contextVault && <span className="block text-xs text-foreground-muted">Current vault</span>}
              </span>
              <DropdownMenu.ItemIndicator><Check className="h-4 w-4" aria-hidden /></DropdownMenu.ItemIndicator>
            </DropdownMenu.RadioItem>)}
          </DropdownMenu.RadioGroup>
          {error ? <div className="p-2">
            <p role="status" className="text-sm text-foreground-muted">Could not load vaults.</p>
            <DropdownMenu.Item onSelect={event => { event.preventDefault(); onRetry(); }}
              aria-label="Retry loading vaults" className={cn(itemClass, "mt-1 text-link")}>Retry</DropdownMenu.Item>
          </div> : vaults === null ? <p role="status" className="flex items-center gap-2 p-3 text-sm text-foreground-muted">
            <LoaderCircle className="h-4 w-4 animate-spin" aria-hidden />Loading vaults…
          </p> : ordered.length === 0 ? <p role="status" className="p-3 text-sm text-foreground-muted">
            {needle ? "No vaults match this filter." : "No accessible vaults."}
          </p> : null}
        </div>
        <p className="shrink-0 border-t border-border px-3 py-2 text-xs text-foreground-muted">Only vaults you can access</p>
      </DropdownMenu.Content>
    </DropdownMenu.Portal>
  </DropdownMenu.Root>;
}
