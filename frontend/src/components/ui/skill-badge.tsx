import { Sparkles } from "lucide-react";
import { Badge, type BadgeProps } from "./badge";

interface SkillBadgeProps extends Omit<BadgeProps, "variant"> {
  defined?: boolean;          // default true
  lineCount?: number;         // shown only when defined, as "✓ {N}L"
  /**
   * Body differs from the seed template. Every normal vault carries the guide
   * now, so "does it exist" is dead information — "has anyone written it yet"
   * is the state worth a chip. `undefined` = not known yet (the template
   * comparison is still resolving); the badge then stays on the plain marker
   * rather than flashing a guessed state.
   */
  customized?: boolean;
}

export function SkillBadge({
  defined = true,
  lineCount,
  customized,
  children,
  ...props
}: SkillBadgeProps) {
  // The defined/undefined state is otherwise carried only by the ✓/✗ glyph +
  // the color, neither of which assistive tech reads as state. Give the badge a
  // text accessible name; callers may override via props.aria-label (e.g. the
  // routing Link in SkillStatusChip). Defined → teal `info-outline` (a passive
  // "configured" marker), NOT orange — orange is reserved for the one marquee
  // CTA per view. Still-on-the-template reads as neutral outline: the guide is
  // there, but nobody has said anything with it yet.
  const state =
    defined && customized !== undefined
      ? customized
        ? "customized"
        : "template"
      : null;
  // The state word supersedes the line count — both in the same tiny chip is
  // noise, and size trivia loses to "has this been written".
  const defaultLabel = !defined
    ? "Vault guide not defined"
    : state
      ? `Vault guide, ${state}`
      : `Vault guide defined${lineCount != null ? `, ${lineCount} lines` : ""}`;
  return (
    <Badge
      variant={defined && customized !== false ? "info-outline" : "outline"}
      aria-label={defaultLabel}
      {...props}
    >
      <Sparkles className="h-3 w-3" aria-hidden />
      Guide
      {state && ` · ${state}`}
      {!state && defined && lineCount != null && ` ✓ ${lineCount}L`}
      {!defined && " ✗"}
      {children}
    </Badge>
  );
}
