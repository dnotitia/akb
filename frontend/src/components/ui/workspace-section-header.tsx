import type { LucideIcon } from "lucide-react";
import type { ReactNode } from "react";

import { TonalIcon, type TonalIconTone } from "@/components/ui/tonal-icon";
import { cn } from "@/lib/utils";

export function WorkspaceSectionHeader({
  id,
  icon: Icon,
  title,
  description,
  tone = "neutral",
  right,
  className,
  testId,
  compact = false,
}: {
  id: string;
  icon: LucideIcon;
  title: string;
  description?: ReactNode;
  tone?: TonalIconTone;
  right?: ReactNode;
  className?: string;
  testId?: string;
  compact?: boolean;
}) {
  return (
    <div
      data-slot="workspace-section-header"
      data-testid={testId}
      className={cn("border-b border-border px-0.5 pb-3", compact ? "mb-4" : "mb-2.5", className)}
    >
      <div className="flex flex-col gap-3 sm:flex-row sm:items-center sm:justify-between">
        <div
          className={cn(
            "flex min-w-0", compact ? "gap-2" : "gap-3",
            description ? "items-start" : "items-center",
          )}
        >
          <TonalIcon tone={tone} size={compact ? "sm" : "md"}>
            <Icon className="h-4 w-4" aria-hidden />
          </TonalIcon>
          <div className="min-w-0">
            <h2
              id={id}
              className={cn("font-display font-semibold tracking-tight text-foreground", compact ? "text-sm" : "text-base")}
            >
              {title}
            </h2>
            {description && (
              <p className="mt-0.5 text-xs leading-relaxed text-foreground-muted">
                {description}
              </p>
            )}
          </div>
        </div>

        {right && (
          <div className="flex shrink-0 flex-wrap items-center gap-2 sm:justify-end">
            {right}
          </div>
        )}
      </div>
    </div>
  );
}
