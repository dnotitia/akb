import { useCallback, useEffect, useId, useLayoutEffect, useMemo, useRef, useState, type KeyboardEvent, type MouseEvent, type ReactNode } from "react";
import { createPortal } from "react-dom";
import { ReadingChromeContext } from "@/contexts/reading-chrome-context";
import { DocumentIconButton } from "@/components/document-reading-controls";
import { DocumentByline } from "@/components/document-byline";
import { documentAuthorName } from "@/lib/document-author";
import { Link, useLocation, useNavigate, useParams } from "react-router-dom";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import * as Popover from "@radix-ui/react-popover";
import { ArrowDownLeft, ArrowLeft, ArrowUpRight, BookOpen, Check, FileText, List, Columns2, Network, Plus, X } from "lucide-react";
import { ApiError, getDocument, getRelations, type RelationRow } from "@/lib/api";
import { readWithAccessBoundary } from "@/lib/query-access-boundary";
import { readingDocumentHref, readingDocumentKey, readingFragment, readingReference, type ReadingDocument } from "@/lib/reading-workspace";
import { parseUri } from "@/lib/uri";
import { useReadingWorkspace } from "@/contexts/reading-workspace-context";
import { useAccessVerification, useCurrentUser } from "@/contexts/current-user-context";
import { MarkdownRender } from "@/components/markdown-render";
import { hrefFor, relationIsInVault } from "@/components/relations/relation-row-utils";
import { Button } from "@/components/ui/button";
import { Alert } from "@/components/ui/alert";
import { LoadingState } from "@/components/ui/loading-state";
import { Skeleton } from "@/components/ui/skeleton";
import { Dialog, DialogContent, DialogTitle } from "@/components/ui/dialog";

/** Route-level previews retain their own dialog/guard. Only full reader pages
 * participate in the persistent document workspace. */
export function DocumentReadingWorkspace({ children, enabled }: { children: ReactNode; enabled: boolean }) {
  const workspace = useReadingWorkspace();
  return enabled && workspace ? <ReadingWorkspace>{children}</ReadingWorkspace> : children;
}

function ReadingWorkspace({ children }: { children: ReactNode }) {
  const workspace = useReadingWorkspace()!;
  const user = useCurrentUser();
  const { checking, revision } = useAccessVerification();
  const { name = "", id = "" } = useParams();
  const location = useLocation();
  const params = new URLSearchParams(location.search);
  const version = params.get("view") === "diff" ? undefined : params.get("commit") || undefined;
  const docId = decodeURIComponent(id);
  const queryClient = useQueryClient();
  const docQuery = useQuery({
    queryKey: ["document", name, docId, version, user?.user_id, revision],
    queryFn: ({ queryKey, signal }) => readWithAccessBoundary(queryClient, queryKey, () => getDocument(name, docId, version), signal),
    enabled: !!name && !!docId && !checking, retry: false,
  });
  const denied = docQuery.error instanceof ApiError && [401, 403, 404].includes(docQuery.error.status);
  const doc = !checking && !denied ? docQuery.data : null;
  const active = doc ? readingDocumentKey({ vault: name, path: doc.path, version }) : "";
  const { remember, forget, positions } = workspace;
  useEffect(() => {
    if (doc) remember({ vault: name, path: doc.path, title: doc.title || doc.path,
      version, href: readingDocumentHref(name, doc.path, location.search) });
  }, [doc, name, version, location.search, remember]);
  useEffect(() => {
    if (denied) {
      // A path/id alias can resolve to a canonical tab, so remove matching routes
      // as well as the raw reference. Do not retain a denied document's label.
      forget(readingDocumentKey({ vault: name, path: docId, version }));
    }
  }, [denied, docId, name, version, forget]);
  const relationsQuery = useQuery({
    queryKey: ["document-relations", name, doc?.path, user?.user_id, revision, doc?.current_commit],
    queryFn: ({ queryKey, signal }) => readWithAccessBoundary(queryClient, queryKey, () => getRelations(name, doc.path), signal),
    enabled: !!doc?.path && !checking, retry: false,
  });
  const relations = relationsQuery.isError ? [] : (relationsQuery.data?.relations ?? []).filter(row => relationIsInVault(row, name));
  const primaryRef = useRef<HTMLDivElement>(null);
  const frameRef = useRef<HTMLDivElement>(null);
  const [narrow, setNarrow] = useState(true);
  useLayoutEffect(() => {
    const frame = frameRef.current;
    if (!frame) return;
    const update = () => {
      const rem = parseFloat(getComputedStyle(document.documentElement).fontSize) || 16;
      const bounds = frame.getBoundingClientRect();
      setNarrow(bounds.width < 50 * rem || bounds.height < 20 * rem);
    };
    update();
    const observer = new ResizeObserver(update);
    observer.observe(frame);
    return () => observer.disconnect();
  }, []);
  const positionKey = active + (params.get("view") === "raw" ? ":raw" : ":rendered");
  const editing = ["edit", "diff"].includes(params.get("view") ?? "");
  useLayoutEffect(() => {
    if (!doc || editing || !active) return;
    const canvas = primaryRef.current?.querySelector<HTMLElement>("#document-reading-canvas");
    if (!canvas) return;
    const restore = window.requestAnimationFrame(() => {
      if (location.hash) {
        const heading = primaryRef.current?.querySelector<HTMLElement>(`[id="${CSS.escape(readingFragment(location.hash))}"]`);
        if (heading) { heading.scrollIntoView({ block: "start" }); return; }
      }
      canvas.scrollTop = positions.get(positionKey) ?? 0;
    });
    const rememberPosition = () => positions.set(positionKey, canvas.scrollTop);
    canvas.addEventListener("scroll", rememberPosition, { passive: true });
    return () => { window.cancelAnimationFrame(restore); rememberPosition(); canvas.removeEventListener("scroll", rememberPosition); };
    // A background refresh must not scroll a reader back to an old position.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [active, editing, positionKey, location.hash, positions]);
  const [preview, setPreview] = useState<ReadingDocument | null>(null);
  const [previewHistory, setPreviewHistory] = useState<ReadingDocument[]>([]);
  const openerRef = useRef<HTMLElement | null>(null);
  const restoreFocusFrame = useRef<number | null>(null);
  useLayoutEffect(() => () => {
    if (restoreFocusFrame.current !== null) window.cancelAnimationFrame(restoreFocusFrame.current);
    restoreFocusFrame.current = null;
  }, [location.key]);
  const [compare, setCompare] = useState(false);
  const [contextMode, setContextMode] = useState<"references" | "preview" | "details" | null>(null);
  const [sheetOpen, setSheetOpen] = useState(false);
  const [toolbarHost, setToolbarHost] = useState<HTMLElement | null>(null);
  const [detailsHost, setDetailsHost] = useState<HTMLElement | null>(null);
  const [contextHeaderHost, setContextHeaderHost] = useState<HTMLElement | null>(null);
  const referenceTriggerRef = useRef<HTMLButtonElement>(null);
  const setDetailsOpen = useCallback((next: boolean | ((previous: boolean) => boolean)) => {
    setPreview(null); setCompare(false); setSheetOpen(false);
    setContextMode(previous => (typeof next === "function" ? next(previous === "details") : next)
      ? "details" : previous === "details" ? null : previous);
  }, []);
  useEffect(() => {
    setPreview(null); setPreviewHistory([]); setCompare(false); setSheetOpen(false); setContextMode(null);
  }, [location.pathname, checking, revision]);
  const showReferences = useCallback(() => {
    setPreview(null); setPreviewHistory([]); setCompare(false); setContextMode("references"); setSheetOpen(true);
  }, []);
  const openReference = useCallback((target: ReadingDocument, opener?: HTMLElement) => {
    if (target.vault !== name) return;
    if (opener) openerRef.current = opener;
    setContextMode("preview"); setSheetOpen(true);
    setPreviewHistory(previous => preview ? [...previous, preview].slice(-20) : []);
    setPreview(target);
  }, [name, preview]);
  const closePreview = useCallback(() => {
    setPreview(null); setPreviewHistory([]); setCompare(false); setContextMode(null); setSheetOpen(false);
    if (restoreFocusFrame.current !== null) window.cancelAnimationFrame(restoreFocusFrame.current);
    restoreFocusFrame.current = window.requestAnimationFrame(() => {
      restoreFocusFrame.current = null;
      const opener = openerRef.current;
      if (opener?.isConnected) opener.focus({ preventScroll: true });
      else primaryRef.current?.querySelector<HTMLElement>("#document-reading-canvas")?.focus({ preventScroll: true });
    });
  }, []);
  const followLink = (event: MouseEvent<HTMLElement> | KeyboardEvent<HTMLElement>) => {
    if (event.defaultPrevented || event.ctrlKey || event.metaKey || event.shiftKey || event.altKey || checking || editing) return;
    if ("button" in event && event.button !== 0) return;
    if ("key" in event && event.key !== "Enter") return;
    const anchor = (event.target as Element).closest<HTMLAnchorElement>("a");
    if (!anchor || anchor.hasAttribute("download")) return;
    // Same-document anchors keep native heading navigation. Inert reference
    // labels have neither a href nor a verified runtime URL and stay plain text.
    const href = anchor.getAttribute("data-markdown-reference-runtime-url") || anchor.getAttribute("href");
    if (!href || href.startsWith("#")) return;
    const target = readingReference(href, window.location.origin);
    if (!target || target.vault !== name) return;
    if (target.path === doc?.path && target.version === version && new URL(target.href, window.location.origin).hash) return;
    event.preventDefault(); event.stopPropagation();
    openReference(target, anchor);
  };
  const panelId = useId();
  const dockOpen = !editing && !!doc && !checking && contextMode !== null;
  const [referenceMode, setReferenceMode] = useState<"map" | "list">("list");
  const references = <ReferenceLens vault={name} title={doc?.title || ""} relations={relations}
    mode={referenceMode} onModeChange={setReferenceMode}
    loading={relationsQuery.isPending} error={relationsQuery.isError} historical={Boolean(version)}
    onRetry={() => void relationsQuery.refetch()} onOpen={openReference} />;
  const referencesHeader = <div className="reading-context-toolbar">
    <span role="heading" aria-level={2} aria-label="Document references" className="reading-context-label reading-context-label-icon"><Network size={16} aria-hidden /><span className="reading-context-count">{relationsQuery.isSuccess ? relations.length : ""}</span></span>
    <div className="reading-segment" role="group" aria-label="Reference view">
      <button type="button" aria-pressed={referenceMode === "map"} aria-label="Reference map" title="Reference map" onClick={() => setReferenceMode("map")}><Network size={14} aria-hidden /></button>
      <button type="button" aria-pressed={referenceMode === "list"} aria-label="Reference list" title="Reference list" onClick={() => setReferenceMode("list")}><List size={14} aria-hidden /></button>
    </div>
    <button type="button" className="reading-icon-button" aria-label="Close references" onClick={() => {
      setContextMode(null); setSheetOpen(false); referenceTriggerRef.current?.focus({ preventScroll: true });
    }}><X size={16} aria-hidden /></button>
  </div>;
  const referenceControl = useMemo(() => !editing && doc && <DocumentIconButton ref={referenceTriggerRef} label={relationsQuery.isSuccess ? `Show document references (${relations.length})` : "Show document references"}
    className="reading-reference-trigger" aria-expanded={dockOpen && contextMode === "references" && (!narrow || sheetOpen)}
    onClick={() => {
      if (contextMode === "references" && (!narrow || sheetOpen)) { setContextMode(null); setSheetOpen(false); }
      else showReferences();
    }}><Network size={16} aria-hidden />{relationsQuery.isSuccess && <span aria-hidden className="reading-reference-count">{relations.length}</span>}</DocumentIconButton>, [editing, doc, dockOpen, narrow, sheetOpen, contextMode, showReferences, relationsQuery.isSuccess, relations.length]);
  const chrome = useMemo(() => ({ toolbarHost, referenceControl, detailsHost, contextHeaderHost, narrow,
    detailsOpen: contextMode === "details", setDetailsOpen }),
  [toolbarHost, referenceControl, detailsHost, contextHeaderHost, narrow, contextMode, setDetailsOpen]);
  return <ReadingChromeContext.Provider value={chrome}>
  <div ref={frameRef} className="reading-studio" data-preview={Boolean(preview)} data-compare={compare}
    data-context={dockOpen && !narrow ? contextMode : "closed"} data-constrained={narrow}>
    <header className="reading-studio-header">
      <div className="reading-primary-toolbar">
        <div ref={setToolbarHost} className="reading-toolbar-host" />

      </div>
      <div ref={setContextHeaderHost} className="reading-context-header-host">
        {dockOpen && contextMode === "references" && !narrow && referencesHeader}
      </div>
    </header>
    <div className="reading-studio-body">
      <div ref={primaryRef} id={panelId} role="tabpanel" aria-label="Active document" aria-labelledby={active ? `${panelId}-tab-${encodeURIComponent(active)}` : undefined}
        className="reading-primary" onClickCapture={followLink} onKeyDownCapture={followLink}>
        {children}
      </div>
      {dockOpen && contextMode === "references" && !narrow && <aside className="reading-context-rail" aria-label="Document references">{references}</aside>}
      <div ref={setDetailsHost} className="reading-details-host" hidden={!dockOpen || contextMode !== "details" || narrow} />
      {contextMode === "preview" && preview && !checking && <ReferencePreview key={`${name}:${revision}`} target={preview} narrow={narrow}
        headerHost={contextHeaderHost} compare={compare} canGoBack={previewHistory.length > 0}
        onBack={() => { if (previewHistory.length) { setPreview(previewHistory.at(-1)!); setPreviewHistory(items => items.slice(0, -1)); } else showReferences(); }}
        onClose={closePreview} onOpen={openReference} onCompare={() => setCompare(value => !value)} />}
    </div>
    <div className="reading-workspace-bottom"><DocumentTabs active={active} panelId={panelId} /></div>
    {narrow && doc && contextMode === "references" && <Dialog open={sheetOpen} onOpenChange={open => {
      setSheetOpen(open); if (!open) referenceTriggerRef.current?.focus({ preventScroll: true });
    }}><DialogContent hideClose className="reading-studio reading-reference-dialog reading-references-dialog" aria-describedby={undefined}
      onCloseAutoFocus={event => { event.preventDefault(); referenceTriggerRef.current?.focus({ preventScroll: true }); }}>
      <DialogTitle className="sr-only">Document references</DialogTitle>{referencesHeader}<div className="reading-reference-sheet-scroll">{references}</div>
    </DialogContent></Dialog>}
  </div>
  </ReadingChromeContext.Provider>;
}

function DocumentTabs({ active, panelId }: { active: string; panelId: string }) {
  const workspace = useReadingWorkspace()!;
  const navigate = useNavigate();
  const location = useLocation();
  const [focused, setFocused] = useState<string | null>(null);
  const [filter, setFilter] = useState("");
  const listRef = useRef<HTMLDivElement>(null);
  const activeButtonRef = useRef<HTMLButtonElement>(null);
  useEffect(() => { activeButtonRef.current?.scrollIntoView?.({ block: "nearest", inline: "nearest" }); }, [active]);
  useEffect(() => {
    setFocused(active || null);
    const closed = (location.state as { readingCloseTab?: string } | null)?.readingCloseTab;
    if (closed && active && active !== closed) activeButtonRef.current?.focus({ preventScroll: true });
  }, [active, location.state]);
  const onKey = (event: KeyboardEvent, index: number) => {
    const buttons = listRef.current?.querySelectorAll<HTMLButtonElement>('[role="tab"]');
    if (!buttons?.length) return;
    const next = event.key === "ArrowRight" ? (index + 1) % buttons.length : event.key === "ArrowLeft" ? (index - 1 + buttons.length) % buttons.length
      : event.key === "Home" ? 0 : event.key === "End" ? buttons.length - 1 : null;
    if (next !== null) { event.preventDefault(); buttons[next].focus(); }
    if (event.key === "Delete") {
      event.preventDefault(); workspace.close(readingDocumentKey(workspace.tabs[index]), active);
      setFocused(active); listRef.current?.querySelector<HTMLButtonElement>('[aria-selected="true"]')?.focus();
    }
  };
  return <>
    <div ref={listRef} className="reading-tabs rail-scroll" role="tablist" aria-label="Open documents" aria-orientation="horizontal">
      {workspace.tabs.map((tab, index) => {
        const key = readingDocumentKey(tab);
        const selected = key === active;
        const duplicate = workspace.tabs.some(other => other !== tab && other.title === tab.title);
        return <div key={key} className="reading-tab-wrap" role="presentation" data-selected={selected}>
          <button ref={selected ? activeButtonRef : undefined} type="button" role="tab" id={`${panelId}-tab-${encodeURIComponent(key)}`}
            aria-selected={selected} aria-controls={selected ? panelId : undefined} tabIndex={(focused ?? active) === key ? 0 : -1}
            title={`${tab.title}\n${tab.vault}/${tab.path}${tab.version ? ` · ${tab.version}` : ""}`} className="reading-tab"
            onFocus={() => setFocused(key)} onKeyDown={event => onKey(event, index)} onClick={() => navigate(tab.href)}>
            <FileText size={14} aria-hidden /><span>{tab.title}{duplicate && <small>{tab.vault} / {tab.path}</small>}{tab.version && <small>Version {tab.version.slice(0, 8)}</small>}</span>
          </button>
          <button type="button" className="reading-icon-button reading-tab-close" aria-label={`Close ${tab.title}`} tabIndex={selected ? 0 : -1}
            onClick={() => { workspace.close(key, active); if (!selected) listRef.current?.querySelector<HTMLButtonElement>('[aria-selected="true"]')?.focus(); }}><X size={14} aria-hidden /></button>
        </div>;
      })}
    </div>
    <Popover.Root onOpenChange={open => { if (!open) setFilter(""); }}>
      <Popover.Trigger asChild><button type="button" className="reading-icon-button" aria-label="Open document list"><List size={18} aria-hidden /></button></Popover.Trigger>
      <Popover.Portal><Popover.Content className="reading-studio reading-doc-menu" side="top" align="end" sideOffset={8} collisionPadding={12}>
        <label className="reading-menu-search">Find open documents<input value={filter} onChange={event => setFilter(event.target.value)} type="search" /></label>
        <div className="reading-doc-menu-list">
          <p className="reading-caption">Open documents · {workspace.tabs.length}</p>
          {workspace.tabs.filter(doc => `${doc.title} ${doc.path} ${doc.vault}`.toLocaleLowerCase().includes(filter.toLocaleLowerCase())).map(doc => <Popover.Close key={readingDocumentKey(doc)} asChild>
            <button type="button" className="reading-doc-menu-item" onClick={() => navigate(doc.href)}><FileText size={16} aria-hidden /><span>{doc.title}<small>{doc.vault} / {doc.path}</small></span>{readingDocumentKey(doc) === active && <Check size={15} aria-hidden />}</button>
          </Popover.Close>)}
          {!!workspace.closed.length && <p className="reading-caption">Recently closed</p>}
          {workspace.closed.filter(doc => `${doc.title} ${doc.path}`.toLocaleLowerCase().includes(filter.toLocaleLowerCase())).map(doc => <Popover.Close key={readingDocumentKey(doc)} asChild>
            <button type="button" className="reading-doc-menu-item" onClick={() => navigate(doc.href)}><BookOpen size={16} aria-hidden /><span>{doc.title}<small>{doc.path}</small></span></button>
          </Popover.Close>)}
        </div>
      </Popover.Content></Popover.Portal>
    </Popover.Root>
  </>;
}

function ReferenceLens({ vault, title, relations, loading, error, historical, mode, onModeChange, onRetry, onOpen }: {
  vault: string; title: string; relations: RelationRow[]; loading: boolean; error: boolean; historical: boolean;
  onRetry: () => void; onOpen: (target: ReadingDocument, opener?: HTMLElement) => void;
  mode: "map" | "list"; onModeChange: (mode: "map" | "list") => void;
}) {
  const graph = relations.slice(0, 6);
  const choose = (event: MouseEvent<HTMLAnchorElement>, row: RelationRow) => {
    if (event.button || event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return;
    const target = readingReference(hrefFor(row, vault), window.location.origin);
    if (!target) return;
    event.preventDefault(); onOpen(target, event.currentTarget);
  };
  return <div className="reading-lens">
    {historical && <p className="reading-lens-note">References from the latest document.</p>}
    {loading ? <LoadingState label="Loading references" delayMs={200}><Skeleton className="m-4 h-24" /></LoadingState>
      : error ? <div className="reading-lens-empty">References could not be loaded.<Button size="sm" variant="ghost" onClick={onRetry}>Try again</Button></div>
        : !relations.length ? <p className="reading-lens-empty">No connected resources yet. Document links and added relations appear here.</p> : <>
          {mode === "map" && <div className="reading-reference-map" role="group" aria-label={`Direct connections to ${title}`}>
            <div className="reading-map-center"><FileText size={14} aria-hidden /><span title={title}>{title}</span></div>
            <div className="reading-map-neighbors">{graph.map((row, index) => <a key={`${row.uri}:${row.relation}:${row.direction}:${index}`} href={hrefFor(row, vault)} onClick={event => choose(event, row)}
              aria-label={`${row.direction === "incoming" ? "From" : "To"}: ${row.name || parseUri(row.uri)?.id}. ${row.relation.replaceAll("_", " ")}`}>
              {row.direction === "incoming" ? <ArrowDownLeft size={13} aria-hidden /> : <ArrowUpRight size={13} aria-hidden />}<span>{row.name || parseUri(row.uri)?.id}</span></a>)}</div>
          </div>}
          {mode === "list" && <div className="reading-reference-list">{(["outgoing", "incoming"] as const).map(direction => {
            const rows = relations.filter(row => row.direction === direction);
            return !!rows.length && <div key={direction}><p className="reading-caption">{direction === "outgoing" ? "From this document" : "To this document"} · {rows.length}</p>
              {rows.map((row, index) => <a key={`${row.uri}:${row.relation}:${index}`} href={hrefFor(row, vault)} onClick={event => choose(event, row)} className="reading-reference-row">
                {direction === "incoming" ? <ArrowDownLeft size={14} aria-hidden /> : <ArrowUpRight size={14} aria-hidden />}<span>{row.name || parseUri(row.uri)?.id}<small>{row.relation.replaceAll("_", " ")} · {row.kind === "implicit" ? "From document" : "Added relation"}</small></span>
              </a>)}</div>;
          })}</div>}
          {mode === "map" && relations.length > graph.length && <button type="button" className="reading-map-more" onClick={() => onModeChange("list")}>Show all {relations.length} connections</button>}
        </>}
  </div>;
}

function ReferencePreview({ target, narrow, headerHost, compare, canGoBack, onBack, onClose, onOpen, onCompare }: {
  target: ReadingDocument; narrow: boolean; headerHost: HTMLElement | null; compare: boolean; canGoBack: boolean; onBack: () => void; onClose: () => void;
  onOpen: (target: ReadingDocument, opener?: HTMLElement) => void; onCompare: () => void;
}) {
  const workspace = useReadingWorkspace()!;
  const user = useCurrentUser();
  const { checking, revision } = useAccessVerification();
  const queryClient = useQueryClient();
  const { data, error, isPending, isFetching, refetch } = useQuery({
    queryKey: ["document", target.vault, target.path, target.version, user?.user_id, revision],
    queryFn: ({ queryKey, signal }) => readWithAccessBoundary(queryClient, queryKey, () => getDocument(target.vault, target.path, target.version), signal),
    enabled: !checking, retry: false,
  });
  const denied = error instanceof ApiError && [401, 403, 404].includes(error.status);
  const doc = !checking && !denied ? data : null;
  const resolved = doc ? { ...target, path: doc.path, title: doc.title || doc.path,
    href: readingDocumentHref(target.vault, doc.path, target.version ? `?commit=${encodeURIComponent(target.version)}` : "") } : null;
  const kept = !!resolved && workspace.tabs.some(tab => readingDocumentKey(tab) === readingDocumentKey(resolved));
  const headingRef = useRef<HTMLSpanElement>(null);
  const scrollRef = useRef<HTMLDivElement>(null);
  const prefix = useId().replaceAll(":", "");
  useEffect(() => {
    scrollRef.current?.scrollTo?.(0, 0);
    headingRef.current?.focus({ preventScroll: true });
  }, [target.href]);
  useEffect(() => {
    if (!doc) return;
    const hash = new URL(target.href, window.location.origin).hash;
    if (!hash) return;
    const heading = scrollRef.current?.querySelector<HTMLElement>(`[id="${CSS.escape(prefix + "-" + readingFragment(hash))}"]`);
    heading?.scrollIntoView({ block: "start" });
  }, [doc, prefix, target.href]);
  const follow = (event: MouseEvent | KeyboardEvent) => {
    if (event.defaultPrevented || event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return;
    if ("button" in event && event.button !== 0 || "key" in event && event.key !== "Enter") return;
    const anchor = (event.target as Element).closest<HTMLAnchorElement>("a");
    if (!anchor || anchor.hasAttribute("download")) return;
    const href = anchor.getAttribute("data-markdown-reference-runtime-url") || anchor.getAttribute("href");
    if (!href) return;
    if (href.startsWith("#")) {
      const slug = readingFragment(href);
      const heading = scrollRef.current?.querySelector<HTMLElement>(`[id="${CSS.escape(slug.startsWith(prefix + "-") ? slug : prefix + "-" + slug)}"]`);
      if (heading) { event.preventDefault(); event.stopPropagation(); heading.scrollIntoView({ block: "start" }); }
      return;
    }
    const next = readingReference(href, window.location.origin);
    if (next?.vault === target.vault) { event.preventDefault(); event.stopPropagation(); onOpen(next); }
  };
  const toolbar = <div role="group" aria-label="Reference preview tools" className="reading-context-toolbar reading-preview-toolbar">
      <button type="button" className="reading-icon-button" aria-label={canGoBack ? "Previous reference" : "Back to references"} onClick={onBack}><ArrowLeft size={17} aria-hidden /></button>
      <span ref={headingRef} tabIndex={-1} className="reading-context-label">Reference preview</span>
      {!narrow && <button type="button" className="reading-icon-button" aria-label={compare ? "End comparison" : "Compare documents"} aria-pressed={compare} onClick={onCompare}><Columns2 size={15} aria-hidden /></button>}
      <button type="button" className="reading-icon-button" aria-label="Close reference preview" onClick={onClose}><X size={17} aria-hidden /></button>
    </div>;
  const metadata = doc && <>
    <DocumentByline author={documentAuthorName(doc)} updatedAt={target.version ? undefined : doc.updated_at} status={doc.status} historical={Boolean(target.version)} version={target.version} />
    <p className="reading-preview-path">{target.vault} / {doc.path}</p>
    <div className="reading-preview-actions">
      <button type="button" className="reading-keep-button" disabled={!resolved} onClick={() => resolved && !kept && workspace.remember(resolved)}>{kept ? <Check size={14} aria-hidden /> : <Plus size={14} aria-hidden />}{kept ? "Kept in tabs" : "Keep in tabs"}</button>
    </div>
  </>;
  const content = <>
    {!narrow && headerHost ? createPortal(toolbar, headerHost) : toolbar}
    <div ref={scrollRef} className="reading-preview-scroll rail-scroll" onClickCapture={follow} onKeyDownCapture={follow}>
      {isPending ? <LoadingState label="Loading reference" delayMs={200}><div className="space-y-5 p-6"><Skeleton className="h-7 w-3/4" /><Skeleton className="h-4 w-full" /><Skeleton className="h-4 w-5/6" /></div></LoadingState>
        : !doc ? <Alert variant="destructive" className="m-5">{denied ? "This reference is unavailable or you no longer have access." : "The reference could not be loaded."}<Button variant="outline" size="sm" loading={isFetching} onClick={() => void refetch()}>Try again</Button></Alert>
          : <div className="reading-preview-identity">{error && <Alert variant="warning" className="m-4">The reference could not be refreshed. Previous content is shown.<Button variant="ghost" size="sm" onClick={() => void refetch()}>Try again</Button></Alert>}
            <MarkdownRender markdown={doc.content || ""} summary={doc.summary} leadingMetadata={metadata} fallbackTitle={doc.title}
              className="document-reading-flow reading-preview-markdown" headingIdPrefix={`${prefix}-`}
              assetContext={{ mode: "authenticated", vault: target.vault, document: doc.path, commit: doc.current_commit }} /></div>}
    </div>
    {resolved && <footer className="reading-preview-footer"><Link to={resolved.href} onClick={onClose}>Open as main document<ArrowUpRight size={14} aria-hidden /></Link></footer>}
  </>;
  return narrow ? <Dialog open onOpenChange={open => { if (!open) onClose(); }}>
    <DialogContent hideClose className="reading-studio reading-reference-dialog" aria-describedby={undefined} onCloseAutoFocus={event => event.preventDefault()}>
      <DialogTitle className="sr-only">Reference preview</DialogTitle>{content}
    </DialogContent>
  </Dialog> : <aside className="reading-reference-preview" aria-label={doc?.title || "Reference preview"} onKeyDown={event => {
    if (event.key === "Escape" && !event.defaultPrevented) { event.preventDefault(); event.stopPropagation(); onClose(); }
  }}>{content}</aside>;
}
