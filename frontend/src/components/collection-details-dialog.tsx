import { useEffect, useState } from "react";
import { FileText, Folder, Paperclip, Table2 } from "lucide-react";
import { ApiError, updateCollection } from "@/lib/api";
import { Alert } from "@/components/ui/alert";
import { Button } from "@/components/ui/button";
import { Label } from "@/components/ui/label";
import { Textarea } from "@/components/ui/textarea";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";

export interface CollectionResourceCounts {
  documents: number;
  tables: number;
  files: number;
}

interface CollectionDetailsDialogProps {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  vault: string;
  path: string;
  summary?: string | null;
  counts: CollectionResourceCounts;
  editable: boolean;
  onUpdated: () => void;
}

export function CollectionDetailsDialog({
  open,
  onOpenChange,
  vault,
  path,
  summary,
  counts,
  editable,
  onUpdated,
}: CollectionDetailsDialogProps) {
  const [draft, setDraft] = useState(summary ?? "");
  const [working, setWorking] = useState(false);
  const [error, setError] = useState("");
  const [compatibilityNotice, setCompatibilityNotice] = useState("");

  useEffect(() => {
    if (open) {
      setDraft(summary ?? "");
      setError("");
      setCompatibilityNotice("");
    }
  }, [open, path, summary]);

  const normalizedDraft = draft.trim();
  const normalizedSummary = (summary ?? "").trim();
  const changed = normalizedDraft !== normalizedSummary;

  async function handleSave() {
    if (!editable || !changed) return;
    setWorking(true);
    setError("");
    setCompatibilityNotice("");
    try {
      await updateCollection(vault, path, normalizedDraft || null);
      onUpdated();
      onOpenChange(false);
    } catch (e) {
      if (e instanceof ApiError && (e.status === 404 || e.status === 405)) {
        setCompatibilityNotice(
          "Collection summary editing is coming soon on this server. You can still review the current summary and contents here.",
        );
      } else {
        setError(e instanceof Error ? e.message : "Couldn't update the collection summary.");
      }
    } finally {
      setWorking(false);
    }
  }

  const name = path.split("/").filter(Boolean).pop() || path;
  const metrics = [
    { label: "Documents", value: counts.documents, icon: FileText },
    { label: "Tables", value: counts.tables, icon: Table2 },
    { label: "Files", value: counts.files, icon: Paperclip },
  ];

  return (
    <Dialog open={open} onOpenChange={(next) => !working && onOpenChange(next)}>
      <DialogContent className="max-w-lg">
        <DialogHeader>
          <div className="flex min-w-0 items-center gap-2">
            <span
              className="inline-flex h-8 w-8 shrink-0 items-center justify-center rounded-[var(--radius-md)] bg-surface-selected text-surface-selected-foreground"
              aria-hidden
            >
              <Folder className="h-4 w-4" aria-hidden />
            </span>
            <div className="min-w-0">
              <DialogTitle className="truncate">{name}</DialogTitle>
              <DialogDescription className="truncate font-mono">
                {path}
              </DialogDescription>
            </div>
          </div>
        </DialogHeader>

        <div className="grid grid-cols-3 divide-x divide-border border-y border-border bg-surface-2/45">
          {metrics.map(({ label, value, icon: Icon }) => (
            <div key={label} className="min-w-0 px-3 py-2.5">
              <div className="flex items-center gap-1.5 text-xs text-foreground-muted">
                <Icon className="h-3.5 w-3.5 shrink-0" aria-hidden />
                <span className="truncate">{label}</span>
              </div>
              <p className="mt-1 text-sm font-semibold tabular-nums text-foreground">
                {value}
              </p>
            </div>
          ))}
        </div>

        <div>
          {editable ? (
            <>
              <Label htmlFor="collection-details-summary" className="coord-ink mb-1.5 block">
                Summary
              </Label>
              <Textarea
                id="collection-details-summary"
                value={draft}
                onChange={(event) => setDraft(event.target.value)}
                placeholder="What belongs in this collection?"
                rows={4}
                disabled={working}
              />
              <p className="mt-1.5 text-xs leading-relaxed text-foreground-muted">
                Help people understand what belongs here before they open individual resources.
              </p>
            </>
          ) : (
            <>
              <p className="coord-ink mb-1.5">Summary</p>
              <p className="min-h-20 rounded-[var(--radius-md)] border border-border bg-surface-2/45 px-3 py-2.5 text-sm leading-relaxed text-foreground-muted">
                {normalizedSummary || "No summary has been added yet."}
              </p>
            </>
          )}
        </div>

        {compatibilityNotice && <Alert variant="info">{compatibilityNotice}</Alert>}
        {error && <Alert variant="destructive">{error}</Alert>}

        <DialogFooter>
          <Button
            type="button"
            variant="outline"
            onClick={() => onOpenChange(false)}
            disabled={working}
          >
            {editable ? "Cancel" : "Close"}
          </Button>
          {editable && (
            <Button
              type="button"
              onClick={handleSave}
              loading={working}
              disabled={!changed}
            >
              {working ? "Saving…" : "Save summary"}
            </Button>
          )}
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
