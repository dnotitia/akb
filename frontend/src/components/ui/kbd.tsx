import type { HTMLAttributes } from "react";
import { cn } from "@/lib/utils";

function Kbd({ className, ...props }: HTMLAttributes<HTMLElement>) {
  return (
    <kbd
      className={cn(
        "inline-flex items-center px-1.5 py-0.5 font-mono text-[10px] font-semibold uppercase tracking-wider",
        "border border-border bg-surface-muted text-foreground-muted",
        className,
      )}
      {...props}
    />
  );
}

export { Kbd };
