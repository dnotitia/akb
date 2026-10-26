import { useRef, type RefObject } from "react";
import * as DropdownMenu from "@radix-ui/react-dropdown-menu";
import { FilePlus, FolderPlus, Plus, Table2, Upload } from "lucide-react";
import { Button } from "@/components/ui/button";

export function VaultCreateMenu({ triggerRef, onDocument, onTable, onUpload, onCollection }: {
  triggerRef: RefObject<HTMLButtonElement | null>;
  onDocument: () => void;
  onTable: () => void;
  onUpload: () => void;
  onCollection: () => void;
}) {
  const launching = useRef(false);
  return <DropdownMenu.Root modal={false} onOpenChange={open => { if (open) launching.current = false; }}>
    <DropdownMenu.Trigger asChild>
      <Button ref={triggerRef} variant="outline" size="icon" className="h-9 w-9 shrink-0 text-link max-sm:h-11 max-sm:w-11 pointer-coarse:h-11 pointer-coarse:w-11" aria-label="New content" title="Add content">
        <Plus className="h-4 w-4" aria-hidden />
      </Button>
    </DropdownMenu.Trigger>
    <DropdownMenu.Portal>
      <DropdownMenu.Content align="end" sideOffset={8} collisionPadding={12} aria-label="Create content"
        onCloseAutoFocus={event => { if (launching.current) event.preventDefault(); }}
        className="z-[var(--z-popover)] min-w-48 rounded-[var(--radius-md)] border border-border bg-surface p-1 shadow-md">
        {[
          { label: "New document", Icon: FilePlus, action: onDocument },
          { label: "New collection", Icon: FolderPlus, action: onCollection },
          { label: "New table", Icon: Table2, action: onTable },
          { label: "Upload file", Icon: Upload, action: onUpload },
        ].map(({ label, Icon, action }) => <DropdownMenu.Item key={label}
          onSelect={() => {
            launching.current = true;
            // The shared document dialog records the active element. Record a
            // persistent trigger, not a menu item that disappears on selection.
            triggerRef.current?.focus();
            action();
          }}
          className="flex min-h-11 cursor-pointer items-center gap-2 rounded-[var(--radius-sm)] px-3 text-sm text-foreground outline-none data-[highlighted]:bg-surface-hover focus-visible:ring-2 focus-visible:ring-inset focus-visible:ring-ring">
          <Icon className="h-4 w-4 text-foreground-muted" aria-hidden />{label}
        </DropdownMenu.Item>)}
      </DropdownMenu.Content>
    </DropdownMenu.Portal>
  </DropdownMenu.Root>;
}
