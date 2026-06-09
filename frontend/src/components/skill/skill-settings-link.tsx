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
      <div className="flex items-center gap-3 text-xs">
        <SkillBadge defined={defined} />
        <span>
          Vault guide · {defined ? "✓ defined" : "✗ undefined"}
          {defined && updatedAt && ` · last updated ${timeAgo(updatedAt)}`}
        </span>
      </div>
      {defined ? (
        // Button asChild renders a single styled <a> (avoids an invalid
        // <button> nested inside <a>) and carries the focus-visible ring.
        <Button asChild variant="outline" size="sm">
          <Link to={`/vault/${vault}/doc/${encodeURIComponent("overview/vault-skill.md")}`}>
            Configure →
          </Link>
        </Button>
      ) : (
        <SkillCreateButton vault={vault} variant="outline" />
      )}
    </div>
  );
}
