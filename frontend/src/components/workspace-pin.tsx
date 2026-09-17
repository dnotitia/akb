import { useState } from "react";
import * as DropdownMenu from "@radix-ui/react-dropdown-menu";
import { Pin, PinOff } from "lucide-react";
import { useCurrentUser } from "@/contexts/current-user-context";
import { Button } from "@/components/ui/button";
import { toggleWorkspaceShortcut, useWorkspaceShortcuts, workspaceShortcutKey, type WorkspaceShortcut } from "@/lib/workspace-shortcuts";

export function WorkspacePin({ item, presentation = "button" }: { item: WorkspaceShortcut; presentation?: "button" | "menu" }) {
  const user = useCurrentUser();
  const shortcuts = useWorkspaceShortcuts(user?.user_id);
  const [error, setError] = useState(false);
  if (!user) return null;
  const pinned = shortcuts.some(row => workspaceShortcutKey(row) === workspaceShortcutKey(item));
  const Icon = pinned ? PinOff : Pin;
  if (presentation === "menu") return <>
    <DropdownMenu.CheckboxItem checked={pinned}
      onSelect={event => { event.preventDefault(); setError(!toggleWorkspaceShortcut(user.user_id, item)); }}
      className="flex cursor-pointer select-none items-center gap-2 rounded-[var(--radius-sm)] px-2.5 py-2 text-sm outline-none data-[highlighted]:bg-surface-hover">
      <Icon className="h-4 w-4 text-foreground-muted" aria-hidden />{pinned ? "Unpin" : "Pin"}
      <span className="sr-only"> · Workspace shortcut in this browser</span>
    </DropdownMenu.CheckboxItem>
    {error && <p role="alert" className="max-w-64 px-2.5 py-2 text-xs text-destructive">Could not save. Check browser storage or remove a pin (limit 30).</p>}
  </>;
  return <div className="flex items-center gap-1">
    <Button variant="ghost" size="sm" className="h-9 gap-1.5 text-xs" aria-pressed={pinned}
      title={pinned ? "Remove from workspace shortcuts" : "Pin to workspace shortcuts on this browser"}
      onClick={() => setError(!toggleWorkspaceShortcut(user.user_id, item))}>
      <Icon className="h-3.5 w-3.5" aria-hidden />{pinned ? "Unpin" : "Pin"}
    </Button>
    {error && <span role="alert" className="max-w-48 text-xs text-destructive">Could not save. Check browser storage or remove a pin (limit 30).</span>}
  </div>;
}
