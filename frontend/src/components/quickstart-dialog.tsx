import { useCallback, useState, type FocusEvent, type RefObject } from "react";
import { Dialog, DialogContent, DialogHeader, DialogTitle, DialogDescription, DialogFooter } from "@/components/ui/dialog";
import { Button } from "@/components/ui/button";
import { ConfirmDialog } from "@/components/ui/confirm-dialog";
import { ConnectionSetup } from "@/components/connection-setup";
import { isModalOpen } from "@/lib/modal-visibility";

export const QUICKSTART_DISMISS_KEY = "akb.quickstartDismissed";

function revealSetupFocus(event: FocusEvent<HTMLDivElement>) {
  const scroller = event.currentTarget;
  // Radix's focus trap wraps with preventScroll. Reveal the full radio label
  // inside this body only, without shifting the modal or background workspace.
  const target = event.target instanceof HTMLInputElement && event.target.type === "radio"
    ? event.target.closest("label") ?? event.target
    : event.target;
  const viewport = scroller.getBoundingClientRect();
  const bounds = target.getBoundingClientRect();
  const top = viewport.top + 4;
  const bottom = viewport.bottom - 4;
  if (bounds.top < top) scroller.scrollTop += bounds.top - top;
  else if (bounds.bottom > bottom) scroller.scrollTop += Math.min(bounds.bottom - bottom, bounds.top - top);
}

export function QuickstartDialog({ open, onOpenChange, onTokenCreated, mcpOauthEnabled, returnFocusRef }: {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  onTokenCreated?: () => void;
  mcpOauthEnabled: boolean;
  returnFocusRef?: RefObject<HTMLElement | null>;
}) {
  const [hasSecret, setHasSecret] = useState(false);
  const [createdSecret, setCreatedSecret] = useState(false);
  const [busy, setBusy] = useState(false);
  const [confirmClose, setConfirmClose] = useState(false);
  const reportDirty = useCallback((dirty: boolean) => { setHasSecret(dirty); if (!dirty) setCreatedSecret(false); }, []);
  function close() {
    setHasSecret(false);
    setCreatedSecret(false);
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
      <DialogContent className="connection-dialog flex max-w-3xl flex-col gap-0 overflow-hidden p-0" onCloseAutoFocus={event => {
        if (!returnFocusRef) return;
        event.preventDefault();
        // The floating trigger becomes visible after the last modal releases
        // ownership. Never steal focus from another modal or a new route.
        requestAnimationFrame(() => {
          const target = returnFocusRef.current;
          if (target?.isConnected && !isModalOpen()) target.focus({ preventScroll: true });
        });
      }}>
        <DialogHeader className="shrink-0 border-b border-border px-5 py-5 pr-14 sm:px-7 sm:pr-14">
          <DialogTitle>Connect an AI tool</DialogTitle>
          <DialogDescription>Choose your tool, add AKB, then check the result in that tool.</DialogDescription>
        </DialogHeader>
        {open && <div data-connection-scroll className="min-h-0 overflow-y-auto px-5 pt-5 sm:px-7" onFocusCapture={revealSetupFocus}><ConnectionSetup mcpOauthEnabled={mcpOauthEnabled} onTokenCreated={onTokenCreated} onDirtyChange={reportDirty} onSecretCreated={() => setCreatedSecret(true)} onBusyChange={setBusy} /></div>}
        <DialogFooter className="connection-dialog-footer shrink-0 border-t border-border px-5 py-3 sm:px-7"><Button variant="outline" disabled={busy} onClick={() => requestClose(false)}>Close</Button></DialogFooter>
      </DialogContent>
    </Dialog>
    <ConfirmDialog className="connection-confirmation" open={confirmClose} onOpenChange={setConfirmClose} title={createdSecret ? "Have you saved your connection key?" : "Leave connection setup?"} description={createdSecret ? "Save the key or its configuration privately before closing. The new key cannot be shown again here. Closing does not revoke it." : "Your setup draft and any entered key will be cleared from this window. Existing keys and connections remain active."} confirmLabel={createdSecret ? "I've saved it — close" : "Leave setup"} cancelLabel="Keep setup open" onConfirm={close} />
  </>;
}
