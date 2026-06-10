import { Sparkles } from "lucide-react";
import { Badge, type BadgeProps } from "./badge";

interface SkillBadgeProps extends Omit<BadgeProps, "variant"> {
  defined?: boolean;          // default true
  lineCount?: number;         // shown only when defined, as "✓ {N}L"
}

export function SkillBadge({
  defined = true,
  lineCount,
  children,
  ...props
}: SkillBadgeProps) {
  // The defined/undefined state is otherwise carried only by the ✓/✗ glyph +
  // the color, neither of which assistive tech reads as state. Give the badge a
  // text accessible name; callers may override via props.aria-label (e.g. the
  // routing Link in SkillStatusChip). Defined → teal `info-outline` (a passive
  // "configured" marker), NOT orange — orange is reserved for the one marquee
  // CTA per view.
  const defaultLabel = defined
    ? `Vault guide defined${lineCount != null ? `, ${lineCount} lines` : ""}`
    : "Vault guide not defined";
  return (
    <Badge variant={defined ? "info-outline" : "outline"} aria-label={defaultLabel} {...props}>
      <Sparkles className="h-3 w-3" aria-hidden />
      Guide
      {defined && lineCount != null && ` ✓ ${lineCount}L`}
      {!defined && " ✗"}
      {children}
    </Badge>
  );
}
