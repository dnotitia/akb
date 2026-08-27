import { useEffect, useMemo, useRef, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import {
  ArrowDown,
  FileText,
  FolderTree,
  GitCommitHorizontal,
} from "lucide-react";
import {
  ApiError,
  browseVault,
  moveDocument,
  type DocumentMoveResult,
} from "@/lib/api";
import { previewDocumentSlug } from "@/lib/document-move";
import { isReservedCollection } from "@/lib/skill";
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
import { SelectMenu, type SelectOption } from "@/components/ui/select-menu";

interface DocumentMoveDialogProps {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  vault: string;
  path: string;
  title: string;
  onMoved: (result: DocumentMoveResult) => void;
}

function splitDocumentPath(path: string) {
  const stem = path.endsWith(".md") ? path.slice(0, -3) : path;
  const separator = stem.lastIndexOf("/");
  return separator < 0
    ? { collection: "", slug: stem }
    : {
        collection: stem.slice(0, separator),
        slug: stem.slice(separator + 1),
      };
}

function moveErrorMessage(error: unknown) {
  const message = error instanceof Error ? error.message : String(error);
  const status = error instanceof ApiError ? error.status : null;

  if (status === 409 || /already exists|\b409\b/i.test(message)) {
    return "A document already uses that file name in the selected collection. Choose another file name.";
  }
  if (/move is a no-op|target path equals/i.test(message)) {
    return "Choose a different collection or file name.";
  }
  if (status === 403 || /forbidden|required role|writer access/i.test(message)) {
    return "Your access changed. Writer access or higher is required to move this document.";
  }
  if (
    status === 404 ||
    status === 405 ||
    /\b405 method not allowed\b/i.test(message) ||
    /^404 not found$/i.test(message.trim())
  ) {
    return "Move or rename is not available on this server yet.";
  }
  return message || "The document could not be moved. Review the destination and try again.";
}

export function DocumentMoveDialog({
  open,
  onOpenChange,
  vault,
  path,
  title,
  onMoved,
}: DocumentMoveDialogProps) {
  const current = useMemo(() => splitDocumentPath(path), [path]);
  const [collection, setCollection] = useState(current.collection);
  const [fileName, setFileName] = useState(current.slug);
  const [message, setMessage] = useState("");
  const [error, setError] = useState("");
  const [moving, setMoving] = useState(false);
  const [discardOpen, setDiscardOpen] = useState(false);
  const hydratedPathRef = useRef(path);

  const collectionsQuery = useQuery({
    queryKey: ["document-move-collections", vault],
    queryFn: () => browseVault(vault, undefined, -1),
    enabled: open && Boolean(vault),
    retry: false,
    staleTime: 30_000,
  });

  useEffect(() => {
    if (!open) return;
    hydratedPathRef.current = path;
    setCollection(current.collection);
    setFileName(current.slug);
    setMessage("");
    setError("");
    setMoving(false);
  }, [current.collection, current.slug, open, path]);

  const collectionOptions = useMemo<SelectOption[]>(() => {
    const paths = new Set<string>();
    for (const item of collectionsQuery.data?.items || []) {
      if (item?.type !== "collection" || typeof item.path !== "string") continue;
      if (isReservedCollection(item.path)) continue;
      paths.add(item.path);
    }
    if (current.collection && !isReservedCollection(current.collection)) {
      paths.add(current.collection);
    }
    return [
      { value: "", label: "Vault root", hint: "Outside every collection" },
      ...Array.from(paths)
        .sort((a, b) => a.localeCompare(b))
        .map((value) => ({
          value,
          label: value,
          hint: value === current.collection ? "Current collection" : undefined,
        })),
    ];
  }, [collectionsQuery.data?.items, current.collection]);

  const normalizedSlug = previewDocumentSlug(fileName);
  const destinationPath = collection
    ? `${collection}/${normalizedSlug}.md`
    : `${normalizedSlug}.md`;
  const destinationChanged = destinationPath !== path;
  const isDirty =
    collection !== current.collection ||
    fileName !== current.slug ||
    message.trim().length > 0;

  function requestClose() {
    if (moving) return;
    if (isDirty) setDiscardOpen(true);
    else onOpenChange(false);
  }

  async function handleSubmit(event: React.FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!fileName.trim()) {
      setError("Enter a file name.");
      return;
    }
    if (!destinationChanged) {
      setError("Choose a different collection or file name.");
      return;
    }

    const payload: { collection?: string; slug?: string; message?: string } = {};
    if (collection !== current.collection) payload.collection = collection;
    if (normalizedSlug !== current.slug) payload.slug = fileName.trim();
    if (message.trim()) payload.message = message.trim();

    setMoving(true);
    setError("");
    let result: DocumentMoveResult;
    try {
      result = await moveDocument(vault, hydratedPathRef.current, payload);
    } catch (nextError) {
      setError(moveErrorMessage(nextError));
      setMoving(false);
      return;
    }

    // The move has already committed when this callback runs. Keep local UI
    // follow-up failures out of the request error state so users are never
    // invited to retry an operation the backend has successfully completed.
    setMoving(false);
    onMoved(result);
    onOpenChange(false);
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
          className="max-w-2xl gap-0 overflow-hidden p-0"
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
              <DialogTitle>Move or rename document</DialogTitle>
              <DialogDescription>
                Change where “{title}” lives without losing its identity or version history.
              </DialogDescription>
            </DialogHeader>

            <div className="space-y-5 px-5 py-5 sm:px-6">
              <section aria-label="Location change" className="overflow-hidden rounded-[var(--radius-lg)] border border-border bg-surface shadow-xs">
                <div className="flex items-center gap-3 px-3.5 py-3">
                  <span className="flex h-8 w-8 shrink-0 items-center justify-center rounded-[var(--radius-md)] bg-surface-2 text-foreground-muted">
                    <FileText className="h-4 w-4" aria-hidden />
                  </span>
                  <div className="min-w-0">
                    <p className="text-xs font-medium text-foreground-muted">Current location</p>
                    <code className="block truncate font-mono text-sm text-foreground" title={path}>{path}</code>
                  </div>
                </div>
                <div className="flex h-7 items-center border-y border-border bg-surface-2 px-4 text-foreground-muted">
                  <ArrowDown className="h-3.5 w-3.5" aria-hidden />
                  <span className="ml-2 text-xs">Destination preview</span>
                </div>
                <div className="flex items-center gap-3 bg-surface-selected px-3.5 py-3 text-surface-selected-foreground">
                  <span className="flex h-8 w-8 shrink-0 items-center justify-center rounded-[var(--radius-md)] border border-primary/20 bg-surface">
                    <FolderTree className="h-4 w-4" aria-hidden />
                  </span>
                  <code className="min-w-0 truncate font-mono text-sm font-medium" title={destinationPath}>
                    {destinationPath}
                  </code>
                </div>
              </section>

              <div className="grid gap-4 sm:grid-cols-2">
                <div className="space-y-1.5">
                  <Label htmlFor="document-move-collection">Target collection</Label>
                  <SelectMenu
                    id="document-move-collection"
                    value={collection}
                    onValueChange={(value) => {
                      setCollection(value);
                      setError("");
                    }}
                    options={collectionOptions}
                    placeholder={collectionsQuery.isPending ? "Loading collections…" : "Select a collection"}
                    disabled={collectionsQuery.isPending || collectionsQuery.isError || moving}
                    searchable
                    searchPlaceholder="Filter collections…"
                    mono
                  />
                  <p className="text-xs leading-relaxed text-foreground-muted">
                    Choose Vault root to move the document outside every collection.
                  </p>
                </div>

                <div className="space-y-1.5">
                  <Label htmlFor="document-move-filename">File name</Label>
                  <Input
                    id="document-move-filename"
                    value={fileName}
                    onChange={(event) => {
                      setFileName(event.currentTarget.value);
                      setError("");
                    }}
                    autoFocus
                    disabled={moving}
                    aria-invalid={Boolean(error && !fileName.trim()) || undefined}
                    aria-describedby="document-move-filename-help"
                  />
                  <p id="document-move-filename-help" className="text-xs leading-relaxed text-foreground-muted">
                    The .md extension is added automatically. The document title stays unchanged.
                  </p>
                </div>
              </div>

              <div className="space-y-1.5">
                <Label htmlFor="document-move-message">
                  Commit message <span className="font-normal text-foreground-muted">(optional)</span>
                </Label>
                <div className="relative">
                  <GitCommitHorizontal className="pointer-events-none absolute left-3 top-3 h-4 w-4 text-foreground-muted" aria-hidden />
                  <Input
                    id="document-move-message"
                    value={message}
                    onChange={(event) => {
                      setMessage(event.currentTarget.value);
                      setError("");
                    }}
                    className="pl-9"
                    placeholder="Explain why this location changed"
                    disabled={moving}
                  />
                </div>
              </div>

              {collectionsQuery.isError && (
                <Alert variant="destructive" title="Collections could not be loaded">
                  <span>
                    You can still rename this document in its current location, or retry loading other destinations.
                  </span>
                  <Button
                    type="button"
                    variant="outline"
                    size="sm"
                    className="mt-2"
                    onClick={() => collectionsQuery.refetch()}
                  >
                    Retry
                  </Button>
                </Alert>
              )}
              {error && <Alert variant="destructive">{error}</Alert>}
            </div>

            <DialogFooter className="border-t border-border bg-surface-2 px-5 py-4 sm:px-6">
              <Button type="button" variant="outline" onClick={requestClose} disabled={moving}>
                Cancel
              </Button>
              <Button
                type="submit"
                variant="accent"
                loading={moving}
                disabled={
                  moving ||
                  collectionsQuery.isPending ||
                  !fileName.trim() ||
                  !destinationChanged
                }
              >
                {moving ? "Moving…" : "Move document"}
              </Button>
            </DialogFooter>
          </form>
        </DialogContent>
      </Dialog>

      <ConfirmDialog
        open={discardOpen}
        onOpenChange={setDiscardOpen}
        title="Discard destination changes?"
        description="The document has not moved yet. Your selected collection, file name, and commit message will be lost."
        confirmLabel="Discard changes"
        variant="destructive"
        onConfirm={() => {
          setDiscardOpen(false);
          onOpenChange(false);
        }}
      />
    </>
  );
}
