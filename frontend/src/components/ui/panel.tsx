import { forwardRef, type HTMLAttributes, type ReactNode } from "react";
import { cn } from "@/lib/utils";
import { Eyebrow } from "@/components/ui/eyebrow";

/**
 * Design-system primitive: the standard panel surface used across the app
 * (cards, sections, lists). `card` is the soft family default; `workspace`
 * tightens the radius and elevation for dense file/admin surfaces.
 *
 * `inset` (default true) clips children to the rounded corner (use for lists
 * with divided rows). Set `flush` to drop the shadow (nested panels).
 */
export const Panel = forwardRef<
  HTMLDivElement,
  HTMLAttributes<HTMLDivElement> & {
    inset?: boolean;
    flush?: boolean;
    variant?: "card" | "workspace";
  }
>(({ className, inset = true, flush = false, variant = "card", ...props }, ref) => (
  <div
    ref={ref}
    className={cn(
      "border border-border bg-surface",
      variant === "workspace"
        ? "rounded-[var(--radius-md)]"
        : "rounded-[var(--radius-lg)]",
      !flush && (variant === "workspace" ? "shadow-xs" : "shadow-sm"),
      inset && "overflow-hidden",
      className,
    )}
    {...props}
  />
));
Panel.displayName = "Panel";

/**
 * Panel header row: an Eyebrow label (§ SECTION) + optional count + right slot.
 * Its variant should match the containing Panel.
 */
export function PanelHeader({
  label,
  count,
  right,
  className,
  variant = "card",
}: {
  label: ReactNode;
  count?: number;
  right?: ReactNode;
  className?: string;
  variant?: "card" | "workspace";
}) {
  return (
    <div
      data-slot="panel-header"
      className={cn(
        "flex items-center justify-between gap-2 border-b border-border",
        variant === "workspace"
          ? "min-h-10 bg-surface-2/60 px-3 py-2"
          : "px-4 py-2.5",
        className,
      )}
    >
      <div className="flex items-baseline gap-2">
        <Eyebrow tone="ink">{label}</Eyebrow>
        {count !== undefined && <Eyebrow className="tabular-nums">[{count}]</Eyebrow>}
      </div>
      {right}
    </div>
  );
}
