import * as DropdownMenu from "@radix-ui/react-dropdown-menu";
import { MoreHorizontal, Trash2 } from "lucide-react";
import { Button } from "@/components/ui/button";
import { cn } from "@/lib/utils";

interface ResourceActionsMenuProps {
  resourceName: string;
  deleteLabel: string;
  onDelete: () => void;
  className?: string;
  side?: "top" | "right" | "bottom" | "left";
  align?: "start" | "center" | "end";
}

/** Shared overflow action for document, file, and table resources.
 *
 * The menu keeps destructive actions out of the primary button row while
 * making them consistently discoverable in both workspace headers and the
 * Vault explorer. Confirmation remains the responsibility of the caller.
 */
export function ResourceActionsMenu({
  resourceName,
  deleteLabel,
  onDelete,
  className,
  side = "bottom",
  align = "end",
}: ResourceActionsMenuProps) {
  return (
    <DropdownMenu.Root>
      <DropdownMenu.Trigger asChild>
        <Button
          type="button"
          variant="ghost"
          size="icon"
          aria-label={`Actions for ${resourceName}`}
          title={`Actions for ${resourceName}`}
          className={cn("shrink-0", className)}
        >
          <MoreHorizontal className="h-4 w-4" aria-hidden />
        </Button>
      </DropdownMenu.Trigger>
      <DropdownMenu.Portal>
        <DropdownMenu.Content
          side={side}
          align={align}
          sideOffset={4}
          className="z-[var(--z-popover)] min-w-48 overflow-hidden rounded-[var(--radius-md)] border border-border bg-surface p-1 shadow-md"
        >
          <DropdownMenu.Item
            onSelect={onDelete}
            className="flex cursor-pointer select-none items-center gap-2 rounded-[var(--radius-sm)] px-2.5 py-2 text-sm text-destructive outline-none data-[highlighted]:bg-destructive-soft data-[highlighted]:text-destructive-soft-foreground"
          >
            <Trash2 className="h-4 w-4" aria-hidden />
            {deleteLabel}
          </DropdownMenu.Item>
        </DropdownMenu.Content>
      </DropdownMenu.Portal>
    </DropdownMenu.Root>
  );
}
