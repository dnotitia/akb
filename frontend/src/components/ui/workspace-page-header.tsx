import type { ReactNode } from "react";
import type { LucideIcon } from "lucide-react";
import { cn } from "@/lib/utils";
import { TonalIcon, type TonalIconTone } from "@/components/ui/tonal-icon";

/**
 * Compact route identity for Vault workspaces. Unlike a marketing-style page
 * masthead, this keeps the H1, current resource, state, and actions in one row.
 * Overview can remove the default card surface so the identity sits above the
 * first data panel; operational workspaces may retain the bounded tool-row form.
 */
export function WorkspacePageHeader({
  icon: Icon,
  title,
  context,
  meta,
  actions,
  iconTone = "neutral",
  className,
}: {
  icon: LucideIcon;
  title: ReactNode;
  context?: ReactNode;
  meta?: ReactNode;
  actions?: ReactNode;
  iconTone?: TonalIconTone;
  className?: string;
}) {
  return (
    <header
      className={cn(
        "flex min-h-14 flex-wrap items-center justify-between gap-3 rounded-[var(--radius-md)] border border-border bg-surface px-3 py-2.5 shadow-xs",
        className,
      )}
    >
      <div className="flex min-w-0 items-center gap-3">
        <TonalIcon tone={iconTone}>
          <Icon className="h-4 w-4" />
        </TonalIcon>
        <div className="min-w-0">
          <div className="flex min-w-0 flex-wrap items-center gap-2">
            <h1 className="truncate font-display text-base font-semibold tracking-tight text-foreground">
              {title}
            </h1>
            {meta}
          </div>
          {context && (
            <div className="mt-0.5 min-w-0 text-xs leading-relaxed text-foreground-muted">
              {context}
            </div>
          )}
        </div>
      </div>
      {actions && (
        <div className="flex shrink-0 flex-wrap items-center justify-end gap-2">
          {actions}
        </div>
      )}
    </header>
  );
}
