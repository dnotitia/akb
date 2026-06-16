import { useEffect, useMemo, useState } from "react";
import { Search } from "lucide-react";
import { searchUsers, grantAccess } from "@/lib/api";
import { useDebounce } from "@/hooks/use-debounce";
import { Alert } from "@/components/ui/alert";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Segmented } from "@/components/ui/segmented";
import { TooltipText } from "@/components/ui/tooltip-text";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";

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

const ROLE_DESCRIPTIONS: Record<Role, string> = {
  reader: "Read documents, browse, search.",
  writer: "Reader + create / edit / delete content.",
  admin: "Writer + grant / revoke other members.",
};

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
      .then((r) => {
        if (cancelled) return;
        setHits((r.users || []).filter((u) => !existingUsernames.has(u.username)));
      })
      .catch((e: any) => {
        if (cancelled) return;
        setHits([]);
        setError(e?.message || "Search failed");
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
    } catch (e: any) {
      setError(e?.message || "Failed to grant access");
    } finally {
      setGranting(false);
    }
  }

  return (
    <Dialog open={open} onOpenChange={(o) => !granting && onOpenChange(o)}>
      <DialogContent className="max-w-xl">
        <DialogHeader>
          <DialogTitle>Add a member to {vault}</DialogTitle>
          <DialogDescription>
            Find a person with an AKB account, pick a role, and grant access. They
            gain access immediately.
          </DialogDescription>
        </DialogHeader>

        <div className="space-y-4">
          {/* User search */}
          <div className="space-y-1.5">
            <Label htmlFor="invite-search" className="coord-ink">
              User
            </Label>
            <div className="relative">
              <Search
                className="absolute left-3 top-1/2 -translate-y-1/2 h-4 w-4 text-foreground-muted pointer-events-none"
                aria-hidden
              />
              <Input
                id="invite-search"
                type="search"
                placeholder="Search by username, display name, or email"
                value={query}
                onChange={(e) => setQuery(e.target.value)}
                className="pl-9"
                autoFocus
              />
            </div>

            <div className="border border-border max-h-[min(14rem,30vh)] overflow-y-auto rail-scroll">
              {searching && hits.length === 0 ? (
                <div className="coord px-3 py-2" role="status" aria-live="polite">Searching…</div>
              ) : hits.length === 0 ? (
                <div className="coord px-3 py-2" role="status">
                  {debouncedQuery
                    ? "No matches — only people with an AKB account appear here."
                    : "Only people with an AKB account appear here."}
                </div>
              ) : (
                <ul role="listbox" aria-label="Search results" className="divide-y divide-border">
                  {hits.map((u) => {
                    const active = selected?.username === u.username;
                    return (
                      <li key={u.username} role="option" aria-selected={active}>
                        <button
                          type="button"
                          onClick={() => setSelected(u)}
                          className={`w-full text-left px-3 py-2 transition-colors focus:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-inset cursor-pointer ${
                            active
                              ? "bg-surface-selected text-surface-selected-foreground"
                              : "hover:bg-surface-hover text-foreground"
                          }`}
                        >
                          <div className="flex items-baseline gap-2">
                            <TooltipText className="text-sm font-medium truncate">
                              {u.username}
                            </TooltipText>
                            {u.display_name && (
                              <span title={u.display_name} className="text-xs text-foreground-muted truncate">
                                {u.display_name}
                              </span>
                            )}
                          </div>
                          <div title={u.email} className="coord truncate">{u.email}</div>
                        </button>
                      </li>
                    );
                  })}
                </ul>
              )}
            </div>
          </div>

          {/* Role picker */}
          <div className="space-y-1.5">
            <Label id="invite-role-label" className="coord-ink">Role</Label>
            <Segmented
              aria-labelledby="invite-role-label"
              value={role}
              onChange={(v) => setRole(v as Role)}
              className="grid-cols-3"
              options={(["reader", "writer", "admin"] as Role[]).map((r) => ({
                value: r,
                label: r,
              }))}
            />
            <p className="text-xs text-foreground-muted leading-relaxed">
              {ROLE_DESCRIPTIONS[role]}
            </p>
          </div>

          {error && <Alert variant="destructive">{error}</Alert>}
        </div>

        <DialogFooter>
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
            variant="default"
            onClick={handleGrant}
            loading={granting}
            disabled={!selected}
          >
            {granting ? "Granting…" : `Grant ${role}`}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
