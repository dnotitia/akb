import { useEffect, useMemo, useState } from "react";
import { Link, useParams } from "react-router-dom";
import { ArrowLeft, Crown, Plus, Trash2, UserCog } from "lucide-react";
import {
  getVaultInfo,
  getVaultMembers,
  revokeAccess,
  transferOwnership,
} from "@/lib/api";
import { Button } from "@/components/ui/button";
import { ConfirmDialog } from "@/components/ui/confirm-dialog";
import { InviteMemberDialog } from "@/components/invite-member-dialog";
import { RoleBadge } from "@/components/status-badge";
import { EmptyState } from "@/components/empty-state";
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

export default function VaultMembersPage() {
  const { name } = useParams<{ name: string }>();
  const [info, setInfo] = useState<VaultInfo | null>(null);
  const [members, setMembers] = useState<Member[] | null>(null);
  const [error, setError] = useState("");
  const [inviteOpen, setInviteOpen] = useState(false);
  const [pendingRevoke, setPendingRevoke] = useState<Member | null>(null);
  const [pendingTransfer, setPendingTransfer] = useState<Member | null>(null);

  useEffect(() => {
    if (!name) return;
    refresh();
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

  if (!name) return null;

  return (
    <div className="fade-up max-w-[1280px] mx-auto">
      {/* Back row + title */}
      <div className="flex items-baseline justify-between mb-6 flex-wrap gap-y-2">
        <Link
          to={`/vault/${name}`}
          className="inline-flex items-center gap-1.5 coord hover:text-accent transition-colors focus:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2 focus-visible:ring-offset-background"
        >
          <ArrowLeft className="h-3 w-3" aria-hidden />
          BACK TO {name.toUpperCase()}
        </Link>
        {info?.role && (
          info.role_source === "public" ? (
            <span
              className="inline-flex items-center gap-1 px-1.5 py-0.5 text-[10px] font-mono uppercase tracking-wider border border-warning/40 bg-warning/10 text-warning"
              title="This role is granted by the vault's public_access setting, not by direct membership. Contact the owner if this was unintended."
              aria-label={`Public ${info.role}`}
            >
              PUBLIC · {info.role.toUpperCase()}
            </span>
          ) : (
            <RoleBadge role={info.role} />
          )
        )}
      </div>

      <div className="coord mb-3">
        VAULT · {name.toUpperCase()} · MEMBERS
      </div>
      <h1 className="font-serif text-[44px] leading-[0.95] tracking-[-0.03em] text-foreground mb-2">
        Members<span className="text-foreground-muted">.</span>
      </h1>
      <p className="font-serif-italic text-[16px] leading-[1.55] text-foreground-muted mb-10 max-w-prose">
        Who can read or write to this vault. The owner holds the keys; admins can
        invite or revoke; writers can mutate content; readers see everything but
        change nothing.
      </p>

      {/* Header with invite button */}
      <header className="flex items-baseline justify-between gap-3 pb-3 border-b border-border mb-0">
        <div className="flex items-baseline gap-3">
          <span className="coord-ink">§ ROSTER</span>
          <span className="coord tabular-nums">
            [{members ? members.length : "··"}]
          </span>
        </div>
        {canManage && (
          <Button
            variant="accent"
            size="sm"
            onClick={() => setInviteOpen(true)}
          >
            <Plus className="h-4 w-4" aria-hidden />
            Invite
          </Button>
        )}
      </header>

      {/* List */}
      {error ? (
        <div role="alert" className="border border-destructive p-3 mt-4 text-sm">
          <span className="coord-spark mb-1 block text-destructive">
            ⚠ FAILED TO LOAD MEMBERS
          </span>
          {error}
        </div>
      ) : members === null ? (
        <div className="coord px-3 py-3">— LOADING —</div>
      ) : members.length === 0 ? (
        <EmptyState title="No members on record" description="Even the owner row should appear here — try refreshing." />
      ) : (
        <ol className="border border-border bg-surface divide-y divide-border">
          {members.map((m, i) => (
            <li
              key={m.username}
              className="grid grid-cols-[40px_minmax(0,1fr)_auto_auto] items-baseline gap-x-4 gap-y-1 px-4 py-3"
            >
              <span className="coord tabular-nums self-baseline">
                {String(i + 1).padStart(2, "0")}
              </span>
              <div className="min-w-0">
                <div className="flex items-baseline gap-2 flex-wrap">
                  <span className="font-mono text-sm font-semibold text-foreground">
                    {m.username}
                  </span>
                  {m.display_name && (
                    <span className="text-xs text-foreground-muted">
                      {m.display_name}
                    </span>
                  )}
                </div>
                <div className="coord truncate">{m.email}</div>
              </div>
              <div className="flex items-center gap-3 shrink-0">
                <RoleBadge role={m.role} />
                <span className="coord tabular-nums w-[64px] text-right">
                  {m.role === "owner"
                    ? "—"
                    : m.since
                      ? timeAgo(m.since)
                      : "—"}
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
                    className="inline-flex items-center gap-1 px-2 h-7 text-xs font-mono uppercase tracking-wider text-foreground-muted hover:text-accent transition-colors cursor-pointer focus:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2 focus-visible:ring-offset-surface"
                  >
                    <Crown className="h-3 w-3" aria-hidden />
                    Transfer
                  </button>
                )}
                {canManage && m.role !== "owner" && (
                  <button
                    type="button"
                    onClick={() => setPendingRevoke(m)}
                    aria-label={`Revoke ${m.username}`}
                    className="inline-flex items-center gap-1 px-2 h-7 text-xs font-mono uppercase tracking-wider text-foreground-muted hover:text-destructive transition-colors cursor-pointer focus:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2 focus-visible:ring-offset-surface"
                  >
                    <Trash2 className="h-3 w-3" aria-hidden />
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
      )}

      {!canManage && members && (
        <p className="coord mt-6 flex items-center gap-2">
          <UserCog className="h-3 w-3" aria-hidden />
          ROSTER IS READ-ONLY · YOUR ROLE IS {info?.role?.toUpperCase() || "—"}
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
        title={
          pendingRevoke
            ? `Revoke ${pendingRevoke.username}?`
            : ""
        }
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
    </div>
  );
}
