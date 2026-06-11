import { useRef, type ReactNode } from "react";
import { cn } from "@/lib/utils";

export interface SegmentedOption {
  value: string;
  label: string;
  /** Optional leading icon element (e.g. a Lucide <Icon className="h-3 w-3" />). */
  icon?: ReactNode;
  /** Escalated/destructive choice — the selected state tints amber (warning)
   *  instead of teal so it never reads as a neutral pick. */
  danger?: boolean;
}

/**
 * Design-system single-select segmented control — a proper WAI-ARIA radiogroup
 * (roving tabindex + arrow keys + aria-checked), NOT a row of independent
 * aria-pressed toggles. Pass grid-cols via `className` (e.g. "grid-cols-3" or
 * "grid-cols-1 sm:grid-cols-3" to stack on mobile).
 */
export function Segmented({
  value,
  onChange,
  options,
  disabled = false,
  className,
  "aria-labelledby": ariaLabelledBy,
  "aria-label": ariaLabel,
}: {
  value: string;
  onChange: (value: string) => void;
  options: SegmentedOption[];
  disabled?: boolean;
  className?: string;
  "aria-labelledby"?: string;
  "aria-label"?: string;
}) {
  const refs = useRef<Array<HTMLButtonElement | null>>([]);
  const order = options.map((o) => o.value);

  function onKeyDown(e: React.KeyboardEvent) {
    const dir =
      e.key === "ArrowRight" || e.key === "ArrowDown"
        ? 1
        : e.key === "ArrowLeft" || e.key === "ArrowUp"
          ? -1
          : 0;
    if (dir === 0 || disabled) return;
    e.preventDefault();
    const idx = order.indexOf(value);
    const next = (idx + dir + order.length) % order.length;
    onChange(order[next]);
    refs.current[next]?.focus();
  }

  return (
    <div
      role="radiogroup"
      aria-labelledby={ariaLabelledBy}
      aria-label={ariaLabel}
      onKeyDown={onKeyDown}
      className={cn(
        "grid gap-px rounded-[var(--radius-md)] overflow-hidden border border-border bg-border",
        className,
      )}
    >
      {options.map((o, i) => {
        const active = value === o.value;
        return (
          <button
            key={o.value}
            ref={(el) => {
              refs.current[i] = el;
            }}
            type="button"
            role="radio"
            aria-checked={active}
            tabIndex={active ? 0 : -1}
            onClick={() => !disabled && onChange(o.value)}
            disabled={disabled}
            className={cn(
              "flex min-h-[36px] items-center justify-center gap-1.5 px-3 py-2 text-sm transition-colors",
              "focus:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-inset",
              "disabled:opacity-50 disabled:cursor-not-allowed",
              active
                ? o.danger
                  ? "bg-warning-soft text-warning-soft-foreground"
                  : "bg-surface-selected text-surface-selected-foreground"
                : "bg-surface text-foreground hover:bg-surface-hover cursor-pointer",
            )}
          >
            {o.icon}
            {o.label}
          </button>
        );
      })}
    </div>
  );
}
