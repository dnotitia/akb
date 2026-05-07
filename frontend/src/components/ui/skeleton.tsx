import type { HTMLAttributes } from "react";
import { cn } from "@/lib/utils";

function Skeleton({ className, ...props }: HTMLAttributes<HTMLDivElement>) {
  return (
    <div
      className={cn("animate-pulse bg-surface-muted", className)}
      aria-hidden
      {...props}
    />
  );
}

export { Skeleton };
