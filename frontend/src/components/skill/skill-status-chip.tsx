import { useId } from "react";
import { Link, useLocation } from "react-router-dom";
import { SkillBadge } from "@/components/ui/skill-badge";
import { documentPreviewState } from "@/lib/document-preview-navigation";
import { vaultGuideHref } from "@/lib/vault-guide-navigation";

interface Props {
  vault: string;
  defined: boolean;
  /** Body differs from the seed template; `undefined` while it is still being
   *  determined (see SkillBadge). */
  customized?: boolean;
}

export function SkillStatusChip({ vault, defined, customized }: Props) {
  const location = useLocation();
  const id = useId();
  const href = defined ? vaultGuideHref(vault) : `/vault/${encodeURIComponent(vault)}/settings#skill`;
  return (
    <Link
      to={href}
      id={id}
      state={defined ? documentPreviewState(location, id) : undefined}
      aria-label={defined ? "Open vault guide" : "Set up vault guide"}
      className="inline-flex rounded-full focus:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2 focus-visible:ring-offset-background"
    >
      <SkillBadge defined={defined} customized={customized} />
    </Link>
  );
}
