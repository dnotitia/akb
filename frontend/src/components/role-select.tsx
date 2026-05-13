import { useState } from "react";
import { Loader2 } from "lucide-react";
import { grantAccess } from "@/lib/api";

export interface MemberLike {
  username: string;
  role: "reader" | "writer" | "admin" | "owner";
}

interface Props {
  vault: string;
  member: MemberLike;
  onChanged: (prev: string, next: string) => void;
}

const OPTIONS: Array<"reader" | "writer" | "admin"> = ["reader", "writer", "admin"];

export function RoleSelect({ vault, member, onChanged }: Props) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function handleChange(e: React.ChangeEvent<HTMLSelectElement>) {
    const next = e.target.value;
    const prev = member.role;
    if (next === prev) return;
    setBusy(true);
    setError(null);
    try {
      await grantAccess(vault, member.username, next);
      onChanged(prev, next);
    } catch (err: any) {
      setError(err?.message || "Failed to change role");
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="relative">
      <select
        value={member.role}
        onChange={handleChange}
        disabled={busy}
        aria-label={`Change role for ${member.username}`}
        className="appearance-none font-mono text-xs uppercase tracking-wider px-2 py-1 pr-6 border border-border bg-surface text-foreground hover:border-accent transition-colors duration-150 disabled:opacity-50 disabled:cursor-not-allowed focus:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2 focus-visible:ring-offset-surface cursor-pointer"
      >
        {OPTIONS.map((r) => (
          <option key={r} value={r}>{r.toUpperCase()}</option>
        ))}
      </select>
      {busy && (
        <Loader2 className="absolute right-1 top-1/2 -translate-y-1/2 h-3 w-3 animate-spin text-foreground-muted" aria-hidden />
      )}
      {error && (
        <p role="alert" className="text-[10px] text-destructive mt-1">{error}</p>
      )}
    </div>
  );
}
