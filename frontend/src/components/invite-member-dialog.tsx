import { useEffect, useMemo, useState } from "react";
import { Loader2, Search } from "lucide-react";
import { searchUsers, grantAccess } from "@/lib/api";
import { useDebounce } from "@/hooks/use-debounce";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
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
          <DialogTitle>Invite to {vault}</DialogTitle>
          <DialogDescription>
            Find a user, pick a role, send the invite. The user gains access immediately.
          </DialogDescription>
        </DialogHeader>

        <div className="space-y-4">
          {/* User search */}
          <div className="space-y-1.5">
            <Label htmlFor="invite-search" className="coord-ink">
              USER
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

            <div className="border border-border max-h-56 overflow-y-auto rail-scroll">
              {searching && hits.length === 0 ? (
                <div className="coord px-3 py-2">— SEARCHING —</div>
              ) : hits.length === 0 ? (
                <div className="coord px-3 py-2">— NO MATCHES —</div>
              ) : (
                <ul className="divide-y divide-border">
                  {hits.map((u) => {
                    const active = selected?.username === u.username;
                    return (
                      <li key={u.username}>
                        <button
                          type="button"
                          onClick={() => setSelected(u)}
                          className={`w-full text-left px-3 py-2 transition-colors focus:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-inset cursor-pointer ${
                            active
                              ? "bg-accent/10 text-accent"
                              : "hover:bg-surface-muted text-foreground"
                          }`}
                        >
                          <div className="flex items-baseline gap-2">
                            <span className="font-mono text-sm font-medium truncate">
                              {u.username}
                            </span>
                            {u.display_name && (
                              <span className="text-xs text-foreground-muted truncate">
                                {u.display_name}
                              </span>
                            )}
                          </div>
                          <div className="coord truncate">{u.email}</div>
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
            <Label className="coord-ink">ROLE</Label>
            <div className="grid grid-cols-3 gap-px border border-border bg-border">
              {(["reader", "writer", "admin"] as Role[]).map((r) => {
                const active = role === r;
                return (
                  <button
                    key={r}
                    type="button"
                    onClick={() => setRole(r)}
                    aria-pressed={active}
                    className={`px-3 py-2 text-sm font-mono uppercase tracking-wider transition-colors cursor-pointer focus:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-inset ${
                      active
                        ? "bg-foreground text-background"
                        : "bg-surface text-foreground hover:bg-surface-muted"
                    }`}
                  >
                    {r}
                  </button>
                );
              })}
            </div>
            <p className="text-xs text-foreground-muted leading-relaxed">
              {ROLE_DESCRIPTIONS[role]}
            </p>
          </div>

          {error && (
            <div role="alert" className="border border-destructive p-2 text-xs text-destructive">
              {error}
            </div>
          )}
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
            variant="accent"
            onClick={handleGrant}
            disabled={!selected || granting}
          >
            {granting ? (
              <>
                <Loader2 className="h-4 w-4 animate-spin" aria-hidden />
                Granting…
              </>
            ) : (
              `Grant ${role}`
            )}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
