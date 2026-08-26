import { useMemo, useState } from "react";
import { Key, Loader2, Search, Trash2, UsersRound } from "lucide-react";
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
import { TooltipText } from "@/components/ui/tooltip-text";
import { Panel } from "@/components/ui/panel";
import type { User } from "./profile-section";

type AdminSort = "recent" | "oldest" | "username" | "vaults";

interface Props {
  user: User;
  users: AdminUser[] | null;
  usersError: boolean;
  localPasswordEnabled: boolean;
  onReloadUsers: () => void;
}

export function AdminSection({
  user,
  users,
  usersError,
  localPasswordEnabled,
  onReloadUsers,
}: Props) {
  const [adminQuery, setAdminQuery] = useState("");
  const [adminSort, setAdminSort] = useState<AdminSort>("recent");
  const debouncedQuery = useDebounce(adminQuery, 200);
  const [deletingId, setDeletingId] = useState<string | null>(null);
  const [pendingDeleteUser, setPendingDeleteUser] = useState<AdminUser | null>(null);
  const [resetTarget, setResetTarget] = useState<AdminUser | null>(null);

  const filteredUsers = useMemo(() => {
    if (!users) return [];
    const q = debouncedQuery.trim().toLowerCase();
    let result = users;
    if (q) {
      result = users.filter(
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
  }, [users, debouncedQuery, adminSort]);

  async function confirmDeleteUser() {
    if (!pendingDeleteUser) return;
    const u = pendingDeleteUser;
    setDeletingId(u.id);
    try {
      await adminDeleteUser(u.id);
      onReloadUsers();
    } finally {
      setDeletingId(null);
    }
  }

  return (
    <>
      <Panel>
        <header className="flex items-start justify-between gap-4 border-b border-border px-5 py-5 sm:px-6">
          <div className="flex items-start gap-3">
            <span className="flex h-10 w-10 shrink-0 items-center justify-center rounded-[var(--radius-md)] bg-surface-selected text-surface-selected-foreground">
              <UsersRound className="h-4 w-4" aria-hidden />
            </span>
            <div>
              <h2 className="text-base font-semibold text-foreground">Users</h2>
              <p className="mt-1 text-sm text-foreground-muted">
                Everyone with an account on this AKB server.
              </p>
            </div>
          </div>
          <Badge variant="default" className="shrink-0 tabular-nums">
            {users ? `${filteredUsers.length} of ${users.length}` : "··"}
          </Badge>
        </header>
        <div className="flex flex-col gap-3 border-b border-border bg-surface-2/60 p-4 sm:flex-row sm:items-center sm:px-6">
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
        <div className="p-5 sm:p-6">
          {usersError ? (
            <EmptyState
              title="Couldn't load users"
              description="Something went wrong fetching the user list."
              action={
                <Button variant="outline" size="sm" onClick={onReloadUsers}>
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
            <div className="rounded-[var(--radius-md)] border border-border divide-y divide-border overflow-hidden">
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
                        {u.username}
                      </TooltipText>
                      {u.display_name && u.display_name !== u.username && (
                        <TooltipText className="truncate text-sm text-foreground-muted">
                          {u.display_name}
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
                      <>
                        {localPasswordEnabled && (
                          <button
                            type="button"
                            onClick={() => setResetTarget(u)}
                            title={`Reset password for ${u.username}`}
                            aria-label={`Reset password for ${u.username}`}
                            className="inline-flex min-h-[36px] cursor-pointer items-center gap-1 rounded-[var(--radius-sm)] px-2 text-xs text-foreground-muted transition-colors hover:bg-surface-hover hover:text-primary focus:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2 focus-visible:ring-offset-surface"
                          >
                            <Key className="h-3 w-3" aria-hidden />
                            Reset
                          </button>
                        )}
                        <button
                          onClick={() => setPendingDeleteUser(u)}
                          disabled={deletingId === u.id}
                          aria-label={`Delete user ${u.username}`}
                          className="inline-flex min-h-[36px] cursor-pointer items-center gap-1 rounded-[var(--radius-sm)] px-2 text-xs text-destructive transition-colors hover:bg-surface-hover focus:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2 focus-visible:ring-offset-surface disabled:opacity-50"
                        >
                          {deletingId === u.id ? (
                            <Loader2 className="h-3 w-3 animate-spin" aria-hidden />
                          ) : (
                            <Trash2 className="h-3 w-3" aria-hidden />
                          )}
                          {deletingId === u.id ? "Deleting" : "Delete"}
                        </button>
                      </>
                    )}
                  </div>
                </div>
              ))}
            </div>
          )}
        </div>
      </Panel>

      <ConfirmDialog
        open={pendingDeleteUser !== null}
        onOpenChange={(o) => !o && setPendingDeleteUser(null)}
        title={pendingDeleteUser ? `Delete user "${pendingDeleteUser.username}"?` : ""}
        description={
          pendingDeleteUser && pendingDeleteUser.owned_vaults > 0
            ? `This will ALSO permanently delete ${pendingDeleteUser.owned_vaults} vault(s) they own — including documents, files, tables, and Git history.\n\nThis cannot be undone.`
            : "This cannot be undone."
        }
        confirmLabel="Delete user"
        variant="destructive"
        onConfirm={confirmDeleteUser}
      />

      {localPasswordEnabled && (
        <AdminResetPasswordDialog
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
