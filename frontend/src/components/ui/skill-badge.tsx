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
  return (
    <Badge variant={defined ? "info" : "outline"} {...props}>
      <Sparkles className="h-3 w-3" aria-hidden />
      SKILL
      {defined && lineCount != null && ` ✓ ${lineCount}L`}
      {!defined && " ✗"}
      {children}
    </Badge>
  );
}
