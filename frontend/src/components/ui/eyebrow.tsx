import type { HTMLAttributes } from "react";
import { cn } from "@/lib/utils";

/**
 * Design-system primitive: the mono "coordinate" section eyebrow (§ LABEL).
 * Centralizes the `.coord*` label so pages stop hand-writing the class.
 *
 * tone: "muted" (default) | "ink" (foreground) | "spark" (accent).
 */
export function Eyebrow({
  tone = "muted",
  className,
  ...props
}: HTMLAttributes<HTMLSpanElement> & { tone?: "muted" | "ink" | "spark" }) {
  const cls = tone === "ink" ? "coord-ink" : tone === "spark" ? "coord-spark" : "coord";
  return <span className={cn(cls, className)} {...props} />;
}
