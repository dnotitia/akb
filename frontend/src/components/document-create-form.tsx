import { Suspense, lazy, useEffect, useMemo, useRef, useState } from "react";
import { flushSync } from "react-dom";
import {
  AlertCircle,
  Box,
  Check,
  ChevronRight,
  Folder,
  FolderPlus,
  Info,
  Loader2,
  X,
} from "lucide-react";
import { ApiError, getDocument, putDocument } from "@/lib/api";
import type { DocType } from "@/lib/doc-constants";
import {
  clearDocumentDraft,
  loadDocumentDraft,
  saveDocumentDraft,
} from "@/lib/document-draft";
import { isReservedCollection } from "@/lib/skill";
import { useVaultTree, type TreeNode } from "@/hooks/use-vault-tree";
import { useVaultRefresh } from "@/contexts/vault-refresh-context";
import { useCurrentUser } from "@/contexts/current-user-context";
import { MarkdownEditorFallback } from "@/components/markdown-editor-fallback";
import { DocumentAuthoringLayout } from "@/components/document-authoring-layout";
import { DocumentDetailsFields } from "@/components/document-details-fields";
import { DocumentTitleConflictNotice } from "@/components/document-title-conflict-notice";
import { Alert } from "@/components/ui/alert";
import { Button } from "@/components/ui/button";
import { DialogDescription, DialogTitle } from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
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
  onSlashOpenChange?: (open: boolean, dismiss?: () => void) => void;
  onAssetIdsChange?: (assetIds: readonly string[]) => void;
  onAssetExpirationsChange?: (expirations: Readonly<Record<string, string>>) => void;
  onUnclaimedAssetIdsChange?: (assetIds: readonly string[]) => void;
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

// The editor serializer represents its visually empty paragraph with a
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
  onSlashOpenChange,
  onAssetIdsChange,
  onAssetExpirationsChange,
  onUnclaimedAssetIdsChange,
}: DocumentCreateFormProps) {
  const userId = useCurrentUser()?.user_id ?? "";
  const restoredDraft = useMemo(() => loadDocumentDraft(userId, vault), [userId, vault]);
  const restoredDraftExpired = Boolean(
    restoredDraft?.expiresAt && Date.parse(restoredDraft.expiresAt) <= Date.now(),
  );
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
  const [bodyAssetExpirations, setBodyAssetExpirations] = useState<Readonly<Record<string, string>>>(
    restoredDraft?.assetExpiresAt ?? {},
  );
  const [unclaimedAssetIds, setUnclaimedAssetIds] = useState<readonly string[]>(
    restoredDraft?.assetIds ?? [],
  );
  const [claimedAssetIds, setClaimedAssetIds] = useState<readonly string[] | null>(null);
  const [error, setError] = useState("");
  const [serverConflict, setServerConflict] = useState<DocumentTitleConflict | null>(null);
  const [invalidField, setInvalidField] = useState<InvalidField>(null);
  const [creating, setCreating] = useState(false);
  const [uploadingImage, setUploadingImage] = useState(false);
  const [draftStatus, setDraftStatus] = useState<
    "idle" | "restored" | "saving" | "saved" | "error" | "expired"
  >(restoredDraftExpired ? "expired" : restoredDraft ? "restored" : "idle");
  const skipInitialDraftSaveRef = useRef(Boolean(restoredDraft));
  const titleRef = useRef<HTMLInputElement>(null);
  const collectionRef = useRef<HTMLInputElement>(null);
  const conflictRef = useRef<HTMLDivElement>(null);

  const collectionTrimmed = collection.trim();
  const isExistingCollection = collectionOptions.includes(collectionTrimmed);
  const isReservedCollectionPath = isReservedCollection(collectionTrimmed);
  const isCollectionSyntaxValid =
    collectionTrimmed === "" ||
    /^[a-z0-9_-]+(?:\/[a-z0-9_-]+)*$/.test(collectionTrimmed);
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
  const hasUnsavedWork = isDirty || uploadingImage || unclaimedAssetIds.length > 0;

  useEffect(() => onDirtyChange?.(hasUnsavedWork), [hasUnsavedWork, onDirtyChange]);
  useEffect(() => onCreatingChange?.(creating), [creating, onCreatingChange]);
  useEffect(() => onUploadingChange?.(uploadingImage), [uploadingImage, onUploadingChange]);
  useEffect(() => onAssetIdsChange?.(bodyAssetIds), [bodyAssetIds, onAssetIdsChange]);
  useEffect(
    () => onUnclaimedAssetIdsChange?.(unclaimedAssetIds),
    [onUnclaimedAssetIdsChange, unclaimedAssetIds],
  );

  useEffect(() => {
    if (creating) return;
    if (skipInitialDraftSaveRef.current) {
      skipInitialDraftSaveRef.current = false;
      return;
    }
    if (!isDirty) {
      clearDocumentDraft(userId, vault);
      setDraftStatus("idle");
      return;
    }
    setDraftStatus("saving");
    const timer = window.setTimeout(() => {
      const saved = saveDocumentDraft({
        userId,
        vault,
        title,
        collection,
        type,
        domain,
        summary,
        tags,
        body,
        assetIds: [...bodyAssetIds],
        assetExpiresAt: bodyAssetExpirations,
      });
      setDraftStatus(saved ? "saved" : "error");
    }, 300);
    return () => window.clearTimeout(timer);
  }, [body, bodyAssetExpirations, bodyAssetIds, collection, creating, domain, isDirty, summary, tags, title, type, userId, vault]);

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
      clearDocumentDraft(userId, vault);
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
    <form onSubmit={handleSubmit} className="@container flex min-h-0 flex-1 flex-col bg-surface" data-testid="document-composer">
      <header className="flex h-14 shrink-0 items-center justify-between gap-3 border-b border-border bg-surface px-3 sm:px-5">
        <div className="flex min-w-0 items-center gap-2 text-sm">
          <Box className="hidden size-4 shrink-0 text-foreground-muted sm:block" aria-hidden />
          <span className="truncate text-foreground-muted" title={vault}>{vault}</span>
          <span className="text-subtle" aria-hidden>/</span>
          <DialogTitle className="shrink-0 text-sm font-semibold">New document</DialogTitle>
          <DialogDescription className="sr-only">Write a document in {vault}. Choose its collection, add a title and content, then create it.</DialogDescription>
        </div>
        <div className="flex shrink-0 items-center gap-2">
          <Button
            type="submit"
            variant="accent"
            size="sm"
            loading={creating}
            disabled={!canSubmit}
            aria-label="Create document"
          >
            <span>{creating ? "Creating…" : "Create"}</span>
          </Button>
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
      <button
        type="button"
        aria-label={`Edit collection: ${collectionTrimmed || "Choose a collection"}`}
        aria-controls="document-properties"
        onClick={() => collectionRef.current?.focus()}
        disabled={creating}
        className="flex min-h-10 shrink-0 items-center gap-2 border-b border-border px-4 text-left text-xs text-foreground-muted hover:bg-surface-hover focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-inset focus-visible:ring-ring @[52rem]:hidden"
      >
        <Folder className="size-3.5 shrink-0" aria-hidden />
        <span>Collection</span>
        <span className={cn("min-w-0 flex-1 truncate font-medium text-foreground", !collectionTrimmed && "text-link")}>{collectionTrimmed || "Choose a collection (required)"}</span>
        <ChevronRight className="size-3.5 shrink-0" aria-hidden />
      </button>

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

      <DocumentAuthoringLayout
        writingAs="main"
        writingProps={{
          className: cn(invalidField === "body" && "ring-1 ring-inset ring-destructive"),
          "aria-label": "Document composition",
          "data-testid": "composer-writing-surface",
        }}
        detailsId="document-properties"
        detailsHeadingId="document-details-heading"
        details={(
          <>
            <div className="border-b border-border px-4 py-3">
              <h2 id="document-details-heading" className="text-sm font-semibold">Document details</h2>
            </div>
            <fieldset disabled={creating} className="min-w-0 space-y-5 p-4">
              <legend className="sr-only">Document location and details</legend>
              <div className="space-y-2">
                <Label htmlFor="doc-collection" className="text-xs text-foreground-muted">Collection <span className="text-destructive">*</span></Label>
                <Input
                  id="doc-collection" ref={collectionRef} value={collection}
                  onChange={(event) => { setCollection(event.target.value); setServerConflict(null); if (invalidField === "collection") setInvalidField(null); }}
                  placeholder="Choose or create a collection"
                  className="h-9 rounded-[var(--radius-sm)]"
                  list="document-collection-options" maxLength={120} required aria-required="true"
                  aria-invalid={invalidField === "collection" || isReservedCollectionPath || (!isCollectionSyntaxValid && collectionTrimmed !== "") || undefined}
                  aria-describedby="doc-collection-status" autoComplete="off"
                />
                <datalist id="document-collection-options">{collectionOptions.map(path => <option key={path} value={path} />)}</datalist>
                <p id="doc-collection-status" className={cn("flex items-start gap-1.5 text-xs leading-relaxed text-foreground-muted", (isReservedCollectionPath || !isCollectionSyntaxValid) && "text-destructive")} aria-live="polite">
                  {isReservedCollectionPath || !isCollectionSyntaxValid ? <AlertCircle className="mt-0.5 size-3.5 shrink-0" aria-hidden />
                    : isExistingCollection ? <Check className="mt-0.5 size-3.5 shrink-0" aria-hidden />
                      : collectionTrimmed ? <FolderPlus className="mt-0.5 size-3.5 shrink-0" aria-hidden /> : <Info className="mt-0.5 size-3.5 shrink-0" aria-hidden />}
                  <span>{locationState}</span>
                </p>
              </div>
              <div className="space-y-4 border-t border-border pt-4">
                <p className="text-xs text-foreground-muted">Optional context for search and agents.</p>
                <DocumentDetailsFields
                  value={{ summary, type, domain, tags, status: "active" }}
                  onChange={(details) => {
                    setSummary(details.summary);
                    setType(details.type as DocType);
                    setDomain(details.domain);
                    setTags(details.tags);
                  }}
                  idPrefix="doc"
                  disabled={creating}
                />
              </div>
            </fieldset>
          </>
        )}
      >
        <div className="border-b border-border">
          <div className="space-y-1.5 px-4 py-4 sm:px-6">
            <Label htmlFor="doc-title" className="text-xs text-foreground-muted">
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
              className="h-11 rounded-[var(--radius-sm)] border-0 bg-transparent px-0 text-xl font-semibold shadow-none sm:text-2xl"
              maxLength={256}
              required
              aria-required="true"
              aria-invalid={invalidField === "title" || undefined}
              aria-describedby={error ? "document-create-error" : undefined}
              disabled={creating}
              autoFocus
            />
          </div>
        </div>
        <section id="document-create-body">
          <Label id="doc-body-label" className="sr-only">Content (required)</Label>
          <Suspense fallback={<MarkdownEditorFallback />}>
            <MarkdownEditor
              value={body}
              onChange={(markdown, assetIds) => {
                setBody(markdown);
                setBodyAssetIds(assetIds);
                setServerConflict(null);
                if (invalidField === "body") setInvalidField(null);
              }}
              onAssetExpirationsChange={(expirations) => {
                setBodyAssetExpirations(expirations);
                onAssetExpirationsChange?.(expirations);
              }}
              onUnclaimedAssetIdsChange={setUnclaimedAssetIds}
              placeholder="Write something worth keeping…"
              ariaLabelledby="doc-body-label"
              required
              readOnly={creating}
              vault={vault}
              appearance="workspace"
              className="!px-4 !text-base sm:!px-6"
              sourceClassName="block !px-4 sm:!px-6"
              onUploadingChange={(uploading) => {
                setUploadingImage(uploading);
                if (uploading) setClaimedAssetIds(null);
              }}
              onSlashOpenChange={onSlashOpenChange}
              initialUnclaimedAssetIds={restoredDraft?.assetIds}
              initialUnclaimedAssetExpirations={restoredDraft?.assetExpiresAt}
              preserveUploadsOnUnmount
              claimedAssetIds={claimedAssetIds}
            />
          </Suspense>
        </section>
      </DocumentAuthoringLayout>
      <footer className="flex min-h-10 shrink-0 flex-wrap items-center justify-between gap-x-4 gap-y-1 border-t border-border bg-surface px-4 py-2 text-xs text-foreground-muted">
        <span className="flex min-w-0 items-center gap-1.5" role="status" aria-live="polite">
          {uploadingImage && <Loader2 className="size-3.5 shrink-0 animate-spin" aria-hidden />}
          {uploadingImage
            ? "Uploading image…"
            : draftStatus === "restored"
              ? "Local draft restored"
              : draftStatus === "expired"
                ? "Draft expired — review attachments"
                : draftStatus === "saving"
                  ? "Saving draft…"
                  : draftStatus === "saved"
                    ? "Draft saved locally"
                    : draftStatus === "error"
                      ? "Draft storage unavailable"
                      : "Unsaved draft"}
        </span>
        <span className="tabular-nums">{(hasMeaningfulMarkdown(body) ? body.length : 0).toLocaleString()} characters</span>
      </footer>
    </form>
  );
}
