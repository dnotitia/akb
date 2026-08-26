import { useEffect, useState, type CSSProperties, type RefObject } from "react";
import { DocumentCreateForm } from "@/components/document-create-form";
import { ConfirmDialog } from "@/components/ui/confirm-dialog";
import { Dialog, DialogContent } from "@/components/ui/dialog";

export interface DocumentCreateDialogProps {
  open: boolean;
  vault: string;
  initialCollection?: string;
  onOpenChange: (open: boolean) => void;
  onCreated: (path?: string) => void;
  returnFocusRef?: RefObject<HTMLElement | null>;
  /** Desktop workspace navigation width plus its visual breathing room. */
  desktopLeftOffset?: number;
}

/**
 * Vault-scoped document workbench. The dialog keeps creation in context while
 * giving the editor and metadata inspector enough room to feel like a focused
 * writing surface rather than a form squeezed into a popup.
 */
export function DocumentCreateDialog({
  open,
  vault,
  initialCollection,
  onOpenChange,
  onCreated,
  returnFocusRef,
  desktopLeftOffset = 112,
}: DocumentCreateDialogProps) {
  const [dirty, setDirty] = useState(false);
  const [creating, setCreating] = useState(false);
  const [uploading, setUploading] = useState(false);
  const [discardOpen, setDiscardOpen] = useState(false);

  useEffect(() => {
    if (open) return;
    setDirty(false);
    setCreating(false);
    setUploading(false);
    setDiscardOpen(false);
  }, [open]);

  function requestClose() {
    if (creating) return;
    if (dirty || uploading) {
      setDiscardOpen(true);
      return;
    }
    onOpenChange(false);
  }

  function handleOpenChange(next: boolean) {
    if (next) onOpenChange(true);
    // Radix can request `false` when focus returns from a native file picker.
    // The composer owns its close paths explicitly so selecting a local image
    // can never dismiss a clean draft before the upload state is observable.
  }

  return (
    <>
      <Dialog open={open} onOpenChange={handleOpenChange}>
        <DialogContent
          hideClose
          className="flex h-dvh max-h-none w-full max-w-none flex-col gap-0 !overflow-hidden rounded-none border-0 p-0 sm:h-[calc(100dvh-1rem)] sm:w-[calc(100%-1rem)] sm:rounded-[var(--radius-xl)] sm:border lg:left-[min(var(--document-dialog-left),calc(100vw-57rem))] lg:right-4 lg:h-[calc(100dvh-2rem)] lg:w-auto lg:translate-x-0"
          style={
            {
              "--document-dialog-left": `${desktopLeftOffset}px`,
            } as CSSProperties
          }
          onEscapeKeyDown={(event) => {
            event.preventDefault();
            requestClose();
          }}
          onInteractOutside={(event) => {
            // A document composer is a workbench, not a lightweight prompt.
            // Native file pickers and imprecise background clicks can move
            // focus outside the Radix layer; never let that dismiss the draft.
            // Cancel, Close, and Escape remain the explicit close paths.
            event.preventDefault();
          }}
          onCloseAutoFocus={(event) => {
            if (!returnFocusRef?.current) return;
            event.preventDefault();
            returnFocusRef.current.focus();
          }}
        >
          {open && (
            <DocumentCreateForm
              vault={vault}
              initialCollection={initialCollection}
              onCreated={onCreated}
              onRequestClose={requestClose}
              onDirtyChange={setDirty}
              onCreatingChange={setCreating}
              onUploadingChange={setUploading}
            />
          )}
        </DialogContent>
      </Dialog>

      <ConfirmDialog
        open={discardOpen}
        onOpenChange={setDiscardOpen}
        title="Discard this draft?"
        description={
          uploading
            ? "The image upload will be cancelled and your unsaved document will be lost."
            : "Your unsaved title, content, and document details will be lost."
        }
        confirmLabel="Discard draft"
        variant="destructive"
        onConfirm={() => onOpenChange(false)}
      />
    </>
  );
}
