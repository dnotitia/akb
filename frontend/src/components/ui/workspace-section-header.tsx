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
}: {
  id: string;
  icon: LucideIcon;
  title: string;
  description?: ReactNode;
  tone?: TonalIconTone;
  right?: ReactNode;
  className?: string;
  testId?: string;
}) {
  return (
    <div
      data-slot="workspace-section-header"
      data-testid={testId}
      className={cn("mb-2.5 border-b border-border px-0.5 pb-3", className)}
    >
      <div className="flex flex-col gap-3 sm:flex-row sm:items-center sm:justify-between">
        <div
          className={cn(
            "flex min-w-0 gap-3",
            description ? "items-start" : "items-center",
          )}
        >
          <TonalIcon tone={tone}>
            <Icon className="h-4 w-4" aria-hidden />
          </TonalIcon>
          <div className="min-w-0">
            <h2
              id={id}
              className="font-display text-base font-semibold tracking-tight text-foreground"
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
