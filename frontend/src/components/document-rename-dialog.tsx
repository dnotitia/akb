import { useEffect, useMemo, useRef, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { browseVault, updateDocument } from "@/lib/api";
import { DocumentTitleConflictNotice } from "@/components/document-title-conflict-notice";
import { Alert } from "@/components/ui/alert";
import { Button } from "@/components/ui/button";
import { ConfirmDialog } from "@/components/ui/confirm-dialog";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import {
  documentCollection,
  documentTitleConflictFromError,
  findDocumentTitleConflict,
  type DocumentTitleConflict,
  type DocumentTitleCandidate,
} from "@/lib/document-title-conflict";

interface DocumentRenameDialogProps {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  vault: string;
  docId: string;
  path: string;
  title: string;
  onRenamed: (title: string) => void;
  onOpenDocument?: (path: string) => void;
}

export function DocumentRenameDialog({
  open,
  onOpenChange,
  vault,
  docId,
  path,
  title: currentTitle,
  onRenamed,
  onOpenDocument,
}: DocumentRenameDialogProps) {
  const [title, setTitle] = useState(currentTitle);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState("");
  const [discardOpen, setDiscardOpen] = useState(false);
  const [serverConflict, setServerConflict] = useState<DocumentTitleConflict | null>(null);
  const titleRef = useRef<HTMLInputElement | null>(null);
  const errorRef = useRef<HTMLDivElement | null>(null);
  const conflictRef = useRef<HTMLDivElement | null>(null);

  const documentsQuery = useQuery({
    queryKey: ["document-rename-documents", vault],
    queryFn: () => browseVault(vault, undefined, -1),
    enabled: open && Boolean(vault),
    retry: false,
    staleTime: 30_000,
  });

  useEffect(() => {
    if (!open) return;
    setTitle(currentTitle);
    setSaving(false);
    setError("");
    setServerConflict(null);
  }, [currentTitle, open]);

  const normalizedTitle = title.trim();
  const isDirty = normalizedTitle !== currentTitle.trim();
  const documents = useMemo<DocumentTitleCandidate[]>(
    () =>
      (documentsQuery.data?.items || []).filter(
        (item): item is DocumentTitleCandidate & { type: "document" } =>
          item?.type === "document" &&
          typeof item.name === "string" &&
          typeof item.path === "string",
      ),
    [documentsQuery.data?.items],
  );
  const localConflict = useMemo(
    () =>
      isDirty
        ? findDocumentTitleConflict(
            documents,
            normalizedTitle,
            documentCollection(path),
            path,
          )
        : null,
    [documents, isDirty, normalizedTitle, path],
  );
  const titleConflict = serverConflict ?? localConflict;

  function requestClose() {
    if (saving) return;
    if (isDirty) setDiscardOpen(true);
    else onOpenChange(false);
  }

  async function performRename(titleConflictPolicy: "allow" | "reject") {
    if (!normalizedTitle) {
      setError("Enter a document title.");
      window.requestAnimationFrame(() => titleRef.current?.focus());
      return;
    }
    if (!isDirty) return;

    setSaving(true);
    setError("");
    try {
      await updateDocument(vault, docId, {
        title: normalizedTitle,
        title_conflict_policy: titleConflictPolicy,
      });
      onRenamed(normalizedTitle);
      onOpenChange(false);
    } catch (nextError) {
      const conflict = documentTitleConflictFromError(nextError);
      if (conflict) {
        setServerConflict(conflict);
        setSaving(false);
        window.requestAnimationFrame(() => conflictRef.current?.focus());
        return;
      }
      setError(
        nextError instanceof Error && nextError.message
          ? nextError.message
          : "The document title could not be changed. Try again.",
      );
      setSaving(false);
      window.requestAnimationFrame(() => errorRef.current?.focus());
    }
  }

  async function handleSubmit(event: React.FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (titleConflict) {
      window.requestAnimationFrame(() => conflictRef.current?.focus());
      return;
    }
    await performRename("reject");
  }

  return (
    <>
      <Dialog
        open={open}
        onOpenChange={(nextOpen) => {
          if (!nextOpen) requestClose();
        }}
      >
        <DialogContent
          className="max-w-lg gap-0 overflow-hidden p-0"
          onEscapeKeyDown={(event) => {
            event.preventDefault();
            requestClose();
          }}
          onInteractOutside={(event) => {
            event.preventDefault();
            requestClose();
          }}
        >
          <form onSubmit={handleSubmit}>
            <DialogHeader className="border-b border-border px-5 py-4 pr-14 sm:px-6">
              <DialogTitle>Rename document</DialogTitle>
              <DialogDescription>
                Change the title people see. The system-managed slug, links, and version history stay unchanged.
              </DialogDescription>
            </DialogHeader>

            <div className="space-y-4 px-5 py-5 sm:px-6">
              <div className="space-y-1.5">
                <Label htmlFor="document-rename-title">
                  Document title <span className="text-destructive">*</span>
                </Label>
                <Input
                  ref={titleRef}
                  id="document-rename-title"
                  value={title}
                  onChange={(event) => {
                    setTitle(event.currentTarget.value);
                    setError("");
                    setServerConflict(null);
                  }}
                  maxLength={256}
                  required
                  aria-required="true"
                  aria-invalid={Boolean(error) || undefined}
                  aria-describedby="document-rename-help document-rename-error"
                  autoFocus
                  disabled={saving}
                />
                <p id="document-rename-help" className="text-xs leading-relaxed text-foreground-muted">
                  Renaming changes the display title only. Existing document addresses continue to work.
                </p>
              </div>

              {titleConflict && (
                <div ref={conflictRef} tabIndex={-1} className="focus:outline-none">
                  <DocumentTitleConflictNotice
                    conflict={titleConflict}
                    onOpenExisting={
                      onOpenDocument
                        ? () => {
                            onOpenChange(false);
                            onOpenDocument(titleConflict.existingPath);
                          }
                        : undefined
                    }
                    onChooseAlternative={() => titleRef.current?.focus()}
                    chooseAlternativeLabel="Choose another title"
                    onKeepBoth={() => performRename("allow")}
                    keepBothLabel="Use duplicate title"
                    keepingBoth={saving}
                  />
                </div>
              )}

              {error && (
                <div
                  ref={errorRef}
                  id="document-rename-error"
                  tabIndex={-1}
                  className="focus:outline-none"
                >
                  <Alert variant="destructive">{error}</Alert>
                </div>
              )}
            </div>

            <DialogFooter className="border-t border-border bg-surface-2 px-5 py-4 sm:px-6">
              <Button type="button" variant="outline" onClick={requestClose} disabled={saving}>
                Cancel
              </Button>
              <Button
                type="submit"
                variant="accent"
                loading={saving}
                disabled={saving || !normalizedTitle || !isDirty || Boolean(titleConflict)}
              >
                {saving ? "Renaming…" : "Rename"}
              </Button>
            </DialogFooter>
          </form>
        </DialogContent>
      </Dialog>

      <ConfirmDialog
        open={discardOpen}
        onOpenChange={setDiscardOpen}
        title="Discard title change?"
        description="The document title has not been changed yet."
        confirmLabel="Discard change"
        variant="destructive"
        onConfirm={() => {
          setDiscardOpen(false);
          onOpenChange(false);
        }}
      />
    </>
  );
}
