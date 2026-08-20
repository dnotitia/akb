import { Link } from "react-router-dom";
import { SkillBadge } from "@/components/ui/skill-badge";

interface Props {
  vault: string;
  defined: boolean;
  /** Body differs from the seed template; `undefined` while it is still being
   *  determined (see SkillBadge). */
  customized?: boolean;
}

export function SkillStatusChip({ vault, defined, customized }: Props) {
  // One destination in every state: the settings section is the only place the
  // guide can be read AND edited, and the canonical doc URL redirects here
  // anyway — link straight at it instead of through the bounce.
  const href = `/vault/${vault}/settings#skill`;
  return (
    <Link
      to={href}
      aria-label={defined ? "Open vault guide" : "Set up vault guide"}
      className="inline-flex rounded-full focus:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2 focus-visible:ring-offset-background"
    >
      <SkillBadge defined={defined} customized={customized} />
    </Link>
  );
}
