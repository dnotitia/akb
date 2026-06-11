import { Link } from "react-router-dom";
import { SkillBadge } from "@/components/ui/skill-badge";

interface Props {
  vault: string;
  defined: boolean;
  lineCount?: number;
}

export function SkillStatusChip({ vault, defined, lineCount }: Props) {
  // Defined → open the underlying doc directly so users land where they
  // can read + edit. Undefined → settings, where SkillSettingsLink offers
  // the Create-from-template button.
  const href = defined
    ? `/vault/${vault}/doc/${encodeURIComponent("overview/vault-skill.md")}`
    : `/vault/${vault}/settings`;
  return (
    <Link
      to={href}
      aria-label={defined ? "Open vault guide" : "Set up vault guide"}
      className="inline-flex rounded-full focus:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2 focus-visible:ring-offset-background"
    >
      <SkillBadge defined={defined} lineCount={lineCount} />
    </Link>
  );
}
