import { useEffect, useState } from "react";
import { AlertTriangle } from "lucide-react";
import { deleteVaultPermanent } from "@/lib/api";
import { Alert } from "@/components/ui/alert";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";

interface DeleteVaultDialogProps {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  vault: string;
  onDeleted: () => void;
}

/** Type-name-to-confirm dialog for permanent vault deletion. The Confirm
 *  button stays disabled until the user types the exact vault name —
 *  protects against muscle-memory-clicking through a destructive flow. */
export function DeleteVaultDialog({
  open,
  onOpenChange,
  vault,
  onDeleted,
}: DeleteVaultDialogProps) {
  const [typed, setTyped] = useState("");
  const [working, setWorking] = useState(false);
  const [error, setError] = useState("");

  useEffect(() => {
    if (!open) {
      setTyped("");
      setError("");
    }
  }, [open]);

  const matches = typed === vault;

  async function handleDelete() {
    if (!matches) return;
    setWorking(true);
    setError("");
    try {
      await deleteVaultPermanent(vault);
      onDeleted();
      onOpenChange(false);
    } catch (e: any) {
      setError(e?.message || "Delete failed");
    } finally {
      setWorking(false);
    }
  }

  return (
    <Dialog open={open} onOpenChange={(o) => !working && onOpenChange(o)}>
      <DialogContent className="max-w-xl">
        <DialogHeader>
          <DialogTitle className="flex items-center gap-2 text-destructive">
            <AlertTriangle className="h-5 w-5" aria-hidden />
            Permanently delete vault
          </DialogTitle>
          <DialogDescription>
            This removes <span className="font-mono font-semibold text-foreground">{vault}</span>{" "}
            and everything inside it: documents, tables, files (including S3 objects),
            relations, embeddings, the git repository, and access grants. This cannot
            be undone.
          </DialogDescription>
        </DialogHeader>

        <div className="space-y-4">
          <Alert variant="warning" title="No recovery path">
            If you only want to make the vault read-only, use{" "}
            <span className="font-mono">Archive</span> in the lifecycle section
            instead — that's reversible. Delete is final.
          </Alert>

          <div>
            <Label htmlFor="confirm-name" className="coord-ink mb-1.5 block">
              TYPE THE VAULT NAME TO CONFIRM
            </Label>
            <Input
              id="confirm-name"
              value={typed}
              onChange={(e) => setTyped(e.target.value)}
              placeholder={vault}
              autoComplete="off"
              autoFocus
              className="font-mono"
              disabled={working}
            />
            <p className="text-xs text-foreground-muted mt-1.5">
              Confirm enables once <span className="font-mono">{vault}</span> is typed exactly.
            </p>
          </div>

          {error && <Alert variant="destructive">{error}</Alert>}
        </div>

        <DialogFooter>
          <Button
            type="button"
            variant="outline"
            onClick={() => onOpenChange(false)}
            disabled={working}
          >
            Cancel
          </Button>
          <Button
            type="button"
            variant="destructive"
            onClick={handleDelete}
            loading={working}
            disabled={!matches}
          >
            {working ? "Deleting…" : "Delete forever"}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
