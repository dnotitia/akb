import { useEffect, useMemo, useState } from "react";
import { Link, useParams } from "react-router-dom";
import { ArrowLeft, Crown, Globe, Plus, Search, Trash2, UserCog, X } from "lucide-react";
import {
  getMe,
  getVaultInfo,
  getVaultMembers,
  grantAccess,
  revokeAccess,
  transferOwnership,
} from "@/lib/api";
import { Badge } from "@/components/ui/badge";
import { Alert } from "@/components/ui/alert";
import { Button } from "@/components/ui/button";
import { ConfirmDialog } from "@/components/ui/confirm-dialog";
import { Input } from "@/components/ui/input";
import { InviteMemberDialog } from "@/components/invite-member-dialog";
import { RoleBadge } from "@/components/status-badge";
import { RoleSelect } from "@/components/role-select";
import { EmptyState } from "@/components/empty-state";
import { TooltipText } from "@/components/ui/tooltip-text";
import { timeAgo } from "@/lib/utils";

interface Member {
  username: string;
  display_name?: string | null;
  email: string;
  role: "owner" | "admin" | "writer" | "reader";
  since?: string | null;
}

interface VaultInfo {
  name: string;
  description?: string;
  role?: "owner" | "admin" | "writer" | "reader";
  role_source?: "member" | "public";
}

// One source of truth for what each role can do — drives the roster legend
// (the badges alone don't explain the capabilities).
const ROLE_CAPABILITIES: Array<[string, string]> = [
  ["reader", "Read documents, browse, and search."],
  ["writer", "Everything a reader can, plus create / edit / delete content."],
  ["admin", "Everything a writer can, plus invite / remove members and change roles."],
  ["owner", "Full control, including transfer of ownership and vault deletion."],
];

// Above this many members, surface a filter so finding one person isn't a scan.
const FILTER_THRESHOLD = 8;

export default function VaultMembersPage() {
  const { name } = useParams<{ name: string }>();
  const [info, setInfo] = useState<VaultInfo | null>(null);
  const [members, setMembers] = useState<Member[] | null>(null);
  const [error, setError] = useState("");
  const [filter, setFilter] = useState("");
  const [inviteOpen, setInviteOpen] = useState(false);
  const [pendingRevoke, setPendingRevoke] = useState<Member | null>(null);
  const [pendingTransfer, setPendingTransfer] = useState<Member | null>(null);
  const [currentUser, setCurrentUser] = useState<{ username: string } | null>(null);
  const [undoTarget, setUndoTarget] = useState<{
    username: string;
    prev: string;
    next: string;
  } | null>(null);
  const [undoError, setUndoError] = useState<string | null>(null);

  useEffect(() => {
    getMe()
      .then((u) => setCurrentUser({ username: u.username }))
      .catch(() => setCurrentUser(null));
  }, []);

  useEffect(() => {
    if (!name) return;
    // Reset stale state from previous param before re-fetch resolves.
    setInfo(null);
    setMembers(null);
    setError("");
    setFilter("");
    refresh();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [name]);

  // Name the tab/history entry (tab switching + SR route-change orientation).
  useEffect(() => {
    if (!name) return;
    const prev = document.title;
    document.title = `${name} · Members · AKB`;
    return () => {
      document.title = prev;
    };
  }, [name]);

  async function refresh() {
    if (!name) return;
    try {
      const [i, m] = await Promise.all([
        getVaultInfo(name).catch(() => null),
        getVaultMembers(name),
      ]);
      if (i) setInfo(i);
      setMembers(m.members || []);
      setError("");
    } catch (e: any) {
      setError(e?.message || "Failed to load members");
      setMembers([]);
    }
  }

  const existingUsernames = useMemo(
    () => new Set((members || []).map((m) => m.username)),
    [members],
  );
  const canManage = info?.role === "owner" || info?.role === "admin";
  const canTransfer = info?.role === "owner";

  const filtered = useMemo(() => {
    const list = members || [];
    const q = filter.trim().toLowerCase();
    if (!q) return list;
    return list.filter((m) =>
      [m.username, m.display_name, m.email].some((f) => f?.toLowerCase().includes(q)),
    );
  }, [members, filter]);

  async function confirmRevoke() {
    if (!name || !pendingRevoke) return;
    await revokeAccess(name, pendingRevoke.username);
    await refresh();
  }

  async function confirmTransfer() {
    if (!name || !pendingTransfer) return;
    await transferOwnership(name, pendingTransfer.username);
    await refresh();
  }

  function handleRoleChanged(m: Member, prev: string, next: string) {
    // Optimistic: reflect the new role + open the undo window immediately (the
    // grant already succeeded server-side by the time RoleSelect calls back),
    // then reconcile with a background refresh so the row never shows a stale
    // badge with no indicator while the refetch is in flight.
    setMembers((cur) =>
      cur
        ? cur.map((x) =>
            x.username === m.username ? { ...x, role: next as Member["role"] } : x,
          )
        : cur,
    );
    setUndoTarget({ username: m.username, prev, next });
    setTimeout(() => {
      setUndoTarget((cur) =>
        cur && cur.username === m.username && cur.next === next ? null : cur,
      );
    }, 5000);
    void refresh();
  }

  async function handleUndo() {
    if (!undoTarget) return;
    const { username, prev } = undoTarget;
    setUndoTarget(null);
    setUndoError(null);
    try {
      await grantAccess(name!, username, prev);
      await refresh();
    } catch (e: any) {
      setUndoError(e?.message || "Undo failed");
    }
  }

  if (!name) return null;

  const total = members?.length ?? 0;
  const noMatches = members !== null && filter.trim() !== "" && filtered.length === 0;

  return (
    <div className="fade-up max-w-[1280px] mx-auto">
      {/* Back row + title */}
      <div className="flex items-baseline justify-between mb-6 flex-wrap gap-y-2">
        <Link
          to={`/vault/${name}`}
          className="inline-flex items-center gap-1.5 min-h-[36px] coord hover:text-link transition-colors focus:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2 focus-visible:ring-offset-background rounded-[var(--radius-sm)]"
        >
          <ArrowLeft className="h-3 w-3" aria-hidden />
          Back to {name}
        </Link>
        {info?.role && (
          info.role_source === "public" ? (
            <div
              className="flex items-center gap-1.5"
              title="This role is granted by the vault's public_access setting, not by direct membership. Contact the owner if this was unintended."
              aria-label={`Public ${info.role}`}
            >
              <Badge variant="info">
                <Globe className="h-3 w-3" aria-hidden />
                Public
              </Badge>
              <RoleBadge role={info.role} />
            </div>
          ) : (
            <RoleBadge role={info.role} />
          )
        )}
      </div>

      <div className="coord mb-3">
        Vault · <span className="text-foreground">{name}</span> · Members
      </div>
      <h1 className="font-display text-3xl tracking-tight text-foreground mb-2">
        Members
      </h1>
      <p className="text-sm leading-relaxed text-foreground-muted mb-4 max-w-prose">
        Who can read or write to this vault. The owner holds the keys; admins can
        invite or revoke; writers can mutate content; readers see everything but
        change nothing.
      </p>

      {/* Role legend — the badges don't explain the capabilities, so make them
          discoverable without leaving the page. */}
      <details className="group mb-8 max-w-prose">
        <summary className="inline-flex cursor-pointer list-none items-center gap-1.5 coord hover:text-link transition-colors focus:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2 focus-visible:ring-offset-background rounded-[var(--radius-sm)]">
          <UserCog className="h-3 w-3" aria-hidden />
          What do roles mean?
        </summary>
        <dl className="mt-2 space-y-1.5 rounded-[var(--radius-md)] border border-border bg-surface-muted px-4 py-3">
          {ROLE_CAPABILITIES.map(([role, desc]) => (
            <div key={role} className="flex flex-wrap items-baseline gap-x-2 text-xs">
              <dt className="w-16 shrink-0 font-medium text-foreground capitalize">{role}</dt>
              <dd className="min-w-0 flex-1 text-foreground-muted leading-relaxed">{desc}</dd>
            </div>
          ))}
        </dl>
      </details>

      {/* Header with filter + invite button */}
      <header className="flex items-baseline justify-between gap-3 pb-3 border-b border-border mb-0 flex-wrap">
        <div className="flex items-baseline gap-3">
          <span className="coord-ink">Roster</span>
          <span className="coord tabular-nums">[{members ? members.length : "··"}]</span>
        </div>
        <div className="flex items-center gap-3">
          {total > FILTER_THRESHOLD && (
            <div className="relative">
              <Search
                className="pointer-events-none absolute left-2.5 top-1/2 -translate-y-1/2 h-3.5 w-3.5 text-foreground-muted"
                aria-hidden
              />
              <Input
                value={filter}
                onChange={(e) => setFilter(e.target.value)}
                placeholder="Filter members…"
                aria-label="Filter members"
                className="h-9 w-48 pl-8 text-sm"
              />
            </div>
          )}
          {canManage && (
            <Button variant="accent" size="sm" onClick={() => setInviteOpen(true)}>
              <Plus className="h-4 w-4" aria-hidden />
              Invite
            </Button>
          )}
        </div>
      </header>

      {undoTarget && (
        <div
          role="status"
          className="flex items-center gap-3 px-3 py-2 rounded-[var(--radius-md)] border border-border bg-surface-muted mb-4 mt-4"
        >
          <span className="text-sm text-foreground">
            Changed {undoTarget.username} from {undoTarget.prev} to {undoTarget.next}.
          </span>
          <button
            type="button"
            onClick={handleUndo}
            className="inline-flex min-h-[36px] items-center text-xs text-link hover:text-link-hover hover:underline cursor-pointer rounded-[var(--radius-sm)] focus:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2 focus-visible:ring-offset-surface-muted"
          >
            Undo
          </button>
          <button
            type="button"
            onClick={() => setUndoTarget(null)}
            aria-label="Dismiss"
            className="ml-auto inline-flex h-9 w-9 items-center justify-center text-foreground-muted hover:text-foreground cursor-pointer rounded-[var(--radius-sm)] focus:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2 focus-visible:ring-offset-surface-muted"
          >
            <X className="h-3.5 w-3.5" aria-hidden />
          </button>
        </div>
      )}
      {undoError && (
        <Alert variant="destructive" className="mb-4 mt-4">Undo failed: {undoError}</Alert>
      )}

      {/* List */}
      {error ? (
        <Alert variant="destructive" title="Failed to load members" className="mt-4">{error}</Alert>
      ) : members === null ? (
        <ol
          className="mt-4 rounded-[var(--radius-lg)] overflow-hidden border border-border bg-surface shadow-sm divide-y divide-border"
          aria-hidden
        >
          {Array.from({ length: 4 }).map((_, i) => (
            <li key={i} className="flex items-center gap-4 px-4 py-3">
              <span className="h-3 w-6 rounded bg-surface-muted animate-pulse" />
              <span className="h-4 flex-1 rounded bg-surface-muted animate-pulse" />
              <span className="h-5 w-16 rounded bg-surface-muted animate-pulse" />
              <span className="h-3 w-12 rounded bg-surface-muted animate-pulse" />
            </li>
          ))}
        </ol>
      ) : members.length === 0 ? (
        <EmptyState title="No members on record" description="Even the owner row should appear here — try refreshing." />
      ) : noMatches ? (
        <EmptyState
          title="No matching members"
          description={`No member matches "${filter.trim()}".`}
        />
      ) : (
        <>
          <span className="sr-only" role="status" aria-live="polite">
            {filter.trim()
              ? `${filtered.length} of ${total} member${total === 1 ? "" : "s"} shown`
              : `${total} member${total === 1 ? "" : "s"}`}
          </span>
          <ol className="mt-4 rounded-[var(--radius-lg)] overflow-hidden border border-border bg-surface shadow-sm divide-y divide-border">
            {filtered.map((m, i) => (
              <li
                key={m.username}
                className="grid grid-cols-[40px_minmax(0,1fr)_auto_auto] items-baseline gap-x-4 gap-y-1 px-4 py-3"
              >
                <span className="coord tabular-nums self-baseline">{i + 1}</span>
                <div className="min-w-0">
                  <div className="flex items-baseline gap-2 flex-wrap">
                    <span className="text-sm font-semibold text-foreground">
                      {m.username}
                    </span>
                    {m.display_name && (
                      <span className="text-xs text-foreground-muted">
                        {m.display_name}
                      </span>
                    )}
                  </div>
                  <TooltipText as="div" className="coord truncate">{m.email}</TooltipText>
                </div>
                <div className="flex items-center gap-3 shrink-0">
                  {canManage && m.role !== "owner" && currentUser && m.username !== currentUser.username ? (
                    <RoleSelect
                      vault={name!}
                      member={m}
                      onChanged={(prev, next) => handleRoleChanged(m, prev, next)}
                    />
                  ) : (
                    <RoleBadge role={m.role} />
                  )}
                  <span className="coord tabular-nums w-[64px] text-right">
                    {m.role === "owner" ? "—" : m.since ? timeAgo(m.since) : "—"}
                  </span>
                </div>
                <div className="flex items-center gap-1.5 shrink-0">
                  {/* Owner: only target of "transfer to". Action lives on each
                      non-owner row when caller is owner. */}
                  {canTransfer && m.role !== "owner" && (
                    <button
                      type="button"
                      onClick={() => setPendingTransfer(m)}
                      aria-label={`Transfer ownership to ${m.username}`}
                      title="Transfer ownership"
                      className="inline-flex min-h-[36px] items-center gap-1 px-2 text-xs text-foreground-muted hover:text-link transition-colors cursor-pointer focus:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2 focus-visible:ring-offset-surface rounded-[var(--radius-sm)]"
                    >
                      <Crown className="h-3.5 w-3.5" aria-hidden />
                      Transfer
                    </button>
                  )}
                  {canManage && m.role !== "owner" && (
                    <button
                      type="button"
                      onClick={() => setPendingRevoke(m)}
                      aria-label={`Revoke ${m.username}`}
                      className="inline-flex min-h-[36px] items-center gap-1 px-2 text-xs text-foreground-muted hover:text-destructive transition-colors cursor-pointer focus:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2 focus-visible:ring-offset-surface rounded-[var(--radius-sm)]"
                    >
                      <Trash2 className="h-3.5 w-3.5" aria-hidden />
                      Revoke
                    </button>
                  )}
                  {!canManage && m.role !== "owner" && (
                    <span className="coord">—</span>
                  )}
                </div>
              </li>
            ))}
          </ol>
        </>
      )}

      {!canManage && members && (
        <p className="coord mt-6 flex items-center gap-2">
          <UserCog className="h-3 w-3" aria-hidden />
          Roster is read-only · your role is {info?.role || "—"}
        </p>
      )}

      {/* Dialogs */}
      <InviteMemberDialog
        open={inviteOpen}
        onOpenChange={setInviteOpen}
        vault={name}
        existingUsernames={existingUsernames}
        onInvited={refresh}
      />
      <ConfirmDialog
        open={pendingRevoke !== null}
        onOpenChange={(o) => !o && setPendingRevoke(null)}
        title={pendingRevoke ? `Revoke ${pendingRevoke.username}?` : ""}
        description={
          pendingRevoke
            ? `${pendingRevoke.username} will lose access to ${name} immediately.\nThis can be re-granted at any time.`
            : ""
        }
        confirmLabel="Revoke access"
        variant="destructive"
        onConfirm={confirmRevoke}
      />
      <ConfirmDialog
        open={pendingTransfer !== null}
        onOpenChange={(o) => !o && setPendingTransfer(null)}
        title={
          pendingTransfer ? `Transfer ownership to ${pendingTransfer.username}?` : ""
        }
        description={
          pendingTransfer
            ? `You will become an admin and ${pendingTransfer.username} becomes the new owner.\nOnly the new owner can transfer ownership again.\nThis cannot be undone by you alone.`
            : ""
        }
        confirmLabel="Transfer ownership"
        variant="destructive"
        onConfirm={confirmTransfer}
      />
    </div>
  );
}
