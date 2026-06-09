import { Suspense, lazy, useEffect, useMemo, useRef, useState } from "react";
import { flushSync } from "react-dom";
import { Link, useNavigate, useParams, useSearchParams } from "react-router-dom";
import { useQuery } from "@tanstack/react-query";
import {
  ArrowLeft,
  CheckCircle2,
  ExternalLink,
  GitGraph,
  Loader2,
  Lock,
  Pencil,
  Trash2,
  Unlock,
} from "lucide-react";
import {
  ApiError,
  deleteDocument,
  getDocument,
  getRelations,
  getVaultInfo,
  unpublishDoc,
  updateDocument,
} from "@/lib/api";
import { timeAgo } from "@/lib/utils";
import { docUri, parseUri } from "@/lib/uri";
import { parseHeadings } from "@/lib/markdown";
import { DocumentOutline } from "@/components/doc-outline";
import { DocumentView } from "@/components/document-view";
import { SummaryFold } from "@/components/summary-fold";
import { Alert } from "@/components/ui/alert";
import { Button } from "@/components/ui/button";
import { Tabs, TabsList, TabsTrigger, TabsContent } from "@/components/ui/tabs";
import { Panel } from "@/components/ui/panel";
import { Badge } from "@/components/ui/badge";
import { HistoryList } from "@/components/history-list";
import { FrontmatterEditDialog } from "@/components/frontmatter-edit-dialog";
import { MarkdownEditorFallback } from "@/components/markdown-editor-fallback";
import { ConfirmDialog } from "@/components/ui/confirm-dialog";
import { PublishOptionsDialog } from "@/components/publish-options-dialog";
import { useVaultRefresh } from "@/contexts/vault-refresh-context";

// Plate is heavy (~hundreds of KB gzipped); lazy-load so the read-only path
// (Rendered / Raw / Agent) stays cheap.
const MarkdownEditor = lazy(() => import("@/components/markdown-editor"));

const RELATION_COLOR: Record<string, string> = {
  implements: "text-success",
  depends_on: "text-info",
  references: "text-info",
  related_to: "text-foreground-muted",
  attached_to: "text-success",
  derived_from: "text-warning",
};

type DocView = "rendered" | "raw" | "agent" | "edit";

export default function DocumentPage() {
  const { name, id } = useParams<{ name: string; id: string }>();
  const navigate = useNavigate();
  const { refetchTree } = useVaultRefresh();
  const [searchParams, setSearchParams] = useSearchParams();
  const commitHash = searchParams.get("commit") || undefined;
  const rawView = searchParams.get("view");
  const view: DocView =
    rawView === "raw"
      ? "raw"
      : rawView === "edit"
        ? "edit"
        : rawView === "agent"
          ? "agent"
          : "rendered";
  const [relations, setRelations] = useState<any[]>([]);
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
  const [editOpen, setEditOpen] = useState(false);
  const [deleteOpen, setDeleteOpen] = useState(false);
  const [publishOpen, setPublishOpen] = useState(false);
  // Plate manages its own state; we remount via `editorKey` when hydrating
  // a fresh server value rather than treating `value` as controlled.
  const [editingContent, setEditingContent] = useState("");
  const [originalContent, setOriginalContent] = useState("");
  const [editorKey, setEditorKey] = useState(0);
  const [savingBody, setSavingBody] = useState(false);
  const [bodyError, setBodyError] = useState("");
  const [savedAt, setSavedAt] = useState<number | null>(null);
  // Plate's markdown roundtrip is not byte-identity: adopt the first
  // post-hydration emission as the new `originalContent` baseline so the
  // editor doesn't flash "UNSAVED" the moment it mounts.
  const hydratedKey = useRef<number | null>(null);
  const isDirty = editingContent !== originalContent;
  const docId = id ? decodeURIComponent(id) : "";
  const canEdit =
    !commitHash &&
    (vaultRole === "writer" || vaultRole === "admin" || vaultRole === "owner");

  const applyView = (next: DocView) => {
    const p = new URLSearchParams(searchParams);
    if (next === "rendered") p.delete("view");
    else p.set("view", next);
    setSearchParams(p, { replace: true });
  };
  const setView = (next: DocView) => {
    // Leaving Edit with unsaved changes routes through a ConfirmDialog
    // (design system bans window.confirm); the actual switch happens on confirm.
    if (view === "edit" && next !== "edit" && isDirty) {
      setPendingView(next);
      return;
    }
    applyView(next);
  };

  useEffect(() => {
    if (!name) return;
    setVaultRole(null);
    getVaultInfo(name)
      .then((d) => setVaultRole(d?.role || null))
      .catch(() => setVaultRole(null));
  }, [name]);

  const docQuery = useQuery({
    queryKey: ["document", name, docId, commitHash],
    queryFn: () => getDocument(name!, docId, commitHash),
    enabled: !!name && !!docId,
    retry: false,
  });

  const doc = docOverride ?? docQuery.data ?? null;
  // Parse headings once for the outline-tab count (the outline + renderer each
  // re-scan internally; this removes the third pass that ran on every render).
  const headingSlugs = useMemo(() => parseHeadings(doc?.content || ""), [doc?.content]);

  useEffect(() => {
    const d = docQuery.data;
    setDocOverride(null);
    setProvenance([]);
    setRelations([]);
    setRelationsError(false);
    setHistoryError(false);
    setBodyError("");
    if (!d) return;
    const body = d.content || "";
    setOriginalContent(body);
    setEditingContent(body);
    // Bump the key so the Plate editor remounts with the new value —
    // it's uncontrolled internally and won't pick up `value` prop
    // changes after mount.
    setEditorKey((k) => k + 1);
    if (d.path && d.path !== docId) {
      navigate(`/vault/${name}/doc/${encodeURIComponent(d.path)}`, { replace: true });
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
    if (!isDirty) return;
    const onBeforeUnload = (e: BeforeUnloadEvent) => {
      e.preventDefault();
      e.returnValue = "";
    };
    window.addEventListener("beforeunload", onBeforeUnload);
    return () => window.removeEventListener("beforeunload", onBeforeUnload);
  }, [isDirty]);

  async function handleSaveBody() {
    if (!name || !docId) return;
    setSavingBody(true);
    setBodyError("");
    try {
      await updateDocument(name, docId, { content: editingContent });
      const now = new Date().toISOString();
      // Optimistically advance content + updated_at so the byline reads
      // "last changed just now" without waiting for a refetch.
      setDocOverride({
        ...(doc || {}),
        content: editingContent,
        updated_at: now,
      });
      setOriginalContent(editingContent);
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
      });
      const p = new URLSearchParams(searchParams);
      p.delete("view");
      setSearchParams(p, { replace: true });
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
    setEditingContent(originalContent);
    setEditorKey((k) => k + 1);
    setBodyError("");
  }

  // History = `git log -- <doc.path>` scoped to this document.
  async function loadHistory(vault: string, docPath: string) {
    const t = localStorage.getItem("akb_token") || "";
    try {
      const r = await fetch(
        `/api/v1/activity/${encodeURIComponent(vault)}?collection=${encodeURIComponent(docPath)}&limit=20`,
        { headers: { Authorization: `Bearer ${t}` } },
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

  if (docQuery.isError) {
    const errorMsg = (docQuery.error as Error)?.message ?? "Unknown error";
    return (
      <div className="py-8 fade-up">
        <div className="coord-spark mb-2">⚠ ERROR</div>
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
    return (
      <div className="py-8 coord">
        <Loader2 className="h-4 w-4 inline animate-spin mr-2" aria-hidden />
        Loading…
      </div>
    );
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

  const commitShort = doc.current_commit?.slice(0, 7);
  const inEditMode = view === "edit";

  return (
    <div
      className={`grid grid-cols-1 gap-x-10 gap-y-6 fade-up ${
        inEditMode ? "" : "lg:grid-cols-[minmax(0,1fr)_280px]"
      }`}
    >
      <article
        ref={setArticleEl}
        aria-labelledby="doc-title"
        className={`min-w-0 w-full ${
          inEditMode ? "max-w-none" : "max-w-[880px]"
        }`}
      >
        {commitHash && (
          <div
            role="status"
            aria-live="polite"
            className="rounded-[var(--radius-lg)] border border-accent bg-accent/5 px-4 py-2 mb-4 flex items-center justify-between gap-3 flex-wrap shadow-sm"
          >
            <div className="flex items-baseline gap-2 min-w-0">
              <span className="coord-spark shrink-0">⊙ HISTORICAL VIEW</span>
              <span className="text-sm text-foreground">
                Viewing version{" "}
                <code className="font-mono text-accent-strong">{commitHash.slice(0, 7)}</code>
                {" "}— writes are disabled until you return to the latest version.
              </span>
            </div>
            <button
              type="button"
              onClick={() => {
                const p = new URLSearchParams(searchParams);
                p.delete("commit");
                setSearchParams(p, { replace: false });
              }}
              className="inline-flex items-center gap-1 px-2 h-7 text-xs font-mono uppercase tracking-wider rounded-[var(--radius-sm)] border border-accent-strong text-accent-strong hover:bg-accent-strong hover:text-accent-strong-foreground transition-colors cursor-pointer focus:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2 focus-visible:ring-offset-background"
            >
              ← Back to latest
            </button>
          </div>
        )}
        {/* Mono meta line */}
        <div className="coord mb-2">
          DOC · {docUri(name!, doc.path)}
          {commitShort && (
            <>
              {" · HEAD "}
              <span className="text-accent-strong">{commitShort}</span>
            </>
          )}
        </div>

        {/* Display title */}
        <h1 id="doc-title" className="font-display text-[28px] lg:text-[34px] xl:text-[40px] leading-[1.05] tracking-tight text-foreground mb-3 break-words">
          {doc.title}
        </h1>

        {/* Byline */}
        {(doc.created_by || doc.updated_at) && (
          <div className="font-medium tracking-[-0.01em] text-[14px] text-foreground-muted mb-7">
            {doc.created_by && (
              <>
                Written by{" "}
                <span className="text-foreground">{doc.created_by}</span>
                {doc.updated_at && <>, last changed {timeAgo(doc.updated_at)}</>}.
              </>
            )}
          </div>
        )}

        {/* Frontmatter card — mono metadata with semantic colors */}
        <FrontmatterCard doc={doc} />

        <SummaryFold summary={doc.summary} className="mt-4 mb-7" />

        {publishError && (
          <Alert variant="destructive" className="mb-6">{publishError}</Alert>
        )}

        {inEditMode ? (
          <>
            <div className="flex items-center justify-end mb-3 gap-3">
              {savedAt && (
                <span
                  role="status"
                  aria-live="polite"
                  className="coord text-success inline-flex items-baseline gap-1"
                >
                  <CheckCircle2 className="h-3 w-3 self-center" aria-hidden />
                  SAVED
                </span>
              )}
              <div
                role="tablist"
                aria-label="Document view"
                className="inline-flex items-center gap-1 rounded-[var(--radius-md)] bg-surface-2 p-1"
                onKeyDown={(e) => {
                  // Roving tabindex within the strip — Arrow keys move
                  // focus, Enter/Space (handled by the button itself)
                  // activates. Matches the WAI-ARIA tabs pattern used in
                  // DocumentView's TabStrip.
                  const buttons = Array.from(
                    e.currentTarget.querySelectorAll<HTMLButtonElement>('[role="tab"]'),
                  );
                  const idx = buttons.indexOf(document.activeElement as HTMLButtonElement);
                  if (idx < 0) return;
                  let next: number;
                  if (e.key === "ArrowRight") next = (idx + 1) % buttons.length;
                  else if (e.key === "ArrowLeft") next = (idx - 1 + buttons.length) % buttons.length;
                  else if (e.key === "Home") next = 0;
                  else if (e.key === "End") next = buttons.length - 1;
                  else return;
                  e.preventDefault();
                  buttons[next]?.focus();
                }}
              >
                {/* Rendered/Raw are navigation triggers here (their panels live
                    in DocumentView, unmounted while editing) — no aria-controls
                    so we don't point at non-existent ids. */}
                <button
                  role="tab"
                  id="doc-tab-rendered"
                  aria-selected={false}
                  tabIndex={-1}
                  onClick={() => setView("rendered")}
                  className="px-3 py-1 text-xs font-medium rounded-[var(--radius-sm)] transition-token cursor-pointer text-foreground-muted hover:text-foreground focus:outline-none focus-visible:ring-2 focus-visible:ring-ring"
                >
                  Rendered
                </button>
                <button
                  role="tab"
                  id="doc-tab-raw"
                  aria-selected={false}
                  tabIndex={-1}
                  onClick={() => setView("raw")}
                  className="px-3 py-1 text-xs font-medium rounded-[var(--radius-sm)] transition-token cursor-pointer text-foreground-muted hover:text-foreground focus:outline-none focus-visible:ring-2 focus-visible:ring-ring"
                >
                  Raw
                </button>
                <button
                  role="tab"
                  id="doc-tab-edit"
                  aria-selected={true}
                  aria-controls="doc-panel-edit"
                  tabIndex={0}
                  className="px-3 py-1 text-xs font-medium rounded-[var(--radius-sm)] bg-surface text-foreground shadow-sm cursor-default"
                >
                  Edit{isDirty ? "*" : ""}
                </button>
              </div>
            </div>
            <div
              id="doc-panel-edit"
              role="tabpanel"
              aria-labelledby="doc-tab-edit"
              className="space-y-3"
            >
              <div className="coord flex items-center justify-between">
                <span>EDITING BODY</span>
                <span className="text-foreground-muted normal-case tracking-normal font-sans">
                  Title, type, tags and other metadata are managed separately
                  via <span className="font-mono uppercase tracking-wider">Edit details</span> →
                </span>
              </div>
              <Suspense fallback={<MarkdownEditorFallback />}>
                <MarkdownEditor
                  key={editorKey}
                  value={originalContent}
                  onChange={(md) => {
                    if (hydratedKey.current !== editorKey) {
                      hydratedKey.current = editorKey;
                      setOriginalContent(md);
                      setEditingContent(md);
                      return;
                    }
                    setEditingContent(md);
                  }}
                  autoFocus
                />
              </Suspense>
              {bodyError && <Alert variant="destructive">{bodyError}</Alert>}
              <div className="flex items-center justify-between">
                <div className="coord">
                  {isDirty && <span className="text-warning">UNSAVED CHANGES</span>}
                </div>
                <div className="flex items-center gap-2">
                  <Button
                    type="button"
                    variant="outline"
                    onClick={handleCancelBody}
                    disabled={savingBody || !isDirty}
                    size="sm"
                  >
                    Cancel
                  </Button>
                  <Button
                    type="button"
                    variant="accent"
                    onClick={handleSaveBody}
                    disabled={savingBody || !isDirty}
                    size="sm"
                  >
                    {savingBody ? (
                      <>
                        <Loader2 className="h-4 w-4 animate-spin" aria-hidden />
                        Saving…
                      </>
                    ) : (
                      "Save"
                    )}
                  </Button>
                </div>
              </div>
            </div>
          </>
        ) : (
          <>
            {savedAt && (
              <div className="flex items-center justify-end mb-2">
                <span
                  role="status"
                  aria-live="polite"
                  className="coord text-success inline-flex items-baseline gap-1"
                >
                  <CheckCircle2 className="h-3 w-3 self-center" aria-hidden />
                  SAVED
                </span>
              </div>
            )}
            <DocumentView
              vault={name!}
              docId={docId}
              version={commitHash}
              view={view}
              onViewChange={(v) => setView(v)}
              extraTab={
                canEdit
                  ? {
                      label: `EDIT${isDirty ? "*" : ""}`,
                      onClick: () => setView("edit"),
                    }
                  : undefined
              }
            />
          </>
        )}
      </article>

      {!inEditMode && (
      <aside className="lg:sticky lg:top-4 lg:self-start lg:max-h-[calc(100dvh-9rem)] flex flex-col text-sm min-h-0">
        {!commitHash && (() => {
          const canWrite = vaultRole === "writer" || vaultRole === "admin" || vaultRole === "owner";
          const rowCls =
            "w-full flex items-center gap-2.5 px-3 py-2.5 text-sm transition-colors cursor-pointer focus:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-inset";
          return (
            <Panel className="shrink-0 mb-4 divide-y divide-border">
              {canWrite && doc.type !== "skill" && (
                <button onClick={() => setEditOpen(true)} className={`${rowCls} text-foreground hover:bg-surface-hover`}>
                  <Pencil className="h-3.5 w-3.5 text-foreground-muted" aria-hidden />
                  Edit details
                </button>
              )}
              {canWrite && (
                <button onClick={() => setDeleteOpen(true)} className={`${rowCls} text-foreground-muted hover:bg-destructive/5 hover:text-destructive`}>
                  <Trash2 className="h-3.5 w-3.5" aria-hidden />
                  Delete document
                </button>
              )}
              {doc.is_public && doc.public_slug ? (
                <div className="px-3 py-2.5 text-xs">
                  <div className="flex items-center justify-between gap-2 mb-1.5">
                    <span className="coord-spark">§ Published</span>
                    <div className="flex items-center gap-2.5">
                      <button
                        onClick={copyPublicLink}
                        className="inline-flex items-center gap-1 text-foreground-muted hover:text-link transition-colors cursor-pointer focus:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-inset"
                      >
                        {copied ? <CheckCircle2 className="h-3 w-3 text-accent" aria-hidden /> : <ExternalLink className="h-3 w-3" aria-hidden />}
                        {copied ? "Copied" : "Copy"}
                      </button>
                      <button
                        onClick={handleUnpublish}
                        disabled={publishing}
                        className="inline-flex items-center gap-1 text-foreground-muted hover:text-destructive transition-colors cursor-pointer focus:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-inset disabled:opacity-50"
                      >
                        {publishing ? <Loader2 className="h-3 w-3 animate-spin" aria-hidden /> : <Lock className="h-3 w-3" aria-hidden />}
                        {publishing ? "…" : "Unpublish"}
                      </button>
                    </div>
                  </div>
                  <div title={`/p/${doc.public_slug}`} className="font-mono text-[11px] text-foreground-muted truncate">
                    /p/{doc.public_slug}
                  </div>
                </div>
              ) : (
                <button
                  onClick={() => setPublishOpen(true)}
                  disabled={publishing}
                  className={`${rowCls} text-foreground hover:bg-surface-hover hover:text-link disabled:opacity-50`}
                >
                  <Unlock className="h-3.5 w-3.5 text-foreground-muted" aria-hidden />
                  Publish to /p/…
                </button>
              )}
            </Panel>
          );
        })()}

        <Tabs defaultValue="outline" className="flex flex-col min-h-0 flex-1">
          <TabsList className="shrink-0 w-full">
            <TabsTrigger value="outline" className="flex-1 min-w-0 gap-1 px-2">
              Outline
              <span className="coord tabular-nums">{headingSlugs.length}</span>
            </TabsTrigger>
            <TabsTrigger value="relations" className="flex-1 min-w-0 gap-1 px-2">
              Relations
              {relations.length > 0 && (
                <span className="coord tabular-nums">{relations.length}</span>
              )}
            </TabsTrigger>
            <TabsTrigger value="history" className="flex-1 min-w-0 gap-1 px-2">
              History
              {provenance.length > 0 && (
                <span className="coord tabular-nums">{provenance.length}</span>
              )}
            </TabsTrigger>
          </TabsList>

          <TabsContent
            value="outline"
            className="flex-1 min-h-0 overflow-y-auto rail-scroll pr-1 pt-3"
          >
            <DocumentOutline markdown={doc.content || ""} articleEl={articleEl} />
          </TabsContent>

          <TabsContent
            value="relations"
            className="flex-1 min-h-0 overflow-y-auto rail-scroll pr-1 pt-3"
          >
            {relationsError ? (
              <Alert variant="destructive">Failed to load relations.</Alert>
            ) : relations.length > 0 ? (
              <>
                <ol className="font-mono text-[11px] leading-[1.9] space-y-0.5">
                  {relations.map((r) => {
                    // Use the canonical URI parser (handles the `/coll/.../doc/`
                    // form) — a flat regex missed collection docs and produced
                    // an empty ref → `/vault/x/doc/` → blank screen.
                    const p = parseUri(r.uri);
                    const targetVault = p?.vault ?? name;
                    const targetType = p?.kind ?? r.resource_type;
                    const targetRef = p?.id ?? "";
                    let href = "#";
                    if (targetRef && targetType === "doc") {
                      href = `/vault/${targetVault}/doc/${encodeURIComponent(targetRef)}`;
                    } else if (targetRef && targetType === "table") {
                      href = `/vault/${targetVault}/table/${encodeURIComponent(targetRef)}`;
                    } else if (targetRef && targetType === "file") {
                      href = `/vault/${targetVault}/file/${encodeURIComponent(targetRef)}`;
                    }
                    const label = r.name || targetRef || r.uri;
                    const relColor = RELATION_COLOR[r.relation] || "text-foreground-muted";
                    return (
                      <li key={`${r.relation}:${r.uri}`}>
                        <Link
                          to={href}
                          className="grid grid-cols-[minmax(64px,88px)_1fr] gap-1.5 py-0.5 group hover:bg-surface-hover -mx-1 px-1 rounded-[var(--radius-sm)] transition-colors focus:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2 focus-visible:ring-offset-background"
                        >
                          <span className={relColor}>{r.relation || "relates"}</span>
                          <span title={label} className="text-foreground truncate group-hover:text-link">
                            → {label}
                          </span>
                        </Link>
                      </li>
                    );
                  })}
                </ol>
                <Link
                  to={`/vault/${name}/graph?focus=${encodeURIComponent(doc.path ? docUri(name!, doc.path) : "")}`}
                  className="mt-3 inline-flex items-center gap-1 text-xs text-link hover:text-link-hover hover:underline rounded-[var(--radius-sm)] focus:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2 focus-visible:ring-offset-background"
                >
                  <GitGraph className="h-3 w-3" aria-hidden /> Open in graph →
                </Link>
              </>
            ) : (
              <div className="coord">No relations yet.</div>
            )}
          </TabsContent>

          <TabsContent
            value="history"
            className="flex-1 min-h-0 overflow-y-auto rail-scroll pr-1 pt-3"
          >
            {historyError ? (
              <Alert variant="destructive">Failed to load history.</Alert>
            ) : (
              <HistoryList
                entries={provenance as any}
                selectedHash={commitHash}
                onSelect={(hash) => {
                  const p = new URLSearchParams(searchParams);
                  if (commitHash === hash) {
                    p.delete("commit");
                  } else {
                    p.set("commit", hash);
                  }
                  setSearchParams(p, { replace: false });
                }}
              />
            )}
          </TabsContent>
        </Tabs>
      </aside>
      )}

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

      <PublishOptionsDialog
        open={publishOpen}
        onOpenChange={setPublishOpen}
        vault={name!}
        docId={docId}
        onPublished={(slug) => setDocOverride({ ...doc, is_public: true, public_slug: slug })}
      />

      <ConfirmDialog
        open={deleteOpen}
        onOpenChange={setDeleteOpen}
        title={`Delete "${doc.title || doc.path}"?`}
        description={
          "The document, its embeddings, and its publication links are removed.\nGit history of the file is preserved in the vault repo.\nThis cannot be undone from the UI."
        }
        confirmLabel="Delete document"
        variant="destructive"
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
        description="Your edits to the document body will be lost."
        confirmLabel="Discard changes"
        variant="destructive"
        onConfirm={() => {
          const next = pendingView;
          setEditingContent(originalContent);
          setEditorKey((k) => k + 1);
          setPendingView(null);
          if (next) applyView(next);
        }}
      />
    </div>
  );
}

// ── Frontmatter metadata card ─────────────────────────────────
function FrontmatterCard({ doc }: { doc: any }) {
  const rows: Array<[string, React.ReactNode]> = [];
  if (doc.type) rows.push(["type", <span className="text-foreground">{doc.type}</span>]);
  if (doc.status) {
    // Status carries a shape (Badge), not color alone (WCAG 1.4.1).
    const variant =
      doc.status === "active" ? "active" :
      doc.status === "archived" ? "archived" :
      "draft";
    rows.push(["status", <Badge variant={variant}>{doc.status}</Badge>]);
  }
  if (doc.domain) rows.push(["domain", <span className="text-foreground">{doc.domain}</span>]);
  if (doc.tags?.length) {
    rows.push([
      "tags",
      <span className="text-info break-words">{doc.tags.map((t: string) => `#${t}`).join(" ")}</span>,
    ]);
  }
  if (doc.depends_on?.length) {
    rows.push([
      "depends_on",
      <span className="text-foreground-muted break-words">{doc.depends_on.join(", ")}</span>,
    ]);
  }
  if (doc.related_to?.length) {
    rows.push([
      "related_to",
      <span className="text-foreground-muted break-words">{doc.related_to.join(", ")}</span>,
    ]);
  }
  if (doc.is_public) {
    rows.push([
      "published",
      <span className="text-foreground break-all">
        /p/{doc.public_slug}
      </span>,
    ]);
  }

  if (rows.length === 0) return null;

  return (
    <Panel className="px-4 py-3 font-mono text-[11px] leading-[1.85]">
      {rows.map(([k, v]) => (
        <div key={k}>
          <span className="text-foreground-muted">{k}:</span> {v}
        </div>
      ))}
    </Panel>
  );
}

