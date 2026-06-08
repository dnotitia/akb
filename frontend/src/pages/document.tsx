import { Suspense, lazy, useEffect, useRef, useState } from "react";
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
import { docUri } from "@/lib/uri";
import { parseHeadings } from "@/lib/markdown";
import { DocumentOutline } from "@/components/doc-outline";
import { DocumentView } from "@/components/document-view";
import { SummaryFold } from "@/components/summary-fold";
import { Button } from "@/components/ui/button";
import { Tabs, TabsList, TabsTrigger, TabsContent } from "@/components/ui/tabs";
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
  implements: "text-good",
  depends_on: "text-info",
  references: "text-info",
  related_to: "text-foreground-muted",
  attached_to: "text-good",
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
  const [provenance, setProvenance] = useState<any[]>([]);
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

  const setView = (next: DocView) => {
    if (view === "edit" && next !== "edit" && isDirty) {
      // Explicit save mirrors the frontmatter dialog UX — losing edits
      // silently on tab switch would be the wrong default.
      const ok = window.confirm(
        "Discard unsaved changes? Your edits will be lost.",
      );
      if (!ok) return;
      setEditingContent(originalContent);
      setEditorKey((k) => k + 1);
    }
    const p = new URLSearchParams(searchParams);
    if (next === "rendered") p.delete("view");
    else p.set("view", next);
    setSearchParams(p, { replace: true });
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

  useEffect(() => {
    const d = docQuery.data;
    setDocOverride(null);
    setProvenance([]);
    setRelations([]);
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
      getRelations(name!, d.path).then((r) => setRelations(r.relations || [])).catch(() => {});
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
      setSavingBody(false);
      return;
    }
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
    setSavingBody(false);
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
        return;
      }
      const d = await r.json();
      setProvenance(d.activity || []);
    } catch {
      setProvenance([]);
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
    await navigator.clipboard.writeText(url);
    setCopied(true);
    setTimeout(() => setCopied(false), 2000);
  }

  const commitShort = doc.current_commit?.slice(0, 7);
  const inEditMode = view === "edit";

  return (
    <div
      className={`grid grid-cols-1 gap-x-10 gap-y-6 fade-up ${
        inEditMode ? "" : "xl:grid-cols-[minmax(0,1fr)_260px]"
      }`}
    >
      <article
        ref={setArticleEl}
        className={`min-w-0 w-full ${
          inEditMode ? "max-w-none" : "max-w-[1020px] justify-self-center"
        }`}
      >
        {commitHash && (
          <div
            role="status"
            aria-live="polite"
            className="border border-accent bg-accent/5 px-4 py-2 mb-4 flex items-center justify-between gap-3 flex-wrap"
          >
            <div className="flex items-baseline gap-2 min-w-0">
              <span className="coord-spark text-accent shrink-0">⊙ HISTORICAL VIEW</span>
              <span className="text-sm text-foreground">
                Viewing version{" "}
                <code className="font-mono text-accent">{commitHash.slice(0, 7)}</code>
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
              className="inline-flex items-center gap-1 px-2 h-7 text-xs font-mono uppercase tracking-wider border border-accent text-accent hover:bg-accent hover:text-accent-foreground transition-colors cursor-pointer focus:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2 focus-visible:ring-offset-background"
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
              <span className="text-accent">{commitShort}</span>
            </>
          )}
        </div>

        {/* Serif display title */}
        <h1 className="font-serif text-[40px] leading-[1.05] tracking-[-0.02em] text-foreground mb-3">
          {doc.title}
        </h1>

        {/* Byline in serif italic */}
        {(doc.created_by || doc.updated_at) && (
          <div className="font-serif-italic text-[14px] text-foreground-muted mb-7">
            {doc.created_by && (
              <>
                Written by{" "}
                <span className="not-italic text-accent">{doc.created_by}</span>
                {doc.updated_at && <>, last changed {timeAgo(doc.updated_at)}</>}.
              </>
            )}
          </div>
        )}

        {/* Frontmatter card — mono metadata with semantic colors */}
        <FrontmatterCard doc={doc} />

        <SummaryFold summary={doc.summary} className="mt-4 mb-7" />

        {publishError && (
          <div
            role="alert"
            aria-live="polite"
            className="border border-destructive px-3 py-2 mb-6 text-xs font-mono uppercase tracking-wider text-destructive"
          >
            ⚠ {publishError.toUpperCase()}
          </div>
        )}

        {inEditMode ? (
          <>
            <div className="flex items-center justify-end mb-3 gap-3">
              {savedAt && (
                <span
                  role="status"
                  aria-live="polite"
                  className="coord text-good inline-flex items-baseline gap-1"
                >
                  <CheckCircle2 className="h-3 w-3 self-center" aria-hidden />
                  SAVED
                </span>
              )}
              <div
                role="tablist"
                aria-label="Document view"
                className="inline-flex border border-border"
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
                <button
                  role="tab"
                  id="doc-tab-rendered"
                  aria-selected={false}
                  aria-controls="doc-panel-rendered"
                  tabIndex={-1}
                  onClick={() => setView("rendered")}
                  className="px-2.5 py-1 text-[11px] font-mono uppercase tracking-wider transition-colors cursor-pointer text-foreground-muted hover:text-foreground hover:bg-surface-muted focus:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2 focus-visible:ring-offset-background"
                >
                  RENDERED
                </button>
                <button
                  role="tab"
                  id="doc-tab-raw"
                  aria-selected={false}
                  aria-controls="doc-panel-raw"
                  tabIndex={-1}
                  onClick={() => setView("raw")}
                  className="px-2.5 py-1 text-[11px] font-mono uppercase tracking-wider border-l border-border transition-colors cursor-pointer text-foreground-muted hover:text-foreground hover:bg-surface-muted focus:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2 focus-visible:ring-offset-background"
                >
                  RAW
                </button>
                <button
                  role="tab"
                  id="doc-tab-edit"
                  aria-selected={true}
                  aria-controls="doc-panel-edit"
                  tabIndex={0}
                  className="px-2.5 py-1 text-[11px] font-mono uppercase tracking-wider border-l border-border bg-foreground text-background cursor-default"
                >
                  EDIT{isDirty ? "*" : ""}
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
              {bodyError && (
                <div
                  role="alert"
                  aria-live="polite"
                  className="border border-destructive px-3 py-2 text-xs font-mono uppercase tracking-wider text-destructive"
                >
                  ⚠ {bodyError.toUpperCase()}
                </div>
              )}
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
                  className="coord text-good inline-flex items-baseline gap-1"
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
      <aside className="xl:sticky xl:top-4 xl:self-start xl:max-h-[calc(100dvh-13rem)] flex flex-col text-sm min-h-0">
        {!commitHash && (vaultRole === "writer" || vaultRole === "admin" || vaultRole === "owner") && (
          <div className="shrink-0 pb-3 mb-3 border-b border-border space-y-2">
            {doc.type !== "skill" && (
              <button
                onClick={() => setEditOpen(true)}
                className="w-full inline-flex items-center justify-center gap-1.5 px-2 py-1.5 text-xs border border-border hover:border-accent hover:text-accent transition-colors cursor-pointer focus:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2 focus-visible:ring-offset-background"
              >
                <Pencil className="h-3 w-3" aria-hidden />
                Edit details
              </button>
            )}
            <button
              onClick={() => setDeleteOpen(true)}
              className="w-full inline-flex items-center justify-center gap-1.5 px-2 py-1.5 text-xs border border-border text-foreground-muted hover:border-destructive hover:text-destructive transition-colors cursor-pointer focus:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2 focus-visible:ring-offset-background"
            >
              <Trash2 className="h-3 w-3" aria-hidden />
              Delete document
            </button>
          </div>
        )}

        {!commitHash && (
          <div className="shrink-0 pb-3 mb-3 border-b border-border">
            {doc.is_public && doc.public_slug ? (
              <div className="flex flex-col gap-1.5 text-xs">
                <div className="coord">§ PUBLISHED</div>
                <div title={`/p/${doc.public_slug}`} className="font-mono text-[11px] text-foreground truncate">
                  /p/{doc.public_slug}
                </div>
                <div className="flex items-center gap-3 text-[11px]">
                  <button
                    onClick={copyPublicLink}
                    className="inline-flex items-center gap-1 text-foreground-muted hover:text-accent transition-colors cursor-pointer focus:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2 focus-visible:ring-offset-background"
                  >
                    {copied ? (
                      <CheckCircle2 className="h-3 w-3 text-accent" aria-hidden />
                    ) : (
                      <ExternalLink className="h-3 w-3" aria-hidden />
                    )}
                    {copied ? "Copied" : "Copy link"}
                  </button>
                  <button
                    onClick={handleUnpublish}
                    disabled={publishing}
                    className="inline-flex items-center gap-1 text-foreground-muted hover:text-destructive transition-colors cursor-pointer focus:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2 focus-visible:ring-offset-background disabled:opacity-50"
                  >
                    {publishing ? (
                      <Loader2 className="h-3 w-3 animate-spin" aria-hidden />
                    ) : (
                      <Lock className="h-3 w-3" aria-hidden />
                    )}
                    {publishing ? "Unpublishing…" : "Unpublish"}
                  </button>
                </div>
              </div>
            ) : (
              <button
                onClick={() => setPublishOpen(true)}
                disabled={publishing}
                className="w-full inline-flex items-center justify-center gap-1.5 px-2 py-1.5 text-xs border border-border hover:border-accent hover:text-accent transition-colors disabled:opacity-50 cursor-pointer focus:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2 focus-visible:ring-offset-background"
              >
                <Unlock className="h-3 w-3" aria-hidden />
                Publish to /p/…
              </button>
            )}
          </div>
        )}

        <Tabs defaultValue="outline" className="flex flex-col min-h-0 flex-1">
          <TabsList className="shrink-0">
            <TabsTrigger value="outline" className="gap-1.5">
              Outline
              <span className="coord tabular-nums">[{headingCount(doc.content)}]</span>
            </TabsTrigger>
            <TabsTrigger value="relations" className="gap-1.5">
              Relations
              {relations.length > 0 && (
                <span className="coord tabular-nums">[{relations.length}]</span>
              )}
            </TabsTrigger>
            <TabsTrigger value="history" className="gap-1.5">
              History
              {provenance.length > 0 && (
                <span className="coord tabular-nums">[{provenance.length}]</span>
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
            {relations.length > 0 ? (
              <>
                <ol className="font-mono text-[11px] leading-[1.9] space-y-0.5">
                  {relations.map((r, i) => {
                    const m = /^akb:\/\/([^/]+)\/(doc|table|file)\/(.+)$/.exec(r.uri || "");
                    const targetVault = m?.[1] ?? name;
                    const targetType = m?.[2] ?? r.resource_type;
                    const targetRef = m?.[3] ?? "";
                    let href = "#";
                    if (targetType === "doc") {
                      href = `/vault/${targetVault}/doc/${encodeURIComponent(targetRef)}`;
                    } else if (targetType === "table") {
                      href = `/vault/${targetVault}/table/${encodeURIComponent(targetRef)}`;
                    } else if (targetType === "file") {
                      href = `/vault/${targetVault}/file/${encodeURIComponent(targetRef)}`;
                    }
                    const label = r.name || targetRef || r.uri;
                    const relColor = RELATION_COLOR[r.relation] || "text-foreground-muted";
                    return (
                      <li key={i}>
                        <Link
                          to={href}
                          className="grid grid-cols-[88px_1fr] gap-1.5 py-0.5 group hover:bg-surface-muted -mx-1 px-1 transition-colors focus:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2 focus-visible:ring-offset-background"
                        >
                          <span className={relColor}>{r.relation || "relates"}</span>
                          <span title={label} className="text-foreground truncate group-hover:text-accent">
                            → {label}
                          </span>
                        </Link>
                      </li>
                    );
                  })}
                </ol>
                <Link
                  to={`/vault/${name}/graph?focus=${encodeURIComponent(doc.path ? docUri(name!, doc.path) : "")}`}
                  className="mt-3 inline-flex items-center gap-1 text-xs text-accent hover:underline"
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
            className="flex-1 min-h-0 overflow-hidden pr-1 pt-3"
          >
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
    </div>
  );
}

// ── Frontmatter metadata card ─────────────────────────────────
function FrontmatterCard({ doc }: { doc: any }) {
  const rows: Array<[string, React.ReactNode]> = [];
  if (doc.type) rows.push(["type", <span className="text-foreground">{doc.type}</span>]);
  if (doc.status) {
    const statusColor =
      doc.status === "active" ? "text-good" :
      doc.status === "archived" ? "text-warning" :
      "text-foreground-muted";
    rows.push(["status", <span className={statusColor}>{doc.status}</span>]);
  }
  if (doc.domain) rows.push(["domain", <span className="text-foreground">{doc.domain}</span>]);
  if (doc.tags?.length) {
    rows.push([
      "tags",
      <span className="text-info">{doc.tags.map((t: string) => `#${t}`).join(" ")}</span>,
    ]);
  }
  if (doc.depends_on?.length) {
    rows.push([
      "depends_on",
      <span className="text-foreground-muted">{doc.depends_on.join(", ")}</span>,
    ]);
  }
  if (doc.related_to?.length) {
    rows.push([
      "related_to",
      <span className="text-foreground-muted">{doc.related_to.join(", ")}</span>,
    ]);
  }
  if (doc.is_public) {
    rows.push([
      "published",
      <span className="text-accent">
        /p/{doc.public_slug}
      </span>,
    ]);
  }

  if (rows.length === 0) return null;

  return (
    <div className="border border-border bg-surface px-4 py-3 font-mono text-[11px] leading-[1.85]">
      {rows.map(([k, v], i) => (
        <div key={i}>
          <span className="text-foreground-muted">{k}:</span> {v}
        </div>
      ))}
    </div>
  );
}

function headingCount(markdown: string | undefined): number {
  return parseHeadings(markdown || "").length;
}
