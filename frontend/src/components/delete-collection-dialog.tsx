import { useEffect, useState } from "react";
import { AlertTriangle, Loader2 } from "lucide-react";
import { ApiError, deleteCollection, type CollectionDeleteResult } from "@/lib/api";
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

interface DeleteCollectionDialogProps {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  vault: string;
  path: string;
  docCount: number;
  fileCount: number;
  onDeleted: () => void;
}

/** Two-mode delete dialog for collections.
 *
 *  - Empty mode (`docCount + fileCount === 0`): one-click confirm.
 *  - Cascade mode (any count > 0): requires the user to type the collection
 *    path exactly before the destructive button enables — mirrors the
 *    type-name-to-confirm pattern used by `DeleteVaultDialog`.
 *
 *  The `recursive` boolean passed to the API reflects which mode submitted:
 *  cascade=true, empty=false. If the server replies 409 (
 *  `collection_not_empty`) for an empty-mode submission, that's the
 *  TOCTOU race where docs landed between the parent's fetch and our
 *  submit — we surface the message and stay open. The parent should
 *  refresh and reopen with updated counts. */
export function DeleteCollectionDialog({
  open,
  onOpenChange,
  vault,
  path,
  docCount,
  fileCount,
  onDeleted,
}: DeleteCollectionDialogProps) {
  const isCascade = docCount > 0 || fileCount > 0;
  const [typed, setTyped] = useState("");
  const [working, setWorking] = useState(false);
  const [error, setError] = useState("");

  useEffect(() => {
    if (!open) {
      setTyped("");
      setError("");
    }
  }, [open]);

  const matches = typed.trim() === path;
  const canDelete = !isCascade || matches;

  async function handleDelete() {
    if (!canDelete) return;
    setWorking(true);
    setError("");
    try {
      const _result: CollectionDeleteResult = await deleteCollection(
        vault,
        path,
        isCascade,
      );
      void _result;
      onDeleted();
      onOpenChange(false);
    } catch (e) {
      // Defensive 409 path: if the server reports collection_not_empty after
      // we submitted in empty mode, that's a TOCTOU race. Encourage refresh.
      if (
        e instanceof ApiError &&
        e.status === 409 &&
        !isCascade
      ) {
        setError(
          `${e.message} — refresh the tree and reopen this dialog to see the latest counts.`,
        );
      } else {
        const msg = e instanceof Error ? e.message : String(e);
        setError(msg || "Delete failed");
      }
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
            {isCascade
              ? "Delete collection and all contents"
              : "Delete empty collection"}
          </DialogTitle>
          <DialogDescription>
            {isCascade ? (
              <>
                This will permanently delete{" "}
                <span className="font-semibold text-foreground">
                  {docCount} document{docCount === 1 ? "" : "s"}
                </span>{" "}
                and{" "}
                <span className="font-semibold text-foreground">
                  {fileCount} file{fileCount === 1 ? "" : "s"}
                </span>{" "}
                in{" "}
                <span className="font-mono font-semibold text-foreground">
                  {path}
                </span>
                .
              </>
            ) : (
              <>
                Delete empty collection{" "}
                <span className="font-mono font-semibold text-foreground">
                  {path}
                </span>
                ?
              </>
            )}
          </DialogDescription>
        </DialogHeader>

        {isCascade && (
          <div className="space-y-4">
            <div>
              <Label
                htmlFor="confirm-collection-path"
                className="coord-ink mb-1.5 block"
              >
                Type the collection path to confirm
              </Label>
              <Input
                id="confirm-collection-path"
                value={typed}
                onChange={(e) => setTyped(e.target.value)}
                placeholder={path}
                autoComplete="off"
                autoFocus
                className="font-mono"
                disabled={working}
              />
              <p className="text-xs text-foreground-muted mt-1.5">
                Delete enables once{" "}
                <span className="font-mono">{path}</span> is typed exactly.
              </p>
            </div>
          </div>
        )}

        {error && (
          <div
            role="alert"
            className="border border-destructive p-2 text-xs text-destructive"
          >
            {error}
          </div>
        )}

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
            disabled={!canDelete || working}
          >
            {working ? (
              <>
                <Loader2 className="h-4 w-4 animate-spin" aria-hidden />
                Deleting…
              </>
            ) : (
              "Delete"
            )}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
