import { useCallback, useEffect, useMemo, useState } from "react";
import { Link, useParams } from "react-router-dom";
import * as DropdownMenu from "@radix-ui/react-dropdown-menu";
import {
  ArrowLeft,
  CheckCircle2,
  Crown,
  Globe,
  Lock,
  MoreHorizontal,
  Plus,
  Search,
  ShieldCheck,
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
import { RoleBadge } from "@/components/status-badge";
import { TooltipText } from "@/components/ui/tooltip-text";
import { TonalIcon } from "@/components/ui/tonal-icon";
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
      if (nextInfo) setInfo(nextInfo);
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
  const canChangePublicAccess = info?.role === "owner";

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

  const roleCounts = useMemo(() => {
    const counts: Record<Role, number> = {
      owner: 0,
      admin: 0,
      writer: 0,
      reader: 0,
    };
    for (const member of members || []) counts[member.role] += 1;
    return counts;
  }, [members]);

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
  const policy = publicPolicy(info?.public_access);

  return (
    <>
      <div className="flex min-h-full w-full flex-col xl:h-full xl:min-h-0 xl:p-4 2xl:p-5">
        <h1 id="members-heading" className="sr-only">
          Members
        </h1>

        <div
          data-testid="members-workspace-frame"
          className="grid min-h-0 flex-1 xl:grid-cols-[minmax(0,1fr)_20rem] xl:overflow-hidden xl:rounded-[var(--radius-md)] xl:border xl:border-border 2xl:grid-cols-[minmax(0,1fr)_22rem]"
        >
          <main className="rail-scroll rail-scroll-auto min-w-0 p-3 sm:p-4 lg:p-5 xl:overflow-y-auto xl:p-0">
            {info?.role_source === "public" && (
              <Alert
                variant="info"
                className="mb-3 shrink-0 xl:m-0 xl:rounded-none xl:border-x-0 xl:border-t-0"
              >
                <div className="flex flex-wrap items-center gap-2">
                  <span>
                    Your current role comes from this vault&apos;s public access
                    policy.
                  </span>
                  <Badge variant="info-outline">
                    <Globe className="h-3 w-3" aria-hidden />
                    Public {info.role}
                  </Badge>
                </div>
              </Alert>
            )}

            {undoTarget && (
              <div
                role="status"
                className="mb-3 flex shrink-0 items-center gap-3 rounded-[var(--radius-md)] border border-border bg-surface-2 px-3 py-2"
              >
                <CheckCircle2
                  className="h-4 w-4 shrink-0 text-success"
                  aria-hidden
                />
                <span className="text-sm text-foreground">
                  Changed {undoTarget.username} from {undoTarget.prev} to{" "}
                  {undoTarget.next}.
                </span>
                <button
                  type="button"
                  onClick={handleUndo}
                  className="inline-flex min-h-8 items-center rounded-[var(--radius-sm)] text-xs font-medium text-link hover:text-link-hover hover:underline focus:outline-none focus-visible:ring-2 focus-visible:ring-ring"
                >
                  Undo
                </button>
                <button
                  type="button"
                  onClick={() => setUndoTarget(null)}
                  aria-label="Dismiss role change message"
                  className="ml-auto inline-flex h-8 w-8 items-center justify-center rounded-[var(--radius-sm)] text-foreground-muted hover:bg-surface-hover hover:text-foreground focus:outline-none focus-visible:ring-2 focus-visible:ring-ring"
                >
                  <X className="h-4 w-4" aria-hidden />
                </button>
              </div>
            )}
            {undoError && (
              <Alert variant="destructive" className="mb-3 shrink-0">
                Undo failed: {undoError}
              </Alert>
            )}

            <Panel
              variant="workspace"
              role="region"
              aria-labelledby="members-heading"
              inset={false}
              className="xl:rounded-none xl:border-x-0 xl:border-t-0 xl:shadow-none"
            >
              <div
                data-testid="member-roster-header"
                className="flex min-h-14 flex-wrap items-center gap-3 border-b border-border-strong bg-surface-2/55 px-4 py-2.5 lg:px-5"
              >
                <TonalIcon tone="people" size="sm">
                  <UsersRound aria-hidden />
                </TonalIcon>
                <div className="flex min-w-0 flex-1 flex-wrap items-center gap-x-2 gap-y-1 text-xs text-foreground-muted">
                  <h2 className="text-sm font-semibold text-foreground">
                    Direct access
                  </h2>
                  {members && (
                    <Badge variant="default">
                      {total} member{total === 1 ? "" : "s"}
                    </Badge>
                  )}
                  <span aria-hidden>·</span>
                  <span>
                    {members === null ? "Loading roster…" : policy.summary}
                  </span>
                </div>

                <div className="ml-auto flex flex-wrap items-center gap-2">
                  {total > FILTER_THRESHOLD && (
                    <div className="relative w-56 max-w-full">
                      <Search
                        className="pointer-events-none absolute left-3 top-1/2 h-4 w-4 -translate-y-1/2 text-foreground-muted"
                        aria-hidden
                      />
                      <Input
                        type="search"
                        value={filter}
                        onChange={(event) => setFilter(event.target.value)}
                        placeholder="Filter members…"
                        aria-label="Filter members"
                        className="bg-surface pl-9"
                      />
                    </div>
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
                </div>
              </div>

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
                    {filter.trim()
                      ? `${filtered.length} of ${total} member${total === 1 ? "" : "s"} shown`
                      : `${total} member${total === 1 ? "" : "s"}`}
                  </span>
                  <div className="overflow-x-auto">
                    <table
                      aria-label="Vault members"
                      className="w-full table-fixed border-collapse"
                    >
                      <thead className="bg-surface-2/60 text-left text-xs font-medium text-foreground-muted">
                        <tr className="border-b border-border">
                          <th
                            scope="col"
                            className="px-4 py-2.5 font-medium lg:px-5"
                          >
                            Member
                          </th>
                          <th
                            scope="col"
                            className="hidden w-64 px-4 py-2.5 font-medium md:table-cell lg:px-5"
                          >
                            Contact
                          </th>
                          <th
                            scope="col"
                            className="hidden w-32 px-4 py-2.5 font-medium lg:table-cell lg:px-5"
                          >
                            Joined
                          </th>
                          <th
                            scope="col"
                            className="w-44 px-4 py-2.5 text-right font-medium lg:px-5"
                          >
                            Access
                          </th>
                        </tr>
                      </thead>
                      <tbody className="divide-y divide-border">
                        {filtered.map((member) => {
                          const isCurrent =
                            currentUser?.username === member.username;
                          const canChangeRole =
                            canManage &&
                            member.role !== "owner" &&
                            currentUser &&
                            !isCurrent;
                          const showActions =
                            canManage && member.role !== "owner" && !isCurrent;
                          return (
                            <tr
                              key={member.username}
                              className="transition-token hover:bg-surface-hover"
                            >
                              <td className="min-w-0 px-4 py-3 align-middle lg:px-5">
                                <div className="flex min-w-0 items-center gap-3">
                                  <MemberAvatar member={member} />
                                  <div className="min-w-0">
                                    <div className="flex min-w-0 flex-wrap items-center gap-2">
                                      <TooltipText className="truncate text-sm font-semibold text-foreground">
                                        {member.display_name?.trim() ||
                                          member.username}
                                      </TooltipText>
                                      {isCurrent && (
                                        <Badge variant="info-outline">
                                          You
                                        </Badge>
                                      )}
                                    </div>
                                    <div className="mt-0.5 flex min-w-0 flex-wrap items-center gap-x-2 text-xs text-foreground-muted">
                                      <TooltipText className="truncate">
                                        @{member.username}
                                      </TooltipText>
                                      <span className="md:hidden" aria-hidden>
                                        ·
                                      </span>
                                      <TooltipText className="truncate md:hidden">
                                        {member.email}
                                      </TooltipText>
                                      {member.since && (
                                        <>
                                          <span
                                            className="lg:hidden"
                                            aria-hidden
                                          >
                                            ·
                                          </span>
                                          <span className="whitespace-nowrap lg:hidden">
                                            Joined {timeAgo(member.since)}
                                          </span>
                                        </>
                                      )}
                                    </div>
                                  </div>
                                </div>
                              </td>
                              <td className="hidden min-w-0 px-4 py-3 align-middle md:table-cell lg:px-5">
                                <TooltipText className="block truncate text-sm text-foreground-muted">
                                  {member.email}
                                </TooltipText>
                              </td>
                              <td className="hidden px-4 py-3 align-middle text-xs text-foreground-muted lg:table-cell lg:px-5">
                                {member.since ? timeAgo(member.since) : "—"}
                              </td>
                              <td className="px-4 py-3 align-middle lg:px-5">
                                <div className="flex items-center justify-end gap-2">
                                  {canChangeRole ? (
                                    <RoleSelect
                                      vault={name}
                                      member={member}
                                      onChanged={(previous, next) =>
                                        handleRoleChanged(
                                          member,
                                          previous,
                                          next,
                                        )
                                      }
                                    />
                                  ) : (
                                    <RoleBadge role={member.role} />
                                  )}
                                  {showActions && (
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
                                </div>
                              </td>
                            </tr>
                          );
                        })}
                      </tbody>
                    </table>
                  </div>
                </>
              )}
            </Panel>

            {!canManage && members && (
              <Alert variant="info" className="mt-3 shrink-0 xl:m-4">
                <UserCog className="h-4 w-4" aria-hidden />
                The roster is read-only for your {info?.role || "current"} role.
              </Alert>
            )}
          </main>

          <aside
            aria-label="Member access context"
            className="min-w-0 border-t border-border bg-surface xl:border-l xl:border-t-0"
          >
            <div
              data-testid="member-access-panel"
              className="border-b border-border-strong bg-surface"
            >
              <div
                data-testid="member-access-header"
                className="flex h-14 items-center justify-between gap-3 border-b border-border-strong bg-surface-2/55 px-4"
              >
                <div className="flex min-w-0 items-center gap-2.5">
                  <TonalIcon tone="info" size="sm">
                    <ShieldCheck aria-hidden />
                  </TonalIcon>
                  <div className="min-w-0">
                    <h2 className="text-sm font-semibold text-foreground">
                      Access model
                    </h2>
                    <p className="truncate text-xs text-foreground-muted">
                      Roles and vault-wide policy
                    </p>
                  </div>
                </div>
                {info?.role && <RoleBadge role={info.role} />}
              </div>

              <section
                aria-labelledby="role-ladder-heading"
                className="border-b border-border"
              >
                <div
                  data-testid="role-ladder-header"
                  className="flex min-h-10 items-center justify-between gap-3 border-b border-border-strong bg-surface-2/40 px-4 py-2"
                >
                  <h3
                    id="role-ladder-heading"
                    className="text-xs font-semibold text-foreground"
                  >
                    Role ladder
                  </h3>
                  <span className="text-xs text-foreground-muted">
                    Highest to lowest
                  </span>
                </div>
                <ol
                  aria-label="Roles from highest to lowest access"
                  className="divide-y divide-border"
                >
                  {ROLE_ORDER.map((role) => {
                    const Icon = ROLE_ICONS[role];
                    const current = info?.role === role;
                    return (
                      <li
                        key={role}
                        aria-current={current ? "true" : undefined}
                        className={
                          current
                            ? "border-l-2 border-primary bg-surface-selected px-4 py-2.5"
                            : "border-l-2 border-transparent px-4 py-2.5"
                        }
                      >
                        <div className="flex items-start gap-3">
                          <span className="flex h-7 w-7 shrink-0 items-center justify-center rounded-[var(--radius-sm)] bg-surface-2 text-foreground-muted">
                            <Icon className="h-3.5 w-3.5" aria-hidden />
                          </span>
                          <div className="min-w-0 flex-1">
                            <div className="flex flex-wrap items-center gap-2">
                              <span className="text-sm font-semibold text-foreground">
                                {ROLE_LABELS[role]}
                              </span>
                              {current && (
                                <Badge variant="owner">Your role</Badge>
                              )}
                              <span className="ml-auto text-xs tabular-nums text-foreground-muted">
                                {roleCounts[role]}
                              </span>
                            </div>
                            <p className="mt-1 text-xs leading-relaxed text-foreground-muted">
                              {ROLE_CAPABILITIES[role]}
                            </p>
                          </div>
                        </div>
                      </li>
                    );
                  })}
                </ol>
              </section>

              <section aria-labelledby="public-access-heading">
                <div
                  data-testid="public-access-header"
                  className="flex min-h-10 items-center justify-between gap-3 border-b border-border-strong bg-surface-2/40 px-4 py-2"
                >
                  <h3
                    id="public-access-heading"
                    className="text-xs font-semibold text-foreground"
                  >
                    Public access
                  </h3>
                  <Badge
                    variant={canChangePublicAccess ? "info-outline" : "outline"}
                  >
                    {canChangePublicAccess ? "Editable" : "View only"}
                  </Badge>
                </div>
                <div className="p-4">
                  <div className="flex min-w-0 items-start gap-3">
                    <TonalIcon
                      tone={
                        info?.public_access === "writer"
                          ? "warning"
                          : info?.public_access === "reader"
                            ? "success"
                            : "info"
                      }
                    >
                      {policy.Icon === Lock ? (
                        <Lock className="h-4 w-4" aria-hidden />
                      ) : (
                        <Globe className="h-4 w-4" aria-hidden />
                      )}
                    </TonalIcon>
                    <div className="min-w-0">
                      <h4 className="text-sm font-semibold text-foreground">
                        {policy.label}
                      </h4>
                      <p className="mt-1 text-xs leading-relaxed text-foreground-muted">
                        {policy.description}
                      </p>
                    </div>
                  </div>
                </div>
                {canChangePublicAccess ? (
                  <Link
                    to={`/vault/${name}/settings#access`}
                    className="flex min-h-10 items-center justify-between gap-3 border-t border-border px-4 text-xs font-medium text-link transition-token hover:bg-surface-hover hover:text-link-hover focus:outline-none focus-visible:ring-2 focus-visible:ring-inset focus-visible:ring-ring"
                  >
                    Change in Settings
                    <ArrowLeft className="h-4 w-4 rotate-180" aria-hidden />
                  </Link>
                ) : (
                  <div className="flex min-h-10 items-start gap-2 border-t border-border bg-surface-2 px-4 py-3 text-xs leading-relaxed text-foreground-muted">
                    <ShieldCheck
                      className="mt-0.5 h-3.5 w-3.5 shrink-0"
                      aria-hidden
                    />
                    <span>
                      {info?.role_source === "public"
                        ? "Your role comes from this policy; only the vault owner can change it."
                        : "Only the vault owner can change public access."}
                    </span>
                  </div>
                )}
              </section>
            </div>
          </aside>
        </div>
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

function publicPolicy(access: VaultInfo["public_access"]) {
  if (access === "writer") {
    return {
      label: "Public write",
      summary: "public write enabled",
      description:
        "Any signed-in person with the link can read and change content.",
      Icon: Globe,
    };
  }
  if (access === "reader") {
    return {
      label: "Public read",
      summary: "public read enabled",
      description: "Any signed-in person with the link can read this vault.",
      Icon: Globe,
    };
  }
  return {
    label: "Private",
    summary: "private vault",
    description: "Only people listed on this page can open the vault.",
    Icon: Lock,
  };
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
