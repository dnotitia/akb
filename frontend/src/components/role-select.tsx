import { useState } from "react";
import { ChevronDown, Loader2 } from "lucide-react";
import { badgeVariants } from "@/components/ui/badge";
import { grantAccess } from "@/lib/api";
import { cn } from "@/lib/utils";

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
    <div className="inline-flex flex-col items-end gap-0.5">
      <div className="relative inline-flex">
        <select
          value={member.role}
          onChange={handleChange}
          disabled={busy}
          aria-label={`Change role for ${member.username}`}
          className={cn(
            badgeVariants({ variant: member.role }),
            "appearance-none pr-5 cursor-pointer",
            "focus:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2 focus-visible:ring-offset-surface",
            "hover:brightness-110 transition-[filter] duration-150",
            "disabled:opacity-60 disabled:cursor-wait",
          )}
        >
          {OPTIONS.map((r) => (
            <option key={r} value={r} className="bg-surface text-foreground">
              {r}
            </option>
          ))}
        </select>
        {busy ? (
          <Loader2
            className="absolute right-1 top-1/2 -translate-y-1/2 h-3 w-3 animate-spin pointer-events-none"
            aria-hidden
          />
        ) : (
          <ChevronDown
            className="absolute right-1 top-1/2 -translate-y-1/2 h-3 w-3 pointer-events-none opacity-70"
            aria-hidden
          />
        )}
      </div>
      {error && (
        <p role="alert" className="coord text-destructive">
          {error}
        </p>
      )}
    </div>
  );
}
