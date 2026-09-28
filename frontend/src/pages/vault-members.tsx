import { useCallback, useEffect, useMemo, useState } from "react";
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

interface Member {
  username: string;
  display_name?: string | null;
  email: string;
  role: Role;
  since?: string | null;
}

interface VaultInfo {
  name: string;
  description?: string;
  role?: Role;
  role_source?: "member" | "public";
  public_access?: "none" | "reader" | "writer";
  is_archived?: boolean;
  is_external_git?: boolean;
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
  const [info, setInfo] = useState<VaultInfo | null>(null);
  const [members, setMembers] = useState<Member[] | null>(null);
  const [error, setError] = useState("");
  const [filter, setFilter] = useState("");
  const [inviteOpen, setInviteOpen] = useState(false);
  const [pendingRevoke, setPendingRevoke] = useState<Member | null>(null);
  const [pendingTransfer, setPendingTransfer] = useState<Member | null>(null);
  const [currentUser, setCurrentUser] = useState<{ username: string } | null>(
    null,
  );
  const [undoTarget, setUndoTarget] = useState<{
    username: string;
    prev: string;
    next: string;
  } | null>(null);
  const [undoError, setUndoError] = useState<string | null>(null);

  useEffect(() => {
    getMe()
      .then((user) => setCurrentUser({ username: user.username }))
      .catch(() => setCurrentUser(null));
  }, []);

  const refresh = useCallback(async () => {
    if (!name) return;
    try {
      const [nextInfo, nextMembers] = await Promise.all([
        getVaultInfo(name).catch(() => null),
        getVaultMembers(name),
      ]);
      setInfo(nextInfo);
      setMembers(nextMembers.members || []);
      setError("");
    } catch (caught: unknown) {
      setError(errorMessage(caught, "Failed to load members"));
      setMembers([]);
    }
  }, [name]);

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

  const existingUsernames = useMemo(
    () => new Set((members || []).map((member) => member.username)),
    [members],
  );
  const canManage = info?.role === "owner" || info?.role === "admin";
  const canTransfer = info?.role === "owner";

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
    if (!name || !pendingRevoke) return;
    await revokeAccess(name, pendingRevoke.username);
    await refresh();
  }

  async function confirmTransfer() {
    if (!name || !pendingTransfer) return;
    await transferOwnership(name, pendingTransfer.username);
    await refresh();
  }

  function handleRoleChanged(member: Member, previous: string, next: string) {
    setMembers((current) =>
      current
        ? current.map((entry) =>
            entry.username === member.username
              ? { ...entry, role: next as Member["role"] }
              : entry,
          )
        : current,
    );
    setUndoTarget({ username: member.username, prev: previous, next });
    setTimeout(() => {
      setUndoTarget((current) =>
        current && current.username === member.username && current.next === next
          ? null
          : current,
      );
    }, 5000);
    void refresh();
  }

  async function handleUndo() {
    if (!undoTarget || !name) return;
    const { username, prev } = undoTarget;
    setUndoTarget(null);
    setUndoError(null);
    try {
      await grantAccess(name, username, prev);
      await refresh();
    } catch (caught: unknown) {
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
      <div className="@container/members w-full max-w-none">
        <h1 id="members-heading" className="sr-only">
          Members
        </h1>
        <WorkspaceSectionHeader
          id="member-roster-heading"
          icon={UsersRound}
          title="Members"
          tone="people"
          testId="member-roster-header"
          right={
            <>
              {members && !error && (
                <Badge variant="default">
                  {total} member{total === 1 ? "" : "s"}
                </Badge>
              )}
              {canManage && (
                <Button
                  variant="accent"
                  size="sm"
                  onClick={() => setInviteOpen(true)}
                >
                  <Plus className="h-3.5 w-3.5" aria-hidden />
                  Invite member
                </Button>
              )}
            </>
          }
        />

        {undoTarget && (
          <div
            role="status"
            className="mb-3 flex flex-wrap items-center gap-3 rounded-[var(--radius-md)] border border-border bg-surface px-3 py-2"
          >
            <CheckCircle2
              className="h-4 w-4 shrink-0 text-success"
              aria-hidden
            />
            <span className="text-sm text-foreground">
              Changed {undoTarget.username} from {undoTarget.prev} to{" "}
              {undoTarget.next}.
            </span>
            <Button variant="ghost" size="sm" onClick={handleUndo}>
              Undo
            </Button>
            <Button
              variant="ghost"
              size="icon"
              className="ml-auto"
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
          inset={false}
          data-testid="members-workspace-frame"
          className="overflow-hidden"
        >
          {(total > FILTER_THRESHOLD || filter) && (
            <div
              data-testid="member-roster-controls"
              className="flex flex-wrap items-center justify-between gap-3 border-b border-border bg-surface px-3 py-2.5 @lg/members:px-4"
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

          {error ? (
            <div className="p-4">
              <Alert variant="destructive" title="Failed to load members">
                {error}
                <div className="mt-3">
                  <Button
                    variant="outline"
                    size="sm"
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
                      className="px-3 py-2.5 font-medium @lg/members:px-4"
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
                    const isCurrent = currentUser?.username === member.username;
                    const canChangeRole =
                      canManage &&
                      member.role !== "owner" &&
                      currentUser &&
                      !isCurrent;
                    return (
                      <tr
                        key={member.username}
                        className="transition-token hover:bg-surface-hover"
                      >
                        <td className="min-w-0 px-3 py-3 align-middle @lg/members:px-4">
                          <div className="flex min-w-0 items-center gap-3">
                            <span className="hidden @md/members:block">
                              <MemberAvatar member={member} />
                            </span>
                            <div className="min-w-0">
                              <div className="flex min-w-0 flex-wrap items-center gap-x-2 gap-y-0.5">
                                <span className="min-w-0 break-words text-sm font-semibold text-foreground [overflow-wrap:anywhere]">
                                  {member.display_name?.trim() ||
                                    member.username}
                                </span>
                                {isCurrent && (
                                  <Badge variant="info-outline">You</Badge>
                                )}
                              </div>
                              <p className="mt-0.5 break-words text-xs leading-relaxed text-foreground-muted [overflow-wrap:anywhere]">
                                {member.email || `@${member.username}`}
                              </p>
                            </div>
                          </div>
                        </td>
                        <td className="hidden px-4 py-3 align-middle text-xs text-foreground-muted @3xl/members:table-cell">
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
                        <td className="px-2 py-3 text-right align-middle">
                          {canChangeRole ? (
                            <RoleSelect
                              vault={name}
                              member={member}
                              className="min-h-9 min-w-20 justify-center rounded-[var(--radius-sm)] border-border bg-surface text-xs text-foreground hover:bg-surface-hover"
                              onChanged={(previous, updated) =>
                                handleRoleChanged(member, previous, updated)
                              }
                            />
                          ) : (
                            <span className="inline-flex min-w-20 justify-center text-xs font-medium text-foreground">
                              {ROLE_LABELS[member.role]}
                            </span>
                          )}
                        </td>
                        <td className="py-3 pr-2 text-right align-middle">
                          {canChangeRole && (
                            <MemberActionsMenu
                              member={member}
                              onTransfer={
                                canTransfer
                                  ? () => setPendingTransfer(member)
                                  : undefined
                              }
                              onRevoke={() => setPendingRevoke(member)}
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
                  <span>
                    The roster is read-only for your {info?.role || "current"}{" "}
                    role.
                  </span>
                </p>
              )}
            </div>
          )}
        </Panel>
      </div>

      <InviteMemberDialog
        open={inviteOpen}
        onOpenChange={setInviteOpen}
        vault={name}
        existingUsernames={existingUsernames}
        onInvited={refresh}
      />
      <ConfirmDialog
        open={pendingRevoke !== null}
        onOpenChange={(open) => !open && setPendingRevoke(null)}
        title={pendingRevoke ? `Revoke ${pendingRevoke.username}?` : ""}
        description={
          pendingRevoke
            ? `${pendingRevoke.username} will lose access to ${name} immediately.\nThis can be re-granted later.`
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
            ? `Transfer ownership to ${pendingTransfer.username}?`
            : ""
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
    </>
  );
}

function MemberAvatar({ member }: { member: Member }) {
  const label = member.display_name?.trim() || member.username;
  return (
    <span
      aria-hidden
      className="flex h-8 w-8 shrink-0 items-center justify-center rounded-full border border-border bg-surface-2 text-xs font-semibold text-foreground"
    >
      {initialsFor(label)}
    </span>
  );
}

function MemberActionsMenu({
  member,
  onTransfer,
  onRevoke,
}: {
  member: Member;
  onTransfer?: () => void;
  onRevoke: () => void;
}) {
  return (
    <DropdownMenu.Root>
      <DropdownMenu.Trigger asChild>
        <Button
          type="button"
          variant="ghost"
          size="icon"
          aria-label={`More actions for ${member.username}`}
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
              className="flex cursor-pointer select-none items-center gap-2 rounded-[var(--radius-sm)] px-3 py-2 text-sm text-foreground outline-none data-[highlighted]:bg-surface-hover"
            >
              <Crown className="h-4 w-4 text-foreground-muted" aria-hidden />
              Transfer ownership
            </DropdownMenu.Item>
          )}
          <DropdownMenu.Item
            onSelect={onRevoke}
            className="flex cursor-pointer select-none items-center gap-2 rounded-[var(--radius-sm)] px-3 py-2 text-sm text-destructive outline-none data-[highlighted]:bg-destructive-soft"
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
          className="flex items-center gap-3 px-4 py-3 lg:px-5"
          aria-hidden
        >
          <span className="h-8 w-8 animate-pulse rounded-full bg-surface-2" />
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
          className="h-11 w-11 text-foreground-muted @lg/members:h-9 @lg/members:w-9"
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
    return info.is_archived || info.is_external_git
      ? "People not listed here can also read this vault when signed in. Content is read-only."
      : "People not listed here can also read and change content when signed in.";
  }
  if (info?.public_access === "reader") {
    return "People not listed here can also read this vault when signed in.";
  }
  return null;
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
