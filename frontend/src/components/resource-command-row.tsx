import type { ReactNode } from "react";
import { cn } from "@/lib/utils";

/** Resource commands and optional metadata. Location belongs to the page header. */
export function ResourceCommandRow({ children, meta, className, appearance = "default" }: {
  children?: ReactNode;
  meta?: ReactNode;
  className?: string;
  appearance?: "default" | "reader";
}) {
  return <div data-slot="resource-command-row" className={cn("@container/resource-commands shrink-0 border-b border-border bg-surface", appearance === "reader" && "document-command-row", className)}>
    <div className={cn("flex min-h-10 min-w-0 flex-wrap items-center gap-x-3 gap-y-1 px-3 py-1 text-sm", appearance === "reader" ? "document-command-inner" : "[&_button]:min-h-11 [&_a]:min-h-11 @min-[48rem]/resource-commands:[&_button]:min-h-8 @min-[48rem]/resource-commands:[&_a]:min-h-8")}>
      {meta && <div data-slot="resource-command-meta" className="flex min-w-0 flex-1 flex-wrap items-center gap-x-3 gap-y-1 text-xs text-foreground-muted">{meta}</div>}
      {children && <div data-slot="resource-command-actions" className={cn("ml-auto flex min-w-0 flex-wrap items-center justify-end", appearance === "reader" ? "gap-2" : "gap-1")}>{children}</div>}
    </div>
  </div>;
}
