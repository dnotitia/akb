import { Activity, useEffect, useMemo, useState } from "react";
import * as DropdownMenu from "@radix-ui/react-dropdown-menu";
import { Key, Loader2, Search, Trash2 } from "lucide-react";
import { adminDeleteUser, type AdminUser } from "@/lib/api";
import { useDebounce } from "@/hooks/use-debounce";
import { formatDate } from "@/lib/utils";
import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { SelectMenu } from "@/components/ui/select-menu";
import { ConfirmDialog } from "@/components/ui/confirm-dialog";
import { RoleBadge } from "@/components/status-badge";
import { EmptyState } from "@/components/empty-state";
import { AdminResetPasswordDialog } from "@/components/admin-reset-password-dialog";
import { ResourceActionsMenu } from "@/components/resource-actions-menu";
import { TooltipText } from "@/components/ui/tooltip-text";
import { Panel } from "@/components/ui/panel";
import { Alert } from "@/components/ui/alert";
import { InlineLoadingState } from "@/components/ui/loading-state";
import type { User } from "./profile-section";

type AdminSort = "recent" | "oldest" | "username" | "vaults";

interface Props {
  user: User;
  users: AdminUser[] | null;
  usersError: boolean;
  usersLoading?: boolean;
  localPasswordEnabled: boolean;
  onReloadUsers: () => void | Promise<boolean>;
  onDirtyChange?: (dirty: boolean) => void;
  onBusyChange?: (busy: boolean) => void;
  ownsOperation?: () => boolean;
  visible?: boolean;
}

export function AdminSection({
  user,
  users,
  usersError,
  usersLoading = false,
  localPasswordEnabled,
  onReloadUsers,
  onDirtyChange,
  onBusyChange,
  ownsOperation,
  visible = true,
}: Props) {
  const [deletedIds, setDeletedIds] = useState<string[]>([]);
  const [notice, setNotice] = useState("");
  const [adminQuery, setAdminQuery] = useState("");
  const [adminSort, setAdminSort] = useState<AdminSort>("recent");
  const debouncedQuery = useDebounce(adminQuery, 200);
  const [deletingId, setDeletingId] = useState<string | null>(null);
  const [pendingDeleteUser, setPendingDeleteUser] = useState<AdminUser | null>(null);
  const [resetTarget, setResetTarget] = useState<AdminUser | null>(null);
  const [resetBusy, setResetBusy] = useState(false);
  useEffect(() => { onDirtyChange?.(resetTarget !== null); }, [resetTarget, onDirtyChange]);
  useEffect(() => { onBusyChange?.(resetBusy || deletingId !== null); }, [resetBusy, deletingId, onBusyChange]);
  useEffect(() => () => { onDirtyChange?.(false); onBusyChange?.(false); }, [onDirtyChange, onBusyChange]);

  const filteredUsers = useMemo(() => {
    if (!users) return [];
    const q = debouncedQuery.trim().toLowerCase();
    let result = users.filter(u => !deletedIds.includes(u.id));
    if (q) {
      result = result.filter(
        (u) =>
          u.username.toLowerCase().includes(q) ||
          u.email?.toLowerCase().includes(q) ||
          u.display_name?.toLowerCase().includes(q),
      );
    }
    const sorted = [...result];
    switch (adminSort) {
      case "recent":
        sorted.sort((a, b) => b.created_at.localeCompare(a.created_at));
        break;
      case "oldest":
        sorted.sort((a, b) => a.created_at.localeCompare(b.created_at));
        break;
      case "username":
        sorted.sort((a, b) => a.username.localeCompare(b.username));
        break;
      case "vaults":
        sorted.sort((a, b) => (b.owned_vaults || 0) - (a.owned_vaults || 0));
        break;
    }
    return sorted;
  }, [users, debouncedQuery, adminSort, deletedIds]);

  async function confirmDeleteUser() {
    if (!pendingDeleteUser) return;
    const u = pendingDeleteUser;
    setDeletingId(u.id);
    try {
      await adminDeleteUser(u.id);
      if (ownsOperation?.() === false) return;
      setDeletedIds(ids => [...ids, u.id]);
      setNotice("User deleted.");
      const refreshed = await onReloadUsers();
      if (ownsOperation?.() !== false && refreshed === false) setNotice("User deleted. The user list could not be updated; retry the list below.");
    } finally {
      setDeletingId(null);
    }
  }

  return (
    <>
      <Activity mode={visible ? "visible" : "hidden"}><section aria-labelledby="settings-users-heading" aria-busy={usersLoading}>
        <header className="mb-4 flex items-start justify-between gap-4 border-b border-border pb-4">
          <div className="flex items-start gap-3">
            <div>
              <h2 id="settings-users-heading" className="text-base font-semibold text-foreground">Users</h2>
              <p className="mt-1 text-sm text-foreground-muted">
                Everyone with an account on this AKB server.
              </p>
            </div>
          </div>
          <Badge variant="default" className="shrink-0 tabular-nums">
            {users ? `${filteredUsers.length} of ${users.filter(u => !deletedIds.includes(u.id)).length}` : "··"}
          </Badge>
        </header>
        {notice && <Alert variant="info" className="mb-3">{notice}</Alert>}
        {usersLoading && users && <InlineLoadingState label="Updating users" className="py-3" />}
        {usersError && users && <Alert variant="warning" className="mb-3">Could not update users. Previous users are shown.<Button variant="outline" size="sm" loading={usersLoading} onClick={() => void onReloadUsers()}>Retry</Button></Alert>}
        <Panel variant="workspace">
        <div className="flex flex-col gap-3 border-b border-border p-3 sm:flex-row sm:items-center">
          <div className="relative min-w-0 flex-1">
            <Search className="pointer-events-none absolute left-3 top-1/2 h-4 w-4 -translate-y-1/2 text-foreground-muted" aria-hidden />
            <Input
              placeholder="Search users by name or email…"
              value={adminQuery}
              onChange={(e) => setAdminQuery(e.target.value)}
              className="w-full pl-9"
              aria-label="Search users"
            />
          </div>
          <Label htmlFor="admin-sort" className="sr-only">Sort</Label>
          <SelectMenu
            id="admin-sort"
            aria-label="Sort users"
            value={adminSort}
            onValueChange={(v) => setAdminSort(v as AdminSort)}
            className="w-full sm:w-auto sm:min-w-[160px]"
            options={[
              { value: "recent", label: "Recent first" },
              { value: "oldest", label: "Oldest first" },
              { value: "username", label: "Username A-Z" },
              { value: "vaults", label: "Most vaults" },
            ]}
          />
        </div>
        <div>
          {usersError && !users ? (
            <EmptyState
              title="Couldn't load users"
              description="Something went wrong fetching the user list."
              action={
                <Button variant="outline" size="sm" loading={usersLoading} onClick={() => void onReloadUsers()}>
                  Retry
                </Button>
              }
            />
          ) : !users ? (
            <>
              <span className="sr-only" role="status" aria-live="polite">
                Loading users
              </span>
              <div
                className="rounded-[var(--radius-md)] border border-border divide-y divide-border overflow-hidden"
                aria-hidden
              >
                {Array.from({ length: 5 }).map((_, i) => (
                  <div key={i} className="flex items-center gap-3 px-4 py-3">
                    <span className="h-3 w-5 rounded bg-surface-muted animate-pulse" />
                    <span className="h-4 flex-1 rounded bg-surface-muted animate-pulse" />
                    <span className="h-3 w-16 rounded bg-surface-muted animate-pulse" />
                  </div>
                ))}
              </div>
            </>
          ) : filteredUsers.length === 0 ? (
            <EmptyState
              title={
                adminQuery
                  ? `No users matching "${adminQuery}"`
                  : "No users"
              }
            />
          ) : (
            <div className="divide-y divide-border overflow-hidden">
              {filteredUsers.map((u) => (
                <div
                  key={u.id}
                  data-testid="admin-user-row"
                  className="flex flex-col gap-3 px-4 py-3 transition-token hover:bg-surface-hover sm:flex-row sm:items-center"
                >
                  <span
                    className="flex h-9 w-9 shrink-0 items-center justify-center rounded-full border border-border bg-surface-2 text-xs font-semibold text-primary"
                    aria-hidden
                  >
                    {userInitials(u)}
                  </span>
                  <div className="min-w-0 flex-1">
                    <div className="flex min-w-0 flex-wrap items-center gap-x-2 gap-y-1">
                      <TooltipText
                        data-testid="admin-user-name"
                        className="truncate text-sm font-semibold text-foreground"
                      >
                        {u.display_name || u.username}
                      </TooltipText>
                      {u.display_name && u.display_name !== u.username && (
                        <TooltipText className="truncate text-sm text-foreground-muted">
                          {u.username}
                        </TooltipText>
                      )}
                      {u.is_admin && (
                        <span className="shrink-0">
                          <RoleBadge role="admin" />
                        </span>
                      )}
                    </div>
                    <div className="mt-1 flex flex-wrap items-center gap-x-2 gap-y-1 text-xs text-foreground-muted">
                      <span title={u.email} className="truncate">{u.email}</span>
                      <span aria-hidden>·</span>
                      <span className="tabular-nums">{u.owned_vaults} vault{u.owned_vaults === 1 ? "" : "s"}</span>
                      <span aria-hidden className="hidden md:inline">·</span>
                      <span className="hidden tabular-nums md:inline">Joined {formatDate(u.created_at)}</span>
                    </div>
                  </div>
                  <div className="flex shrink-0 items-center gap-1 sm:ml-auto">
                    {u.id === user.user_id ? (
                      <Badge variant="outline">You</Badge>
                    ) : (
                      <ResourceActionsMenu resourceName={u.username} onCloseAutoFocus={event => { if (resetTarget || pendingDeleteUser) event.preventDefault(); }}>
                        {localPasswordEnabled && (
                          <DropdownMenu.Item
                            onSelect={() => setResetTarget(u)}
                            aria-label={`Reset password for ${u.username}`}
                            className="flex min-h-10 cursor-pointer items-center gap-2 rounded-[var(--radius-sm)] px-2.5 py-2 text-sm outline-none data-[highlighted]:bg-surface-hover"
                          >
                            <Key className="h-3 w-3" aria-hidden />
                            Reset password
                          </DropdownMenu.Item>
                        )}
                        <DropdownMenu.Item
                          onSelect={() => setPendingDeleteUser(u)}
                          disabled={deletingId === u.id}
                          aria-label={`Delete user ${u.username}`}
                          className="flex min-h-10 cursor-pointer items-center gap-2 rounded-[var(--radius-sm)] px-2.5 py-2 text-sm text-destructive outline-none data-[highlighted]:bg-surface-hover data-[disabled]:opacity-50"
                        >
                          {deletingId === u.id ? (
                            <Loader2 className="h-3 w-3 animate-spin" aria-hidden />
                          ) : (
                            <Trash2 className="h-3 w-3" aria-hidden />
                          )}
                          {deletingId === u.id ? "Deleting" : "Delete account"}
                        </DropdownMenu.Item>
                      </ResourceActionsMenu>
                    )}
                  </div>
                </div>
              ))}
            </div>
          )}
        </div>
      </Panel>
      </section></Activity>

      <ConfirmDialog
        open={visible && pendingDeleteUser !== null}
        onOpenChange={(o) => !o && setPendingDeleteUser(null)}
        title={pendingDeleteUser ? `Delete user "${pendingDeleteUser.username}"?` : ""}
        description={
          pendingDeleteUser && pendingDeleteUser.owned_vaults > 0
            ? `This will ALSO permanently delete ${pendingDeleteUser.owned_vaults} vault(s) they own — including documents, files, tables, and version history.\n\nThis cannot be undone.`
            : "This cannot be undone."
        }
        confirmLabel="Delete user"
        variant="destructive"
        onConfirm={confirmDeleteUser}
      />

      {localPasswordEnabled && (
        <AdminResetPasswordDialog
          ownsOperation={ownsOperation}
          visible={visible}
          onBusyChange={setResetBusy}
          userId={resetTarget?.id ?? ""}
          username={resetTarget?.username ?? ""}
          open={resetTarget !== null}
          onOpenChange={(o) => {
            if (!o) setResetTarget(null);
          }}
        />
      )}
    </>
  );
}

function userInitials(user: AdminUser): string {
  const label = user.display_name?.trim() || user.username;
  const words = label.split(/\s+/).filter(Boolean);
  if (words.length > 1) {
    return `${Array.from(words[0])[0] ?? ""}${Array.from(words[words.length - 1])[0] ?? ""}`;
  }
  return Array.from(words[0] ?? "?").slice(0, 2).join("");
}
