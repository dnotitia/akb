import { useEffect, useState, type RefObject } from "react";
import { VaultCreateForm } from "@/components/vault-create-form";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";

export function VaultCreateDialog({
  open,
  onOpenChange,
  onCreated,
  returnFocusRef,
}: {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  onCreated: (name: string) => void;
  returnFocusRef?: RefObject<HTMLElement | null>;
}) {
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    if (!open) setBusy(false);
  }, [open]);

  function handleOpenChange(next: boolean) {
    if (!next && busy) return;
    onOpenChange(next);
  }

  return (
    <Dialog open={open} onOpenChange={handleOpenChange}>
      <DialogContent
        className="max-w-xl gap-0 p-0"
        onEscapeKeyDown={(event) => {
          if (busy) event.preventDefault();
        }}
        onPointerDownOutside={(event) => {
          if (busy) event.preventDefault();
        }}
        onCloseAutoFocus={(event) => {
          if (!returnFocusRef?.current) return;
          event.preventDefault();
          returnFocusRef.current.focus();
        }}
      >
        <DialogHeader className="border-b border-border px-6 py-5 pr-14">
          <DialogTitle>Create a vault</DialogTitle>
          <DialogDescription>
            Start with a short lowercase name. You can add collections, members, and connections after creation.
          </DialogDescription>
        </DialogHeader>
        <VaultCreateForm
          onCreated={onCreated}
          onCancel={() => handleOpenChange(false)}
          onBusyChange={setBusy}
          className="p-6"
        />
      </DialogContent>
    </Dialog>
  );
}
