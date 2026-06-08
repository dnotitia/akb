import { forwardRef, type HTMLAttributes, type ReactNode } from "react";
import { cn } from "@/lib/utils";
import { Eyebrow } from "@/components/ui/eyebrow";

/**
 * Design-system primitive: the standard rounded "panel" surface used across
 * the app (cards, sections, lists). One source of truth for the family
 * container look — `rounded-[var(--radius-lg)] border bg-surface shadow-sm`.
 *
 * `inset` (default true) clips children to the rounded corner (use for lists
 * with divided rows). Set `flush` to drop the shadow (nested panels).
 */
export const Panel = forwardRef<
  HTMLDivElement,
  HTMLAttributes<HTMLDivElement> & { inset?: boolean; flush?: boolean }
>(({ className, inset = true, flush = false, ...props }, ref) => (
  <div
    ref={ref}
    className={cn(
      "rounded-[var(--radius-lg)] border border-border bg-surface",
      !flush && "shadow-sm",
      inset && "overflow-hidden",
      className,
    )}
    {...props}
  />
));
Panel.displayName = "Panel";

/**
 * Panel header row: an Eyebrow label (§ SECTION) + optional count + right slot.
 */
export function PanelHeader({
  label,
  count,
  right,
  className,
}: {
  label: ReactNode;
  count?: number;
  right?: ReactNode;
  className?: string;
}) {
  return (
    <div className={cn("flex items-center justify-between gap-2 px-4 py-2.5 border-b border-border", className)}>
      <div className="flex items-baseline gap-2">
        <Eyebrow tone="ink">{label}</Eyebrow>
        {count !== undefined && <Eyebrow className="tabular-nums">[{count}]</Eyebrow>}
      </div>
      {right}
    </div>
  );
}
