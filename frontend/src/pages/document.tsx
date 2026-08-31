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
  authenticatedFetch,
  ApiError,
  deleteDocument,
  getDocument,
  getRelations,
  getVaultInfo,
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
import { DocumentRenameDialog } from "@/components/document-rename-dialog";

// Plate is heavy (~hundreds of KB gzipped); lazy-load so the read-only path
// (Rendered / Raw) stays cheap.
const MarkdownEditor = lazy(() => import("@/components/markdown-editor"));

type DocView = "rendered" | "raw" | "edit";

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
    rawView === "raw" ? "raw" : rawView === "edit" ? "edit" : "rendered";
  const [relations, setRelations] = useState<RelationRow[]>([]);
  const [relationsError, setRelationsError] = useState(false);
  const [provenance, setProvenance] = useState<any[]>([]);
  const [historyError, setHistoryError] = useState(false);
  const [pendingView, setPendingView] = useState<DocView | null>(null);
  const [docOverride, setDocOverride] = useState<any>(null);
  const [publishing, setPublishing] = useState(false);
  const [publishError, setPublishError] = useState("");
  const [copied, setCopied] = useState(false);
  const [articleEl, setArticleEl] = useState<HTMLElement | null>(null);
  const [vaultRole, setVaultRole] = useState<string | null>(null);
  const [vaultKind, setVaultKind] = useState<"normal" | "mirror" | "error" | null>(null);
  const [vaultReadOnly, setVaultReadOnly] = useState(true);
  const [editOpen, setEditOpen] = useState(false);
  const [renameOpen, setRenameOpen] = useState(false);
  const [moveOpen, setMoveOpen] = useState(false);
  const [deleteOpen, setDeleteOpen] = useState(false);
  const [publishOpen, setPublishOpen] = useState(false);
  const [detailsOpen, setDetailsOpen] = useState(false);
  const [detailsTab, setDetailsTab] = useState<"info" | "outline" | "relations" | "history">("info");
  const detailsToggleRef = useRef<HTMLButtonElement | null>(null);
  const detailsCloseRef = useRef<HTMLButtonElement | null>(null);
  const editButtonRef = useRef<HTMLButtonElement | null>(null);
  const cancelEditButtonRef = useRef<HTMLButtonElement | null>(null);
  const restoreEditFocusRef = useRef(false);
  // Plate manages its own state; we remount via `editorKey` when hydrating
  // a fresh server value rather than treating `value` as controlled.
  const [editingContent, setEditingContent] = useState("");
  const [editingAssetIds, setEditingAssetIds] = useState<readonly string[]>([]);
  const [originalContent, setOriginalContent] = useState("");
  const [editorKey, setEditorKey] = useState(0);
  const [savingBody, setSavingBody] = useState(false);
  const [uploadingImage, setUploadingImage] = useState(false);
  const [claimedAssetIds, setClaimedAssetIds] = useState<readonly string[] | null>(null);
  const [bodyError, setBodyError] = useState("");
  const [savedAt, setSavedAt] = useState<number | null>(null);
  const [moveNotice, setMoveNotice] = useState<{ collection: string } | null>(null);
  const [renameNotice, setRenameNotice] = useState(false);
  // Plate's markdown roundtrip is not byte-identity: adopt the first
  // post-hydration emission as the new `originalContent` baseline so the
  // editor doesn't flash "UNSAVED" the moment it mounts.
  const hydratedKey = useRef<number | null>(null);
  const isDirty = editingContent !== originalContent;
  const hasUnsavedWork = isDirty || uploadingImage;

  useEffect(() => {
    if (!moveNotice) return;
    const timer = window.setTimeout(() => setMoveNotice(null), 4_500);
    return () => window.clearTimeout(timer);
  }, [moveNotice]);

  useEffect(() => {
    if (!renameNotice) return;
    const timer = window.setTimeout(() => setRenameNotice(false), 4_500);
    return () => window.clearTimeout(timer);
  }, [renameNotice]);

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
    queryKey: ["document", name, docId, commitHash],
    queryFn: () => getDocument(name!, docId, commitHash),
    enabled: !!name && !!docId,
    retry: false,
  });

  const doc = docOverride ?? docQuery.data ?? null;
  const currentUserId = currentUser?.user_id;

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
    enabled: !!name && !!docId && !!commitHash,
    retry: false,
  });
  const headCommit = commitHash ? headQuery.data?.current_commit : doc?.current_commit;
  // Genuinely historical only when the pin points at a commit OTHER than HEAD.
  // sameCommitRef does a prefix-tolerant compare (the commit log links 12-char
  // short hashes; current_commit is the full SHA) and returns false while HEAD
  // is still loading — so a real older version is never briefly editable.
  const isHistorical = !!commitHash && !sameCommitRef(commitHash, headCommit);
  const canEdit =
    !isHistorical &&
    (vaultRole === "writer" || vaultRole === "admin" || vaultRole === "owner");
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
    setDocOverride(null);
    setProvenance([]);
    setRelations([]);
    setRelationsError(false);
    setHistoryError(false);
    setBodyError("");
    setClaimedAssetIds(null);
    if (!d) return;
    const body = d.content || "";
    setOriginalContent(body);
    setEditingContent(body);
    // Bump the key so the Plate editor remounts with the new value —
    // it's uncontrolled internally and won't pick up `value` prop
    // changes after mount.
    setEditorKey((k) => k + 1);
    if (d.path && d.path !== docId) {
      navigate(`/vault/${name}/doc/${encodeURIComponent(d.path)}`, {
        replace: true,
        state: routeLocation.state,
      });
    }
    if (d.path) {
      // getRelations builds the canonical akb:// URI from the vault-relative
      // *path* (docUri). The GET response exposes no internal `id` — `uri`/
      // `path` is the sole identifier — so keying this off `d.id` (always
      // undefined) meant relations never loaded on the document page.
      getRelations(name!, d.path)
        .then((r) => setRelations(r.relations || []))
        .catch(() => setRelationsError(true));
    }
    if (d.path) {
      loadHistory(name!, d.path);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [docQuery.data]);

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

  async function handleSaveBody() {
    if (!name || !docId || uploadingImage) return;
    const contentToSave = editingContent;
    const assetIdsToClaim = editingAssetIds;
    setSavingBody(true);
    setBodyError("");
    try {
      const saved = await updateDocument(name, docId, { content: contentToSave });
      const now = new Date().toISOString();
      // Optimistically advance content + updated_at so the byline reads
      // "last changed just now" without waiting for a refetch. DocumentView
      // consumes the same query key independently, so update that cache too;
      // a local page override alone leaves its Rendered tab stale.
      const nextDoc = {
        ...(doc || {}),
        content: contentToSave,
        updated_at: now,
        current_commit: saved.current_commit ?? saved.commit_hash ?? doc?.current_commit,
      };
      setDocOverride(nextDoc);
      queryClient.setQueryData(
        ["document", name, docId, undefined],
        (cached: Record<string, unknown> | undefined) => ({
          ...(cached || {}),
          ...nextDoc,
        }),
      );
      setOriginalContent(contentToSave);
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

  // History = `git log -- <doc.path>` scoped to this document.
  async function loadHistory(vault: string, docPath: string) {
    try {
      const r = await authenticatedFetch(
        `/api/v1/activity/${encodeURIComponent(vault)}?collection=${encodeURIComponent(docPath)}&limit=20`,
      );
      if (!r.ok) {
        setProvenance([]);
        setHistoryError(true);
        return;
      }
      const d = await r.json();
      setProvenance(d.activity || []);
    } catch {
      setProvenance([]);
      setHistoryError(true);
    }
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

  if (docQuery.isError) {
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

  const commitShort = headCommit?.slice(0, 7);
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
    isHistorical,
  });
  const renameDisabledReason = documentTitleRenameDisabledReason({
    vaultRole,
    vaultKind,
    vaultReadOnly,
    isHistorical,
  });
  const canDelete =
    canWrite &&
    !vaultReadOnly &&
    !isHistorical &&
    doc.path !== VAULT_SKILL_PATH;

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
                {isHistorical ? (
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
                  onClick={handleSaveBody}
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
                  renameLabel="Rename title"
                  onRename={renameDisabledReason ? undefined : () => setRenameOpen(true)}
                  renameDisabledReason={renameDisabledReason || undefined}
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

        {isHistorical && (
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
              onClick={() => {
                const p = new URLSearchParams(searchParams);
                p.delete("commit");
                updateRouteParams(p, { replace: false });
              }}
            >
              Back to latest
            </Button>
          </div>
        )}

        {publishError && (
          <div className="shrink-0 border-b border-border bg-surface px-4 py-3 sm:px-6">
            <Alert variant="destructive">{publishError}</Alert>
          </div>
        )}

        <div className="relative min-h-0 flex-1 overflow-hidden">
          <main
            id="document-reading-canvas"
            className="h-full overflow-y-auto bg-background"
          >
            <article
              ref={setArticleEl}
              aria-labelledby="doc-title"
              className={cn(
                "w-full",
                inEditMode
                  ? "px-3 py-4 sm:px-4 sm:py-5 lg:px-5 xl:px-6 2xl:px-8"
                  : "p-2 sm:p-3",
              )}
            >
              <div className="mb-3 flex min-h-11 min-w-0 items-center gap-3 rounded-[var(--radius-lg)] border border-border bg-surface px-3 shadow-xs">
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
                      {uploadingImage ? "Uploading image…" : isDirty ? "Unsaved changes" : "No changes"}
                    </span>
                  </div>
                  <div
                    className="p-4 sm:p-6"
                  >
                    <Suspense fallback={<MarkdownEditorFallback />}>
                      <MarkdownEditor
                        key={editorKey}
                        value={originalContent}
                        onChange={(markdown, assetIds) => {
                          setEditingAssetIds(assetIds);
                          if (hydratedKey.current !== editorKey) {
                            hydratedKey.current = editorKey;
                            setOriginalContent(markdown);
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
                        commit={doc?.current_commit ?? undefined}
                        appearance="workspace"
                        onUploadingChange={(uploading) => {
                          setUploadingImage(uploading);
                          if (uploading) setClaimedAssetIds(null);
                        }}
                        preserveUploadsOnUnmount={savingBody}
                        claimedAssetIds={claimedAssetIds}
                      />
                    </Suspense>
                    {bodyError && <Alert variant="destructive" className="mt-4">{bodyError}</Alert>}
                  </div>
                </section>
              ) : (
                <DocumentView
                  vault={name!}
                  docId={docId}
                  version={commitHash}
                  view={view}
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
                  {canWrite && !isHistorical && (
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
                    <p className="mt-0.5 text-xs text-foreground-muted">Select a commit to inspect that version.</p>
                  </div>
                  {historyError ? (
                    <Alert variant="destructive">Failed to load history.</Alert>
                  ) : (
                    <HistoryList
                      entries={provenance as any}
                      selectedHash={commitHash}
                      onSelect={(hash) => {
                        const p = new URLSearchParams(searchParams);
                        if (commitHash === hash) p.delete("commit");
                        else p.set("commit", hash);
                        updateRouteParams(p, { replace: false });
                      }}
                    />
                  )}
                </TabsContent>
              </Tabs>
              </aside>
            </>
          )}
        </div>
      </section>

      <DocumentRenameDialog
        open={renameOpen}
        onOpenChange={setRenameOpen}
        vault={name!}
        docId={docId}
        path={doc.path}
        title={doc.title || fileName}
        onOpenDocument={(existingPath) => {
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
        }}
        onRenamed={(nextTitle) => {
          setDocOverride({ ...doc, title: nextTitle });
          setRenameNotice(true);
          refetchTree();
        }}
      />

      <FrontmatterEditDialog
        open={editOpen}
        onOpenChange={setEditOpen}
        vault={name!}
        docId={docId}
        doc={doc}
        onSaved={(next) => {
          setDocOverride({ ...doc, ...next });
          refetchTree();
        }}
      />

      <DocumentMoveDialog
        open={moveOpen}
        onOpenChange={setMoveOpen}
        vault={name!}
        path={doc.path}
        title={doc.title || fileName}
        onOpenDocument={(existingPath) => {
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
        }}
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
        open={pendingView !== null}
        onOpenChange={(o) => !o && setPendingView(null)}
        title="Discard unsaved changes?"
        description={
          uploadingImage
            ? "The image upload will be cancelled and your edits to the document body will be lost."
            : "Your edits to the document body will be lost."
        }
        confirmLabel="Discard changes"
        variant="destructive"
        returnFocusRef={cancelEditButtonRef}
        onConfirm={() => {
          const next = pendingView;
          setEditingContent(originalContent);
          setEditingAssetIds([]);
          setEditorKey((k) => k + 1);
          setBodyError("");
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

      {renameNotice && (
        <div
          role="status"
          aria-live="polite"
          className="fixed bottom-4 right-4 z-[var(--z-toast)] flex max-w-sm items-start gap-3 rounded-[var(--radius-lg)] border border-success/30 bg-surface px-4 py-3 text-sm text-foreground shadow-lg"
        >
          <CheckCircle2 className="mt-0.5 h-4 w-4 shrink-0 text-success" aria-hidden />
          <div className="min-w-0">
            <p className="font-semibold">Document renamed</p>
            <p className="mt-0.5 text-xs text-foreground-muted">
              Its stable file identity, links, and version history were preserved.
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

function documentTitleRenameDisabledReason({
  vaultRole,
  vaultKind,
  vaultReadOnly,
  isHistorical,
}: Omit<DocumentMovePermissionState, "path">) {
  if (isHistorical) {
    return "Return to the latest version before renaming this document.";
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
  const reason = documentTitleRenameDisabledReason({
    vaultRole,
    vaultKind,
    vaultReadOnly,
    isHistorical,
  });
  return reason?.replace("renaming", "moving") ?? null;
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
