import type { ReactNode, Ref } from "react";
import { Filter, PanelLeftClose, PanelLeftOpen, Search, X } from "lucide-react";
import { cn } from "@/lib/utils";

export function RailIdentity({ children, slot, workspace = false }: { children: ReactNode; slot?: string; workspace?: boolean }) {
  return <div data-slot={slot} className={cn("flex shrink-0 items-center gap-1 border-b border-border px-2", workspace ? "h-14" : "h-10 lg:h-14")}>{children}</div>;
}

/** Search has its own full-width row below the list's management actions. */
export function RailSearchToolbar({ search }: { search: ReactNode }) {
  return <div data-slot="sidebar-search-toolbar" className="shrink-0 px-2 py-2">{search}</div>;
}

export function RailManagement({ label, children, leading, slot, workspace = false }: { label: string; children: ReactNode; leading?: ReactNode; slot: string; workspace?: boolean }) {
  return <div data-slot={slot} className={cn("flex shrink-0 items-center justify-between border-b border-border", workspace ? "h-10 px-2 max-sm:h-12 pointer-coarse:h-12" : "h-10 px-3")}>
    <div className="flex min-w-0 items-center gap-0.5">{leading}<span className={cn("whitespace-nowrap font-medium text-foreground", workspace ? "px-1 text-ui" : "text-xs")}>{label}</span></div>
    <div className={cn("flex items-center", workspace ? "gap-1" : "gap-0.5")}>{children}</div>
  </div>;
}

export function RailCollapseButton({ collapsed, label, onClick }: { collapsed: boolean; label: string; onClick: () => void }) {
  const Icon = collapsed ? PanelLeftOpen : PanelLeftClose;
  return <button type="button" title={label} aria-label={label} aria-expanded={!collapsed} onClick={onClick}
    className="flex h-8 w-8 shrink-0 items-center justify-center rounded-[var(--radius-sm)] text-foreground-muted transition-token hover:bg-surface-hover hover:text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-inset">
    <Icon className="h-4 w-4" aria-hidden />
  </button>;
}

export function RailFilterToggle({ label, open, count, controls, onClick, compact = false }: { label: string; open: boolean; count: number; controls: string; onClick: () => void; compact?: boolean }) {
  return <button type="button" title={label} aria-label={count ? `${label}, ${count} active` : label} aria-expanded={open} aria-controls={controls} onClick={onClick}
    className={cn("relative flex shrink-0 items-center justify-center rounded-[var(--radius-sm)] transition-token hover:bg-surface-hover hover:text-link focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-inset", compact ? "h-9 w-9 max-sm:h-11 max-sm:w-11 pointer-coarse:h-11 pointer-coarse:w-11" : "h-8 w-8", count ? "text-link" : "text-foreground-muted")}>
    <Filter className="h-3.5 w-3.5" aria-hidden />
    {count > 0 && <span aria-hidden className={cn("absolute rounded-full bg-surface-selected px-1 text-xs tabular-nums text-surface-selected-foreground", compact ? "right-0 top-0" : "-right-0.5 -top-0.5")}>{count}</span>}
  </button>;
}

export function RailFilterField({ label, value, onChange, compact = false, inputRef, placeholder = label }: { label: string; value: string; onChange: (value: string) => void; compact?: boolean; inputRef?: Ref<HTMLInputElement>; placeholder?: string }) {
  if (compact) return <div data-slot="sidebar-search-field" className="min-w-0 w-full">
    <div data-slot="sidebar-search-input" className="flex min-w-0 flex-1 items-center rounded-[var(--radius-md)] bg-background ring-1 ring-inset ring-border focus-within:ring-2 focus-within:ring-ring">
      <Search className="ml-2.5 h-3.5 w-3.5 shrink-0 text-foreground-muted" aria-hidden />
      <input ref={inputRef} type="search" aria-label={label} placeholder={placeholder} value={value} onChange={event => onChange(event.target.value)}
        className="block h-9 min-w-0 flex-1 rounded-[var(--radius-md)] border-0 bg-transparent pl-2 pr-1 text-ui text-foreground placeholder:text-foreground-muted focus:outline-none max-sm:h-11 max-sm:text-base pointer-coarse:h-11 pointer-coarse:text-base [&::-webkit-search-cancel-button]:appearance-none" />
      {value && <button type="button" aria-label={`Clear ${label.toLowerCase()}`} onClick={event => { onChange(""); event.currentTarget.parentElement?.querySelector("input")?.focus(); }} className="flex h-9 w-9 shrink-0 items-center justify-center rounded-[var(--radius-sm)] text-foreground-muted hover:text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-inset focus-visible:ring-ring max-sm:h-11 max-sm:w-11 pointer-coarse:h-11 pointer-coarse:w-11"><X className="h-3.5 w-3.5" aria-hidden /></button>}
    </div>
  </div>;
  return <div className="relative min-w-0 w-full">
    <Search className="pointer-events-none absolute left-2 top-1/2 h-3.5 w-3.5 -translate-y-1/2 text-foreground-muted" aria-hidden />
    <input ref={inputRef} type="search" aria-label={label} placeholder={placeholder} value={value} onChange={event => onChange(event.target.value)}
      className="block h-8 w-full rounded-[var(--radius-md)] border border-border bg-background pl-7 pr-8 text-xs text-foreground placeholder:text-foreground-muted focus:border-primary focus:outline-none focus-visible:ring-2 focus-visible:ring-ring [&::-webkit-search-cancel-button]:appearance-none" />
    {value && <button type="button" aria-label={`Clear ${label.toLowerCase()}`} onClick={event => { onChange(""); event.currentTarget.parentElement?.querySelector("input")?.focus(); }} className="absolute right-0 top-0 flex h-8 w-8 items-center justify-center rounded-[var(--radius-sm)] text-foreground-muted hover:text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-inset"><X className="h-3.5 w-3.5" aria-hidden /></button>}
  </div>;
}
