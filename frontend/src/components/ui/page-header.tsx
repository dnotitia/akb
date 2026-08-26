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
  compact = false,
  className,
}: {
  title: ReactNode;
  subtitle?: ReactNode;
  eyebrow?: ReactNode;
  actions?: ReactNode;
  size?: "md" | "lg";
  compact?: boolean;
  className?: string;
}) {
  return (
    <div
      className={cn(
        "flex flex-wrap items-start justify-between",
        compact ? "mb-6 gap-3" : "mb-8 gap-4",
        className,
      )}
    >
      <div className="flex min-w-0 flex-col">
        {eyebrow && (
          <Eyebrow tone={compact ? "ink" : "spark"} className={compact ? "mb-1.5 block" : "mb-2 block"}>
            {eyebrow}
          </Eyebrow>
        )}
        <h1
          className={cn(
            "font-display font-semibold tracking-tight text-foreground",
            compact
              ? size === "lg"
                ? "text-2xl"
                : "text-xl"
              : size === "lg"
                ? "text-3xl sm:text-[34px]"
                : "text-2xl",
          )}
        >
          {title}
        </h1>
        {subtitle && (
          <p
            className={cn(
              "text-sm text-foreground-muted",
              compact ? "mt-1 max-w-3xl leading-relaxed" : "mt-1.5 max-w-2xl",
            )}
          >
            {subtitle}
          </p>
        )}
      </div>
      {actions && <div className="flex shrink-0 flex-wrap items-center justify-end gap-2">{actions}</div>}
    </div>
  );
}
