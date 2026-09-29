import * as DropdownMenu from "@radix-ui/react-dropdown-menu";
import { Box, Check, ChevronDown, Globe, LoaderCircle, Plus, Search, X } from "lucide-react";
import { useEffect, useRef, useState } from "react";
import { Input } from "@/components/ui/input";
import { TooltipText } from "@/components/ui/tooltip-text";
import { cn } from "@/lib/utils";

interface SearchVaultPickerProps {
  selected: string[];
  contextVault?: string;
  vaults: string[] | null;
  error: boolean;
  onRetry: () => void;
  onChange: (vaults: string[]) => void;
}

/** A search scope, not a workspace switch: selecting never navigates away. */
export function SearchVaultPicker({ selected, contextVault, vaults, error, onRetry, onChange }: SearchVaultPickerProps) {
  const [open, setOpen] = useState(false);
  const [filter, setFilter] = useState("");
  const inputRef = useRef<HTMLInputElement>(null);
  const contentRef = useRef<HTMLDivElement>(null);
  const triggerRef = useRef<HTMLButtonElement>(null);
  const label = selected.length ? selected.join(", ") : "All vaults";
  const needle = filter.trim().toLocaleLowerCase();
  // Scope names are already explicit user choices. Keep every selected name
  // removable even while directory refresh fails or an entry becomes stale.
  const matches = [...new Set([...selected, ...(vaults || [])])]
    .filter(name => name.toLocaleLowerCase().includes(needle));
  const ordered = [...matches].sort((a, b) =>
    Number(selected.includes(b)) - Number(selected.includes(a)) ||
    Number(b === contextVault) - Number(a === contextVault) || a.localeCompare(b),
  );
  const visibleSelected = selected.slice(0, 2);
  const overflowCount = selected.length - visibleSelected.length;

  useEffect(() => {
    if (!open) return;
    const frame = requestAnimationFrame(() => inputRef.current?.focus());
    return () => cancelAnimationFrame(frame);
  }, [open]);

  const itemClass = "relative flex min-h-11 cursor-pointer select-none items-center gap-2 rounded-[var(--radius-sm)] px-2 py-2 text-sm outline-none transition-token data-[highlighted]:bg-surface-hover data-[state=checked]:text-link sm:min-h-9";

  function toggle(name: string) {
    onChange(selected.includes(name) ? selected.filter(item => item !== name) : [...selected, name]);
  }

  return <div className="flex w-full min-w-0 max-w-full items-center gap-1 @min-[30rem]/scope-query:w-auto @min-[30rem]/scope-query:max-w-[45%] @min-[30rem]/scope-query:shrink-0" role="group" aria-label="Vault search scope">
    {visibleSelected.map(name => <span key={name} className="inline-flex min-w-0 max-w-36 flex-1 items-center gap-1 rounded-[var(--radius-sm)] border border-border bg-surface-selected pl-1.5 text-xs font-medium text-surface-selected-foreground">
      <Box className="h-3 w-3 shrink-0" aria-hidden />
      <TooltipText className="min-w-0 truncate" tip={name}>{name}</TooltipText>
      <button type="button" aria-label={`Remove ${name} from search scope`}
        onClick={() => { toggle(name); triggerRef.current?.focus({ preventScroll: true }); }}
        className="inline-flex h-9 w-9 shrink-0 items-center justify-center rounded-[var(--radius-sm)] transition-token hover:bg-surface-hover focus:outline-none focus-visible:ring-2 focus-visible:ring-ring">
        <X className="h-3 w-3" aria-hidden />
      </button>
    </span>)}
    <DropdownMenu.Root open={open} onOpenChange={next => { setOpen(next); setFilter(""); }}>
    <DropdownMenu.Trigger
      type="button"
      ref={triggerRef}
      aria-label={`Search scope: ${label}`}
      title={selected.length ? `Change search scope (${selected.length} selected)` : "Choose vaults to search"}
      className="flex h-9 min-w-9 max-w-full shrink-0 items-center justify-center gap-1 rounded-[var(--radius-sm)] px-1.5 text-xs font-medium text-foreground-muted transition-token hover:bg-surface-hover hover:text-foreground focus:outline-none focus-visible:ring-2 focus-visible:ring-ring"
    >
      {!selected.length && <Globe className="h-3.5 w-3.5 shrink-0" aria-hidden />}
      {!selected.length ? <span>All vaults</span> : overflowCount > 0 ? <span className="tabular-nums">+{overflowCount}</span> : null}
      <ChevronDown className="h-3.5 w-3.5 shrink-0" aria-hidden />
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
                  const items = contentRef.current?.querySelectorAll<HTMLElement>('[role="menuitemcheckbox"]');
                  (event.key === "ArrowDown" ? items?.[0] : items?.[items.length - 1])?.focus();
                }
              }}
            />
          </div>
        </div>
        <div className="min-h-0 flex-1 overflow-y-auto overscroll-contain p-1">
            <DropdownMenu.Item onSelect={() => onChange([])} className={itemClass}>
              <Globe className="h-4 w-4 shrink-0 text-foreground-muted" aria-hidden />
              <span className="min-w-0 flex-1">All vaults</span>
              {!selected.length && <Check className="h-4 w-4 text-link" aria-hidden />}
            </DropdownMenu.Item>
            <DropdownMenu.Separator className="my-1 h-px bg-border" />
            {ordered.map(name => <DropdownMenu.CheckboxItem key={name} checked={selected.includes(name)}
              onCheckedChange={() => toggle(name)} onSelect={event => event.preventDefault()} className={itemClass}>
              <Box className="h-4 w-4 shrink-0 text-foreground-muted" aria-hidden />
              <span className="min-w-0 flex-1">
                <span className="block break-words">{name}</span>
                {name === contextVault && <span className="block text-xs text-foreground-muted">Current vault</span>}
              </span>
              <DropdownMenu.ItemIndicator><Check className="h-4 w-4" aria-hidden /></DropdownMenu.ItemIndicator>
            </DropdownMenu.CheckboxItem>)}
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
        <div className="flex shrink-0 items-center justify-between gap-2 border-t border-border px-3 py-1.5">
          <p className="text-xs text-foreground-muted">{selected.length ? `${selected.length} selected · Search any selected vault` : "All accessible vaults"}</p>
          <DropdownMenu.Item className={cn(itemClass, "text-link")}>Done</DropdownMenu.Item>
        </div>
      </DropdownMenu.Content>
    </DropdownMenu.Portal>
  </DropdownMenu.Root></div>;
}

/** Query text is only a suggestion: scope changes require an explicit choice. */
export function VaultQuerySuggestions({ query, selected, vaults, onSelect }: {
  query: string;
  selected: string[];
  vaults: string[] | null;
  onSelect: (name: string) => void;
}) {
  const needle = query.trim().toLocaleLowerCase();
  const matches = needle.length >= 2 ? (vaults || []).filter(name =>
    !selected.includes(name) && name.toLocaleLowerCase().includes(needle),
  ).slice(0, 5) : [];
  if (!matches.length) return null;
  return <div role="group" aria-label="Matching vaults" className="flex min-w-0 flex-wrap items-center gap-1.5 border-b border-border bg-surface px-3 py-2">
    <span className="mr-1 text-xs text-foreground-muted">Search in</span>
    {matches.map(name => <button key={name} type="button" aria-label={`Add ${name} to search scope`} onClick={() => onSelect(name)}
      className="inline-flex min-h-9 min-w-0 max-w-full items-center gap-1.5 rounded-[var(--radius-sm)] border border-border px-2.5 text-sm text-link transition-token hover:bg-surface-hover focus:outline-none focus-visible:ring-2 focus-visible:ring-ring">
      <Box className="h-3.5 w-3.5 shrink-0" aria-hidden />
      <span className="min-w-0 truncate" title={name}>{name}</span>
      <Plus className="h-3.5 w-3.5 shrink-0" aria-hidden />
    </button>)}
  </div>;
}
