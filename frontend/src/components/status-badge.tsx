import {
  AlertTriangle,
  Archive,
  CircleDashed,
  GitBranch,
  Globe,
  Unlock,
} from "lucide-react";
import { Badge } from "@/components/ui/badge";
import { ROLE_ICONS, type Role } from "@/lib/roles";

export function RoleBadge({ role }: { role: string }) {
  const Icon = ROLE_ICONS[role as Role];
  const known = Icon !== undefined;
  return (
    <Badge variant={known ? (role as Role) : "outline"}>
      {known && <Icon className="h-3 w-3" aria-hidden />}
      {role}
    </Badge>
  );
}

type DocStatus = "draft" | "active" | "archived";
export function DocStatusBadge({ status }: { status: DocStatus }) {
  return <Badge variant={status}>{status}</Badge>;
}

type PublicAccess = "none" | "reader" | "writer";
interface VaultStateBadgeProps {
  archived?: boolean;
  externalGit?: boolean;
  publicAccess?: PublicAccess;
}
export function VaultStateBadge({
  archived,
  externalGit,
  publicAccess,
}: VaultStateBadgeProps) {
  const showPublic = publicAccess && publicAccess !== "none";
  if (!archived && !externalGit && !showPublic) return null;
  return (
    <div className="flex flex-wrap gap-1">
      {archived && (
        <Badge variant="archived">
          <Archive className="h-3 w-3" aria-hidden />
          archived
        </Badge>
      )}
      {externalGit && (
        <Badge variant="syncing">
          <GitBranch className="h-3 w-3" aria-hidden />
          external git
        </Badge>
      )}
      {showPublic &&
        (publicAccess === "writer" ? (
          // Most-open state: an OPEN padlock + amber warning — never a closed
          // Lock, which read as "secured" (the inverse of the truth).
          <Badge variant="warning">
            <Unlock className="h-3 w-3" aria-hidden />
            public:writer
          </Badge>
        ) : (
          <Badge variant="info">
            <Globe className="h-3 w-3" aria-hidden />
            public:reader
          </Badge>
        ))}
    </div>
  );
}

/**
 * Presentational chunk badge retained for the component catalog. Product
 * surfaces use the shared SearchStatusProvider and PendingIndexingBadge.
 * Pending excludes abandoned chunks; do not subtract abandoned or add retrying.
 */
export function IndexingBadge({
  pending,
  abandoned = 0,
}: {
  pending: number | null | undefined;
  abandoned?: number;
}) {
  const abandonedChip = abandoned > 0 ? (
    <Badge
      variant="error"
      title={`${abandoned.toLocaleString()} chunk(s) need indexing attention`}
    >
      <AlertTriangle className="h-3 w-3" aria-hidden />
      {abandoned.toLocaleString()} abandoned
    </Badge>
  ) : null;

  if (pending == null) {
    return (
      <>
        <Badge
          variant="outline"
          className="opacity-50 animate-pulse"
          title="Checking indexing status…"
          aria-busy="true"
        >
          <CircleDashed className="h-3 w-3" aria-hidden />
          checking…
        </Badge>
        {abandonedChip}
      </>
    );
  }
  if (pending <= 0) {
    return abandonedChip;
  }
  return (
    <>
      <Badge
        variant="pending"
        className="fade-in"
        title={`${pending.toLocaleString()} items pending`}
        aria-busy="true"
      >
        <CircleDashed className="h-3 w-3 motion-safe:animate-spin" aria-hidden />
        indexing {pending.toLocaleString()}
      </Badge>
      {abandonedChip}
    </>
  );
}
