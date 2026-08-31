import { Suspense, lazy, useEffect, useMemo, useRef, useState } from "react";
import { flushSync } from "react-dom";
import {
  AlertCircle,
  ArrowRight,
  Check,
  FilePlus2,
  FileText,
  FolderPlus,
  FolderTree,
  Info,
  Loader2,
  MapPin,
  PanelRightClose,
  PanelRightOpen,
  Shapes,
  Tags,
  X,
} from "lucide-react";
import { ApiError, getDocument, putDocument } from "@/lib/api";
import { DOC_TYPES, type DocType } from "@/lib/doc-constants";
import {
  clearDocumentDraft,
  loadDocumentDraft,
  saveDocumentDraft,
} from "@/lib/document-draft";
import { isReservedCollection } from "@/lib/skill";
import { useVaultTree, type TreeNode } from "@/hooks/use-vault-tree";
import { useVaultRefresh } from "@/contexts/vault-refresh-context";
import { MarkdownEditorFallback } from "@/components/markdown-editor-fallback";
import { DocumentTitleConflictNotice } from "@/components/document-title-conflict-notice";
import { Alert } from "@/components/ui/alert";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { DialogDescription, DialogTitle } from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { SelectMenu } from "@/components/ui/select-menu";
import { TagInput } from "@/components/ui/tag-input";
import { Textarea } from "@/components/ui/textarea";
import { cn } from "@/lib/utils";
import {
  documentTitleConflictFromError,
  findDocumentTitleConflict,
  type DocumentTitleCandidate,
  type DocumentTitleConflict,
} from "@/lib/document-title-conflict";

const MarkdownEditor = lazy(() => import("@/components/markdown-editor"));

type InvalidField = "title" | "collection" | "body" | null;

export interface DocumentCreateFormProps {
  vault: string;
  initialCollection?: string;
  onCreated: (path?: string) => void;
  onRequestClose: () => void;
  onDirtyChange?: (dirty: boolean) => void;
  onCreatingChange?: (creating: boolean) => void;
  onUploadingChange?: (uploading: boolean) => void;
  onAssetIdsChange?: (assetIds: readonly string[]) => void;
}

/** Depth-first collection paths for the location suggestions. */
function collectCollectionPaths(nodes: TreeNode[], out: string[] = []): string[] {
  for (const node of nodes) {
    if (node.kind !== "collection") continue;
    out.push(node.path);
    if (node.children) collectCollectionPaths(node.children, out);
  }
  return out;
}

function collectDocuments(
  nodes: TreeNode[],
  out: DocumentTitleCandidate[] = [],
): DocumentTitleCandidate[] {
  for (const node of nodes) {
    if (node.kind === "document") out.push({ name: node.name, path: node.path });
    if (node.children) collectDocuments(node.children, out);
  }
  return out;
}

// Plate's markdown serializer represents its visually empty paragraph with a
// whitespace/escape-only value. Treat that transport detail as empty so the
// composer neither marks a fresh draft dirty nor enables Create prematurely.
function hasMeaningfulMarkdown(markdown: string): boolean {
  return markdown.replace(/[\s\\\u200b\ufeff]/g, "").length > 0;
}

export function DocumentCreateForm({
  vault,
  initialCollection = "",
  onCreated,
  onRequestClose,
  onDirtyChange,
  onCreatingChange,
  onUploadingChange,
  onAssetIdsChange,
}: DocumentCreateFormProps) {
  const restoredDraft = useMemo(() => loadDocumentDraft(vault), [vault]);
  const { tree } = useVaultTree(vault);
  const { refetchTree, refetchVaults } = useVaultRefresh();
  const collectionOptions = useMemo(
    () =>
      Array.from(new Set(collectCollectionPaths(tree ?? [])))
        .filter((path) => !isReservedCollection(path))
        .sort(),
    [tree],
  );

  const [title, setTitle] = useState(restoredDraft?.title ?? "");
  const [collection, setCollection] = useState(
    restoredDraft?.collection ?? initialCollection,
  );
  const [type, setType] = useState<DocType>(restoredDraft?.type ?? "note");
  const [domain, setDomain] = useState(restoredDraft?.domain ?? "");
  const [summary, setSummary] = useState(restoredDraft?.summary ?? "");
  const [tags, setTags] = useState<string[]>(restoredDraft?.tags ?? []);
  const [body, setBody] = useState(restoredDraft?.body ?? "");
  const [bodyAssetIds, setBodyAssetIds] = useState<readonly string[]>(
    restoredDraft?.assetIds ?? [],
  );
  const [claimedAssetIds, setClaimedAssetIds] = useState<readonly string[] | null>(null);
  const [error, setError] = useState("");
  const [serverConflict, setServerConflict] = useState<DocumentTitleConflict | null>(null);
  const [invalidField, setInvalidField] = useState<InvalidField>(null);
  const [creating, setCreating] = useState(false);
  const [uploadingImage, setUploadingImage] = useState(false);
  const [draftStatus, setDraftStatus] = useState<
    "idle" | "restored" | "saving" | "saved" | "error"
  >(restoredDraft ? "restored" : "idle");
  const skipInitialDraftSaveRef = useRef(Boolean(restoredDraft));
  const [detailsOpen, setDetailsOpen] = useState(() =>
    typeof window === "undefined"
      ? true
      : window.matchMedia("(min-width: 1024px)").matches,
  );
  const titleRef = useRef<HTMLInputElement>(null);
  const collectionRef = useRef<HTMLInputElement>(null);
  const conflictRef = useRef<HTMLDivElement>(null);

  const collectionTrimmed = collection.trim();
  const isExistingCollection = collectionOptions.includes(collectionTrimmed);
  const isReservedCollectionPath = isReservedCollection(collectionTrimmed);
  const isCollectionSyntaxValid =
    collectionTrimmed === "" ||
    /^[a-z0-9_-]+(?:\/[a-z0-9_-]+)*$/.test(collectionTrimmed);
  const matchingCollections = collectionOptions
    .filter(
      (path) =>
        path !== collectionTrimmed &&
        path.toLowerCase().includes(collectionTrimmed.toLowerCase()),
    )
    .slice(0, 6);
  const documents = useMemo(() => collectDocuments(tree ?? []), [tree]);
  const localConflict = useMemo(
    () => findDocumentTitleConflict(documents, title, collectionTrimmed),
    [collectionTrimmed, documents, title],
  );
  const titleConflict = serverConflict ?? localConflict;

  const isDirty =
    title.trim() !== "" ||
    collection.trim() !== initialCollection.trim() ||
    type !== "note" ||
    domain.trim() !== "" ||
    summary.trim() !== "" ||
    tags.length > 0 ||
    hasMeaningfulMarkdown(body);
  const hasUnsavedWork = isDirty || uploadingImage;

  useEffect(() => onDirtyChange?.(hasUnsavedWork), [hasUnsavedWork, onDirtyChange]);
  useEffect(() => onCreatingChange?.(creating), [creating, onCreatingChange]);
  useEffect(() => onUploadingChange?.(uploadingImage), [uploadingImage, onUploadingChange]);
  useEffect(() => onAssetIdsChange?.(bodyAssetIds), [bodyAssetIds, onAssetIdsChange]);

  useEffect(() => {
    if (creating) return;
    if (skipInitialDraftSaveRef.current) {
      skipInitialDraftSaveRef.current = false;
      return;
    }
    if (!isDirty) {
      clearDocumentDraft(vault);
      setDraftStatus("idle");
      return;
    }
    setDraftStatus("saving");
    const timer = window.setTimeout(() => {
      const saved = saveDocumentDraft({
        vault,
        title,
        collection,
        type,
        domain,
        summary,
        tags,
        body,
        assetIds: [...bodyAssetIds],
      });
      setDraftStatus(saved ? "saved" : "error");
    }, 300);
    return () => window.clearTimeout(timer);
  }, [body, bodyAssetIds, collection, creating, domain, isDirty, summary, tags, title, type, vault]);

  useEffect(() => {
    const media = window.matchMedia("(min-width: 1024px)");
    const sync = () => setDetailsOpen(media.matches);
    media.addEventListener("change", sync);
    return () => media.removeEventListener("change", sync);
  }, []);

  useEffect(() => {
    if (!hasUnsavedWork || creating) return;
    const onBeforeUnload = (event: BeforeUnloadEvent) => {
      event.preventDefault();
      event.returnValue = "";
    };
    window.addEventListener("beforeunload", onBeforeUnload);
    return () => window.removeEventListener("beforeunload", onBeforeUnload);
  }, [hasUnsavedWork, creating]);

  function fail(field: Exclude<InvalidField, null>, message: string) {
    setError(message);
    setInvalidField(field);
    window.setTimeout(() => {
      if (field === "title") titleRef.current?.focus();
      if (field === "collection") collectionRef.current?.focus();
      if (field === "body") {
        document
          .querySelector<HTMLElement>("#document-create-body [contenteditable='true']")
          ?.focus();
      }
    }, 0);
  }

  function validateDraft() {
    if (creating || uploadingImage) return;

    setError("");
    setInvalidField(null);
    const nextTitle = title.trim();
    const nextCollection = collection.trim();

    if (!nextTitle) {
      fail("title", "Title is required.");
      return;
    }
    if (nextTitle.length > 256) {
      fail("title", "Title is too long (256 chars max).");
      return;
    }
    if (!nextCollection) {
      fail("collection", "Collection is required.");
      return;
    }
    if (!/^[a-z0-9_-]+(?:\/[a-z0-9_-]+)*$/.test(nextCollection)) {
      fail(
        "collection",
        "Collection must use lowercase letters, digits, hyphens, underscores, and / only.",
      );
      return;
    }
    if (isReservedCollection(nextCollection)) {
      fail(
        "collection",
        "'overview' is a system collection reserved for the vault guide. Pick a different collection.",
      );
      return;
    }
    if (!hasMeaningfulMarkdown(body)) {
      fail("body", "Body cannot be empty.");
      return;
    }
    if (body.length > 1_000_000) {
      fail("body", "Body is too large (1 MB max).");
      return;
    }
    return { nextTitle, nextCollection };
  }

  async function performCreate(titleConflictPolicy: "allow" | "reject") {
    const validated = validateDraft();
    if (!validated) return;
    const { nextTitle, nextCollection } = validated;

    const assetIdsToClaim = bodyAssetIds;
    let created = false;
    setCreating(true);
    try {
      const result = await putDocument({
        vault,
        collection: nextCollection,
        title: nextTitle,
        content: body,
        type,
        tags,
        domain: domain.trim() || undefined,
        summary: summary.trim() || undefined,
        title_conflict_policy: titleConflictPolicy,
      });
      refetchTree();
      refetchVaults();
      flushSync(() => {
        setCreating(false);
        setClaimedAssetIds(assetIdsToClaim);
      });
      created = true;
      clearDocumentDraft(vault);
      onCreated(result?.path);
    } catch (caught: unknown) {
      const conflict = documentTitleConflictFromError(caught);
      if (conflict) {
        setServerConflict(conflict);
        window.requestAnimationFrame(() => conflictRef.current?.focus());
        return;
      }
      setError(
        caught instanceof ApiError
          ? caught.message
          : caught instanceof Error
            ? caught.message
            : "Failed to create document.",
      );
    } finally {
      if (!created) setCreating(false);
    }
  }

  async function handleSubmit(event: React.FormEvent) {
    event.preventDefault();
    if (titleConflict) {
      try {
        const existing = await getDocument(vault, titleConflict.existingPath);
        setServerConflict({
          ...titleConflict,
          exactContent:
            typeof existing?.content === "string" && existing.content === body,
        });
      } catch {
        setServerConflict(titleConflict);
      }
      window.requestAnimationFrame(() => conflictRef.current?.focus());
      return;
    }
    await performCreate("reject");
  }

  const canSubmit =
    title.trim() !== "" &&
    collectionTrimmed !== "" &&
    hasMeaningfulMarkdown(body) &&
    isCollectionSyntaxValid &&
    !isReservedCollectionPath &&
    !uploadingImage;

  const locationState = collectionTrimmed === ""
    ? "Choose where this document belongs."
    : isReservedCollectionPath
      ? "System collection reserved for the vault guide."
      : !isCollectionSyntaxValid
        ? "Use lowercase letters, numbers, hyphens, underscores, and / only."
        : isExistingCollection
          ? "Existing collection"
          : "New collection — created with the document";

  return (
    <form onSubmit={handleSubmit} className="flex min-h-0 flex-1 flex-col bg-background">
      <header className="relative z-[var(--z-sticky)] flex h-16 shrink-0 items-center gap-3 border-b border-border bg-surface px-3 sm:px-4 lg:px-5">
        <div className="flex min-w-0 items-center gap-3">
          <div className="hidden h-9 w-9 shrink-0 items-center justify-center rounded-[var(--radius-md)] border border-primary/20 bg-surface-selected text-surface-selected-foreground sm:flex">
            <FilePlus2 className="h-4 w-4" aria-hidden />
          </div>
          <div className="min-w-0">
            <div className="flex items-center gap-2">
              <DialogTitle className="truncate font-display text-base sm:text-lg">
                New document
              </DialogTitle>
              <Badge variant="draft" className="hidden sm:inline-flex">Draft</Badge>
            </div>
            <DialogDescription className="truncate text-xs">
              Writing in <span className="font-medium text-foreground">{vault}</span>
            </DialogDescription>
          </div>
        </div>

        <div className="mx-auto hidden min-w-0 max-w-md items-center gap-2 rounded-[var(--radius-md)] border border-border bg-surface-2 px-3 py-2 text-xs md:flex">
          <MapPin className="h-3.5 w-3.5 shrink-0 text-link" aria-hidden />
          <span className="truncate font-mono text-foreground-muted">
            akb://{vault}/{collectionTrimmed || "select-collection"}
          </span>
        </div>

        <div className="ml-auto flex shrink-0 items-center gap-2">
          <div className="mr-1 hidden items-center gap-2 text-xs text-foreground-muted xl:flex" role="status" aria-live="polite">
            {uploadingImage ? (
              <Loader2 className="h-3.5 w-3.5 animate-spin text-link" aria-hidden />
            ) : (
              <span
                className={cn("h-2 w-2 rounded-full", canSubmit ? "bg-success" : "bg-foreground-muted")}
                aria-hidden
              />
            )}
            <span>
              {uploadingImage
                ? "Uploading image…"
                : draftStatus === "restored"
                  ? "Local draft restored"
                  : draftStatus === "saving"
                    ? "Saving draft…"
                    : draftStatus === "saved"
                      ? "Draft saved locally"
                      : draftStatus === "error"
                        ? "Draft storage unavailable"
                        : canSubmit
                          ? "Ready to create"
                          : "Unsaved draft"}
            </span>
          </div>
          <Button
            type="button"
            variant="outline"
            size="sm"
            aria-controls="document-properties"
            aria-expanded={detailsOpen}
            aria-label={detailsOpen ? "Hide document details" : "Show document details"}
            onClick={() => setDetailsOpen((open) => !open)}
          >
            {detailsOpen ? (
              <PanelRightClose className="h-4 w-4" aria-hidden />
            ) : (
              <PanelRightOpen className="h-4 w-4" aria-hidden />
            )}
            <span className="hidden sm:inline">Details</span>
          </Button>
          <Button
            type="submit"
            variant="accent"
            size="sm"
            loading={creating}
            disabled={!canSubmit}
            aria-label="Create document"
          >
            <span>{creating ? "Creating…" : "Create"}</span>
            {!creating && <ArrowRight className="h-4 w-4" aria-hidden />}
          </Button>
          <div className="mx-0.5 h-6 w-px bg-border" aria-hidden />
          <Button
            type="button"
            variant="ghost"
            size="icon"
            aria-label="Close document composer"
            onClick={onRequestClose}
            disabled={creating}
            className="shrink-0"
          >
            <X className="h-4 w-4" aria-hidden />
          </Button>
        </div>
      </header>

      {error && (
        <div className="relative z-[var(--z-raised)] shrink-0 border-b border-border bg-surface px-4 py-3 sm:px-6">
          <Alert variant="destructive" id="document-create-error" className="mx-auto max-w-6xl">
            {error}
          </Alert>
        </div>
      )}

      {titleConflict && (
        <div
          ref={conflictRef}
          tabIndex={-1}
          className="relative z-[var(--z-raised)] shrink-0 border-b border-border bg-surface px-4 py-3 focus:outline-none sm:px-6"
        >
          <div className="mx-auto max-w-6xl">
            <DocumentTitleConflictNotice
              conflict={titleConflict}
              onOpenExisting={() => onCreated(titleConflict.existingPath)}
              onChooseAlternative={() => titleRef.current?.focus()}
              chooseAlternativeLabel="Choose another title"
              onKeepBoth={() => performCreate("allow")}
              keepBothLabel="Create duplicate"
              keepingBoth={creating}
            />
          </div>
        </div>
      )}

      <div className="relative min-h-0 flex-1 overflow-hidden">
        <main
          className={cn(
            "h-full overflow-y-auto bg-surface transition-[padding] duration-200",
            detailsOpen && "lg:pr-96",
          )}
        >
          <div className="flex min-h-full w-full flex-col gap-6 px-4 py-5 sm:px-6 sm:py-6 lg:px-8">
            <div className="flex min-w-0 items-center gap-2 text-xs text-foreground-muted md:hidden">
              <MapPin className="h-3.5 w-3.5 shrink-0 text-link" aria-hidden />
              <span className="truncate font-mono">
                akb://{vault}/{collectionTrimmed || "select-collection"}
              </span>
            </div>

            <section className="space-y-2">
              <Label htmlFor="doc-title">
                Title <span className="text-destructive">*</span>
              </Label>
              <Input
                id="doc-title"
                ref={titleRef}
                value={title}
                onChange={(event) => {
                  setTitle(event.target.value);
                  setServerConflict(null);
                  if (invalidField === "title") setInvalidField(null);
                }}
                placeholder="Document title"
                maxLength={256}
                required
                aria-required="true"
                aria-invalid={invalidField === "title" || undefined}
                aria-describedby={error ? "document-create-error" : undefined}
                autoFocus
              />
            </section>

            <section id="document-create-body" className="group space-y-2">
              <div className="flex items-center justify-between gap-4">
                <Label id="doc-body-label" className="flex items-center gap-2">
                  <FileText
                    className={cn(
                      "h-4 w-4 text-foreground-muted transition-colors group-focus-within:text-link",
                      invalidField === "body" && "text-destructive",
                    )}
                    aria-hidden
                  />
                  Content <span className="text-destructive">*</span>
                </Label>
                <span className="shrink-0 text-xs tabular-nums text-foreground-muted">
                  {(hasMeaningfulMarkdown(body) ? body.length : 0).toLocaleString()}
                  <span className="hidden sm:inline"> characters</span>
                </span>
              </div>
              <div
                className={cn(
                  "overflow-hidden rounded-[var(--radius-lg)] border border-border bg-surface transition-colors focus-within:border-primary",
                  invalidField === "body" && "border-destructive",
                )}
              >
                <Suspense fallback={<MarkdownEditorFallback />}>
                  <MarkdownEditor
                    value={body}
                    onChange={(markdown, assetIds) => {
                      setBody(markdown);
                      setBodyAssetIds(assetIds);
                      setServerConflict(null);
                      if (invalidField === "body") setInvalidField(null);
                    }}
                    placeholder="Start with the idea, decision, or context worth keeping…"
                    ariaLabelledby="doc-body-label"
                    required
                    readOnly={creating}
                    vault={vault}
                    appearance="workspace"
                    onUploadingChange={(uploading) => {
                      setUploadingImage(uploading);
                      if (uploading) setClaimedAssetIds(null);
                    }}
                    initialUnclaimedAssetIds={restoredDraft?.assetIds}
                    preserveUploadsOnUnmount
                    claimedAssetIds={claimedAssetIds}
                  />
                </Suspense>
              </div>
              <div className="flex flex-col gap-1 text-xs text-foreground-muted sm:flex-row sm:items-center sm:justify-between">
                <span>Paste or drop images directly into the document.</span>
                <span>Markdown · saved to Git when created</span>
              </div>
            </section>
          </div>
        </main>

        <aside
          id="document-properties"
          aria-label="Document details"
          className={cn(
            "absolute inset-y-0 right-0 z-[var(--z-raised)] w-full max-w-md overflow-y-auto border-l border-border bg-surface shadow-lg lg:w-96 lg:shadow-sm",
            !detailsOpen && "hidden",
          )}
        >
          <div className="sticky top-0 z-10 flex h-14 items-center justify-between border-b border-border bg-surface px-5">
            <div>
              <h2 className="text-sm font-semibold text-foreground">Document details</h2>
              <p className="text-xs text-foreground-muted">Location and retrieval context</p>
            </div>
            <Button
              type="button"
              variant="ghost"
              size="icon"
              aria-label="Hide document details"
              onClick={() => setDetailsOpen(false)}
            >
              <X className="h-4 w-4" aria-hidden />
            </Button>
          </div>

          <section aria-labelledby="document-location-heading" className="border-b border-border px-5 py-6">
            <div className="mb-4">
              <h3 id="document-location-heading" className="flex items-center gap-2 text-sm font-semibold text-foreground">
                <FolderTree className="h-4 w-4 text-link" aria-hidden />
                Destination
              </h3>
              <p className="mt-1 text-xs leading-relaxed text-foreground-muted">
                Choose an existing collection or create a nested path.
              </p>
            </div>
            <div className="space-y-2">
              <Label htmlFor="doc-collection">
                Collection <span className="text-destructive">*</span>
              </Label>
              <Input
                id="doc-collection"
                ref={collectionRef}
                value={collection}
                onChange={(event) => {
                  setCollection(event.target.value);
                  setServerConflict(null);
                  if (invalidField === "collection") setInvalidField(null);
                }}
                placeholder="engineering/specs"
                className="font-mono"
                maxLength={120}
                required
                aria-required="true"
                aria-invalid={
                  invalidField === "collection" ||
                  isReservedCollectionPath ||
                  (!isCollectionSyntaxValid && collectionTrimmed !== "") ||
                  undefined
                }
                aria-describedby="doc-collection-status"
                autoComplete="off"
              />
              <p
                id="doc-collection-status"
                className={cn(
                  "flex items-start gap-1.5 text-xs leading-relaxed text-foreground-muted",
                  (isReservedCollectionPath || !isCollectionSyntaxValid) && "text-destructive",
                )}
                aria-live="polite"
              >
                {isReservedCollectionPath || !isCollectionSyntaxValid ? (
                  <AlertCircle className="mt-0.5 h-3.5 w-3.5 shrink-0" aria-hidden />
                ) : isExistingCollection ? (
                  <Check className="mt-0.5 h-3.5 w-3.5 shrink-0 text-success" aria-hidden />
                ) : collectionTrimmed ? (
                  <FolderPlus className="mt-0.5 h-3.5 w-3.5 shrink-0 text-link" aria-hidden />
                ) : (
                  <Info className="mt-0.5 h-3.5 w-3.5 shrink-0" aria-hidden />
                )}
                <span>{locationState}</span>
              </p>
            </div>
            {matchingCollections.length > 0 && (
              <div className="mt-3 flex flex-wrap gap-1.5" aria-label="Collection suggestions">
                {matchingCollections.map((path) => (
                  <button
                    key={path}
                    type="button"
                    onClick={() => {
                      setCollection(path);
                      setServerConflict(null);
                      if (invalidField === "collection") setInvalidField(null);
                      collectionRef.current?.focus();
                    }}
                    className="inline-flex min-h-9 max-w-full cursor-pointer items-center rounded-[var(--radius-md)] border border-border bg-surface px-2.5 py-1 font-mono text-[11px] text-foreground-muted transition-token hover:border-border-strong hover:text-link focus:outline-none focus-visible:ring-2 focus-visible:ring-ring"
                  >
                    <span className="truncate">{path}</span>
                  </button>
                ))}
              </div>
            )}
          </section>

          <section aria-labelledby="document-details-heading" className="px-5 py-6">
            <div className="mb-5">
              <h3 id="document-details-heading" className="flex items-center gap-2 text-sm font-semibold text-foreground">
                <Shapes className="h-4 w-4 text-link" aria-hidden />
                Retrieval context
              </h3>
              <p className="mt-1 text-xs leading-relaxed text-foreground-muted">
                Optional details help search and agents find the right context.
              </p>
            </div>
            <div className="space-y-5">
              <div className="space-y-1.5">
                <Label htmlFor="doc-summary">
                  Summary <span className="font-normal text-foreground-muted">Optional</span>
                </Label>
                <Textarea
                  id="doc-summary"
                  value={summary}
                  onChange={(event) => setSummary(event.target.value)}
                  rows={4}
                  maxLength={500}
                  placeholder="Describe what this document contains."
                />
              </div>
              <div className="space-y-1.5">
                <Label htmlFor="doc-type">Document type</Label>
                <SelectMenu
                  id="doc-type"
                  aria-label="Document type"
                  value={type}
                  onValueChange={(value) => setType(value as DocType)}
                  options={DOC_TYPES.map((item) => ({ value: item, label: item }))}
                />
              </div>
              <div className="space-y-1.5">
                <Label htmlFor="doc-domain">
                  Domain <span className="font-normal text-foreground-muted">Optional</span>
                </Label>
                <Input
                  id="doc-domain"
                  value={domain}
                  onChange={(event) => setDomain(event.target.value)}
                  placeholder="engineering, product, ops…"
                />
              </div>
              <div className="space-y-1.5">
                <Label htmlFor="doc-tags" className="flex items-center gap-1.5">
                  <Tags className="h-3.5 w-3.5 text-foreground-muted" aria-hidden />
                  Tags <span className="font-normal text-foreground-muted">Optional</span>
                </Label>
                <TagInput id="doc-tags" value={tags} onChange={setTags} />
              </div>
            </div>
          </section>
        </aside>
      </div>
    </form>
  );
}
