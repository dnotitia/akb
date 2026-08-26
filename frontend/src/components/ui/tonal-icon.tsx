import type { CSSProperties, ReactNode } from "react";
import { cn } from "@/lib/utils";

export type TonalIconTone =
  | "knowledge"
  | "collection"
  | "data"
  | "file"
  | "people"
  | "guide"
  | "publish"
  | "neutral"
  | "info"
  | "success"
  | "warning"
  | "danger";

const TONE_COLOR: Record<TonalIconTone, string> = {
  knowledge: "var(--color-cat-1)",
  collection: "var(--color-cat-2)",
  people: "var(--color-cat-2)",
  data: "var(--color-cat-3)",
  file: "var(--color-cat-4)",
  guide: "var(--color-cat-4)",
  publish: "var(--color-cat-4)",
  neutral: "var(--color-cat-6)",
  info: "var(--color-info)",
  success: "var(--color-success)",
  warning: "var(--color-warning)",
  danger: "var(--color-destructive)",
};

/**
 * A quiet, theme-aware category/state marker. The glyph still carries the
 * meaning; color is a secondary scan cue and therefore never stands alone.
 * Tones deliberately reuse the existing categorical and semantic ramps so
 * Vault pages gain hierarchy without turning into a rainbow dashboard.
 */
export function TonalIcon({
  tone = "neutral",
  children,
  size = "md",
  className,
}: {
  tone?: TonalIconTone;
  children: ReactNode;
  size?: "sm" | "md" | "lg";
  className?: string;
}) {
  const color = TONE_COLOR[tone];
  const style: CSSProperties = {
    color,
    backgroundColor: `color-mix(in srgb, ${color} 12%, transparent)`,
    borderColor: `color-mix(in srgb, ${color} 24%, var(--color-border))`,
  };

  return (
    <span
      aria-hidden
      className={cn(
        "inline-flex shrink-0 items-center justify-center rounded-[var(--radius-sm)] border",
        size === "sm" && "h-7 w-7 [&>svg]:h-3.5 [&>svg]:w-3.5",
        size === "md" && "h-8 w-8 [&>svg]:h-4 [&>svg]:w-4",
        size === "lg" && "h-9 w-9 [&>svg]:h-[18px] [&>svg]:w-[18px]",
        className,
      )}
      style={style}
    >
      {children}
    </span>
  );
}
