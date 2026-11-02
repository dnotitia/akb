import type { ReactNode } from "react";
import type { LucideIcon } from "lucide-react";
import { cn } from "@/lib/utils";
import { TonalIcon, type TonalIconTone } from "@/components/ui/tonal-icon";

/**
 * Compact route identity for Vault workspaces. Unlike a marketing-style page
 * masthead, this keeps the H1, current resource, state, and actions in one row.
 * Page identities share the Vault title and icon scale; embedded tools can
 * retain the bounded tool-row form.
 */
export function WorkspacePageHeader({
  icon: Icon,
  title,
  context,
  meta,
  actions,
  iconTone = "neutral",
  variant = "tool",
  titleId,
  testId,
  className,
}: {
  icon: LucideIcon;
  title: ReactNode;
  context?: ReactNode;
  meta?: ReactNode;
  actions?: ReactNode;
  iconTone?: TonalIconTone;
  variant?: "tool" | "page";
  titleId?: string;
  testId?: string;
  className?: string;
}) {
  return (
    <header
      data-slot="workspace-page-header"
      data-testid={testId}
      className={cn(
        "flex min-w-0 flex-wrap items-center justify-between gap-3",
        variant === "page"
          ? "mb-4 border-b border-border pb-3"
          : "min-h-14 rounded-[var(--radius-md)] border border-border bg-surface px-3 py-2.5 shadow-xs",
        className,
      )}
    >
      <div className="flex min-w-0 items-center gap-2.5">
        {variant === "page" ? (
          <span className="workspace-route-mark"><Icon aria-hidden /></span>
        ) : (
          <TonalIcon tone={iconTone}>
            <Icon className="h-4 w-4" aria-hidden />
          </TonalIcon>
        )}
        <div className="min-w-0">
          <div className="flex min-w-0 flex-wrap items-center gap-2">
            <h1
              id={titleId}
              className={cn(
                "min-w-0 font-display text-foreground",
                variant === "page"
                  ? "workspace-route-title break-words [overflow-wrap:anywhere]"
                  : "truncate text-base font-semibold tracking-tight",
              )}
            >
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
