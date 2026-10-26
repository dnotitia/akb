import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useParams } from "react-router-dom";
import * as DropdownMenu from "@radix-ui/react-dropdown-menu";
import {
  CircleHelp,
  CheckCircle2,
  Crown,
  Globe,
  MoreHorizontal,
  Plus,
  Search,
  Trash2,
  UserCog,
  UsersRound,
  X,
} from "lucide-react";
import {
  getMe,
  getVaultInfo,
  getVaultMembers,
  grantAccess,
  revokeAccess,
  transferOwnership,
} from "@/lib/api";
import { Alert } from "@/components/ui/alert";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { ConfirmDialog } from "@/components/ui/confirm-dialog";
import { EmptyState } from "@/components/empty-state";
import { Input } from "@/components/ui/input";
import { InviteMemberDialog } from "@/components/invite-member-dialog";
import { InlineLoadingState } from "@/components/ui/loading-state";
import { Panel } from "@/components/ui/panel";
import { RoleSelect } from "@/components/role-select";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogTitle,
  DialogTrigger,
} from "@/components/ui/dialog";
import { WorkspaceSectionHeader } from "@/components/ui/workspace-section-header";
import { ROLE_ICONS, type Role } from "@/lib/roles";
import { timeAgo } from "@/lib/utils";
import {
  useAccessVerification,
  useCurrentUser,
} from "@/contexts/current-user-context";

interface Member {
  id: string;
  username: string;
  display_name?: string | null;
  email: string;
  account_kind: "human" | "service";
  account_status: string;
  role: Role;
  since?: string | null;
}

interface VaultInfo {
  name: string;
  description?: string;
  role?: Role;
  role_source?: "member" | "public" | "system_admin";
  public_access?: "none" | "reader" | "writer";
  is_archived?: boolean;
  is_external_git?: boolean;
  managed_by?: string | null;
}

const ROLE_ORDER: Role[] = ["owner", "admin", "writer", "reader"];
const ROLE_LABELS: Record<Role, string> = {
  owner: "Owner",
  admin: "Admin",
  writer: "Writer",
  reader: "Reader",
};
const ROLE_CAPABILITIES: Record<Role, string> = {
  owner: "Full control, ownership transfer, and vault deletion.",
  admin: "Manage members, roles, and vault content.",
  writer: "Create and change vault content.",
  reader: "Browse and search without making changes.",
};
const FILTER_THRESHOLD = 8;

export default function VaultMembersPage() {
  const { name } = useParams<{ name: string }>();
  const user = useCurrentUser();
  const { checking, revision } = useAccessVerification();
  return (
    <VaultMembersWorkspace
      key={JSON.stringify([user?.user_id, revision, name, checking])}
      name={name}
      checking={checking}
    />
  );
}

function VaultMembersWorkspace({
  name,
  checking,
}: {
  name?: string;
  checking: boolean;
}) {
  const active = useRef(true);
  const loadGeneration = useRef(0);
  const [info, setInfo] = useState<VaultInfo | null>(null);
  const [members, setMembers] = useState<Member[] | null>(null);
  const [error, setError] = useState("");
  const [refreshing, setRefreshing] = useState(false);
  const [permissionError, setPermissionError] = useState("");
  const [notice, setNotice] = useState("");
  const [filter, setFilter] = useState("");
  const [inviteOpen, setInviteOpen] = useState(false);
  const inviteTriggerRef = useRef<HTMLButtonElement>(null);
  const [pendingRevoke, setPendingRevoke] = useState<Member | null>(null);
  const [pendingTransfer, setPendingTransfer] = useState<Member | null>(null);
  const [currentUser, setCurrentUser] = useState<{ id: string; username: string } | null>(
    null,
  );
  const [undoTarget, setUndoTarget] = useState<{
    id: string;
    username: string;
    prev: string;
    next: string;
  } | null>(null);
  const [undoError, setUndoError] = useState<string | null>(null);

  useEffect(() => {
    active.current = true;
    return () => {
      active.current = false;
      loadGeneration.current += 1;
    };
  }, []);

  useEffect(() => {
    if (checking) return;
    let cancelled = false;
    getMe()
      .then((user) => {
        if (!cancelled) setCurrentUser({ id: user.user_id, username: user.username });
      })
      .catch(() => {
        if (!cancelled) setCurrentUser(null);
      });
    return () => {
      cancelled = true;
    };
  }, [checking]);

  const refresh = useCallback(async () => {
    if (!name || checking || !active.current) return;
    const generation = ++loadGeneration.current;
    setRefreshing(true);
    setError("");
    setPermissionError("");
    try {
      const [permission, roster] = await Promise.allSettled([getVaultInfo(name), getVaultMembers(name)]);
      if (!active.current || generation !== loadGeneration.current) return;
      if (permission.status === "fulfilled") setInfo(permission.value);
      else {
        setInfo(null);
        setPermissionError(errorMessage(permission.reason, "Could not verify Vault permissions"));
      }
      const denied = [permission, roster].some(result => result.status === "rejected" && [401, 403].includes(result.reason?.status));
      if (denied) {
        setInfo(null); setMembers(null);
        setError("Access could not be verified. Retry to check your membership.");
      } else if (roster.status === "fulfilled") setMembers(roster.value.members || []);
      else setError(errorMessage(roster.reason, "Failed to load members"));
    } finally {
      if (active.current && generation === loadGeneration.current) setRefreshing(false);
    }
  }, [name, checking]);

  useEffect(() => {
    if (!name) return;
    setInfo(null);
    setMembers(null);
    setError("");
    setFilter("");
    void refresh();
  }, [name, refresh]);

  useEffect(() => {
    if (!name) return;
    const previous = document.title;
    document.title = `${name} · Members · AKB`;
    return () => {
      document.title = previous;
    };
  }, [name]);

  const existingUserIds = useMemo(
    () => new Set((members || []).map((member) => member.id)),
    [members],
  );
  const hasManageRole = info?.role === "owner" || info?.role === "admin";
  const managedWriteDisabledReason = info?.managed_by && info.role_source !== "system_admin"
    ? `Vault '${name}' is write-managed by ${info.managed_by}; writes require a granted service token with the required write action`
    : undefined;
  const membershipDisabledReason = info?.is_archived
    ? `Vault '${name}' is archived (read-only)`
    : managedWriteDisabledReason;
  const canManage = hasManageRole && !membershipDisabledReason && !refreshing && !permissionError;
  // Ownership transfer is allowed on archived Vaults, but still passes the
  // backend's owner-level managed-write policy guard.
  const hasTransferRole = info?.role === "owner";
  const canTransfer = hasTransferRole && !managedWriteDisabledReason && !refreshing && !permissionError;

  const filtered = useMemo(() => {
    const list = members || [];
    const query = filter.trim().toLowerCase();
    if (!query) return list;
    return list.filter((member) =>
      [member.username, member.display_name, member.email].some((field) =>
        field?.toLowerCase().includes(query),
      ),
    );
  }, [members, filter]);

  async function confirmRevoke() {
    if (!name || !pendingRevoke || !active.current) return;
    if (!canManage) {
      throw new Error(membershipDisabledReason || "An Admin or Owner role is required to revoke membership.");
    }
    await revokeAccess(name, {
      id: pendingRevoke.id,
      username: pendingRevoke.username,
    });
    if (!active.current) return;
    setMembers(current => current?.filter(member => member.id !== pendingRevoke.id) ?? null);
    setNotice(`Access revoked for ${pendingRevoke.username}.`);
    await refresh();
  }

  async function confirmTransfer() {
    if (!name || !pendingTransfer || !active.current) return;
    if (!canTransfer) {
      throw new Error(managedWriteDisabledReason || "An Owner role is required to transfer ownership.");
    }
    await transferOwnership(name, {
      id: pendingTransfer.id,
      username: pendingTransfer.username,
    });
    if (!active.current) return;
    setInfo(null);
    setNotice(`Ownership transferred to ${pendingTransfer.username}.`);
    await refresh();
  }

  function handleRoleChanged(member: Member, previous: string, next: string) {
    if (!active.current) return;
    setMembers((current) =>
      current
        ? current.map((entry) =>
            entry.id === member.id
              ? { ...entry, role: next as Member["role"] }
              : entry,
          )
        : current,
    );
    setUndoTarget({ id: member.id, username: member.username, prev: previous, next });
    setTimeout(() => {
      if (!active.current) return;
      setUndoTarget((current) =>
        current && current.id === member.id && current.next === next
          ? null
          : current,
      );
    }, 5000);
    void refresh();
  }

  async function handleUndo() {
    if (!undoTarget || !name || !canManage || !active.current) return;
    const { id, username, prev } = undoTarget;
    setUndoTarget(null);
    setUndoError(null);
    try {
      await grantAccess(name, { id, username }, prev);
      await refresh();
    } catch (caught: unknown) {
      if (!active.current) return;
      setUndoError(errorMessage(caught, "Undo failed"));
    }
  }

  if (!name) return null;

  const total = members?.length ?? 0;
  const noMatches =
    members !== null && filter.trim() !== "" && filtered.length === 0;
  const publicNote = publicAccessNote(info);

  return (
    <>
      <div className="workspace-density-compact @container/members mx-auto w-full max-w-7xl">
        <h1 id="members-heading" className="sr-only">
          Members
        </h1>
        <WorkspaceSectionHeader
          id="member-roster-heading"
          icon={UsersRound}
          title="Members"
          tone="people"
          testId="member-roster-header" compact
          right={
            <>
              {members && !error && (
                <Badge variant="default">
                  {total} member{total === 1 ? "" : "s"}
                </Badge>
              )}
              {hasManageRole && (
                <Button
                  ref={inviteTriggerRef}
                  variant="accent"
                  size="sm"
                  disabled={!canManage}
                  title={membershipDisabledReason}
                  aria-describedby={
                    membershipDisabledReason ? "membership-restriction" : undefined
                  }
                  onClick={() => setInviteOpen(true)}
                >
                  <Plus className="h-3.5 w-3.5" aria-hidden />
                  Invite member
                </Button>
              )}
            </>
          }
        />

        {notice && <Alert variant="success" className="mb-3">{notice}</Alert>}
        {undoTarget && (
          <div
            role="status"
            className="mb-3 flex flex-wrap items-center gap-3 rounded-[var(--radius-md)] border border-border bg-surface px-3 py-2"
          >
            <CheckCircle2
              className="h-4 w-4 shrink-0 text-success"
              aria-hidden
            />
            <span className="text-ui text-foreground">
              Changed {undoTarget.username} from {undoTarget.prev} to{" "}
              {undoTarget.next}.
            </span>
            <Button variant="ghost" size="sm" onClick={handleUndo}>
              Undo
            </Button>
            <Button
              variant="ghost"
              size="icon"
              className="workspace-icon-control ml-auto"
              onClick={() => setUndoTarget(null)}
              aria-label="Dismiss role change message"
            >
              <X className="h-4 w-4" aria-hidden />
            </Button>
          </div>
        )}
        {undoError && (
          <Alert variant="destructive" className="mb-3">
            Undo failed: {undoError}
          </Alert>
        )}

        <Panel
          variant="workspace"
          role="region"
          aria-labelledby="members-heading"
          aria-busy={refreshing}
          inset={false}
          data-testid="members-workspace-frame"
          className="overflow-hidden"
        >
          {(total > FILTER_THRESHOLD || filter) && (
            <div
              data-testid="member-roster-controls"
              className="flex flex-wrap items-center justify-between gap-2 border-b border-border bg-surface px-4 py-3 @lg/members:px-5 @lg/members:py-2.5"
            >
              <div className="relative w-full @md/members:w-72">
                <Search
                  className="pointer-events-none absolute left-3 top-1/2 h-4 w-4 -translate-y-1/2 text-foreground-muted"
                  aria-hidden
                />
                <Input
                  type="search"
                  value={filter}
                  onChange={(event) => setFilter(event.target.value)}
                  placeholder="Filter by name or email…"
                  aria-label="Filter members"
                  className="bg-surface pl-9"
                />
              </div>
              {filter && (
                <span className="text-xs text-foreground-muted">
                  {filtered.length} of {total} members
                </span>
              )}
            </div>
          )}

          {refreshing && members !== null && <InlineLoadingState label="Updating members" className="px-4 py-3" />}
          {permissionError && <Alert variant="warning" className="m-4" title="Could not verify Vault permissions">{permissionError}<Button variant="outline" size="sm" loading={refreshing} onClick={() => void refresh()}>Retry permissions</Button></Alert>}
          {error && members !== null && <Alert variant="destructive" className="m-4" title="Could not update members">{error} Previous members are shown.<Button variant="outline" size="sm" loading={refreshing} onClick={() => void refresh()}>Try again</Button></Alert>}
          {error && members === null ? (
            <div className="p-4">
              <Alert variant="destructive" title="Failed to load members">
                {error}
                <div className="mt-3">
                  <Button
                    variant="outline"
                    size="sm"
                    loading={refreshing}
                    onClick={() => void refresh()}
                  >
                    Try again
                  </Button>
                </div>
              </Alert>
            </div>
          ) : members === null ? (
            <MemberListSkeleton />
          ) : members.length === 0 ? (
            <div className="p-4">
              <EmptyState
                title="No members on record"
                description="Even the owner should appear here. Refresh to try loading the roster again."
              />
            </div>
          ) : noMatches ? (
            <div className="p-4">
              <EmptyState
                title="No matching members"
                description={`No member matches "${filter.trim()}".`}
              />
              <div className="mt-4 flex justify-center">
                <Button
                  variant="outline"
                  size="sm"
                  onClick={() => setFilter("")}
                >
                  Clear filter
                </Button>
              </div>
            </div>
          ) : (
            <>
              <span className="sr-only" role="status" aria-live="polite">
                Showing {filtered.length} of {total} member
                {total === 1 ? "" : "s"}.
              </span>
              <table
                aria-label="Vault members"
                className="w-full table-fixed border-collapse"
              >
                <thead className="border-b border-border bg-surface-2/45 text-left text-xs text-foreground-muted">
                  <tr>
                    <th
                      scope="col"
                      className="px-4 py-2.5 font-medium @lg/members:px-5"
                    >
                      Member
                    </th>
                    <th
                      scope="col"
                      className="hidden w-32 px-4 py-2.5 font-medium @3xl/members:table-cell"
                    >
                      Joined
                    </th>
                    <th
                      scope="col"
                      className="w-24 px-2 py-0.5 text-right font-medium @lg/members:w-28"
                    >
                      <div className="flex items-center justify-end gap-1">
                        Role
                        <RolePermissions currentRole={info?.role} />
                      </div>
                    </th>
                    <th scope="col" className="w-12">
                      <span className="sr-only">Actions</span>
                    </th>
                  </tr>
                </thead>
                <tbody className="divide-y divide-border">
                  {filtered.map((member) => {
                    const isCurrent = currentUser?.id === member.id;
                    const canManageMember =
                      hasManageRole &&
                      member.role !== "owner" &&
                      currentUser &&
                      !isCurrent;
                    const canChangeRole = canManageMember && canManage;
                    return (
                      <tr
                        key={member.id}
                        className="transition-token hover:bg-surface-hover"
                      >
                        <td className="min-w-0 px-4 py-3 align-middle @lg/members:px-5 @lg/members:py-2.5">
                          <div className="flex min-w-0 items-center gap-2.5">
                            <span className="hidden @md/members:block">
                              <MemberAvatar member={member} />
                            </span>
                            <div className="min-w-0">
                              <div className="flex min-w-0 flex-wrap items-center gap-x-2 gap-y-0.5">
                                <span className="min-w-0 break-words text-ui font-medium text-foreground [overflow-wrap:anywhere]">
                                  {member.display_name?.trim() ||
                                    member.username}
                                </span>
                                {isCurrent && (
                                  <Badge variant="info-outline">You</Badge>
                                )}
                              </div>
                              <div className="mt-0.5 flex min-w-0 flex-wrap items-center gap-x-1.5 gap-y-0.5 text-xs leading-4 text-foreground-muted">
                                <span className="break-all">@{member.username}</span>
                                {member.email && (
                                  <>
                                    <span aria-hidden>·</span>
                                    <span className="break-all">{member.email}</span>
                                  </>
                                )}
                                {member.account_kind === "service" && (
                                  <Badge variant="default">Service account</Badge>
                                )}
                              </div>
                            </div>
                          </div>
                        </td>
                        <td className="hidden px-4 py-3 align-middle text-xs text-foreground-muted @lg/members:py-2.5 @3xl/members:table-cell">
                          {member.since ? (
                            <time
                              dateTime={member.since}
                              title={new Date(member.since).toLocaleString()}
                            >
                              {timeAgo(member.since)}
                            </time>
                          ) : (
                            "—"
                          )}
                        </td>
                        <td className="px-2 py-3 text-right align-middle @lg/members:py-2.5">
                          {canChangeRole ? (
                            <RoleSelect
                              vault={name}
                              member={member}
                              className="min-h-9 min-w-20 justify-center rounded-[var(--radius-sm)] border-border bg-surface text-xs text-foreground hover:bg-surface-hover"
                              onChanged={(previous, updated) =>
                                handleRoleChanged(member, previous, updated)
                              }
                            />
                          ) : canManageMember && membershipDisabledReason ? (
                            <Button
                              variant="ghost"
                              size="sm"
                              disabled
                              aria-label={`Change role for ${member.username}`}
                              aria-describedby="membership-restriction"
                              title={membershipDisabledReason}
                            >
                              {ROLE_LABELS[member.role]}
                            </Button>
                          ) : (
                            <span className="inline-flex min-w-20 justify-center text-xs font-medium text-foreground">
                              {ROLE_LABELS[member.role]}
                            </span>
                          )}
                        </td>
                        <td className="py-3 pr-3 text-right align-middle @lg/members:py-2.5">
                          {canManageMember && (
                            <MemberActionsMenu
                              member={member}
                              onTransfer={
                                hasTransferRole
                                  ? () => setPendingTransfer(member)
                                  : undefined
                              }
                              transferDisabledReason={managedWriteDisabledReason}
                              onRevoke={() => setPendingRevoke(member)}
                              revokeDisabledReason={membershipDisabledReason}
                            />
                          )}
                        </td>
                      </tr>
                    );
                  })}
                </tbody>
              </table>
            </>
          )}
          {members && !error && (publicNote || !canManage) && (
            <div className="space-y-2 border-t border-border px-3 py-3 text-xs leading-relaxed text-foreground-muted @lg/members:px-4">
              {publicNote && (
                <p className="flex items-start gap-2">
                  <Globe className="mt-0.5 h-4 w-4 shrink-0" aria-hidden />
                  <span>{publicNote}</span>
                </p>
              )}
              {!canManage && (
                <p className="flex items-start gap-2">
                  <UserCog className="mt-0.5 h-4 w-4 shrink-0" aria-hidden />
                  <span id="membership-restriction">
                    {membershipDisabledReason
                      ? info?.is_archived
                        ? `${membershipDisabledReason}. Restore the Vault to invite members, change roles, or revoke membership.`
                        : membershipDisabledReason
                      : `The roster is read-only for your ${info?.role || "current"} role.`}
                  </span>
                </p>
              )}
            </div>
          )}
        </Panel>
      </div>

      <InviteMemberDialog
        open={inviteOpen && canManage}
        onOpenChange={setInviteOpen}
        vault={name}
        existingUserIds={existingUserIds}
        onInvited={refresh}
        returnFocusRef={inviteTriggerRef}
      />
      <ConfirmDialog
        open={pendingRevoke !== null}
        onOpenChange={(open) => !open && setPendingRevoke(null)}
        title={pendingRevoke ? `Revoke ${memberIdentity(pendingRevoke)}?` : ""}
        description={
          pendingRevoke
            ? revokeDescription(memberIdentity(pendingRevoke), name, info)
            : ""
        }
        confirmLabel="Revoke access"
        variant="destructive"
        onConfirm={confirmRevoke}
      />
      <ConfirmDialog
        open={pendingTransfer !== null}
        onOpenChange={(open) => !open && setPendingTransfer(null)}
        title={
          pendingTransfer
            ? `Transfer ownership to ${memberIdentity(pendingTransfer)}?`
            : ""
        }
        description={
          pendingTransfer
            ? `You will become an admin and ${memberIdentity(pendingTransfer)} becomes the new owner.\nOnly the new owner can transfer ownership again.\nThis cannot be undone by you alone.`
            : ""
        }
        confirmLabel="Transfer ownership"
        variant="destructive"
        onConfirm={confirmTransfer}
      />
    </>
  );
}

function memberIdentity(member: Member): string {
  const displayName = member.display_name?.trim();
  return displayName ? `${displayName} (@${member.username})` : `@${member.username}`;
}

function MemberAvatar({ member }: { member: Member }) {
  const label = member.display_name?.trim() || member.username;
  return (
    <span
      aria-hidden
      className="flex h-7 w-7 shrink-0 items-center justify-center rounded-full border border-border bg-surface-2 text-xs font-medium text-foreground"
    >
      {initialsFor(label)}
    </span>
  );
}

function MemberActionsMenu({
  member,
  onTransfer,
  onRevoke,
  revokeDisabledReason,
  transferDisabledReason,
}: {
  member: Member;
  onTransfer?: () => void;
  onRevoke: () => void;
  revokeDisabledReason?: string;
  transferDisabledReason?: string;
}) {
  return (
    <DropdownMenu.Root>
      <DropdownMenu.Trigger asChild>
        <Button
          type="button"
          variant="ghost"
          size="icon"
          aria-label={`More actions for ${member.username}`}
          className="workspace-icon-control"
        >
          <MoreHorizontal className="h-4 w-4" aria-hidden />
        </Button>
      </DropdownMenu.Trigger>
      <DropdownMenu.Portal>
        <DropdownMenu.Content
          align="end"
          sideOffset={6}
          className="z-[var(--z-popover)] min-w-44 overflow-hidden rounded-[var(--radius-md)] border border-border bg-surface p-1 shadow-md"
        >
          {onTransfer && (
            <DropdownMenu.Item
              onSelect={onTransfer}
              disabled={Boolean(transferDisabledReason)}
              title={transferDisabledReason}
              className="flex cursor-pointer select-none items-center gap-2 rounded-[var(--radius-sm)] px-3 py-2 text-sm text-foreground outline-none data-[highlighted]:bg-surface-hover data-[disabled]:cursor-not-allowed data-[disabled]:opacity-50"
            >
              <Crown className="h-4 w-4 text-foreground-muted" aria-hidden />
              Transfer ownership
            </DropdownMenu.Item>
          )}
          <DropdownMenu.Item
            onSelect={onRevoke}
            disabled={Boolean(revokeDisabledReason)}
            title={revokeDisabledReason}
            aria-describedby={revokeDisabledReason ? "membership-restriction" : undefined}
            className="flex cursor-pointer select-none items-center gap-2 rounded-[var(--radius-sm)] px-3 py-2 text-sm text-destructive outline-none data-[highlighted]:bg-destructive-soft data-[disabled]:cursor-not-allowed data-[disabled]:opacity-50"
          >
            <Trash2 className="h-4 w-4" aria-hidden />
            Revoke access
          </DropdownMenu.Item>
        </DropdownMenu.Content>
      </DropdownMenu.Portal>
    </DropdownMenu.Root>
  );
}

function MemberListSkeleton() {
  return (
    <div
      role="status"
      aria-label="Loading members"
      className="divide-y divide-border"
    >
      {[0, 1, 2, 3].map((index) => (
        <div
          key={index}
          className="flex items-center gap-2.5 px-4 py-3 @lg/members:px-5 @lg/members:py-2.5"
          aria-hidden
        >
          <span className="h-7 w-7 animate-pulse rounded-full bg-surface-2" />
          <div className="flex-1 space-y-2">
            <span className="block h-3 w-32 animate-pulse rounded bg-surface-2" />
            <span className="block h-3 w-48 animate-pulse rounded bg-surface-2" />
          </div>
          <span className="h-7 w-20 animate-pulse rounded-full bg-surface-2" />
        </div>
      ))}
    </div>
  );
}

function RolePermissions({ currentRole }: { currentRole?: Role }) {
  return (
    <Dialog>
      <DialogTrigger asChild>
        <Button
          variant="ghost"
          size="icon"
          aria-label="Role permissions"
          title="Role permissions"
          className="workspace-icon-control h-11 w-11 text-foreground-muted @lg/members:h-9 @lg/members:w-9"
        >
          <CircleHelp className="h-4 w-4" aria-hidden />
        </Button>
      </DialogTrigger>
      <DialogContent>
        <div className="space-y-1.5 pr-6">
          <DialogTitle>Role permissions</DialogTitle>
          <DialogDescription>
            Roles apply across the vault. Highest access first.
          </DialogDescription>
        </div>
        <ol
          aria-label="Roles from highest to lowest access"
          className="divide-y divide-border"
        >
          {ROLE_ORDER.map((role) => {
            const Icon = ROLE_ICONS[role];
            const current = currentRole === role;
            return (
              <li
                key={role}
                aria-current={current ? "true" : undefined}
                className="flex items-start gap-3 py-3"
              >
                <Icon
                  className="mt-0.5 h-4 w-4 shrink-0 text-foreground-muted"
                  aria-hidden
                />
                <div>
                  <div className="flex flex-wrap items-center gap-2">
                    <span className="text-sm font-semibold">
                      {ROLE_LABELS[role]}
                    </span>
                    {current && <Badge variant="info-outline">Your role</Badge>}
                  </div>
                  <p className="mt-1 text-sm text-foreground-muted">
                    {ROLE_CAPABILITIES[role]}
                  </p>
                </div>
              </li>
            );
          })}
        </ol>
      </DialogContent>
    </Dialog>
  );
}

function publicAccessNote(info: VaultInfo | null): string | null {
  if (info?.public_access === "writer") {
    if (info.is_archived || info.is_external_git) {
      return "People not listed here can also read this vault when signed in. Content is read-only.";
    }
    return info.managed_by
      ? "People not listed here can also read this vault when signed in. Content writes require a granted service token."
      : "People not listed here can also read and change content when signed in.";
  }
  if (info?.public_access === "reader") {
    return "People not listed here can also read this vault when signed in.";
  }
  return null;
}

function revokeDescription(username: string, vault: string, info: VaultInfo | null): string {
  const remaining = info?.public_access === "reader"
    ? "They can still read this Vault when signed in under its public access policy."
    : info?.public_access === "writer"
      ? info.is_archived || info.is_external_git
        ? "They can still read this Vault when signed in under its public access policy. Content is read-only in this Vault."
        : info.managed_by
          ? "They can still read this Vault when signed in under its public access policy. Content writes require a granted service token."
          : "They can still read and change content when signed in under this Vault's public access policy."
      : info?.public_access === "none"
        ? "This Vault does not grant public access to signed-in non-members."
        : "The public access policy is unknown, so remaining access cannot be confirmed.";
  return `${username}'s member permissions on ${vault} will be removed.\n${remaining}\nMembership can be granted again later.`;
}

function initialsFor(label: string): string {
  const parts = label.trim().split(/\s+/).filter(Boolean);
  if (parts.length > 1) {
    return `${parts[0][0]}${parts[parts.length - 1][0]}`.toUpperCase();
  }
  return label.trim().slice(0, 2).toUpperCase();
}

function errorMessage(error: unknown, fallback: string): string {
  return error instanceof Error && error.message ? error.message : fallback;
}
