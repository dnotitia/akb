import { Suspense, lazy, useEffect, useMemo, useRef, useState } from "react";
import { flushSync } from "react-dom";
import {
  Link,
  Navigate,
  useLocation,
  useNavigate,
  useParams,
  useSearchParams,
} from "react-router-dom";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import {
  ArrowLeft,
  Box,
  CheckCircle2,
  ChevronRight,
  Clock3,
  ExternalLink,
  FileText,
  FolderTree,
  GitCompareArrows,
  GitCommitHorizontal,
  History,
  Info,
  Link2,
  ListTree,
  Loader2,
  Lock,
  Maximize2,
  PanelRightClose,
  PanelRightOpen,
  Pencil,
  Share2,
} from "lucide-react";
import {
  ApiError,
  browseVault,
  discardAsset,
  deleteDocument,
  getDocument,
  getDocumentHistoryWithFallback,
  getRelations,
  getVaultInfo,
  type DocumentUpdateInput,
  type DocumentHistoryEntry,
  type RelationRow,
  unpublishDoc,
  updateDocument,
} from "@/lib/api";
import { cn, timeAgo } from "@/lib/utils";
import { docUri } from "@/lib/uri";
import { parseHeadings } from "@/lib/markdown";
import { sameCommitRef } from "@/lib/commit";
import { VAULT_SKILL_PATH } from "@/lib/skill";
import { DocumentOutline } from "@/components/doc-outline";
import { DocumentView } from "@/components/document-view";
import { SummaryFold } from "@/components/summary-fold";
import { Alert } from "@/components/ui/alert";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Tabs, TabsList, TabsTrigger, TabsContent } from "@/components/ui/tabs";
import { Badge } from "@/components/ui/badge";
import { HistoryList } from "@/components/history-list";
import { FrontmatterEditDialog } from "@/components/frontmatter-edit-dialog";
import { MarkdownEditorFallback } from "@/components/markdown-editor-fallback";
import { ConfirmDialog } from "@/components/ui/confirm-dialog";
import { PublishOptionsDialog } from "@/components/publish-options-dialog";
import { TooltipText } from "@/components/ui/tooltip-text";
import { LoadingState } from "@/components/ui/loading-state";
import { Skeleton } from "@/components/ui/skeleton";
import { useVaultRefresh } from "@/contexts/vault-refresh-context";
import { RelationsPanel } from "@/components/relations/relations-panel";
import { relationIsInVault } from "@/components/relations/relation-row-utils";
import { useCurrentUser } from "@/contexts/current-user-context";
import { recordRecentDocumentView } from "@/lib/recent-document-views";
import { ResourceActionsMenu } from "@/components/resource-actions-menu";
import { ResourceDeleteDialog } from "@/components/resource-delete-dialog";
import { DocumentMoveDialog } from "@/components/document-move-dialog";
import { ArchiveVerificationError, changeDocumentArchiveState, checkDocumentArchiveState, documentArchiveDisabledReason } from "@/lib/document-archive";
import { DocumentTitleConflictNotice } from "@/components/document-title-conflict-notice";
import {
  DocumentConflictNotice,
  type DocumentConflictSnapshot,
} from "@/components/document-conflict-notice";
import {
  clearDocumentEditDraft,
  createDocumentEditDraftId,
  DOCUMENT_EDIT_DRAFT_EDITOR_VERSION,
  DOCUMENT_EDIT_DRAFT_MARKDOWN_PROFILE,
  documentEditDraftTabId,
  loadDocumentEditDraft,
  listDocumentEditDrafts,
  saveDocumentEditDraft,
  type DocumentEditDraftLoadResult,
  type DocumentEditDraftInput,
  type StoredDocumentEditDraft,
} from "@/lib/document-draft";
import {
  documentCollection,
  documentTitleConflictFromError,
  documentTitleKey,
  findDocumentTitleConflict,
  type DocumentTitleConflict,
} from "@/lib/document-title-conflict";

// Plate is heavy (~hundreds of KB gzipped); lazy-load so the read-only path
// (Rendered / Raw) stays cheap.
const MarkdownEditor = lazy(() => import("@/components/markdown-editor"));
const DocumentDiffView = lazy(() => import("@/components/document-diff-view"));

type DocView = "rendered" | "raw" | "edit" | "diff";
const EMPTY_DOCUMENT_HISTORY: DocumentHistoryEntry[] = [];

type BrowsedDocumentTitle = {
  type: "document";
  name: string;
  path: string;
};

function isBrowsedDocumentTitle(item: unknown): item is BrowsedDocumentTitle {
  if (!item || typeof item !== "object") return false;
  const candidate = item as Record<string, unknown>;
  return (
    candidate.type === "document" &&
    typeof candidate.name === "string" &&
    typeof candidate.path === "string"
  );
}

function documentIdentity(vault: string, document: Record<string, any>): string {
  return typeof document.uri === "string" && document.uri
    ? document.uri
    : `${vault}:${String(document.path || "")}`;
}

function validRevision(value: unknown): value is string {
  return typeof value === "string" && value.trim().length > 0;
}

function isRevisionConflict(error: unknown): error is ApiError {
  return (
    error instanceof ApiError &&
    error.status === 409 &&
    (error.detail as { code?: unknown } | null)?.code === "conflict"
  );
}

interface DocumentPageProps {
  /** Search-launched previews are read-first and keep the search route behind them. */
  presentation?: "page" | "preview";
}

export default function DocumentPage({
  presentation = "page",
}: DocumentPageProps) {
  const { name, id } = useParams<{ name: string; id: string }>();
  const currentUser = useCurrentUser();
  const navigate = useNavigate();
  const routeLocation = useLocation();
  const queryClient = useQueryClient();
  const { refetchTree } = useVaultRefresh();
  const [searchParams] = useSearchParams();
  const commitHash = searchParams.get("commit") || undefined;
  const rawView = searchParams.get("view");
  const view: DocView =
    rawView === "raw"
      ? "raw"
      : rawView === "edit"
        ? "edit"
        : rawView === "diff" && commitHash
          ? "diff"
          : "rendered";
  const isDiffMode = view === "diff";
  // Diff owns revision validation and its compatibility/error states. Keep the
  // surrounding document identity on HEAD so a stale or mistyped revision can
  // still render the comparison frame instead of replacing the whole page with
  // the generic document error state.
  const documentVersion = isDiffMode ? undefined : commitHash;
  const [relations, setRelations] = useState<RelationRow[]>([]);
  const [relationsError, setRelationsError] = useState(false);
  const [pendingView, setPendingView] = useState<DocView | null>(null);
  const [pendingExistingPath, setPendingExistingPath] = useState<string | null>(null);
  const [docOverride, setDocOverride] = useState<any>(null);
  const [publishing, setPublishing] = useState(false);
  const [publishError, setPublishError] = useState("");
  const [copied, setCopied] = useState(false);
  const [articleEl, setArticleEl] = useState<HTMLElement | null>(null);
  const [vaultRole, setVaultRole] = useState<string | null>(null);
  const [vaultKind, setVaultKind] = useState<"normal" | "mirror" | "error" | null>(null);
  const [vaultReadOnly, setVaultReadOnly] = useState(true);
  const [editOpen, setEditOpen] = useState(false);
  const [moveOpen, setMoveOpen] = useState(false);
  const [deleteOpen, setDeleteOpen] = useState(false);
  const [archiveOpen, setArchiveOpen] = useState(false);
  const [archiveNotice, setArchiveNotice] = useState("");
  const [archivePending, setArchivePending] = useState<{ vault: string; ref: string; status: "active" | "archived" } | null>(null);
  const [publishOpen, setPublishOpen] = useState(false);
  const [detailsOpen, setDetailsOpen] = useState(false);
  const [detailsTab, setDetailsTab] = useState<"info" | "outline" | "relations" | "history">("info");
  const detailsToggleRef = useRef<HTMLButtonElement | null>(null);
  const detailsCloseRef = useRef<HTMLButtonElement | null>(null);
  const editButtonRef = useRef<HTMLButtonElement | null>(null);
  const cancelEditButtonRef = useRef<HTMLButtonElement | null>(null);
  const editTitleRef = useRef<HTMLInputElement | null>(null);
  const titleConflictRef = useRef<HTMLDivElement | null>(null);
  const diffOriginHashRef = useRef<string | null>(null);
  const wasDiffModeRef = useRef(false);
  const restoreEditFocusRef = useRef(false);
  // Plate manages its own state; we remount via `editorKey` when hydrating
  // a fresh server value rather than treating `value` as controlled.
  const [editingContent, setEditingContent] = useState("");
  const [editingTitle, setEditingTitle] = useState("");
  const [editingAssetIds, setEditingAssetIds] = useState<readonly string[]>([]);
  const [originalContent, setOriginalContent] = useState("");
  const [originalTitle, setOriginalTitle] = useState("");
  const [titleTouched, setTitleTouched] = useState(false);
  const [serverTitleConflict, setServerTitleConflict] = useState<DocumentTitleConflict | null>(null);
  const [editorKey, setEditorKey] = useState(0);
  const [savingBody, setSavingBody] = useState(false);
  const [uploadingImage, setUploadingImage] = useState(false);
  const [claimedAssetIds, setClaimedAssetIds] = useState<readonly string[] | null>(null);
  const [bodyError, setBodyError] = useState("");
  const [savedAt, setSavedAt] = useState<number | null>(null);
  const [moveNotice, setMoveNotice] = useState<{ collection: string } | null>(null);
  const [editBaseCommit, setEditBaseCommit] = useState<string | null>(null);
  const [latestServerDocument, setLatestServerDocument] = useState<any>(null);
  const [saveConflict, setSaveConflict] = useState<{
    base: DocumentConflictSnapshot;
    local: DocumentConflictSnapshot;
    latest: DocumentConflictSnapshot | null;
    latestError?: string;
  } | null>(null);
  const [rebasing, setRebasing] = useState(false);
  const [draftStatus, setDraftStatus] = useState<
    "idle" | "restored" | "saving" | "saved" | "error" | "expired" | "incompatible" | "unavailable"
  >("idle");
  const [draftNotice, setDraftNotice] = useState("");
  const [draftRecovery, setDraftRecovery] = useState<DocumentEditDraftLoadResult | null>(null);
  const [editorInitialContent, setEditorInitialContent] = useState("");
  // Plate's markdown roundtrip is not byte-identity: adopt the first
  // post-hydration emission as the new `originalContent` baseline so the
  // editor doesn't flash "UNSAVED" the moment it mounts.
  const hydratedKey = useRef<number | null>(null);
  const editorHydrationModeRef = useRef<"server" | "draft">("server");
  const documentIdentityRef = useRef<string | null>(null);
  const editBaseCommitRef = useRef<string | null>(null);
  const draftHydratedForRef = useRef<string | null>(null);
  const draftSessionRef = useRef<StoredDocumentEditDraft | null>(null);
  const draftTabIdRef = useRef<string | null>(null);
  const skipNextDraftSaveRef = useRef(false);
  const restoredDraftRevisionRef = useRef<number | null>(null);
  const draftRevisionRef = useRef(0);
  const latestServerDocumentRef = useRef<any>(null);
  const latestServerErrorRef = useRef("");
  const storageSaveTimerRef = useRef<number | null>(null);
  const latestDraftInputRef = useRef<DocumentEditDraftInput | null>(null);
  const editingSnapshotRef = useRef({
    title: "",
    content: "",
    assetIds: [] as readonly string[],
  });
  const contentChanged = editingContent !== originalContent;
  const normalizedEditingTitle = documentTitleKey(editingTitle);
  const titleChanged = normalizedEditingTitle !== documentTitleKey(originalTitle);
  const isDirty = contentChanged || titleChanged;
  const hasUnsavedWork = isDirty || uploadingImage;

  useEffect(() => {
    editingSnapshotRef.current = {
      title: editingTitle,
      content: editingContent,
      assetIds: editingAssetIds,
    };
  }, [editingAssetIds, editingContent, editingTitle]);

  useEffect(() => {
    // A real editor can emit one canonicalization event while mounting. The
    // page state already owns the server/draft baseline, so user input from
    // the mounted editor must not be mistaken for that first emission (the
    // mock editor has no mount event and otherwise makes clear() rewrite the
    // OCC base to an empty string).
    hydratedKey.current = editorKey;
  }, [editorKey]);

  function setBaseCommit(commit: string | null) {
    editBaseCommitRef.current = commit;
    setEditBaseCommit(commit);
  }

  function setLatestServer(document: any, error = "") {
    latestServerDocumentRef.current = document;
    latestServerErrorRef.current = error;
    setLatestServerDocument(document);
  }

  function currentDraftInput(
    overrides: Partial<{
      baseCommit: string;
      baseTitle: string;
      baseBody: string;
      title: string;
      body: string;
      assetIds: readonly string[];
    }> = {},
  ) {
    if (!currentUserId || !name || !doc?.path || !editBaseCommitRef.current) return null;
    if (!draftSessionRef.current) {
      draftSessionRef.current = {
        version: 1,
        kind: "document-edit",
        draftId: createDocumentEditDraftId(),
        tabId: draftTabIdRef.current || documentEditDraftTabId(),
        userId: currentUserId,
        vault: name,
        document: documentIdentity(name, doc),
        baseCommit: editBaseCommitRef.current,
        baseTitle: originalTitle,
        baseBody: originalContent,
        title: editingTitle,
        body: editingContent,
        assetIds: [...editingAssetIds],
        editorVersion: DOCUMENT_EDIT_DRAFT_EDITOR_VERSION,
        markdownProfile: DOCUMENT_EDIT_DRAFT_MARKDOWN_PROFILE,
        updatedAt: new Date().toISOString(),
        expiresAt: new Date().toISOString(),
      };
    }
    const session = draftSessionRef.current;
    return {
      ...session,
      userId: currentUserId,
      vault: name,
      document: documentIdentity(name, doc),
      baseCommit: overrides.baseCommit ?? editBaseCommitRef.current,
      baseTitle: overrides.baseTitle ?? originalTitle,
      baseBody: overrides.baseBody ?? originalContent,
      title: overrides.title ?? editingTitle,
      body: overrides.body ?? editingContent,
      assetIds: [...(overrides.assetIds ?? editingAssetIds)],
      tabId: draftTabIdRef.current || session.tabId,
    };
  }

  function persistEditDraft(overrides: Parameters<typeof currentDraftInput>[0] = {}): boolean {
    const input = currentDraftInput(overrides);
    if (!input) return false;
    latestDraftInputRef.current = input;
    const saved = saveDocumentEditDraft(input);
    setDraftStatus(saved ? "saved" : "error");
    setDraftNotice(saved ? "Draft saved locally" : "Could not save this draft locally; it remains only in this tab.");
    return saved;
  }

  useEffect(() => {
    if (!moveNotice) return;
    const timer = window.setTimeout(() => setMoveNotice(null), 4_500);
    return () => window.clearTimeout(timer);
  }, [moveNotice]);

  const docId = id ? decodeURIComponent(id) : "";
  const visibleRelationCount = useMemo(
    () => relations.filter((row) => name && relationIsInVault(row, name)).length,
    [name, relations],
  );

  const applyView = (next: DocView) => {
    const p = new URLSearchParams(searchParams);
    if (next === "rendered") p.delete("view");
    else p.set("view", next);
    updateRouteParams(p, { replace: true });
  };
  const setView = (next: DocView) => {
    // Leaving Edit with unsaved changes routes through a ConfirmDialog
    // (design system bans window.confirm); the actual switch happens on confirm.
    if (view === "edit" && next !== "edit" && hasUnsavedWork) {
      setPendingView(next);
      return;
    }
    if (view === "edit" && next !== "edit") {
      restoreEditFocusRef.current = true;
    }
    applyView(next);
  };

  function updateRouteParams(
    params: URLSearchParams,
    options: { replace: boolean },
  ) {
    const search = params.toString();
    navigate(
      {
        pathname: routeLocation.pathname,
        search: search ? `?${search}` : "",
      },
      {
        ...options,
        // Rendered/Raw and version changes must not discard the search route
        // stored in history state, otherwise the preview unexpectedly becomes
        // a full-page document navigation.
        state: routeLocation.state,
      },
    );
  }

  function openFullPage(nextView: DocView = view) {
    const p = new URLSearchParams(searchParams);
    if (nextView === "rendered") p.delete("view");
    else p.set("view", nextView);
    const search = p.toString();
    navigate(
      {
        pathname: routeLocation.pathname,
        search: search ? `?${search}` : "",
      },
      { replace: true, state: null },
    );
  }

  function requestEdit() {
    if (presentation === "preview") {
      openFullPage("edit");
      return;
    }
    setView("edit");
  }

  useEffect(() => {
    if (!name) return;
    setVaultRole(null);
    setVaultKind(null);
    setVaultReadOnly(true);
    getVaultInfo(name)
      .then((d) => {
        setVaultRole(d?.role || null);
        setVaultKind(d?.is_external_git ? "mirror" : "normal");
        setVaultReadOnly(Boolean(d?.is_archived || d?.is_external_git));
      })
      .catch(() => {
        setVaultRole(null);
        setVaultKind("error");
        setVaultReadOnly(true);
      });
  }, [name]);

  useEffect(() => {
    if (!detailsOpen) return;

    const focusFrame = window.matchMedia?.("(max-width: 1023px)").matches
      ? window.requestAnimationFrame(() => detailsCloseRef.current?.focus())
      : null;

    const handleKeyDown = (event: KeyboardEvent) => {
      if (event.key !== "Escape") return;
      event.preventDefault();
      setDetailsOpen(false);
      window.requestAnimationFrame(() => detailsToggleRef.current?.focus());
    };

    window.addEventListener("keydown", handleKeyDown);
    return () => {
      if (focusFrame !== null) window.cancelAnimationFrame(focusFrame);
      window.removeEventListener("keydown", handleKeyDown);
    };
  }, [detailsOpen]);

  useEffect(() => {
    if (view === "edit" || !restoreEditFocusRef.current) return;
    restoreEditFocusRef.current = false;
    const frame = window.requestAnimationFrame(() => editButtonRef.current?.focus());
    return () => window.cancelAnimationFrame(frame);
  }, [view]);

  const docQuery = useQuery({
    queryKey: ["document", name, docId, documentVersion],
    queryFn: () => getDocument(name!, docId, documentVersion),
    enabled: !!name && !!docId,
    retry: false,
  });

  useEffect(() => {
    if (import.meta.env.VITE_AKB_TEST_MODE !== "mock" || !name || !docId) return;
    const onMockRefetch = (event: Event) => {
      const detail = (event as CustomEvent<{ vault?: unknown; document?: unknown }>).detail;
      if (detail?.vault !== name || detail.document !== docId) return;
      void queryClient.refetchQueries({ queryKey: ["document", name, docId] });
    };
    window.addEventListener("akb:mock-document-refetch", onMockRefetch);
    return () => window.removeEventListener("akb:mock-document-refetch", onMockRefetch);
  }, [docId, name, queryClient]);

  const doc = docOverride ?? docQuery.data ?? null;
  const historyQuery = useQuery({
    queryKey: ["document-history", name, doc?.path],
    queryFn: () => getDocumentHistoryWithFallback(name!, doc!.path, 20),
    enabled: Boolean(name && doc?.path),
    staleTime: 30_000,
    retry: false,
  });
  const provenance = historyQuery.data?.history ?? EMPTY_DOCUMENT_HISTORY;
  const currentUserId = currentUser?.user_id;
  const editTitleCandidatesQuery = useQuery({
    queryKey: ["document-edit-title-candidates", name],
    queryFn: () => browseVault(name!),
    enabled: Boolean(name && view === "edit"),
    staleTime: 30_000,
  });
  const editTitleCandidates = useMemo(
    () =>
      (editTitleCandidatesQuery.data?.items || [])
        .filter(isBrowsedDocumentTitle)
        .map((item) => ({ name: item.name, path: item.path })),
    [editTitleCandidatesQuery.data?.items],
  );
  const detectedTitleConflict = useMemo(
    () =>
      titleChanged && doc?.path
        ? findDocumentTitleConflict(
            editTitleCandidates,
            normalizedEditingTitle,
            documentCollection(doc.path),
            doc.path,
          )
        : null,
    [
      doc?.path,
      editTitleCandidates,
      normalizedEditingTitle,
      titleChanged,
    ],
  );
  const titleConflict =
    serverTitleConflict ?? (titleTouched ? detectedTitleConflict : null);
  const titleError =
    titleTouched && !normalizedEditingTitle ? "Enter a document title." : "";
  const titleDescriptionIds = [
    "document-edit-title-help",
    titleError ? "document-edit-title-error" : "",
    titleConflict ? "document-edit-title-conflict" : "",
  ]
    .filter(Boolean)
    .join(" ");

  useEffect(() => {
    const loaded = docQuery.data;
    if (!currentUserId || !name || !loaded?.path || !loaded?.title) return;
    recordRecentDocumentView(currentUserId, {
      vault: name,
      path: loaded.path,
      title: loaded.title,
      type: loaded.type,
      updatedAt: loaded.updated_at,
    });
  }, [currentUserId, docQuery.data, name]);

  // A versioned read reports current_commit = the requested version (not HEAD),
  // so `doc` alone can't reveal the doc's true latest commit. While a commit is
  // pinned, fetch HEAD separately so we can tell a pin that IS the latest (a
  // Recent-activity / commit-log click on the newest commit) apart from a
  // genuinely older one. The key matches the un-pinned docQuery key so the two
  // share a cache entry rather than double-fetching HEAD.
  const headQuery = useQuery({
    queryKey: ["document", name, docId, undefined],
    queryFn: () => getDocument(name!, docId),
    enabled: !!name && !!docId && !!commitHash && !isDiffMode,
    retry: false,
  });
  const headCommit = commitHash
    ? isDiffMode
      ? doc?.current_commit
      : headQuery.data?.current_commit
    : doc?.current_commit;
  // Genuinely historical only when the pin points at a commit OTHER than HEAD.
  // sameCommitRef does a prefix-tolerant compare (the commit log links 12-char
  // short hashes; current_commit is the full SHA) and returns false while HEAD
  // is still loading — so a real older version is never briefly editable.
  const isHistorical = !!commitHash && !sameCommitRef(commitHash, headCommit);
  const canEdit =
    !vaultReadOnly &&
    (doc?.path !== VAULT_SKILL_PATH || vaultRole === "owner") &&
    !isHistorical &&
    !isDiffMode &&
    (vaultRole === "writer" || vaultRole === "admin" || vaultRole === "owner");

  const selectedHistoryIndex = useMemo(
    () =>
      commitHash
        ? provenance.findIndex((entry) => sameCommitRef(entry.hash, commitHash))
        : -1,
    [commitHash, provenance],
  );
  const selectedHistoryEntry: DocumentHistoryEntry | undefined =
    selectedHistoryIndex >= 0 ? provenance[selectedHistoryIndex] : undefined;
  const baseHistoryEntry: DocumentHistoryEntry | undefined =
    selectedHistoryIndex >= 0 ? provenance[selectedHistoryIndex + 1] : undefined;

  useEffect(() => {
    if (isDiffMode) {
      wasDiffModeRef.current = true;
      return;
    }
    if (!wasDiffModeRef.current || !diffOriginHashRef.current) return;
    wasDiffModeRef.current = false;
    const originHash = diffOriginHashRef.current;
    setDetailsTab("history");
    setDetailsOpen(true);
    const firstFrame = window.requestAnimationFrame(() => {
      window.requestAnimationFrame(() => {
        const triggers = document.querySelectorAll<HTMLButtonElement>(
          "[data-document-diff-trigger]",
        );
        Array.from(triggers).find(
          (trigger) => trigger.dataset.documentDiffTrigger === originHash,
        )?.focus();
      });
    });
    return () => window.cancelAnimationFrame(firstFrame);
  }, [isDiffMode]);
  // Parse headings once for the outline-tab count (the outline + renderer each
  // re-scan internally; this removes the third pass that ran on every render).
  const headingSlugs = useMemo(() => parseHeadings(doc?.content || ""), [doc?.content]);

  // Re-pull relations after an add/remove from the Relations panel. Keyed off
  // the doc *path* (the GET response has no internal id — see the load effect).
  const reloadRelations = () => {
    const p = doc?.path;
    if (!p) return;
    getRelations(name!, p)
      .then((r) => setRelations(r.relations || []))
      .catch(() => setRelationsError(true));
  };

  useEffect(() => {
    const d = docQuery.data;
    if (!d?.path || !name) return;

    const identity = documentIdentity(name, d);
    const identityChanged = documentIdentityRef.current !== identity;
    const preserveEditState =
      view === "edit" &&
      (isDirty ||
        draftStatus === "restored" ||
        draftStatus === "saving" ||
        draftStatus === "saved" ||
        draftStatus === "error" ||
        saveConflict !== null);

    if (identityChanged) {
      documentIdentityRef.current = identity;
      draftHydratedForRef.current = null;
      draftSessionRef.current = null;
      draftTabIdRef.current = currentUserId ? documentEditDraftTabId() : null;
      setDocOverride(null);
      setRelations([]);
      setRelationsError(false);
      setBodyError("");
      setTitleTouched(false);
      setServerTitleConflict(null);
      setSaveConflict(null);
      setLatestServer(null);
      setDraftRecovery(null);
      setDraftStatus("idle");
      setDraftNotice("");
      setClaimedAssetIds(null);
      setOriginalContent(d.content || "");
      setEditingContent(d.content || "");
      setOriginalTitle(d.title || "");
      setEditingTitle(d.title || "");
      setEditingAssetIds([]);
      setEditorInitialContent(d.content || "");
      editorHydrationModeRef.current = "server";
      hydratedKey.current = null;
      setBaseCommit(validRevision(d.current_commit) ? d.current_commit : null);
      setEditorKey((k) => k + 1);
    } else if (preserveEditState) {
      if (
        validRevision(d.current_commit) &&
        d.current_commit !== editBaseCommitRef.current
      ) {
        setLatestServer(d);
      }
    } else {
      // A clean editor can follow an external refresh. Once the user has a
      // dirty draft, this branch is never used, so the base revision remains
      // pinned until the user explicitly reapplies it to the latest version.
      setOriginalContent(d.content || "");
      setEditingContent(d.content || "");
      setOriginalTitle(d.title || "");
      setEditingTitle(d.title || "");
      setEditorInitialContent(d.content || "");
      editorHydrationModeRef.current = "server";
      hydratedKey.current = null;
      setBaseCommit(validRevision(d.current_commit) ? d.current_commit : null);
      setEditorKey((k) => k + 1);
    }

    if (d.path && d.path !== docId) {
      navigate(`/vault/${name}/doc/${encodeURIComponent(d.path)}`, {
        replace: true,
        state: routeLocation.state,
      });
    }
    // getRelations builds the canonical akb:// URI from the vault-relative
    // path. The GET response exposes no internal `id`; `uri`/`path` is the
    // sole document identifier.
    getRelations(name, d.path)
      .then((r) => setRelations(r.relations || []))
      .catch(() => setRelationsError(true));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [docQuery.data]);

  useEffect(() => {
    const d = docQuery.data;
    if (view !== "edit" || !d?.path || !name || !currentUserId) return;
    const identity = documentIdentity(name, d);
    const hydrationKey = `${currentUserId}\u0000${identity}`;
    if (draftHydratedForRef.current === hydrationKey) return;
    draftHydratedForRef.current = hydrationKey;
    draftTabIdRef.current ||= documentEditDraftTabId();
    const result = loadDocumentEditDraft(
      currentUserId,
      name,
      identity,
      draftTabIdRef.current,
    );
    setDraftRecovery(
      result.status === "expired" || result.status === "incompatible" ? result : null,
    );

    if (result.status === "restored" || result.status === "expired") {
      const stored = result.draft;
      const sameTab = stored.tabId === draftTabIdRef.current;
      draftSessionRef.current = sameTab
        ? stored
        : {
            ...stored,
            draftId: createDocumentEditDraftId(),
            tabId: draftTabIdRef.current,
          };
      skipNextDraftSaveRef.current = true;
      restoredDraftRevisionRef.current = draftRevisionRef.current;
      setOriginalContent(stored.baseBody);
      setOriginalTitle(stored.baseTitle);
      setEditingContent(stored.body);
      setEditingTitle(stored.title);
      setEditingAssetIds(stored.assetIds);
      setEditorInitialContent(stored.body);
      editorHydrationModeRef.current = "draft";
      hydratedKey.current = null;
      setBaseCommit(validRevision(stored.baseCommit) ? stored.baseCommit : null);
      setEditorKey((k) => k + 1);
      setDraftStatus(result.status === "expired" ? "expired" : "restored");
      setDraftNotice(
        result.status === "expired"
          ? "This draft has expired. Its text is kept for copying; attached images may no longer be available."
          : "Local draft restored",
      );
      if (result.status === "expired") {
        const protectedAssetIds = new Set(
          listDocumentEditDrafts(currentUserId, name, identity)
            .filter(
              (draft) =>
                draft.draftId !== stored.draftId &&
                Date.parse(draft.expiresAt) > Date.now(),
            )
            .flatMap((draft) => draft.assetIds),
        );
        void Promise.allSettled(
          stored.assetIds
            .filter((assetId) => !protectedAssetIds.has(assetId))
            .map((assetId) => discardAsset(name, assetId)),
        );
      }
    } else if (result.status === "incompatible") {
      setDraftStatus("incompatible");
      setDraftNotice(
        "This draft uses an incompatible editor profile. Its original text remains available to copy.",
      );
    } else if (result.status === "storage-unavailable") {
      setDraftStatus("unavailable");
      setDraftNotice("Local draft storage is unavailable; edits remain only in this tab.");
    }
  }, [currentUserId, docQuery.data, name, view]);

  useEffect(() => {
    if (view !== "edit" || !currentUserId || !name || !doc?.path) return;
    if (draftStatus === "expired" || draftStatus === "incompatible" || draftStatus === "unavailable") {
      return;
    }
    if (
      skipNextDraftSaveRef.current ||
      (draftStatus === "restored" &&
        draftRevisionRef.current === restoredDraftRevisionRef.current)
    ) {
      skipNextDraftSaveRef.current = false;
      return;
    }
    if (!isDirty) {
      const existing = draftSessionRef.current;
      if (existing) {
        clearDocumentEditDraft(existing);
        const currentAssets = new Set(editingAssetIds);
        const protectedAssets = protectedDraftAssetIds(existing.draftId);
        void Promise.allSettled(
          existing.assetIds
            .filter(
              (assetId) =>
                !currentAssets.has(assetId) && !protectedAssets.has(assetId),
            )
            .map((assetId) => discardAsset(name, assetId)),
        );
      }
      draftSessionRef.current = null;
      setDraftStatus("idle");
      setDraftNotice("");
      return;
    }

    if (storageSaveTimerRef.current !== null) {
      window.clearTimeout(storageSaveTimerRef.current);
    }
    setDraftStatus("saving");
    setDraftNotice("Saving draft locally…");
    storageSaveTimerRef.current = window.setTimeout(() => {
      storageSaveTimerRef.current = null;
      persistEditDraft();
    }, 300);
    return () => {
      if (storageSaveTimerRef.current !== null) {
        window.clearTimeout(storageSaveTimerRef.current);
        storageSaveTimerRef.current = null;
      }
    };
    // `draftStatus` is intentionally read from the render that scheduled this
    // write. Including it here would restart the debounce after every status
    // update and could mark an expired draft active again.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [
    currentUserId,
    doc?.path,
    editingAssetIds,
    editingContent,
    editingTitle,
    isDirty,
    name,
    originalContent,
    originalTitle,
    view,
  ]);

  useEffect(() => {
    if (
      view !== "edit" ||
      !isDirty ||
      draftStatus === "expired" ||
      draftStatus === "incompatible" ||
      (draftStatus === "restored" &&
        draftRevisionRef.current === restoredDraftRevisionRef.current)
    ) {
      latestDraftInputRef.current = null;
      return;
    }
    latestDraftInputRef.current = currentDraftInput();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [
    currentUserId,
    doc?.path,
    draftStatus,
    editingAssetIds,
    editingContent,
    editingTitle,
    isDirty,
    name,
    originalContent,
    originalTitle,
    view,
  ]);

  // Persist the latest snapshot synchronously before a fast tab close, Back
  // navigation, or preview dismissal. This effect intentionally has no state
  // dependencies: its cleanup is an actual component-unmount boundary.
  useEffect(
    () => () => {
      const input = latestDraftInputRef.current;
      if (input) saveDocumentEditDraft(input);
    },
    [],
  );

  // Warn before page navigation (close tab, browser back) when dirty.
  useEffect(() => {
    if (!hasUnsavedWork) return;
    const onBeforeUnload = (e: BeforeUnloadEvent) => {
      e.preventDefault();
      e.returnValue = "";
    };
    window.addEventListener("beforeunload", onBeforeUnload);
    return () => window.removeEventListener("beforeunload", onBeforeUnload);
  }, [hasUnsavedWork]);

  function openExistingDocument(existingPath: string) {
    const nextSearch = new URLSearchParams(searchParams);
    nextSearch.delete("commit");
    nextSearch.delete("view");
    const search = nextSearch.toString();
    navigate(
      {
        pathname: `/vault/${name}/doc/${encodeURIComponent(existingPath)}`,
        search: search ? `?${search}` : "",
      },
      { state: routeLocation.state },
    );
  }

  async function showTitleConflict(conflict: DocumentTitleConflict) {
    let exactContent = false;
    try {
      const existing = await getDocument(name!, conflict.existingPath);
      exactContent =
        typeof existing?.content === "string" &&
        existing.content === editingContent;
    } catch {
      // The structured conflict still gives enough information to recover.
    }
    setServerTitleConflict({ ...conflict, exactContent });
    window.requestAnimationFrame(() => titleConflictRef.current?.focus());
  }

  function makeConflictSnapshot(
    label: string,
    commit: string | null,
    title: string,
    body: string,
  ): DocumentConflictSnapshot {
    return { label, commit, title, body };
  }

  async function readLatestForConflict(): Promise<any | null> {
    try {
      const latest = await getDocument(name!, docId);
      setLatestServer(latest);
      return latest;
    } catch {
      setLatestServer(
        null,
        "The latest version could not be loaded. Your draft remains available.",
      );
      return null;
    }
  }

  async function applyDraftToLatest() {
    const latest = latestServerDocument || (await readLatestForConflict());
    if (!latest || !validRevision(latest.current_commit)) return;
    setRebasing(true);
    try {
      const baseBody = typeof latest.content === "string" ? latest.content : "";
      const baseTitle = typeof latest.title === "string" ? latest.title : "";
      setOriginalContent(baseBody);
      setOriginalTitle(baseTitle);
      setBaseCommit(latest.current_commit);
      setDocOverride(latest);
      queryClient.setQueryData(
        ["document", name, docId, undefined],
        latest,
      );
      setLatestServer(null);
      setSaveConflict(null);
      setBodyError("");
      persistEditDraft({
        baseCommit: latest.current_commit,
        baseTitle,
        baseBody,
        title: editingTitle,
        body: editingContent,
        assetIds: editingAssetIds,
      });
    } finally {
      setRebasing(false);
    }
  }

  async function copyDraftText(text: string, message: string) {
    try {
      await navigator.clipboard?.writeText(text);
      setDraftNotice(message);
    } catch {
      setDraftNotice("Clipboard access is unavailable; select the preserved text manually.");
    }
  }

  async function handleSaveDocument(
    titleConflictPolicy: "allow" | "reject" = "reject",
  ) {
    if (!name || !docId || uploadingImage) return;
    setTitleTouched(true);
    if (!normalizedEditingTitle) {
      window.requestAnimationFrame(() => editTitleRef.current?.focus());
      return;
    }
    const activeConflict = serverTitleConflict ?? detectedTitleConflict;
    if (titleConflictPolicy === "reject" && activeConflict) {
      await showTitleConflict(activeConflict);
      return;
    }

    const contentToSave = editingContent;
    const assetIdsToClaim = editingAssetIds;
    const payload: DocumentUpdateInput = {};
    if (contentChanged) payload.content = contentToSave;
    if (titleChanged) {
      payload.title = normalizedEditingTitle;
      payload.title_conflict_policy = titleConflictPolicy;
    }
    if (Object.keys(payload).length === 0) return;

    const baseCommit = editBaseCommitRef.current;
    if (!validRevision(baseCommit)) {
      setBodyError("This document has no valid revision. Reload it before saving.");
      return;
    }
    payload.expected_commit = baseCommit;

    const saveRevision = draftRevisionRef.current;
    setSavingBody(true);
    setBodyError("");
    try {
      const saved = await updateDocument(name, docId, payload);
      const savedCommit = saved.current_commit ?? saved.commit_hash ?? null;
      if (!validRevision(savedCommit)) {
        throw new Error("The server did not return a document revision after saving.");
      }
      const now = new Date().toISOString();
      const titleToSave = titleChanged ? normalizedEditingTitle : originalTitle;
      const hasFollowupEdits = draftRevisionRef.current !== saveRevision;
      const followup = editingSnapshotRef.current;
      // Optimistically advance content + updated_at so the byline reads
      // "last changed just now" without waiting for a refetch. DocumentView
      // consumes the same query key independently, so update that cache too;
      // a local page override alone leaves its Rendered tab stale.
      const nextDoc = {
        ...(doc || {}),
        content: contentToSave,
        title: titleToSave,
        updated_at: now,
        current_commit: savedCommit,
      };
      setBaseCommit(savedCommit);
      setDocOverride(nextDoc);
      queryClient.setQueryData(
        ["document", name, docId, undefined],
        (cached: Record<string, unknown> | undefined) => ({
          ...(cached || {}),
          ...nextDoc,
        }),
      );
      setOriginalContent(contentToSave);
      setOriginalTitle(titleToSave);
      if (!hasFollowupEdits) setEditingTitle(titleToSave);
      setTitleTouched(false);
      setServerTitleConflict(null);
      setSaveConflict(null);
      setLatestServer(null);
      if (hasFollowupEdits) {
        // Keep later edits in a new draft whose base is the commit that just
        // succeeded. The accepted snapshot alone is the one being claimed.
        persistEditDraft({
          baseCommit: savedCommit,
          baseTitle: titleToSave,
          baseBody: contentToSave,
          title: followup.title,
          body: followup.content,
          assetIds: followup.assetIds,
        });
      } else if (draftSessionRef.current) {
        clearDocumentEditDraft(draftSessionRef.current);
        draftSessionRef.current = null;
        latestDraftInputRef.current = null;
        setDraftStatus("idle");
        setDraftNotice("");
      }
      // Sidebar refresh is best-effort — its failure must not leave the
      // user looking at a "still dirty" editor after a successful save.
      try {
        refetchTree();
      } catch {
        // intentionally swallowed
      }
      // Commit `savedAt` before the view switch so the SAVED badge
      // renders in its own paint; bundling it with `setSearchParams`
      // lets React squash the indicator into the same commit as the
      // tab-strip remount and the user never sees it.
      flushSync(() => {
        flashSaved();
        // Let the editor's unmount cleanup run only after the server has
        // atomically claimed referenced uploads. During the request this stays
        // true so navigation cleanup cannot discard assets being saved.
        setSavingBody(false);
        setClaimedAssetIds(assetIdsToClaim);
      });
      const p = new URLSearchParams(searchParams);
      p.delete("view");
      // A commit pin equal to HEAD is editable, but this save just created a
      // newer HEAD. Return to the live document instead of leaving the URL on
      // the now-historical revision while showing the new body.
      p.delete("commit");
      restoreEditFocusRef.current = true;
      updateRouteParams(p, { replace: true });
    } catch (e: unknown) {
      const conflict = documentTitleConflictFromError(e);
      if (conflict) {
        await showTitleConflict(conflict);
        return;
      }
      if (isRevisionConflict(e)) {
        const latest = await readLatestForConflict();
        setSaveConflict({
          base: makeConflictSnapshot(
            "Original base",
            baseCommit,
            originalTitle,
            originalContent,
          ),
          local: makeConflictSnapshot(
            "Your draft",
            baseCommit,
            normalizedEditingTitle,
            contentToSave,
          ),
          latest: latest
            ? makeConflictSnapshot(
                "Latest server version",
                validRevision(latest.current_commit) ? latest.current_commit : null,
                latest.title || "",
                latest.content || "",
              )
            : null,
          latestError: latest ? undefined : latestServerErrorRef.current,
        });
        setDraftNotice("Your local draft is preserved after the conflict.");
        return;
      }
      const status = e instanceof ApiError ? e.status : 0;
      // 5xx responses can carry stack traces or SQL fragments — never
      // surface those verbatim. 4xx are intentional API errors so the
      // message is OK to show.
      const safe =
        status >= 500
          ? "The server hit an error while saving. Please retry."
          : e instanceof Error
            ? e.message
            : "Save failed.";
      setBodyError(safe);
      setDraftNotice("Your local draft is preserved. Retry when the server is available.");
    } finally {
      // Always clear the spinner — a post-await setState throwing must not
      // leave the editor stuck on "Saving…".
      setSavingBody(false);
    }
  }

  const savedTimerRef = useRef<number | null>(null);
  function flashSaved() {
    setSavedAt(Date.now());
    if (savedTimerRef.current !== null) {
      window.clearTimeout(savedTimerRef.current);
    }
    savedTimerRef.current = window.setTimeout(() => {
      setSavedAt(null);
      savedTimerRef.current = null;
    }, 2500);
  }
  useEffect(
    () => () => {
      if (savedTimerRef.current !== null) {
        window.clearTimeout(savedTimerRef.current);
      }
    },
    [],
  );

  function handleCancelBody() {
    setBodyError("");
    setView("rendered");
  }

  function protectedDraftAssetIds(excludeDraftId?: string): Set<string> {
    if (!currentUserId || !name || !doc?.path) return new Set();
    return new Set(
      listDocumentEditDrafts(
        currentUserId,
        name,
        documentIdentity(name, doc),
      )
        .filter(
          (draft) =>
            draft.draftId !== excludeDraftId &&
            Date.parse(draft.expiresAt) > Date.now(),
        )
        .flatMap((draft) => draft.assetIds),
    );
  }

  async function discardEditDraft() {
    const draft = draftSessionRef.current;
    const protectedAssets = protectedDraftAssetIds(draft?.draftId);
    latestDraftInputRef.current = null;
    const assets = new Set<string>([
      ...(draft?.assetIds || []),
      ...editingAssetIds,
    ]);
    if (draft) clearDocumentEditDraft(draft);
    if (draftRecovery?.status === "expired") {
      clearDocumentEditDraft(draftRecovery.draft);
    }
    await Promise.allSettled(
      [...assets]
        .filter((assetId) => !protectedAssets.has(assetId))
        .map((assetId) => discardAsset(name!, assetId)),
    );
    draftSessionRef.current = null;
    setDraftRecovery(null);
    setDraftStatus("idle");
    setDraftNotice("");
    setSaveConflict(null);
  }

  // The vault guide is system-managed: its only editing surface is the guide
  // section in vault settings, so the plain full-page viewer bounces there.
  // Search preview is exempt so every document result keeps the same modal
  // reading flow. A ?commit= pin is also exempt — that is the URL-addressable
  // version view (commit-log and activity links, and the settings editor's
  // history entry point), and it must keep resolving. The gate is the raw
  // param, not the computed
  // `isHistorical`: that one reads false until the HEAD query resolves, which
  // would redirect every version link away before it could settle.
  if (
    presentation !== "preview" &&
    docId === VAULT_SKILL_PATH &&
    !commitHash &&
    vaultKind === "normal"
  ) {
    return <Navigate to={`/vault/${name}/settings#skill`} replace />;
  }

  if (docQuery.isError && !doc) {
    const errorMsg = (docQuery.error as Error)?.message ?? "Unknown error";
    return (
      <div className="py-8 fade-up">
        <div className="coord-spark mb-2">⚠ Error</div>
        <p className="text-destructive mb-6 max-w-xl">{errorMsg}</p>
        <Button asChild variant="outline">
          <Link to={`/vault/${name}`}>
            <ArrowLeft className="h-4 w-4" aria-hidden />
            Back to {name}
          </Link>
        </Button>
      </div>
    );
  }

  if (!doc) {
    return <DocumentPageLoading presentation={presentation} />;
  }

  async function handleUnpublish() {
    setPublishing(true);
    setPublishError("");
    try {
      await unpublishDoc(name!, docId);
      setDocOverride({ ...doc, is_public: false, public_slug: null });
    } catch (e: any) {
      setPublishError(e?.message || "Failed to unpublish");
    }
    setPublishing(false);
  }

  async function copyPublicLink() {
    const url = `${location.origin}/p/${doc.public_slug}`;
    // clipboard is undefined on insecure (plain-HTTP) origins — guard so the
    // copy never throws an unhandled rejection and the UI doesn't stick.
    try {
      await navigator.clipboard?.writeText(url);
      setCopied(true);
      setTimeout(() => setCopied(false), 2000);
    } catch {
      /* clipboard blocked — link stays visible to copy manually */
    }
  }

  const commitShort = (commitHash || headCommit)?.slice(0, 7);
  const inEditMode = view === "edit";
  const fileName = doc.path?.split("/").pop() || doc.title || "Document";
  const collectionPath = doc.path?.includes("/")
    ? doc.path.slice(0, doc.path.lastIndexOf("/"))
    : "Vault root";
  const isUuid = (value: string) =>
    /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i.test(value);
  const authorName =
    doc.created_by_name ||
    (doc.created_by && !isUuid(doc.created_by) ? doc.created_by : null);
  const canWrite =
    vaultRole === "writer" || vaultRole === "admin" || vaultRole === "owner";
  const moveDisabledReason = documentMoveDisabledReason({
    path: doc.path,
    vaultRole,
    vaultKind,
    vaultReadOnly,
    isHistorical: isHistorical || isDiffMode,
  });
  const canDelete =
    canWrite &&
    !vaultReadOnly &&
    !isHistorical &&
    !isDiffMode &&
    doc.path !== VAULT_SKILL_PATH;
  const archiveDisabledReason = documentArchiveDisabledReason({
    role: vaultRole, readOnly: vaultReadOnly,
    historical: isHistorical || isDiffMode,
    guide: doc.path === VAULT_SKILL_PATH,
  });
  const pendingArchiveCheck = archivePending && archivePending.vault === name && archivePending.ref === docId ? archivePending : null;

  const openVersion = (hash?: string, options: { replace?: boolean } = {}) => {
    const params = new URLSearchParams(searchParams);
    params.delete("view");
    if (hash) params.set("commit", hash);
    else params.delete("commit");
    updateRouteParams(params, { replace: options.replace ?? false });
  };

  const openDiff = (hash: string, trigger: HTMLButtonElement) => {
    diffOriginHashRef.current = hash;
    trigger.blur();
    setDetailsOpen(false);
    const params = new URLSearchParams(searchParams);
    params.set("commit", hash);
    params.set("view", "diff");
    updateRouteParams(params, { replace: false });
  };

  const closeDetails = () => {
    setDetailsOpen(false);
    window.requestAnimationFrame(() => detailsToggleRef.current?.focus());
  };

  return (
    <>
      <section
        aria-label="Document workspace"
        className="flex h-full min-h-0 flex-col overflow-hidden bg-background fade-in"
        data-presentation={presentation}
      >
        <header
          className={cn(
            "relative z-[var(--z-sticky)] flex h-16 shrink-0 items-center gap-3 border-b border-border bg-surface pl-3 sm:pl-4 lg:pl-5",
            presentation === "preview"
              ? "pr-12 sm:pr-14"
              : "pr-3 sm:pr-4 lg:pr-5",
          )}
        >
          <div className="flex min-w-0 items-center gap-3">
            <div className="hidden h-9 w-9 shrink-0 items-center justify-center rounded-[var(--radius-md)] border border-primary/20 bg-surface-selected text-surface-selected-foreground sm:flex">
              <FileText className="h-4 w-4" aria-hidden />
            </div>
            <div className="min-w-0">
              <div className="flex min-w-0 items-center gap-2">
                <h1 id="doc-title" className="truncate font-display text-base font-semibold text-foreground sm:text-lg">
                  {doc.title}
                </h1>
                {isDiffMode ? (
                  <Badge variant="info-outline">Comparing</Badge>
                ) : isHistorical ? (
                  <Badge variant="warning">Historical</Badge>
                ) : (
                  <Badge variant={doc.status === "archived" ? "archived" : doc.status === "draft" ? "draft" : "active"}>
                    {doc.status || "Current"}
                  </Badge>
                )}
              </div>
              <p className="truncate text-xs text-foreground-muted">
                {presentation === "preview" ? (
                  <Link
                    to={`/vault/${name}`}
                    aria-label={`Open ${name} Vault overview`}
                    className="inline-flex items-center gap-1 rounded-[var(--radius-sm)] font-medium text-link transition-token hover:text-link-hover hover:underline focus:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2 focus-visible:ring-offset-surface"
                  >
                    <Box className="h-3 w-3" aria-hidden />
                    {name}
                  </Link>
                ) : (
                  <span className="inline-flex items-center gap-1 font-medium text-foreground">
                    <Box className="h-3 w-3" aria-hidden />
                    {name}
                  </span>
                )}
                <span aria-hidden> · </span>
                <span>{collectionPath}</span>
              </p>
            </div>
          </div>

          <div className="ml-auto flex shrink-0 items-center gap-2">
            <div className="mr-1 hidden items-center gap-2 text-xs text-foreground-muted xl:flex" role="status" aria-live="polite">
              {savedAt ? (
                <CheckCircle2 className="h-3.5 w-3.5 text-success" aria-hidden />
              ) : (
                <GitCommitHorizontal className="h-3.5 w-3.5" aria-hidden />
              )}
              <span>{savedAt ? "Saved just now" : doc.updated_at ? `Changed ${timeAgo(doc.updated_at)}` : "Versioned in Git"}</span>
            </div>
            {presentation === "preview" && !inEditMode && (
              <Button
                type="button"
                variant="outline"
                size="sm"
                onClick={() => openFullPage(view)}
              >
                <Maximize2 className="h-4 w-4" aria-hidden />
                <span className="hidden lg:inline">Full page</span>
              </Button>
            )}
            {inEditMode ? (
              <>
                <Button
                  ref={cancelEditButtonRef}
                  type="button"
                  variant="outline"
                  size="sm"
                  onClick={handleCancelBody}
                  disabled={savingBody}
                >
                  Cancel
                </Button>
                <Button
                  type="button"
                  variant="accent"
                  size="sm"
                  aria-label="Save"
                  loading={savingBody}
                  onClick={() => void handleSaveDocument("reject")}
                  disabled={uploadingImage || !isDirty}
                >
                  {savingBody ? "Saving…" : "Save changes"}
                </Button>
              </>
            ) : (
              <>
                {canEdit && (
                  <Button ref={editButtonRef} type="button" size="sm" onClick={requestEdit}>
                    <Pencil className="h-4 w-4" aria-hidden />
                    <span className="hidden sm:inline">Edit</span>
                  </Button>
                )}
                <ResourceActionsMenu
                  resourceName={doc.title || fileName}
                  archiveAction={doc.status === "archived" ? "restore" : "archive"}
                  archiveDisabledReason={archiveDisabledReason}
                  onArchiveAction={() => setArchiveOpen(true)}
                  moveLabel="Move document"
                  onMove={moveDisabledReason ? undefined : () => setMoveOpen(true)}
                  moveDisabledReason={moveDisabledReason || undefined}
                  deleteLabel={canDelete ? "Delete document" : undefined}
                  onDelete={canDelete ? () => setDeleteOpen(true) : undefined}
                />
              </>
            )}
          </div>
        </header>

        {archiveNotice && <Alert variant="success" className="shrink-0">{archiveNotice}<Button variant="ghost" size="sm" onClick={() => setArchiveNotice("")}>Dismiss</Button></Alert>}
        {doc.status === "archived" && !isHistorical && !isDiffMode && view !== "edit" && (
          <div className="flex shrink-0 flex-wrap items-center justify-between gap-2 border-b border-border bg-surface-2 px-4 py-2 text-xs text-foreground-muted">
            <span>Archived · Hidden from current documents. Existing links and access remain unchanged.</span>
            <Button variant="outline" size="sm" disabled={!!archiveDisabledReason} title={archiveDisabledReason} onClick={() => setArchiveOpen(true)}>Restore document</Button>
            {archiveDisabledReason && <span>{archiveDisabledReason}</span>}
          </div>
        )}

        {isDiffMode ? (
          <div
            role="status"
            aria-live="polite"
            className="flex shrink-0 flex-wrap items-center justify-between gap-3 border-b border-info/30 bg-info-soft px-4 py-2 text-sm text-info-soft-foreground sm:px-6"
          >
            <div className="flex min-w-0 items-center gap-2">
              <GitCompareArrows className="h-4 w-4 shrink-0" aria-hidden />
              <span>
                Comparing {baseHistoryEntry ? <code className="font-mono font-medium">{baseHistoryEntry.hash.slice(0, 7)}</code> : "the previous revision"}
                <span aria-hidden> → </span>
                <code className="font-mono font-medium">{commitHash?.slice(0, 7)}</code>
              </span>
            </div>
            <div className="flex items-center gap-2">
              <Button type="button" variant="outline" size="sm" onClick={() => openVersion(commitHash, { replace: true })}>
                Back to version
              </Button>
              <Button type="button" variant="ghost" size="sm" onClick={() => openVersion(undefined, { replace: true })}>
                Back to latest
              </Button>
            </div>
          </div>
        ) : isHistorical ? (
          <div
            role="status"
            aria-live="polite"
            className="flex shrink-0 flex-wrap items-center justify-between gap-3 border-b border-warning/30 bg-warning-soft px-4 py-2 text-sm text-warning-soft-foreground sm:px-6"
          >
            <div className="flex min-w-0 items-center gap-2">
              <History className="h-4 w-4 shrink-0" aria-hidden />
              <span>
                Viewing commit <code className="font-mono font-medium">{commitHash.slice(0, 7)}</code>. Editing is disabled for historical versions.
              </span>
            </div>
            <Button
              type="button"
              variant="outline"
              size="sm"
              onClick={() => openVersion()}
            >
              Back to latest
            </Button>
          </div>
        ) : null}

        {publishError && (
          <div className="shrink-0 border-b border-border bg-surface px-4 py-3 sm:px-6">
            <Alert variant="destructive">{publishError}</Alert>
          </div>
        )}
        {docQuery.isRefetchError && (
          <div className="shrink-0 border-b border-border bg-surface px-4 py-3 sm:px-6">
            <Alert variant="warning" title="Couldn’t refresh the document">
              {isDirty
                ? "Your local draft is still open. The latest server version will be loaded before any conflict resolution."
                : "The current document remains visible. Try again when the server is available."}
            </Alert>
          </div>
        )}

        <div className="relative min-h-0 flex-1 overflow-hidden">
          <main
            id="document-reading-canvas"
            className={cn(
              "h-full bg-background",
              isDiffMode ? "overflow-hidden" : "overflow-y-auto",
            )}
          >
            <article
              ref={setArticleEl}
              aria-labelledby="doc-title"
              className={cn(
                "w-full",
                inEditMode
                  ? "px-3 py-4 sm:px-4 sm:py-5 lg:px-5 xl:px-6 2xl:px-8"
                  : isDiffMode
                    ? "flex h-full min-h-0 flex-col p-2 sm:p-3"
                    : "p-2 sm:p-3",
              )}
            >
              <div className="mb-3 flex min-h-11 shrink-0 min-w-0 items-center gap-3 rounded-[var(--radius-lg)] border border-border bg-surface px-3 shadow-xs">
                <div className="flex min-w-0 items-center gap-2 text-xs text-foreground-muted">
                  <FolderTree className="h-3.5 w-3.5 shrink-0 text-link" aria-hidden />
                  <span className="truncate font-medium text-foreground">{collectionPath}</span>
                  {authorName && (
                    <span className="hidden shrink-0 items-center gap-1.5 border-l border-border pl-3 xl:inline-flex">
                      <span
                        className="inline-flex h-5 w-5 items-center justify-center rounded-full bg-surface-selected text-[10px] font-semibold uppercase text-surface-selected-foreground"
                        aria-hidden
                      >
                        {authorName.trim()[0] || "?"}
                      </span>
                      <span className="font-medium text-foreground">{authorName}</span>
                    </span>
                  )}
                </div>
                <div className="ml-auto flex shrink-0 items-center gap-3 text-xs text-foreground-muted">
                  {commitShort && (
                    <span className="inline-flex items-center gap-1.5">
                      <GitCommitHorizontal className="h-3.5 w-3.5" aria-hidden />
                      <code className="font-mono text-foreground">{commitShort}</code>
                    </span>
                  )}
                  {doc.updated_at && (
                    <span className="inline-flex items-center gap-1.5">
                      <Clock3 className="h-3.5 w-3.5" aria-hidden />
                      {timeAgo(doc.updated_at)}
                    </span>
                  )}
                  <button
                    type="button"
                    onClick={() => {
                      setDetailsOpen(true);
                      setDetailsTab("history");
                    }}
                    className="inline-flex h-8 items-center gap-1.5 rounded-[var(--radius-md)] px-2 text-xs font-medium text-foreground-muted transition-token hover:bg-surface-hover hover:text-link focus:outline-none focus-visible:ring-2 focus-visible:ring-ring"
                  >
                    <History className="h-3.5 w-3.5" aria-hidden />
                    <span className="hidden sm:inline">History</span>
                  </button>
                  <button
                    ref={detailsToggleRef}
                    type="button"
                    aria-label={detailsOpen ? "Hide document panel" : "Open document panel"}
                    title={detailsOpen ? "Hide document panel" : "Open document panel"}
                    aria-controls="document-details-panel"
                    aria-expanded={detailsOpen}
                    onClick={() => {
                      if (detailsOpen) closeDetails();
                      else setDetailsOpen(true);
                    }}
                    className="inline-flex h-8 w-8 shrink-0 items-center justify-center rounded-[var(--radius-md)] border border-border bg-surface text-foreground-muted transition-token hover:border-border-strong hover:bg-surface-hover hover:text-link focus:outline-none focus-visible:ring-2 focus-visible:ring-ring"
                  >
                    {detailsOpen ? (
                      <PanelRightClose className="h-3.5 w-3.5" aria-hidden />
                    ) : (
                      <PanelRightOpen className="h-3.5 w-3.5" aria-hidden />
                    )}
                  </button>
                </div>
              </div>

              {inEditMode ? (
                <section className="overflow-hidden rounded-[var(--radius-lg)] border border-border bg-surface shadow-sm">
                  <div
                    className="flex min-h-11 items-center gap-2 border-b border-border bg-surface-2/60 px-3"
                  >
                    <span className="inline-flex items-center gap-1.5 text-xs font-semibold text-foreground">
                      <Pencil className="h-3.5 w-3.5" aria-hidden />
                      Editing document
                    </span>
                    <span role="status" aria-live="polite" className="ml-auto text-xs text-foreground-muted">
                      {uploadingImage
                        ? "Uploading image…"
                        : isDirty
                          ? "Unsaved changes"
                          : draftStatus === "saving"
                            ? "Saving draft locally…"
                            : draftStatus === "saved"
                              ? "Draft saved locally"
                              : "No changes"}
                    </span>
                  </div>
                  <div
                    className="p-4 sm:p-6"
                  >
                    <div className="mb-5 space-y-2 border-b border-border pb-5">
                      <Label htmlFor="document-edit-title">Document title</Label>
                      <Input
                        ref={editTitleRef}
                        id="document-edit-title"
                        value={editingTitle}
                        onChange={(event) => {
                          setEditingTitle(event.currentTarget.value);
                          draftRevisionRef.current += 1;
                          setServerTitleConflict(null);
                          setBodyError("");
                        }}
                        onBlur={() => setTitleTouched(true)}
                        disabled={savingBody}
                        aria-invalid={Boolean(titleError || titleConflict) || undefined}
                        aria-describedby={titleDescriptionIds}
                        className="font-display text-base font-semibold"
                      />
                      <p
                        id="document-edit-title-help"
                        className="text-xs leading-relaxed text-foreground-muted"
                      >
                        This is the visible title. Editing it keeps the document path,
                        links, and version history unchanged.
                      </p>
                      {titleError && (
                        <p
                          id="document-edit-title-error"
                          role="alert"
                          className="text-xs font-medium text-destructive"
                        >
                          {titleError}
                        </p>
                      )}
                      {titleConflict && (
                        <div
                          id="document-edit-title-conflict"
                          ref={titleConflictRef}
                          tabIndex={-1}
                          className="pt-1 focus:outline-none"
                        >
                          <DocumentTitleConflictNotice
                            conflict={titleConflict}
                            onOpenExisting={() =>
                              setPendingExistingPath(titleConflict.existingPath)
                            }
                            onChooseAlternative={() => editTitleRef.current?.focus()}
                            chooseAlternativeLabel="Choose another title"
                            onKeepBoth={() => void handleSaveDocument("allow")}
                            keepBothLabel="Save duplicate title"
                            keepingBoth={savingBody}
                          />
                        </div>
                      )}
                    </div>
                    <Suspense fallback={<MarkdownEditorFallback />}>
                      <MarkdownEditor
                        key={editorKey}
                        value={editorInitialContent}
                        onChange={(markdown, assetIds) => {
                          const nextAssetIds = assetIds || [];
                          draftRevisionRef.current += 1;
                          editingSnapshotRef.current = {
                            title: editingTitle,
                            content: markdown,
                            assetIds: nextAssetIds,
                          };
                          setEditingAssetIds(nextAssetIds);
                          setServerTitleConflict((current) =>
                            current ? { ...current, exactContent: false } : null,
                          );
                          if (hydratedKey.current !== editorKey) {
                            hydratedKey.current = editorKey;
                            if (editorHydrationModeRef.current === "server") {
                              setOriginalContent(markdown);
                            }
                            setEditingContent(markdown);
                            return;
                          }
                          setEditingContent(markdown);
                        }}
                        ariaLabel="Document body (markdown)"
                        autoFocus
                        readOnly={savingBody}
                        vault={name!}
                        document={doc?.path}
                        commit={editBaseCommit ?? undefined}
                        appearance="workspace"
                        onUploadingChange={(uploading) => {
                          setUploadingImage(uploading);
                          if (uploading) setClaimedAssetIds(null);
                        }}
                        preserveUploadsOnUnmount={
                          savingBody ||
                          draftStatus === "saving" ||
                          draftStatus === "saved" ||
                          draftStatus === "restored" ||
                          draftStatus === "expired"
                        }
                        claimedAssetIds={claimedAssetIds}
                        initialUnclaimedAssetIds={editingAssetIds}
                      />
                    </Suspense>
                    {draftNotice && (draftStatus === "restored" || draftStatus === "saved" || draftStatus === "error" || draftStatus === "unavailable") && (
                      <p className="mt-3 text-xs text-foreground-muted" role="status" aria-live="polite">
                        {draftNotice}
                      </p>
                    )}
                    {draftRecovery?.status === "expired" && (
                      <Alert variant="warning" title="This draft has expired" className="mt-4">
                        <p>{draftRecovery.draft.body ? "The preserved text can still be copied, but attached images are not guaranteed to be recoverable." : "The draft record is expired."}</p>
                        <div className="mt-2 flex flex-wrap gap-2">
                          <Button
                            type="button"
                            variant="outline"
                            size="sm"
                            className="shrink-0 focus-ring-instant"
                            onClick={() => void copyDraftText(draftRecovery.draft.body, "Expired draft Markdown copied")}
                          >
                            Copy expired Markdown
                          </Button>
                        </div>
                      </Alert>
                    )}
                    {draftRecovery?.status === "incompatible" && (
                      <Alert variant="warning" title="Draft editor profile is not supported here" className="mt-4">
                        <p>The original draft was kept without conversion or deletion. Copy its text before choosing a new draft.</p>
                        <div className="mt-2 flex flex-wrap gap-2">
                          <Button
                            type="button"
                            variant="outline"
                            size="sm"
                            className="shrink-0 focus-ring-instant"
                            onClick={() => void copyDraftText(draftRecovery.draft.copyBody, "Incompatible draft Markdown copied")}
                          >
                            Copy preserved Markdown
                          </Button>
                        </div>
                      </Alert>
                    )}
                    {saveConflict && (
                      <DocumentConflictNotice
                        base={saveConflict.base}
                        local={saveConflict.local}
                        latest={saveConflict.latest}
                        latestError={saveConflict.latestError}
                        rebasing={rebasing}
                        onRebase={() => void applyDraftToLatest()}
                        onRetryLatest={() => void readLatestForConflict()}
                      />
                    )}
                    {bodyError && <Alert variant="destructive" className="mt-4">{bodyError}</Alert>}
                  </div>
                </section>
              ) : isDiffMode && commitHash ? (
                <Suspense fallback={<DocumentDiffModuleLoading />}>
                  <DocumentDiffView
                    vault={name!}
                    docId={doc.path || docId}
                    revision={commitHash}
                    baseRevision={baseHistoryEntry?.hash}
                    targetEntry={selectedHistoryEntry}
                    onBackToVersion={() => openVersion(commitHash, { replace: true })}
                    onBackToLatest={() => openVersion(undefined, { replace: true })}
                    onOpenBase={baseHistoryEntry ? () => openVersion(baseHistoryEntry.hash, { replace: true }) : undefined}
                  />
                </Suspense>
              ) : (
                <DocumentView
                  vault={name!}
                  docId={docId}
                  version={commitHash}
                  view={view === "raw" ? "raw" : "rendered"}
                  onViewChange={(next) => setView(next)}
                  appearance="file"
                />
              )}
            </article>
          </main>

          {!inEditMode && (
            <>
              {detailsOpen && (
                <button
                  type="button"
                  aria-label="Dismiss document panel"
                  onClick={closeDetails}
                  className="absolute inset-0 z-[var(--z-raised)] bg-black/40 lg:hidden"
                />
              )}
              <aside
                id="document-details-panel"
                aria-label="Document panel"
                aria-hidden={!detailsOpen}
                inert={!detailsOpen}
                className={cn(
                  "absolute inset-y-0 right-0 z-[var(--z-overlay)] flex w-full max-w-lg flex-col overflow-hidden border-l border-border bg-surface shadow-xl transition-transform duration-[var(--duration-base)] ease-[var(--ease-out)] lg:w-96",
                  detailsOpen
                    ? "translate-x-0"
                    : "pointer-events-none translate-x-full",
                )}
              >
              <div className="flex h-14 shrink-0 items-center justify-between border-b border-border px-4">
                <div>
                  <h2 className="text-sm font-semibold text-foreground">Document panel</h2>
                  <p className="text-xs text-foreground-muted">Info, structure, links and versions</p>
                </div>
                <Button
                  ref={detailsCloseRef}
                  type="button"
                  variant="ghost"
                  size="icon"
                  aria-label="Close document panel"
                  onClick={closeDetails}
                >
                  <PanelRightClose className="h-4 w-4" aria-hidden />
                </Button>
              </div>

              <Tabs
                value={detailsTab}
                onValueChange={(value) => setDetailsTab(value as typeof detailsTab)}
                className="flex min-h-0 flex-1 flex-col"
              >
                <TabsList
                  aria-label="Document detail views"
                  className="mx-4 mt-3 w-[calc(100%-2rem)] shrink-0"
                >
                  <TabsTrigger value="info" className="min-w-0 flex-1 gap-1 px-2 text-xs">
                    <Info className="h-3.5 w-3.5" aria-hidden />
                    Info
                  </TabsTrigger>
                  <TabsTrigger value="outline" className="min-w-0 flex-1 gap-1 px-2 text-xs">
                    <ListTree className="h-3.5 w-3.5" aria-hidden />
                    Outline
                    <span className="coord tabular-nums">{headingSlugs.length}</span>
                  </TabsTrigger>
                  <TabsTrigger value="relations" className="min-w-0 flex-1 gap-1 px-2 text-xs">
                    <Link2 className="h-3.5 w-3.5" aria-hidden />
                    Relations
                    {visibleRelationCount > 0 && <span className="coord tabular-nums">{visibleRelationCount}</span>}
                  </TabsTrigger>
                  <TabsTrigger value="history" className="min-w-0 flex-1 gap-1 px-2 text-xs">
                    <History className="h-3.5 w-3.5" aria-hidden />
                    History
                    {provenance.length > 0 && <span className="coord tabular-nums">{provenance.length}</span>}
                  </TabsTrigger>
                </TabsList>

                <TabsContent value="info" className="min-h-0 flex-1 overflow-y-auto px-4 pb-4 pt-3 rail-scroll">
              <section aria-labelledby="document-properties-heading">
                <div className="mb-3 flex items-center justify-between gap-3">
                  <h3 id="document-properties-heading" className="flex items-center gap-2 text-sm font-semibold text-foreground">
                    <Info className="h-4 w-4 text-link" aria-hidden />
                    Properties
                  </h3>
                  {canEdit && (
                    <Button type="button" variant="ghost" size="sm" onClick={() => setEditOpen(true)}>
                      <Pencil className="h-3.5 w-3.5" aria-hidden />
                      Edit
                    </Button>
                  )}
                </div>
                <dl className="space-y-2.5 text-xs">
                  {doc.summary && (
                    <div className="mb-3 rounded-[var(--radius-md)] bg-surface-2 px-3 py-2.5">
                      <dt className="mb-1 text-[11px] font-medium text-foreground-muted">Summary</dt>
                      <dd>
                        <SummaryFold summary={doc.summary} className="" />
                      </dd>
                    </div>
                  )}
                  <PropertyRow label="Title">
                    <span className="block truncate text-foreground" title={doc.title}>{doc.title}</span>
                  </PropertyRow>
                  {authorName && (
                    <PropertyRow label="Author">
                      <span className="text-foreground">{authorName}</span>
                    </PropertyRow>
                  )}
                  <PropertyRow label="Collection">
                    <TooltipText as="span" tip={collectionPath} className="block truncate text-foreground">
                      {collectionPath}
                    </TooltipText>
                  </PropertyRow>
                  <PropertyRow label="Type">
                    <span className="text-foreground">{doc.type || "document"}</span>
                  </PropertyRow>
                  <PropertyRow label="Status">
                    <Badge variant={doc.status === "archived" ? "archived" : doc.status === "draft" ? "draft" : "active"}>
                      {doc.status || "active"}
                    </Badge>
                  </PropertyRow>
                  {doc.domain && (
                    <PropertyRow label="Domain">
                      <span className="text-foreground">{doc.domain}</span>
                    </PropertyRow>
                  )}
                  {doc.tags?.length > 0 && (
                    <PropertyRow label="Tags">
                      <span className="flex min-w-0 flex-wrap justify-end gap-1">
                        {doc.tags.map((tag: string) => (
                          <Badge key={tag} variant="outline">{tag}</Badge>
                        ))}
                      </span>
                    </PropertyRow>
                  )}
                  <PropertyRow label="Commit">
                    <code className="font-mono text-foreground">{commitShort || "—"}</code>
                  </PropertyRow>
                </dl>

                <details className="group mt-4 border-t border-border pt-3">
                  <summary className="flex min-h-8 cursor-pointer list-none items-center gap-2 rounded-[var(--radius-md)] px-1 text-xs font-medium text-foreground transition-token hover:bg-surface-hover hover:text-link focus:outline-none focus-visible:ring-2 focus-visible:ring-ring">
                    <ChevronRight
                      className="h-3.5 w-3.5 shrink-0 text-foreground-muted transition-transform group-open:rotate-90"
                      aria-hidden
                    />
                    Technical details
                  </summary>
                  <dl className="mt-2 space-y-2.5 rounded-[var(--radius-md)] bg-surface-2 px-3 py-2.5 text-xs">
                    <PropertyRow label="File name">
                      <TooltipText as="span" tip={fileName} className="block truncate font-mono text-foreground">
                        {fileName}
                      </TooltipText>
                    </PropertyRow>
                    <PropertyRow label="Path">
                      <TooltipText as="span" tip={doc.path} className="block truncate font-mono text-foreground">
                        {doc.path}
                      </TooltipText>
                    </PropertyRow>
                    <PropertyRow label="URI">
                      <TooltipText
                        as="span"
                        tip={docUri(name!, doc.path)}
                        className="block truncate font-mono text-foreground"
                      >
                        {docUri(name!, doc.path)}
                      </TooltipText>
                    </PropertyRow>
                  </dl>
                </details>

                {!isHistorical && (
                  <div className="mt-4 grid grid-cols-2 gap-2">
                    {doc.is_public && doc.public_slug ? (
                      <>
                        <Button type="button" variant="outline" size="sm" onClick={copyPublicLink}>
                          {copied ? <CheckCircle2 className="h-3.5 w-3.5 text-success" aria-hidden /> : <ExternalLink className="h-3.5 w-3.5" aria-hidden />}
                          {copied ? "Copied" : "Copy link"}
                        </Button>
                        <Button type="button" variant="outline" size="sm" onClick={handleUnpublish} disabled={publishing}>
                          {publishing ? <Loader2 className="h-3.5 w-3.5 animate-spin" aria-hidden /> : <Lock className="h-3.5 w-3.5" aria-hidden />}
                          Unpublish
                        </Button>
                      </>
                    ) : (
                      <Button type="button" variant="outline" size="sm" className="col-span-2" onClick={() => setPublishOpen(true)} disabled={publishing}>
                        <Share2 className="h-3.5 w-3.5" aria-hidden />
                        Publish document
                      </Button>
                    )}
                  </div>
                )}
              </section>

                </TabsContent>

                <TabsContent value="outline" className="min-h-0 flex-1 overflow-y-auto px-4 pb-4 pt-3 rail-scroll">
                  <div className="mb-3 border-b border-border pb-3">
                    <h3 className="text-sm font-semibold text-foreground">On this page</h3>
                    <p className="mt-0.5 text-xs text-foreground-muted">Jump to a heading without leaving the document.</p>
                  </div>
                  <DocumentOutline markdown={doc.content || ""} articleEl={articleEl} />
                </TabsContent>
                <TabsContent value="relations" className="min-h-0 flex-1 overflow-y-auto px-4 pb-4 pt-3 rail-scroll">
                  <RelationsPanel
                    vault={name!}
                    sourceUri={doc.path ? docUri(name!, doc.path) : ""}
                    relations={relations}
                    relationsError={relationsError}
                    canWrite={canEdit}
                    graphHref={`/vault/${name}/graph${doc.path ? `?entry=${encodeURIComponent(doc.path)}` : ""}`}
                    onReload={reloadRelations}
                  />
                </TabsContent>
                <TabsContent value="history" className="min-h-0 flex-1 overflow-y-auto px-4 pb-4 pt-3 rail-scroll">
                  <div className="mb-3 border-b border-border pb-3">
                    <h3 className="text-sm font-semibold text-foreground">Version history</h3>
                    <p className="mt-0.5 text-xs text-foreground-muted">Open a version or inspect what changed from its parent.</p>
                  </div>
                  {historyQuery.isPending ? (
                    <LoadingState label="Loading version history" className="space-y-2">
                      <Skeleton className="h-16 w-full rounded-[var(--radius-md)]" />
                      <Skeleton className="h-16 w-full rounded-[var(--radius-md)]" />
                      <Skeleton className="h-16 w-full rounded-[var(--radius-md)]" />
                    </LoadingState>
                  ) : historyQuery.isError ? (
                    <Alert variant="destructive" title="Couldn't load version history">
                      <div className="mt-2">
                        <Button type="button" variant="outline" size="sm" onClick={() => void historyQuery.refetch()}>
                          Try again
                        </Button>
                      </div>
                    </Alert>
                  ) : (
                    <div className="space-y-3">
                      {historyQuery.data?.source === "activity" && (
                        <Alert variant="info" title="Limited history from an older server">
                          Version browsing remains available. Exact document lineage and change comparison require a newer backend.
                        </Alert>
                      )}
                      <HistoryList
                        entries={provenance}
                        selectedHash={commitHash}
                        diffHash={isDiffMode ? commitHash : undefined}
                        onSelect={(hash) => openVersion(hash)}
                        onCompare={openDiff}
                      />
                    </div>
                  )}
                </TabsContent>
              </Tabs>
              </aside>
            </>
          )}
        </div>
      </section>

      <FrontmatterEditDialog
        open={editOpen}
        onOpenChange={setEditOpen}
        vault={name!}
        docId={docId}
        doc={doc}
        onSaved={(next) => {
          setDocOverride({ ...doc, ...next });
          void queryClient.invalidateQueries({ queryKey: ["document", name] });
          if (next.status !== doc.status) window.dispatchEvent(new CustomEvent("akb:document-status-changed", { detail: { vault: name, path: next.path, status: next.status } }));
          refetchTree();
        }}
      />

      <DocumentMoveDialog
        open={moveOpen}
        onOpenChange={setMoveOpen}
        vault={name!}
        path={doc.path}
        title={doc.title || fileName}
        onOpenDocument={openExistingDocument}
        onMoved={(result) => {
          const changedAt = new Date().toISOString();
          const nextDoc = {
            ...doc,
            uri: result.uri,
            path: result.path,
            current_commit: result.current_commit ?? result.commit_hash,
            updated_at: changedAt,
          };
          queryClient.setQueryData(
            ["document", name, result.path, undefined],
            nextDoc,
          );
          setDocOverride(nextDoc);
          // Sidebar refresh is best-effort after the server has committed the
          // move. A stale tree must not turn a successful move into a retryable
          // dialog error (which could cause a second, conflicting request).
          try {
            refetchTree();
          } catch {
            // intentionally swallowed
          }
          const nextCollection = result.path.includes("/")
            ? result.path.slice(0, result.path.lastIndexOf("/"))
            : "Vault root";
          setMoveNotice({ collection: nextCollection });

          const nextSearch = new URLSearchParams(searchParams);
          nextSearch.delete("commit");
          const encodedPath = encodeURIComponent(result.path);
          const search = nextSearch.toString();
          navigate(
            {
              pathname: `/vault/${name}/doc/${encodedPath}`,
              search: search ? `?${search}` : "",
            },
            { replace: true, state: routeLocation.state },
          );
        }}
      />

      <PublishOptionsDialog
        open={publishOpen}
        onOpenChange={setPublishOpen}
        vault={name!}
        docId={docId}
        onPublished={(slug) => setDocOverride({ ...doc, is_public: true, public_slug: slug })}
      />

      <ResourceDeleteDialog
        open={deleteOpen}
        onOpenChange={setDeleteOpen}
        kind="document"
        name={doc.title || doc.path}
        onConfirm={async () => {
          await deleteDocument(name!, docId);
          refetchTree();
          navigate(`/vault/${name}`);
        }}
      />

      <ConfirmDialog
        open={archiveOpen}
        onOpenChange={setArchiveOpen}
        title={pendingArchiveCheck ? "Check document state" : doc.status === "archived" ? "Restore this document?" : "Archive this document?"}
        description={pendingArchiveCheck ? "The change was accepted. Only the current document will be fetched; the change will not be sent again." : doc.status === "archived"
          ? `Restore as an active document in ${collectionPath || "Vault root"}. Its identity, links, and history stay unchanged. Search may take a moment to catch up.`
          : "Hide this document from current documents and default search. You can find it with the Archived documents filter and restore it later. This does not delete it, revoke access, or disable public links."}
        confirmLabel={pendingArchiveCheck ? "Check current state" : doc.status === "archived" ? "Restore document" : "Archive document"}
        onConfirm={async () => {
          if (!pendingArchiveCheck && archiveDisabledReason) throw new Error(archiveDisabledReason);
          const status = pendingArchiveCheck?.status ?? (doc.status === "archived" ? "active" : "archived");
          let next;
          try {
            next = pendingArchiveCheck
              ? await checkDocumentArchiveState(name!, docId)
              : await changeDocumentArchiveState(name!, docId, status, doc.current_commit);
          } catch (error) {
            if (error instanceof ArchiveVerificationError) setArchivePending({ vault: name!, ref: docId, status });
            throw error;
          }
          setArchivePending(null);
          setDocOverride(next);
          await queryClient.invalidateQueries({ queryKey: ["document", name] });
          void queryClient.invalidateQueries({ queryKey: ["document-history", name] });
          refetchTree();
          setArchiveNotice(next.status !== status
            ? "Current state loaded. The requested state is not present; review the document before making another change."
            : status === "active" ? `Restored to ${collectionPath || "Vault root"}.` : "Document archived. Find it using the Archived documents filter.");
          if (commitHash) openVersion(undefined, { replace: true });
        }}
      />

      <ConfirmDialog
        open={pendingExistingPath !== null}
        onOpenChange={(open) => !open && setPendingExistingPath(null)}
        title="Discard changes and open the existing document?"
        description="Your unsaved title or body changes will be lost."
        confirmLabel="Discard and open"
        cancelLabel="Keep editing"
        variant="destructive"
        returnFocusRef={editTitleRef}
        onConfirm={async () => {
          const existingPath = pendingExistingPath;
          await discardEditDraft();
          setEditingContent(originalContent);
          setEditingTitle(originalTitle);
          setEditingAssetIds([]);
          setTitleTouched(false);
          setServerTitleConflict(null);
          if (existingPath) openExistingDocument(existingPath);
        }}
      />

      <ConfirmDialog
        open={pendingView !== null}
        onOpenChange={(o) => !o && setPendingView(null)}
        title="Discard unsaved changes?"
        description={
          uploadingImage
            ? "The image upload will be cancelled and your unsaved document changes will be lost."
            : "Your unsaved title or body changes will be lost."
        }
        confirmLabel="Discard changes"
        variant="destructive"
        returnFocusRef={cancelEditButtonRef}
        onConfirm={async () => {
          const next = pendingView;
          await discardEditDraft();
          setEditingContent(originalContent);
          setEditingTitle(originalTitle);
          setEditingAssetIds([]);
          setEditorKey((k) => k + 1);
          setBodyError("");
          setTitleTouched(false);
          setServerTitleConflict(null);
          setPendingView(null);
          if (next) {
            restoreEditFocusRef.current = true;
            applyView(next);
          }
        }}
      />

      {moveNotice && (
        <div
          role="status"
          aria-live="polite"
          className="fixed bottom-4 right-4 z-[var(--z-toast)] flex max-w-sm items-start gap-3 rounded-[var(--radius-lg)] border border-success/30 bg-surface px-4 py-3 text-sm text-foreground shadow-lg"
        >
          <CheckCircle2 className="mt-0.5 h-4 w-4 shrink-0 text-success" aria-hidden />
          <div className="min-w-0">
            <p className="font-semibold">Moved to {moveNotice.collection}</p>
            <p className="mt-0.5 text-xs text-foreground-muted">
              The document title, links, and version history were preserved.
            </p>
          </div>
        </div>
      )}

    </>
  );
}

interface DocumentMovePermissionState {
  path: string;
  vaultRole: string | null;
  vaultKind: "normal" | "mirror" | "error" | null;
  vaultReadOnly: boolean;
  isHistorical: boolean;
}

function documentWriteDisabledReason({
  vaultRole,
  vaultKind,
  vaultReadOnly,
  isHistorical,
}: Omit<DocumentMovePermissionState, "path">) {
  if (isHistorical) {
    return "Return to the latest version before changing this document.";
  }
  if (vaultKind === "error") {
    return "Permissions could not be verified. Refresh the page and try again.";
  }
  if (vaultKind === null || vaultRole === null) {
    return "Permissions are still loading.";
  }
  if (vaultReadOnly || vaultKind === "mirror") {
    return "This Vault is read-only.";
  }
  if (!(["writer", "admin", "owner"] as const).includes(vaultRole as "writer" | "admin" | "owner")) {
    return "Writer access or higher is required.";
  }
  return null;
}

function documentMoveDisabledReason({
  path,
  vaultRole,
  vaultKind,
  vaultReadOnly,
  isHistorical,
}: DocumentMovePermissionState) {
  if (path === VAULT_SKILL_PATH) {
    return "The Vault guide has a reserved location and cannot be moved.";
  }
  const reason = documentWriteDisabledReason({
    vaultRole,
    vaultKind,
    vaultReadOnly,
    isHistorical,
  });
  return reason?.replace("renaming", "moving") ?? null;
}

function DocumentDiffModuleLoading() {
  return (
    <LoadingState
      label="Opening document changes"
      className="min-h-0 flex-1 overflow-hidden rounded-[var(--radius-lg)] border border-border bg-surface shadow-sm"
    >
      <div className="flex h-full min-h-[28rem] flex-col">
        <div className="flex min-h-16 items-center gap-3 border-b border-border px-4">
          <Skeleton className="h-8 w-8 rounded-[var(--radius-md)]" />
          <div className="flex-1 space-y-2">
            <Skeleton className="h-4 w-40 rounded-[var(--radius-sm)]" />
            <Skeleton className="h-3 w-64 rounded-[var(--radius-sm)]" />
          </div>
        </div>
        <div className="flex min-h-11 items-center gap-2 border-b border-border bg-surface-2 px-3">
          <Skeleton className="h-3 w-24 rounded-[var(--radius-sm)]" />
          <Skeleton className="h-3 w-24 rounded-[var(--radius-sm)]" />
        </div>
        <div className="flex-1 space-y-2 p-4">
          {Array.from({ length: 10 }, (_, index) => (
            <Skeleton key={index} className="h-5 w-full rounded-[var(--radius-sm)]" />
          ))}
        </div>
      </div>
    </LoadingState>
  );
}

function DocumentPageLoading({ presentation }: { presentation: "page" | "preview" }) {
  return (
    <LoadingState
      label="Loading document"
      className="flex h-full min-h-0 flex-col overflow-hidden bg-background"
    >
      <div className="flex h-full min-h-0 flex-col overflow-hidden">
        <header
          className={cn(
            "flex h-16 shrink-0 items-center gap-3 border-b border-border bg-surface pl-3 sm:pl-4 lg:pl-5",
            presentation === "preview" ? "pr-12 sm:pr-14" : "pr-3 sm:pr-4 lg:pr-5",
          )}
        >
          <Skeleton className="hidden h-9 w-9 shrink-0 rounded-[var(--radius-md)] sm:block" />
          <div className="min-w-0 flex-1 space-y-2">
            <Skeleton className="h-5 w-2/3 max-w-56 rounded-[var(--radius-sm)]" />
            <Skeleton className="h-3 w-1/2 max-w-40 rounded-[var(--radius-sm)]" />
          </div>
          <div className="flex shrink-0 gap-2">
            <Skeleton className="h-8 w-20 rounded-[var(--radius-md)]" />
            <Skeleton className="hidden h-8 w-24 rounded-[var(--radius-md)] sm:block" />
          </div>
        </header>

        <div className="min-h-0 flex-1 overflow-hidden">
          <main className="h-full overflow-hidden bg-background p-2 sm:p-3">
            <div className="mb-3 flex min-h-11 items-center gap-3 rounded-[var(--radius-lg)] border border-border bg-surface px-3 shadow-xs">
              <Skeleton className="h-4 w-4 shrink-0 rounded-[var(--radius-sm)]" />
              <Skeleton className="h-3 w-2/3 max-w-64 rounded-[var(--radius-sm)]" />
              <Skeleton className="ml-auto hidden h-3 w-32 rounded-[var(--radius-sm)] sm:block" />
            </div>

            <section className="min-h-[32rem] overflow-hidden rounded-[var(--radius-lg)] border border-border bg-surface shadow-sm">
              <div className="flex min-h-11 items-center gap-2 border-b border-border bg-surface-2/60 px-3">
                <Skeleton className="h-7 w-24 rounded-[var(--radius-sm)]" />
                <Skeleton className="h-7 w-16 rounded-[var(--radius-sm)]" />
                <Skeleton className="ml-auto h-3 w-28 rounded-[var(--radius-sm)]" />
              </div>
              <div className="mx-auto max-w-4xl space-y-4 px-5 py-8 sm:px-8 lg:px-12">
                <Skeleton className="h-8 w-3/5 rounded-[var(--radius-md)]" />
                <Skeleton className="h-4 w-full rounded-[var(--radius-sm)]" />
                <Skeleton className="h-4 w-11/12 rounded-[var(--radius-sm)]" />
                <Skeleton className="h-4 w-4/5 rounded-[var(--radius-sm)]" />
                <Skeleton className="mt-7 h-6 w-2/5 rounded-[var(--radius-md)]" />
                <Skeleton className="h-4 w-full rounded-[var(--radius-sm)]" />
                <Skeleton className="h-4 w-5/6 rounded-[var(--radius-sm)]" />
                <Skeleton className="mt-6 h-32 w-full rounded-[var(--radius-lg)]" />
              </div>
            </section>
          </main>
        </div>
      </div>
    </LoadingState>
  );
}

function PropertyRow({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div className="grid grid-cols-[5rem_minmax(0,1fr)] items-start gap-3">
      <dt className="text-foreground-muted">{label}</dt>
      <dd className="min-w-0 text-right">{children}</dd>
    </div>
  );
}
