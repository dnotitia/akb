import { useEffect, useState, type ReactNode } from "react";
import { Alert } from "@/components/ui/alert";
import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";

type Variant = "default" | "destructive";

interface ConfirmDialogProps {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  title: string;
  description?: ReactNode;
  confirmLabel?: string;
  cancelLabel?: string;
  variant?: Variant;
  onConfirm: () => void | Promise<void>;
  busy?: boolean;
}

export function ConfirmDialog({
  open,
  onOpenChange,
  title,
  description,
  confirmLabel = "Confirm",
  cancelLabel = "Cancel",
  variant = "default",
  onConfirm,
  busy = false,
}: ConfirmDialogProps) {
  const [internalBusy, setInternalBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const isBusy = busy || internalBusy;

  // Clear any stale error each time the dialog (re)opens so a prior failure
  // doesn't bleed into the next confirmation.
  useEffect(() => {
    if (open) setError(null);
  }, [open]);

  async function handleConfirm() {
    setError(null);
    setInternalBusy(true);
    try {
      await onConfirm();
      onOpenChange(false);
    } catch (e) {
      // Keep the dialog open and surface the failure inline — previously a
      // rejected onConfirm left the dialog stuck with no feedback.
      setError(e instanceof Error ? e.message : "Something went wrong. Please try again.");
    } finally {
      setInternalBusy(false);
    }
  }

  return (
    <Dialog open={open} onOpenChange={(o) => !isBusy && onOpenChange(o)}>
      <DialogContent>
        <DialogHeader>
          <DialogTitle>{title}</DialogTitle>
          {description && (
            <DialogDescription className="whitespace-pre-line leading-relaxed">
              {description}
            </DialogDescription>
          )}
        </DialogHeader>
        {error && <Alert variant="destructive">{error}</Alert>}
        <DialogFooter>
          <Button
            type="button"
            variant="outline"
            onClick={() => onOpenChange(false)}
            disabled={isBusy}
            // Destructive dialogs focus Cancel, not Confirm, so a stray Enter on
            // open can't fire the irreversible action.
            autoFocus={variant === "destructive"}
          >
            {cancelLabel}
          </Button>
          <Button
            type="button"
            variant={variant === "destructive" ? "destructive" : "default"}
            onClick={handleConfirm}
            disabled={isBusy}
            autoFocus={variant !== "destructive"}
          >
            {isBusy ? "Working…" : confirmLabel}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}

interface UseConfirmReturn {
  open: boolean;
  setOpen: (open: boolean) => void;
}

export function useConfirm(): UseConfirmReturn {
  const [open, setOpen] = useState(false);
  return { open, setOpen };
}
