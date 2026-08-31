import { useEffect, useMemo, useRef, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import {
  ArrowRight,
  ChevronRight,
  FileText,
  GitCommitHorizontal,
} from "lucide-react";
import {
  ApiError,
  browseVault,
  moveDocument,
  type DocumentMoveResult,
} from "@/lib/api";
import { isReservedCollection } from "@/lib/skill";
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
import { SelectMenu, type SelectOption } from "@/components/ui/select-menu";
import {
  documentTitleConflictFromError,
  findDocumentTitleConflict,
  type DocumentTitleConflict,
} from "@/lib/document-title-conflict";

interface DocumentMoveDialogProps {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  vault: string;
  path: string;
  title: string;
  onMoved: (result: DocumentMoveResult) => void;
  onOpenDocument?: (path: string) => void;
}

interface BrowseDocument {
  type: "document";
  name: string;
  path: string;
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

function locationLabel(collection: string) {
  return collection || "Vault root";
}

function moveErrorMessage(error: unknown) {
  const message = error instanceof Error ? error.message : String(error);
  const status = error instanceof ApiError ? error.status : null;

  if (status === 409 || /already exists|\b409\b/i.test(message)) {
    return "The destination changed before the move completed. Refresh the Collections list and try again.";
  }
  if (/move is a no-op|target path equals/i.test(message)) {
    return "Choose a different Collection.";
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
    return "Document move is not available on this server yet.";
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
  onOpenDocument,
}: DocumentMoveDialogProps) {
  const current = useMemo(() => splitDocumentPath(path), [path]);
  const [collection, setCollection] = useState(current.collection);
  const [message, setMessage] = useState("");
  const [error, setError] = useState("");
  const [moving, setMoving] = useState(false);
  const [discardOpen, setDiscardOpen] = useState(false);
  const [serverConflict, setServerConflict] = useState<DocumentTitleConflict | null>(null);
  const hydratedPathRef = useRef(path);
  const errorRef = useRef<HTMLDivElement | null>(null);
  const conflictRef = useRef<HTMLDivElement | null>(null);

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
    setMessage("");
    setError("");
    setServerConflict(null);
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

  const documents = useMemo<BrowseDocument[]>(
    () =>
      (collectionsQuery.data?.items || []).filter(
        (item): item is BrowseDocument =>
          item?.type === "document" &&
          typeof item.name === "string" &&
          typeof item.path === "string",
      ),
    [collectionsQuery.data?.items],
  );

  const destinationPath = collection
    ? `${collection}/${current.slug}.md`
    : `${current.slug}.md`;
  const destinationChanged = collection !== current.collection;
  const isDirty = destinationChanged || message.trim().length > 0;

  const pathCollision = useMemo(
    () =>
      documents.find(
        (item) => item.path !== path && item.path === destinationPath,
      ) ?? null,
    [destinationPath, documents, path],
  );
  const localConflict = useMemo(
    () => findDocumentTitleConflict(documents, title, collection, path),
    [collection, documents, path, title],
  );
  const titleConflict = serverConflict ?? localConflict;

  function requestClose() {
    if (moving) return;
    if (isDirty) setDiscardOpen(true);
    else onOpenChange(false);
  }

  function openExistingDocument(existingPath: string) {
    onOpenChange(false);
    onOpenDocument?.(existingPath);
  }

  async function performMove(titleConflictPolicy: "allow" | "reject") {
    if (!destinationChanged) {
      setError("Choose a different Collection.");
      window.requestAnimationFrame(() => errorRef.current?.focus());
      return;
    }

    const payload: {
      collection: string;
      message?: string;
      title_conflict_policy: "allow" | "reject";
    } = { collection, title_conflict_policy: titleConflictPolicy };
    if (message.trim()) payload.message = message.trim();

    setMoving(true);
    setError("");
    let result: DocumentMoveResult;
    try {
      result = await moveDocument(vault, hydratedPathRef.current, payload);
    } catch (nextError) {
      const conflict = documentTitleConflictFromError(nextError);
      if (conflict) {
        setServerConflict(conflict);
        setMoving(false);
        window.requestAnimationFrame(() => conflictRef.current?.focus());
        return;
      }
      setError(moveErrorMessage(nextError));
      setMoving(false);
      window.requestAnimationFrame(() => errorRef.current?.focus());
      return;
    }

    // The move has already committed when this callback runs. Keep local UI
    // follow-up failures out of the request error state so users are never
    // invited to retry an operation the backend has successfully completed.
    setMoving(false);
    onMoved(result);
    onOpenChange(false);
  }

  async function handleSubmit(event: React.FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (titleConflict) {
      window.requestAnimationFrame(() => conflictRef.current?.focus());
      return;
    }
    await performMove("reject");
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
              <DialogTitle>Move document</DialogTitle>
              <DialogDescription>
                Choose a new Collection. The title, stable file identity, links, and version history stay unchanged.
              </DialogDescription>
            </DialogHeader>

            <div className="max-h-[min(70vh,44rem)] space-y-5 overflow-y-auto px-5 py-5 rail-scroll sm:px-6">
              <section
                aria-label="Document destination"
                className="overflow-hidden rounded-[var(--radius-lg)] border border-border bg-surface shadow-xs"
              >
                <div className="flex items-start gap-3 px-4 py-3.5">
                  <span className="flex h-9 w-9 shrink-0 items-center justify-center rounded-[var(--radius-md)] bg-surface-selected text-surface-selected-foreground">
                    <FileText className="h-4 w-4" aria-hidden />
                  </span>
                  <div className="min-w-0">
                    <p className="text-xs font-medium text-foreground-muted">Document</p>
                    <p className="truncate font-display text-base font-semibold text-foreground" title={title}>
                      {title}
                    </p>
                    <p className="mt-0.5 text-xs text-foreground-muted">
                      Moving changes location only.
                    </p>
                  </div>
                </div>
                <div className="grid border-t border-border bg-surface-2 sm:grid-cols-[minmax(0,1fr)_2.5rem_minmax(0,1fr)]">
                  <div className="min-w-0 px-4 py-3">
                    <p className="text-xs text-foreground-muted">Current location</p>
                    <p className="mt-0.5 truncate text-sm font-medium text-foreground" title={locationLabel(current.collection)}>
                      {locationLabel(current.collection)}
                    </p>
                  </div>
                  <div className="hidden items-center justify-center border-x border-border text-foreground-muted sm:flex">
                    <ArrowRight className="h-4 w-4" aria-hidden />
                  </div>
                  <div className="min-w-0 border-t border-border px-4 py-3 sm:border-t-0">
                    <p className="text-xs text-foreground-muted">Destination</p>
                    <p className="mt-0.5 truncate text-sm font-semibold text-link" title={locationLabel(collection)}>
                      {locationLabel(collection)}
                    </p>
                  </div>
                </div>
              </section>

              <div className="space-y-1.5">
                <Label htmlFor="document-move-collection">Target collection</Label>
                <SelectMenu
                  id="document-move-collection"
                  value={collection}
                  onValueChange={(value) => {
                    setCollection(value);
                    setError("");
                    setServerConflict(null);
                  }}
                  options={collectionOptions}
                  placeholder={collectionsQuery.isPending ? "Loading collections…" : "Select a collection"}
                  disabled={collectionsQuery.isPending || collectionsQuery.isError || moving}
                  searchable
                  searchPlaceholder="Filter collections…"
                />
                <p className="text-xs leading-relaxed text-foreground-muted">
                  Choose Vault root to move the document outside every collection.
                </p>
              </div>

              {pathCollision && !titleConflict && (
                <Alert variant="info" title="The system path is already in use">
                  <p>
                    “{pathCollision.name}” resolves to the same technical slug. AKB will
                    assign a collision-safe path automatically; the visible title stays unchanged.
                  </p>
                </Alert>
              )}

              {titleConflict && (
                <div ref={conflictRef} tabIndex={-1} className="focus:outline-none">
                  <DocumentTitleConflictNotice
                    conflict={titleConflict}
                    onOpenExisting={
                      onOpenDocument
                        ? () => openExistingDocument(titleConflict.existingPath)
                        : undefined
                    }
                    onChooseAlternative={() => {
                      setServerConflict(null);
                      document.getElementById("document-move-collection")?.focus();
                    }}
                    chooseAlternativeLabel="Choose another Collection"
                    onKeepBoth={() => performMove("allow")}
                    keepBothLabel="Keep both and move"
                    keepingBoth={moving}
                  />
                </div>
              )}

              <details className="group rounded-[var(--radius-lg)] border border-border bg-surface">
                <summary className="flex min-h-10 cursor-pointer list-none items-center gap-2 rounded-[var(--radius-lg)] px-3 text-sm font-medium text-foreground transition-token hover:bg-surface-hover focus:outline-none focus-visible:ring-2 focus-visible:ring-ring">
                  <ChevronRight
                    className="h-4 w-4 shrink-0 text-foreground-muted transition-transform group-open:rotate-90"
                    aria-hidden
                  />
                  Review technical paths
                </summary>
                <dl className="space-y-3 border-t border-border bg-surface-2 px-4 py-3 text-xs">
                  <div>
                    <dt className="text-foreground-muted">Current path</dt>
                    <dd className="mt-1 overflow-x-auto">
                      <code className="font-mono text-foreground">{path}</code>
                    </dd>
                  </div>
                  <div>
                    <dt className="text-foreground-muted">
                      {pathCollision ? "Requested path" : "Expected path"}
                    </dt>
                    <dd className="mt-1 overflow-x-auto">
                      <code className="font-mono text-foreground">{destinationPath}</code>
                    </dd>
                    {pathCollision && (
                      <p className="mt-1 text-foreground-muted">
                        The server will return the final collision-safe path after the move.
                      </p>
                    )}
                  </div>
                </dl>
              </details>

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
                    Retry before choosing a destination.
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
              {error && (
                <div ref={errorRef} tabIndex={-1} className="focus:outline-none">
                  <Alert variant="destructive">{error}</Alert>
                </div>
              )}
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
                  collectionsQuery.isError ||
                  !destinationChanged ||
                  Boolean(titleConflict)
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
        description="The document has not moved yet. Your selected Collection and commit message will be lost."
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
