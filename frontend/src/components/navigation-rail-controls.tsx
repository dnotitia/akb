import type { ReactNode } from "react";
import { Filter, PanelLeftClose, PanelLeftOpen, Search, X } from "lucide-react";
import { cn } from "@/lib/utils";

export function RailIdentity({ children, slot }: { children: ReactNode; slot?: string }) {
  return <div data-slot={slot} className="flex h-10 shrink-0 items-center gap-1 border-b border-border px-2 lg:h-14">{children}</div>;
}

export function RailManagement({ label, children, slot }: { label: string; children: ReactNode; slot: string }) {
  return <div data-slot={slot} className="flex h-10 shrink-0 items-center justify-between border-b border-border px-3">
    <span className="text-xs font-medium text-foreground">{label}</span>
    <div className="flex items-center gap-0.5">{children}</div>
  </div>;
}

export function RailCollapseButton({ collapsed, label, onClick }: { collapsed: boolean; label: string; onClick: () => void }) {
  const Icon = collapsed ? PanelLeftOpen : PanelLeftClose;
  return <button type="button" title={label} aria-label={label} aria-expanded={!collapsed} onClick={onClick}
    className="flex h-8 w-8 shrink-0 items-center justify-center rounded-[var(--radius-sm)] text-foreground-muted transition-token hover:bg-surface-hover hover:text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-inset">
    <Icon className="h-4 w-4" aria-hidden />
  </button>;
}

export function RailFilterToggle({ label, open, count, controls, onClick }: { label: string; open: boolean; count: number; controls: string; onClick: () => void }) {
  return <button type="button" title={label} aria-label={count ? `${label}, ${count} active` : label} aria-expanded={open} aria-controls={controls} onClick={onClick}
    className={cn("relative flex h-8 w-8 shrink-0 items-center justify-center rounded-[var(--radius-sm)] transition-token hover:bg-surface-hover focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-inset", count ? "text-link" : "text-foreground-muted")}>
    <Filter className="h-3.5 w-3.5" aria-hidden />
    {count > 0 && <span aria-hidden className="absolute -right-0.5 -top-0.5 rounded-full bg-surface-selected px-1 text-xs tabular-nums text-surface-selected-foreground">{count}</span>}
  </button>;
}

export function RailFilterField({ label, value, onChange }: { label: string; value: string; onChange: (value: string) => void }) {
  return <div className="relative min-w-0 w-full">
    <Search className="pointer-events-none absolute left-2 top-1/2 h-3.5 w-3.5 -translate-y-1/2 text-foreground-muted" aria-hidden />
    <input type="search" aria-label={label} placeholder={label} value={value} onChange={event => onChange(event.target.value)}
      className="block h-8 w-full rounded-[var(--radius-md)] border border-border bg-background pl-7 pr-8 text-xs text-foreground placeholder:text-foreground-muted focus:border-primary focus:outline-none focus-visible:ring-2 focus-visible:ring-ring [&::-webkit-search-cancel-button]:appearance-none" />
    {value && <button type="button" aria-label={`Clear ${label.toLowerCase()}`} onClick={event => { onChange(""); event.currentTarget.parentElement?.querySelector("input")?.focus(); }} className="absolute right-0 top-0 flex h-8 w-8 items-center justify-center rounded-[var(--radius-sm)] text-foreground-muted hover:text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-inset"><X className="h-3.5 w-3.5" aria-hidden /></button>}
  </div>;
}
