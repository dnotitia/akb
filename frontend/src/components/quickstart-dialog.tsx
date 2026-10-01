import { useState, type RefObject } from "react";
import { Dialog, DialogContent, DialogHeader, DialogTitle, DialogDescription, DialogFooter } from "@/components/ui/dialog";
import { Button } from "@/components/ui/button";
import { ConfirmDialog } from "@/components/ui/confirm-dialog";
import { ConnectionSetup } from "@/components/connection-setup";
import { isModalOpen } from "@/lib/modal-visibility";

export const QUICKSTART_DISMISS_KEY = "akb.quickstartDismissed";

export function QuickstartDialog({ open, onOpenChange, onTokenCreated, mcpOauthEnabled, returnFocusRef }: {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  onTokenCreated?: () => void;
  mcpOauthEnabled: boolean;
  returnFocusRef?: RefObject<HTMLElement | null>;
}) {
  const [hasSecret, setHasSecret] = useState(false);
  const [busy, setBusy] = useState(false);
  const [confirmClose, setConfirmClose] = useState(false);
  function close() {
    setHasSecret(false);
    setConfirmClose(false);
    onOpenChange(false);
  }
  function requestClose(next: boolean) {
    if (busy) return;
    if (next) onOpenChange(true);
    else if (hasSecret) setConfirmClose(true);
    else close();
  }
  return <>
    <Dialog open={open} onOpenChange={requestClose}>
      <DialogContent className="flex max-w-3xl flex-col gap-0 overflow-hidden p-0" onCloseAutoFocus={event => {
        if (!returnFocusRef) return;
        event.preventDefault();
        // The floating trigger becomes visible after the last modal releases
        // ownership. Never steal focus from another modal or a new route.
        requestAnimationFrame(() => {
          const target = returnFocusRef.current;
          if (target?.isConnected && !isModalOpen()) target.focus({ preventScroll: true });
        });
      }}>
        <DialogHeader className="shrink-0 border-b border-border px-5 py-5 pr-12 sm:px-7 sm:pr-12">
          <DialogTitle>Connect an agent</DialogTitle>
          <DialogDescription>Choose your tool. Follow the three steps below.</DialogDescription>
        </DialogHeader>
        {open && <div className="min-h-0 overflow-y-auto px-5 pt-5 sm:px-7"><ConnectionSetup mcpOauthEnabled={mcpOauthEnabled} onTokenCreated={onTokenCreated} onDirtyChange={setHasSecret} onBusyChange={setBusy} /></div>}
        <DialogFooter className="shrink-0 border-t border-border px-5 py-3 sm:px-7"><Button variant="outline" disabled={busy} onClick={() => requestClose(false)}>Close</Button></DialogFooter>
      </DialogContent>
    </Dialog>
    <ConfirmDialog open={confirmClose} onOpenChange={setConfirmClose} title="Have you saved your token?" description="Unsaved token options will be discarded. Any new token cannot be shown again after closing; save it or its configuration somewhere private first. Closing does not revoke it." confirmLabel="I've saved it — close" cancelLabel="Keep setup open" onConfirm={close} />
  </>;
}
