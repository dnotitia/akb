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
  /**
   * Number of descendant collection rows under this path. Surfaces the
   * nested-parent case where the client tree synthesizes a parent that
   * has no backing collection row but does have children (e.g. only
   * `test/test` exists, user clicks trash on synthesized `test`).
   * Without this, the dialog would open in empty-mode and the backend
   * would 409 with `sub_collection_count >= 1`.
   */
  subCollectionCount: number;
  onDeleted: () => void;
}

/** Two-mode delete dialog for collections.
 *
 *  - Empty mode (`docCount + fileCount + subCollectionCount === 0`):
 *    one-click confirm.
 *  - Cascade mode (any count > 0): requires the user to type the collection
 *    path exactly before the destructive button enables, and shows a
 *    prominent destructive alert banner listing the affected categories.
 *
 *  The `recursive` boolean passed to the API reflects which mode submitted:
 *  cascade=true, empty=false. If the server replies 409 (
 *  `collection_not_empty`) for an empty-mode submission, that's the
 *  TOCTOU race where content landed between the parent's snapshot and
 *  our submit — we surface the message and stay open. The parent
 *  should refresh and reopen with updated counts. */
export function DeleteCollectionDialog({
  open,
  onOpenChange,
  vault,
  path,
  docCount,
  fileCount,
  subCollectionCount,
  onDeleted,
}: DeleteCollectionDialogProps) {
  const isCascade = docCount + fileCount + subCollectionCount > 0;
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

  // Bulleted item list for the destructive banner. Each line is skipped
  // when its count is zero so the banner only enumerates real impact.
  const items: string[] = [];
  if (docCount > 0) {
    items.push(`${docCount} document${docCount === 1 ? "" : "s"}`);
  }
  if (fileCount > 0) {
    items.push(`${fileCount} file${fileCount === 1 ? "" : "s"}`);
  }
  if (subCollectionCount > 0) {
    items.push(
      `${subCollectionCount} sub-collection${subCollectionCount === 1 ? "" : "s"}`,
    );
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
                Cascade delete of{" "}
                <code className="font-mono font-semibold text-foreground">
                  {path}
                </code>
                .
              </>
            ) : (
              <>
                Delete empty collection{" "}
                <code className="font-mono font-semibold text-foreground">
                  {path}
                </code>
                ?
              </>
            )}
          </DialogDescription>
        </DialogHeader>

        {isCascade && (
          <div className="space-y-4">
            {/* Destructive alert banner — strongest visual cue. The
             *  bulleted enumeration replaces the previous inline doc/file
             *  body sentence so users see counts in a list form that
             *  scans faster, and the heading is mono uppercase to mirror
             *  the visual language used elsewhere for warnings. */}
            <div
              role="alert"
              className="border border-destructive/40 bg-destructive/10 p-3 flex items-start gap-3"
            >
              <AlertTriangle
                className="h-5 w-5 shrink-0 text-destructive mt-0.5"
                aria-hidden
              />
              <div className="flex-1 min-w-0 space-y-2">
                <p className="font-mono text-xs uppercase tracking-wide font-semibold text-destructive">
                  Permanent deletion · cannot be undone
                </p>
                <ul className="list-disc pl-5 text-sm text-foreground space-y-0.5">
                  {items.map((line) => (
                    <li key={line}>{line}</li>
                  ))}
                  <li>
                    Path{" "}
                    <code className="font-mono font-semibold">{path}</code>
                  </li>
                </ul>
              </div>
            </div>

            <div>
              <Label
                htmlFor="confirm-collection-path"
                className="coord-ink mb-1.5 block"
              >
                Type the path to confirm permanent deletion
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
                <code className="font-mono">{path}</code> is typed exactly.
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
