import {
  Archive,
  CircleDashed,
  Eye,
  GitBranch,
  Globe,
  Key,
  Lock,
  Pencil,
  ShieldCheck,
} from "lucide-react";
import { Badge } from "@/components/ui/badge";

type Role = "owner" | "admin" | "writer" | "reader";

const ROLE_ICONS: Record<Role, React.ComponentType<{ className?: string; "aria-hidden"?: boolean }>> = {
  owner: Key,            // holds the key — filled accent
  admin: ShieldCheck,    // admin — filled foreground
  writer: Pencil,        // can write — outlined
  reader: Eye,           // read-only — muted outlined
};

// Accept any string at runtime — backend can introduce new role levels ahead
// of the frontend enum, and an unknown key used to render <undefined /> and
// crash the whole view with React #130.
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

type DocStatus = "draft" | "active" | "archived" | "superseded";
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
      {showPublic && (
        <Badge variant="info">
          {publicAccess === "reader" ? (
            <Globe className="h-3 w-3" aria-hidden />
          ) : (
            <Lock className="h-3 w-3" aria-hidden />
          )}
          public:{publicAccess}
        </Badge>
      )}
    </div>
  );
}

/**
 * Indexing activity indicator.
 *
 * Pass `pending` as:
 *   - `null` / `undefined` — `/health` hasn't resolved yet; renders a muted
 *     skeleton chip that reserves layout space so the badge doesn't pop-in
 *     jarringly once the real value arrives.
 *   - `0` — loaded and nothing pending; renders nothing.
 *   - `> 0` — renders the active spinner badge with a fade-in transition.
 */
export function IndexingBadge({ pending }: { pending: number | null | undefined }) {
  if (pending == null) {
    return (
      <Badge
        variant="outline"
        className="opacity-40 animate-pulse"
        title="Checking indexing status…"
        aria-busy="true"
      >
        <CircleDashed className="h-3 w-3" aria-hidden />
        checking…
      </Badge>
    );
  }
  if (pending <= 0) return null;
  return (
    <Badge
      variant="pending"
      className="fade-in"
      title={`${pending.toLocaleString()} items pending`}
    >
      <CircleDashed className="h-3 w-3 animate-spin" aria-hidden />
      indexing {pending.toLocaleString()}
    </Badge>
  );
}
