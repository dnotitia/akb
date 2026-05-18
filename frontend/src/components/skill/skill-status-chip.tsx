import { Link } from "react-router-dom";
import { SkillBadge } from "@/components/ui/skill-badge";

interface Props {
  vault: string;
  defined: boolean;
  lineCount?: number;
}

export function SkillStatusChip({ vault, defined, lineCount }: Props) {
  return (
    <Link to={`/vault/${vault}/skill`} className="inline-flex">
      <SkillBadge defined={defined} lineCount={lineCount} />
    </Link>
  );
}
