import { useState } from "react";
import { Pin, PinOff } from "lucide-react";
import { useCurrentUser } from "@/contexts/current-user-context";
import { Button } from "@/components/ui/button";
import { toggleWorkspaceShortcut, useWorkspaceShortcuts, workspaceShortcutKey, type WorkspaceShortcut } from "@/lib/workspace-shortcuts";

export function WorkspacePin({ item }: { item: WorkspaceShortcut }) {
  const user = useCurrentUser();
  const shortcuts = useWorkspaceShortcuts(user?.user_id);
  const [error, setError] = useState(false);
  if (!user) return null;
  const pinned = shortcuts.some(row => workspaceShortcutKey(row) === workspaceShortcutKey(item));
  const Icon = pinned ? PinOff : Pin;
  return <div className="flex items-center gap-1">
    <Button variant="ghost" size="sm" className="h-9 gap-1.5 text-xs" aria-pressed={pinned}
      title={pinned ? "Remove from workspace shortcuts" : "Pin to workspace shortcuts on this browser"}
      onClick={() => setError(!toggleWorkspaceShortcut(user.user_id, item))}>
      <Icon className="h-3.5 w-3.5" aria-hidden />{pinned ? "Unpin" : "Pin"}
    </Button>
    {error && <span role="alert" className="max-w-48 text-xs text-destructive">Could not save. Check browser storage or remove a pin (limit 30).</span>}
  </div>;
}
