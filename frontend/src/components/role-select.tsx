import { useState } from "react";
import * as DropdownMenu from "@radix-ui/react-dropdown-menu";
import { Check, ChevronDown, Loader2 } from "lucide-react";
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

  async function changeRole(next: string) {
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
      <DropdownMenu.Root>
        <DropdownMenu.Trigger
          disabled={busy}
          aria-label={`Change role for ${member.username}`}
          className={cn(
            // Reads as the member's role badge; opening reveals the themed list.
            badgeVariants({ variant: member.role }),
            "inline-flex items-center gap-1 cursor-pointer transition-token capitalize",
            "focus:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2 focus-visible:ring-offset-surface",
            "hover:border-border-strong",
            "disabled:opacity-50 disabled:cursor-wait",
          )}
        >
          {member.role}
          {busy ? (
            <Loader2 className="h-3 w-3 animate-spin" aria-hidden />
          ) : (
            <ChevronDown className="h-3 w-3 opacity-70" aria-hidden />
          )}
        </DropdownMenu.Trigger>
        <DropdownMenu.Portal>
          <DropdownMenu.Content
            align="end"
            sideOffset={6}
            className="z-50 min-w-[8rem] overflow-hidden rounded-[var(--radius-md)] border border-border bg-surface p-1 shadow-md"
          >
            <DropdownMenu.RadioGroup value={member.role} onValueChange={changeRole}>
              {OPTIONS.map((r) => (
                <DropdownMenu.RadioItem
                  key={r}
                  value={r}
                  className="relative flex cursor-pointer select-none items-center gap-2 rounded-[var(--radius-sm)] py-1.5 pl-7 pr-3 text-sm capitalize text-foreground outline-none data-[highlighted]:bg-surface-hover data-[state=checked]:text-link"
                >
                  <DropdownMenu.ItemIndicator className="absolute left-2 inline-flex">
                    <Check className="h-3.5 w-3.5" aria-hidden />
                  </DropdownMenu.ItemIndicator>
                  {r}
                </DropdownMenu.RadioItem>
              ))}
            </DropdownMenu.RadioGroup>
          </DropdownMenu.Content>
        </DropdownMenu.Portal>
      </DropdownMenu.Root>
      {error && (
        <p role="alert" className="coord text-destructive">
          {error}
        </p>
      )}
    </div>
  );
}
