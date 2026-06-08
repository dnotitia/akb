import type { ReactNode } from "react";
import { cn } from "@/lib/utils";
import { Eyebrow } from "@/components/ui/eyebrow";

/**
 * Design-system primitive: the canonical page header. Centralizes the
 * family title treatment (Pretendard `font-display`) + muted subtitle +
 * right-aligned action slot, so every page renders its masthead identically.
 */
export function PageHeader({
  title,
  subtitle,
  eyebrow,
  actions,
  size = "lg",
  className,
}: {
  title: ReactNode;
  subtitle?: ReactNode;
  eyebrow?: ReactNode;
  actions?: ReactNode;
  size?: "md" | "lg";
  className?: string;
}) {
  return (
    <div className={cn("mb-8 flex items-start justify-between gap-4 flex-wrap", className)}>
      <div className="min-w-0">
        {eyebrow && <Eyebrow tone="spark" className="mb-2 block">{eyebrow}</Eyebrow>}
        <h1
          className={cn(
            "font-display text-foreground",
            size === "lg" ? "text-3xl sm:text-[34px]" : "text-2xl",
          )}
        >
          {title}
        </h1>
        {subtitle && (
          <p className="mt-1.5 text-sm text-foreground-muted max-w-2xl">{subtitle}</p>
        )}
      </div>
      {actions && <div className="flex items-center gap-2 shrink-0">{actions}</div>}
    </div>
  );
}
