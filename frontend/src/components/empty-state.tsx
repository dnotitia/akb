import type { ReactNode } from "react";
import { cn } from "@/lib/utils";

interface EmptyStateProps {
  title: string;
  description?: string;
  action?: ReactNode;
  icon?: ReactNode;
  className?: string;
}

export function EmptyState({
  title,
  description,
  action,
  icon,
  className,
}: EmptyStateProps) {
  return (
    <div
      className={cn(
        "flex flex-col items-center justify-center py-12 px-6 text-center",
        "border border-dashed border-border",
        className,
      )}
    >
      {icon && <div className="mb-4 text-foreground-muted">{icon}</div>}
      <p className="text-base font-medium text-foreground">{title}</p>
      {description && (
        <p className="mt-1 text-sm text-foreground-muted max-w-md">
          {description}
        </p>
      )}
      {action && <div className="mt-4">{action}</div>}
    </div>
  );
}
