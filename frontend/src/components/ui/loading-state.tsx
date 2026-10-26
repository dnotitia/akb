import { useEffect, useState, type HTMLAttributes, type ReactNode } from "react";
import { Loader2 } from "lucide-react";

import { cn } from "@/lib/utils";

interface LoadingStateProps
  extends Omit<HTMLAttributes<HTMLDivElement>, "children"> {
  /** A complete, user-facing description of the work in progress. */
  label: string;
  /** Reserve layout immediately, but skip visual/status flashes for fast reads. */
  delayMs?: number;
  /** Visual placeholder content. It is hidden from assistive technology. */
  children: ReactNode;
}

/**
 * Accessible boundary for page, panel, and ledger skeletons.
 *
 * The boundary owns the single live status while its visual skeleton stays out
 * of the accessibility tree. This prevents a page made from many placeholder
 * blocks from producing repeated or meaningless announcements.
 */
function LoadingState({ label, children, className, delayMs = 0, ...props }: LoadingStateProps) {
  const [revealed, setRevealed] = useState(delayMs === 0);
  useEffect(() => {
    if (delayMs === 0) return;
    const timer = setTimeout(() => setRevealed(true), delayMs);
    return () => clearTimeout(timer);
  }, [delayMs]);
  return (
    <div
      {...props}
      role={revealed ? "status" : undefined}
      aria-live={revealed ? "polite" : undefined}
      aria-atomic="true"
      aria-busy="true"
      aria-label={label}
      className={className}
    >
      <div aria-hidden style={revealed ? undefined : { visibility: "hidden" }}>{children}</div>
    </div>
  );
}

interface InlineLoadingStateProps
  extends Omit<HTMLAttributes<HTMLDivElement>, "children"> {
  label: string;
  size?: "sm" | "md";
}

/** Compact feedback for brief transitions where a structural skeleton is not useful. */
function InlineLoadingState({
  label,
  size = "md",
  className,
  ...props
}: InlineLoadingStateProps) {
  return (
    <div
      {...props}
      role="status"
      aria-live="polite"
      aria-atomic="true"
      aria-busy="true"
      className={cn(
        "inline-flex items-center justify-center text-foreground-muted",
        size === "sm" ? "gap-1.5 text-xs" : "gap-2 text-sm",
        className,
      )}
    >
      <Loader2
        className={cn("shrink-0 animate-spin text-link", size === "sm" ? "h-3.5 w-3.5" : "h-4 w-4")}
        aria-hidden
      />
      <span>{label}</span>
    </div>
  );
}

export { InlineLoadingState, LoadingState };
export type { InlineLoadingStateProps, LoadingStateProps };
