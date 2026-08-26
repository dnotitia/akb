import { useEffect, useMemo, useState } from "react";
import {
  Box,
  Check,
  Eye,
  PenLine,
  Search,
  ShieldCheck,
  UserPlus,
  UserRound,
  type LucideIcon,
} from "lucide-react";
import { searchUsers, grantAccess } from "@/lib/api";
import { useDebounce } from "@/hooks/use-debounce";
import { Alert } from "@/components/ui/alert";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { cn } from "@/lib/utils";

type Role = "reader" | "writer" | "admin";

interface UserHit {
  username: string;
  display_name?: string | null;
  email: string;
}

interface InviteMemberDialogProps {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  vault: string;
  /** Usernames already in the vault — hidden from search results. */
  existingUsernames: Set<string>;
  onInvited: () => void;
}

const ROLE_LABELS: Record<Role, string> = {
  reader: "Reader",
  writer: "Writer",
  admin: "Admin",
};
const ROLE_OPTIONS: Array<{
  value: Role;
  label: string;
  description: string;
  Icon: LucideIcon;
}> = [
  {
    value: "reader",
    label: "Reader",
    description: "Browse and search vault knowledge.",
    Icon: Eye,
  },
  {
    value: "writer",
    label: "Writer",
    description: "Create, edit, and remove content.",
    Icon: PenLine,
  },
  {
    value: "admin",
    label: "Admin",
    description: "Manage content, members, and roles.",
    Icon: ShieldCheck,
  },
];

export function InviteMemberDialog({
  open,
  onOpenChange,
  vault,
  existingUsernames,
  onInvited,
}: InviteMemberDialogProps) {
  const [query, setQuery] = useState("");
  const debouncedQuery = useDebounce(query.trim(), 250);
  const [hits, setHits] = useState<UserHit[]>([]);
  const [searching, setSearching] = useState(false);
  const [selected, setSelected] = useState<UserHit | null>(null);
  const [role, setRole] = useState<Role>("reader");
  const [error, setError] = useState("");
  const [granting, setGranting] = useState(false);

  // Stabilize the Set so its identity matches the underlying string set —
  // otherwise every parent render produces a fresh Set ref and the search
  // effect refires unnecessarily.
  const exclusionKey = useMemo(
    () => Array.from(existingUsernames).sort().join(","),
    [existingUsernames],
  );

  // Reset on open/close so a stale picked user from a previous invite doesn't
  // leak into the next session.
  useEffect(() => {
    if (!open) {
      setQuery("");
      setHits([]);
      setSelected(null);
      setRole("reader");
      setError("");
    }
  }, [open]);

  // Empty query shows the first 20 users (server default) so the picker is
  // browsable. Debouncing happens upstream via useDebounce on `query`.
  useEffect(() => {
    if (!open) return;
    let cancelled = false;
    setSearching(true);
    searchUsers(debouncedQuery || undefined)
      .then((result) => {
        if (cancelled) return;
        setHits(
          (result.users || []).filter((user) => !existingUsernames.has(user.username)),
        );
      })
      .catch((caught: unknown) => {
        if (cancelled) return;
        setHits([]);
        setError(errorMessage(caught, "Search failed"));
      })
      .finally(() => {
        if (!cancelled) setSearching(false);
      });
    return () => {
      cancelled = true;
    };
    // existingUsernames identity may change across parent renders even when
    // contents are stable — gate on the canonical key instead.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [debouncedQuery, open, exclusionKey]);

  async function handleGrant() {
    if (!selected) return;
    setGranting(true);
    setError("");
    try {
      await grantAccess(vault, selected.username, role);
      onInvited();
      onOpenChange(false);
    } catch (caught: unknown) {
      setError(errorMessage(caught, "Failed to grant access"));
    } finally {
      setGranting(false);
    }
  }

  return (
    <Dialog open={open} onOpenChange={(nextOpen) => !granting && onOpenChange(nextOpen)}>
      <DialogContent className="max-w-3xl gap-0 overflow-hidden p-0">
        <DialogHeader className="border-b border-border-strong bg-workspace-section px-5 py-4 pr-14">
          <div className="flex items-start gap-3">
            <span className="flex h-9 w-9 shrink-0 items-center justify-center rounded-[var(--radius-md)] border border-border-strong bg-surface text-primary shadow-xs">
              <UserPlus className="h-4 w-4" aria-hidden />
            </span>
            <div className="min-w-0">
              <DialogTitle>Invite member</DialogTitle>
              <DialogDescription className="mt-1">
                Choose an AKB account and grant access to this vault.
              </DialogDescription>
              <div className="mt-2 inline-flex max-w-full items-center gap-1.5 rounded-[var(--radius-sm)] border border-border bg-surface px-2 py-1 text-xs text-foreground-muted">
                <Box className="h-3.5 w-3.5 shrink-0 text-primary" aria-hidden />
                <span className="truncate font-medium text-foreground">{vault}</span>
              </div>
            </div>
          </div>
        </DialogHeader>

        <div className="grid min-h-0 lg:grid-cols-[minmax(0,1.25fr)_minmax(17rem,0.75fr)]">
          <section aria-labelledby="invite-person-heading" className="min-w-0 border-b border-border lg:border-b-0 lg:border-r">
            <div className="border-b border-border bg-surface px-5 py-4">
              <div className="flex items-center justify-between gap-3">
                <div>
                  <h3 id="invite-person-heading" className="text-sm font-semibold text-foreground">
                    Find a person
                  </h3>
                  <p className="mt-0.5 text-xs text-foreground-muted">
                    Existing vault members are hidden.
                  </p>
                </div>
                {!searching && hits.length > 0 && (
                  <span className="shrink-0 text-xs tabular-nums text-foreground-muted">
                    {hits.length} available
                  </span>
                )}
              </div>

              <Label htmlFor="invite-search" className="sr-only">
                Search AKB accounts
              </Label>
              <div className="relative mt-3">
                <Search
                  className="pointer-events-none absolute left-3 top-1/2 h-4 w-4 -translate-y-1/2 text-foreground-muted"
                  aria-hidden
                />
                <Input
                  id="invite-search"
                  type="search"
                  placeholder="Search name, username, or email"
                  value={query}
                  onChange={(event) => setQuery(event.target.value)}
                  className="bg-background pl-9"
                  autoFocus
                />
              </div>
            </div>

            <div className="rail-scroll min-h-48 max-h-[min(22rem,42vh)] overflow-y-auto bg-surface">
              {searching && hits.length === 0 ? (
                <div className="flex min-h-48 items-center justify-center px-5 text-sm text-foreground-muted" role="status" aria-live="polite">
                  Searching accounts…
                </div>
              ) : hits.length === 0 ? (
                <div className="flex min-h-48 flex-col items-center justify-center px-8 text-center" role="status">
                  <span className="flex h-9 w-9 items-center justify-center rounded-full bg-surface-2 text-foreground-muted">
                    <UserRound className="h-4 w-4" aria-hidden />
                  </span>
                  <p className="mt-3 text-sm font-medium text-foreground">
                    {debouncedQuery ? "No matching accounts" : "No accounts available"}
                  </p>
                  <p className="mt-1 text-xs leading-relaxed text-foreground-muted">
                    Only people with an AKB account can be invited.
                  </p>
                </div>
              ) : (
                <ul role="listbox" aria-label="AKB accounts" className="divide-y divide-border">
                  {hits.map((user) => {
                    const active = selected?.username === user.username;
                    return (
                      <li key={user.username} role="none">
                        <button
                          type="button"
                          role="option"
                          aria-selected={active}
                          onClick={() => setSelected(user)}
                          className={cn(
                            "flex w-full cursor-pointer items-center gap-3 px-5 py-3 text-left transition-token focus:outline-none focus-visible:ring-2 focus-visible:ring-inset focus-visible:ring-ring",
                            active
                              ? "bg-surface-selected text-surface-selected-foreground"
                              : "bg-surface text-foreground hover:bg-surface-hover",
                          )}
                        >
                          <UserAvatar user={user} />
                          <span className="min-w-0 flex-1">
                            <span className="flex min-w-0 items-center gap-2">
                              <span className="truncate text-sm font-semibold text-foreground">
                                {user.display_name?.trim() || user.username}
                              </span>
                              {user.display_name?.trim() && (
                                <span className="shrink-0 text-xs text-foreground-muted">
                                  @{user.username}
                                </span>
                              )}
                            </span>
                            <span className="mt-0.5 block truncate text-xs text-foreground-muted">
                              {user.email}
                            </span>
                          </span>
                          <span
                            className={cn(
                              "flex h-6 w-6 shrink-0 items-center justify-center rounded-full border",
                              active
                                ? "border-primary bg-primary text-primary-foreground"
                                : "border-border bg-surface text-transparent",
                            )}
                            aria-hidden
                          >
                            <Check className="h-3.5 w-3.5" />
                          </span>
                        </button>
                      </li>
                    );
                  })}
                </ul>
              )}
            </div>
          </section>

          <section aria-labelledby="invite-role-heading" className="min-w-0 bg-background px-5 py-4">
            <h3 id="invite-role-heading" className="text-sm font-semibold text-foreground">
              Access level
            </h3>
            <p className="mt-0.5 text-xs text-foreground-muted">
              Choose the lowest role that fits their work.
            </p>

            <div className="mt-3 rounded-[var(--radius-md)] border border-border-strong bg-surface p-3" aria-live="polite">
              {selected ? (
                <div className="flex min-w-0 items-center gap-3">
                  <UserAvatar user={selected} />
                  <div className="min-w-0">
                    <p className="truncate text-sm font-semibold text-foreground">
                      {selected.display_name?.trim() || selected.username}
                    </p>
                    <p className="truncate text-xs text-foreground-muted">@{selected.username}</p>
                  </div>
                </div>
              ) : (
                <div className="flex items-center gap-3 text-foreground-muted">
                  <span className="flex h-8 w-8 shrink-0 items-center justify-center rounded-full bg-surface-2">
                    <UserRound className="h-4 w-4" aria-hidden />
                  </span>
                  <p className="text-xs leading-relaxed">Select a person from the account list.</p>
                </div>
              )}
            </div>

            <fieldset className="mt-4">
              <legend className="sr-only">Role</legend>
              <div className="space-y-2">
                {ROLE_OPTIONS.map((option) => {
                  const active = role === option.value;
                  const Icon = option.Icon;
                  return (
                    <label
                      key={option.value}
                      className={cn(
                        "flex min-h-16 cursor-pointer items-start gap-3 rounded-[var(--radius-md)] border px-3 py-2.5 transition-token has-[:focus-visible]:ring-2 has-[:focus-visible]:ring-ring",
                        active
                          ? "border-primary bg-surface-selected"
                          : "border-border bg-surface hover:border-border-strong hover:bg-surface-hover",
                      )}
                    >
                      <input
                        type="radio"
                        name="invite-role"
                        value={option.value}
                        checked={active}
                        onChange={() => setRole(option.value)}
                        className="sr-only"
                      />
                      <span
                        className={cn(
                          "mt-0.5 flex h-7 w-7 shrink-0 items-center justify-center rounded-[var(--radius-sm)]",
                          active
                            ? "bg-primary text-primary-foreground"
                            : "bg-surface-2 text-foreground-muted",
                        )}
                      >
                        <Icon className="h-3.5 w-3.5" aria-hidden />
                      </span>
                      <span className="min-w-0 flex-1">
                        <span className="flex items-center justify-between gap-2">
                          <span className="text-sm font-semibold text-foreground">{option.label}</span>
                          {active && <Check className="h-3.5 w-3.5 shrink-0 text-primary" aria-hidden />}
                        </span>
                        <span className="mt-0.5 block text-xs leading-relaxed text-foreground-muted">
                          {option.description}
                        </span>
                      </span>
                    </label>
                  );
                })}
              </div>
            </fieldset>

            {error && <Alert variant="destructive" className="mt-4">{error}</Alert>}
          </section>
        </div>

        <div className="flex flex-col-reverse gap-3 border-t border-border-strong bg-surface px-5 py-3 sm:flex-row sm:items-center sm:justify-between">
          <p
            data-testid="invite-summary"
            className="min-w-0 text-xs text-foreground-muted"
            role="status"
            aria-live="polite"
          >
            {selected ? (
              <>
                <span className="font-semibold text-foreground">
                  {selected.display_name?.trim() || selected.username}
                </span>{" "}
                will join as {ROLE_LABELS[role]}.
              </>
            ) : (
              "Select a person to continue."
            )}
          </p>
          <div className="flex shrink-0 items-center justify-end gap-2">
            <Button
              type="button"
              variant="outline"
              onClick={() => onOpenChange(false)}
              disabled={granting}
            >
              Cancel
            </Button>
            <Button
              type="button"
              variant="accent"
              onClick={handleGrant}
              loading={granting}
              disabled={!selected}
            >
              {granting ? "Inviting…" : `Invite as ${ROLE_LABELS[role]}`}
            </Button>
          </div>
        </div>
      </DialogContent>
    </Dialog>
  );
}

function UserAvatar({ user }: { user: UserHit }) {
  const label = user.display_name?.trim() || user.username;
  return (
    <span
      aria-hidden
      className="flex h-9 w-9 shrink-0 items-center justify-center rounded-full border border-border bg-surface-2 text-xs font-semibold text-foreground"
    >
      {initialsFor(label)}
    </span>
  );
}

function initialsFor(value: string): string {
  const parts = value.trim().split(/\s+/).filter(Boolean);
  if (parts.length > 1) return `${parts[0][0]}${parts[1][0]}`.toUpperCase();
  return value.slice(0, 2).toUpperCase();
}

function errorMessage(error: unknown, fallback: string): string {
  return error instanceof Error && error.message ? error.message : fallback;
}
