import { useMemo, useState } from "react";
import { Key, Loader2, Trash2 } from "lucide-react";
import { adminDeleteUser, type AdminUser } from "@/lib/api";
import { useDebounce } from "@/hooks/use-debounce";
import { formatDate } from "@/lib/utils";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { SelectMenu } from "@/components/ui/select-menu";
import { ConfirmDialog } from "@/components/ui/confirm-dialog";
import { RoleBadge } from "@/components/status-badge";
import { EmptyState } from "@/components/empty-state";
import { AdminResetPasswordDialog } from "@/components/admin-reset-password-dialog";
import { TooltipText } from "@/components/ui/tooltip-text";
import type { User } from "./profile-section";

type AdminSort = "recent" | "oldest" | "username" | "vaults";

interface Props {
  user: User;
  users: AdminUser[] | null;
  usersError: boolean;
  onReloadUsers: () => void;
}

export function AdminSection({ user, users, usersError, onReloadUsers }: Props) {
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
      {/* Search + sort bar */}
      <div className="flex flex-wrap items-center gap-3">
        <Input
          placeholder="Search users by name or email…"
          value={adminQuery}
          onChange={(e) => setAdminQuery(e.target.value)}
          className="flex-1 min-w-[260px]"
          aria-label="Search users"
        />
        <Label htmlFor="admin-sort" className="sr-only">Sort</Label>
        <SelectMenu
          id="admin-sort"
          aria-label="Sort users"
          value={adminSort}
          onValueChange={(v) => setAdminSort(v as AdminSort)}
          className="w-auto min-w-[160px]"
          options={[
            { value: "recent", label: "Recent first" },
            { value: "oldest", label: "Oldest first" },
            { value: "username", label: "Username A-Z" },
            { value: "vaults", label: "Most vaults" },
          ]}
        />
      </div>

      <div className="rounded-[var(--radius-lg)] border border-border bg-surface shadow-sm overflow-hidden">
        {/* Count lives in the card header (parity with the Tokens card),
            not an orphan line floating above the card. */}
        <header className="border-b border-border px-6 py-3 flex items-baseline gap-3">
          <span className="coord-ink">Users</span>
          <span className="coord tabular-nums">
            [{users ? `${filteredUsers.length} of ${users.length}` : "··"}]
          </span>
        </header>
        <div className="p-6">
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
              {filteredUsers.map((u, i) => (
                <div
                  key={u.id}
                  data-testid="admin-user-row"
                  className="flex items-center justify-between gap-3 px-4 py-3"
                >
                  <div className="flex items-baseline gap-3 min-w-0 flex-1">
                    <span className="coord tabular-nums shrink-0">
                      {String(i + 1).padStart(2, "0")}
                    </span>
                    <TooltipText
                      data-testid="admin-user-name"
                      className="text-sm font-medium truncate text-foreground"
                    >
                      {u.username}
                    </TooltipText>
                    {u.display_name && u.display_name !== u.username && (
                      <TooltipText className="text-sm text-foreground-muted truncate hidden sm:inline">
                        {u.display_name}
                      </TooltipText>
                    )}
                    <span title={u.email} className="text-[11px] text-foreground-muted truncate hidden md:inline">
                      {u.email}
                    </span>
                    {u.is_admin && (
                      <span className="shrink-0">
                        <RoleBadge role="admin" />
                      </span>
                    )}
                  </div>
                  <div className="flex items-center gap-3 shrink-0">
                    <span className="coord tabular-nums hidden sm:inline">
                      Vaults {u.owned_vaults}
                    </span>
                    <span className="coord tabular-nums hidden md:inline">
                      Joined {formatDate(u.created_at)}
                    </span>
                    {u.id === user.user_id ? (
                      <span className="coord text-foreground-muted">
                        Self
                      </span>
                    ) : (
                      <>
                        <button
                          type="button"
                          onClick={() => setResetTarget(u)}
                          title={`Reset password for ${u.username}`}
                          aria-label={`Reset password for ${u.username}`}
                          className="inline-flex items-center gap-1 px-2 min-h-[36px] rounded-[var(--radius-sm)] text-xs text-foreground-muted hover:text-primary hover:bg-surface-hover transition-colors cursor-pointer focus:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2 focus-visible:ring-offset-surface"
                        >
                          <Key className="h-3 w-3" aria-hidden />
                          Reset
                        </button>
                        <button
                          onClick={() => setPendingDeleteUser(u)}
                          disabled={deletingId === u.id}
                          aria-label={`Delete user ${u.username}`}
                          className="inline-flex items-center gap-1 px-2 min-h-[36px] rounded-[var(--radius-sm)] text-xs text-destructive hover:bg-surface-hover disabled:opacity-50 transition-colors cursor-pointer focus:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2 focus-visible:ring-offset-surface"
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
      </div>

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

      <AdminResetPasswordDialog
        userId={resetTarget?.id ?? ""}
        username={resetTarget?.username ?? ""}
        open={resetTarget !== null}
        onOpenChange={(o) => {
          if (!o) setResetTarget(null);
        }}
      />
    </>
  );
}
