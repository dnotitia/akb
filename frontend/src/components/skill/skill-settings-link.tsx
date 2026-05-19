import { Link } from "react-router-dom";
import { Button } from "@/components/ui/button";
import { SkillBadge } from "@/components/ui/skill-badge";
import { SkillCreateButton } from "./skill-create-button";
import { timeAgo } from "@/lib/utils";

interface Props {
  vault: string;
  defined: boolean;
  updatedAt?: string;
}

export function SkillSettingsLink({ vault, defined, updatedAt }: Props) {
  return (
    <div className="flex items-center justify-between gap-3 py-3 border-b border-border">
      <div className="flex items-center gap-3 text-[12px]">
        <SkillBadge defined={defined} />
        <span>
          Vault guide · {defined ? "✓ defined" : "✗ undefined"}
          {defined && updatedAt && ` · last updated ${timeAgo(updatedAt)}`}
        </span>
      </div>
      {defined ? (
        <Link to={`/vault/${vault}/doc/${encodeURIComponent("overview/vault-skill.md")}`}>
          <Button variant="outline" size="sm">Configure →</Button>
        </Link>
      ) : (
        <SkillCreateButton vault={vault} variant="outline" />
      )}
    </div>
  );
}
