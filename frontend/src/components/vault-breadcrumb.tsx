import type { ReactNode } from "react";
import { Link } from "react-router-dom";
import { Box, type LucideIcon } from "lucide-react";
import { TooltipText } from "@/components/ui/tooltip-text";
import { cn } from "@/lib/utils";

/** Shared location grammar for Vault pages and their resource readers. */
export function VaultBreadcrumb({
  vault, title, kind, icon: KindIcon, children, className, ariaLabel = "Current page",
}: {
  vault: string;
  title: string;
  kind?: string;
  icon?: LucideIcon;
  /** Optional Collection list items between the Vault and current location. */
  children?: ReactNode;
  className?: string;
  ariaLabel?: string;
}) {
  return (
    <nav aria-label={ariaLabel} className={cn("@container/resource-location min-w-0 text-sm", className)}>
      <ol className="flex min-w-0 items-center gap-1.5">
        <li className="flex min-w-0 max-w-[30%] items-center">
          <Link
            to={`/vault/${encodeURIComponent(vault)}`}
            title={vault}
            className="inline-flex min-w-0 items-center gap-1.5 rounded-[var(--radius-sm)] text-foreground-muted transition-token hover:text-link focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-inset"
          >
            <Box className="h-4 w-4 shrink-0" aria-hidden />
            <span className="truncate">{vault}</span>
          </Link>
        </li>
        {children}
        <li className="flex min-w-0 flex-1 items-center gap-1.5">
          <span aria-hidden className="shrink-0 text-subtle">/</span>
          <TooltipText
            aria-current="page"
            tabIndex={kind ? 0 : undefined}
            side="bottom"
            className="min-w-0 truncate rounded-[var(--radius-sm)] font-semibold text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-inset"
          >{title}</TooltipText>
          {KindIcon && <span title={kind} className="inline-flex shrink-0 text-foreground-muted">
            <KindIcon className="h-4 w-4" aria-hidden />
          </span>}
          {kind && <span className="sr-only">({kind})</span>}
        </li>
      </ol>
    </nav>
  );
}
