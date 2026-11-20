import { useCallback, useEffect, useId, useLayoutEffect, useRef, useState, type KeyboardEvent, type MouseEvent, type ReactNode, type RefObject } from "react";
import { createPortal } from "react-dom";
import { ReadingChromeContext, type ReferenceNavigationRequest } from "@/contexts/reading-chrome-context";
import { DocumentIconButton } from "@/components/document-reading-controls";
import { ReaderToolsProvider, useReaderTools } from "@/contexts/reader-tools-context";
import { useReaderPanelLayout } from "@/hooks/use-reader-panel-layout";
import { ReaderPanelWidthMenu } from "@/components/reader-panel-width-menu";
import { DocumentTabs } from "@/components/reading-document-tabs";
import { DocumentFindBar } from "@/components/document-reader-tools";
import { DocumentReferences } from "@/components/document-references";
import { DocumentByline } from "@/components/document-byline";
import { documentAuthorName } from "@/lib/document-author";
import { Link, useLocation, useParams } from "react-router-dom";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { ArrowLeft, ArrowUpRight, Check, Columns2, Plus, X } from "lucide-react";
import { ApiError, getDocument, getRelations } from "@/lib/api";
import { readWithAccessBoundary } from "@/lib/query-access-boundary";
import { DocumentUnavailableState } from "@/components/document-unavailable-state";
import { readingDocumentHref, readingDocumentKey, readingFragment, readingReference, type ReadingDocument } from "@/lib/reading-workspace";
import { useReadingWorkspace } from "@/contexts/reading-workspace-context";
import { useAccessVerification, useCurrentUser } from "@/contexts/current-user-context";
import { MarkdownRender } from "@/components/markdown-render";
import { relationIsInVault } from "@/components/relations/relation-row-utils";
import { Button } from "@/components/ui/button";
import { Alert } from "@/components/ui/alert";
import { LoadingState } from "@/components/ui/loading-state";
import { Skeleton } from "@/components/ui/skeleton";
import { Dialog, DialogContent, DialogTitle } from "@/components/ui/dialog";
import { revealMarkdownTarget } from "@akb/markdown-editor/react";

/** Route-level previews retain their own dialog/guard. Only full reader pages
 * participate in the persistent document workspace. */
export function DocumentReadingWorkspace({ children, enabled }: { children: ReactNode; enabled: boolean }) {
  const workspace = useReadingWorkspace();
  return <ReaderToolsProvider>{enabled && workspace ? <ReadingWorkspace>{children}</ReadingWorkspace> : children}</ReaderToolsProvider>;
}

function ReadingWorkspace({ children }: { children: ReactNode }) {
  const readerTools = useReaderTools();
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
  const { remember, forget, activate, positions, savePosition } = workspace;
  useEffect(() => {
    if (!doc) return;
    remember({ vault: name, path: doc.path, title: doc.title || doc.path, version, href: readingDocumentHref(name, doc.path, location.search) });
    activate(readingDocumentKey({ vault: name, path: doc.path, version }));
  }, [doc, name, version, location.search, remember, activate]);
  useEffect(() => { if (denied) forget(readingDocumentKey({ vault: name, path: docId, version })); }, [denied, docId, name, version, forget]);
  const relationsQuery = useQuery({
    queryKey: ["document-relations", name, doc?.path, user?.user_id, revision, doc?.current_commit],
    queryFn: ({ queryKey, signal }) => readWithAccessBoundary(queryClient, queryKey, () => getRelations(name, doc.path), signal),
    enabled: !!doc?.path && !checking, retry: false,
  });
  const relations = relationsQuery.isError ? [] : (relationsQuery.data?.relations ?? []).filter(row => relationIsInVault(row, name));
  const primaryRef = useRef<HTMLDivElement>(null);
  const frameRef = useRef<HTMLDivElement>(null);
  const [preview, setPreview] = useState<ReadingDocument | null>(null);
  const [previewHistory, setPreviewHistory] = useState<ReadingDocument[]>([]);
  const [sideHistory, setSideHistory] = useState<ReadingDocument[]>([]);
  const [contextMode, setContextMode] = useState<"preview" | null>(null);
  const [detailsOpen, setDetailsOpen] = useState(false);
  const [connectionManagerRequest, setConnectionManagerRequest] = useState(0);
  const connectionManagerOpener = useRef<HTMLButtonElement>(null);
  const [referencesRequest, setReferencesRequest] = useState<ReferenceNavigationRequest>();
  const referencesRequestId = useRef(0);
  const referenceOrigin = useRef<Omit<ReferenceNavigationRequest, "id"> | undefined>(undefined);
  const [referenceMode, setReferenceMode] = useState<"map" | "list">("list");
  const [sideSheetOpen, setSideSheetOpen] = useState(false);
  const sideVisible = Boolean(workspace.companion) && contextMode === null;
  const sizing = useReaderPanelLayout(frameRef, sideVisible);
  const [narrow, setNarrow] = useState(sizing.narrow);
  useLayoutEffect(() => {
    // Preserve a supporting editor and its reference-surface opener on resize.
    if (!detailsOpen) setNarrow(sizing.narrow);
  }, [detailsOpen, sizing.narrow]);
  const editing = ["edit", "diff"].includes(params.get("view") ?? "");
  useEffect(() => {
    if (editing) { setContextMode(null); setSideSheetOpen(false); setDetailsOpen(false); }
  }, [editing]);
  const positionKey = active + (params.get("view") === "raw" ? ":raw" : ":rendered");
  useLayoutEffect(() => {
    if (!doc || editing || !active) return;
    const canvas = primaryRef.current?.querySelector<HTMLElement>("#document-reading-canvas");
    if (!canvas) return;
    const restore = requestAnimationFrame(() => {
      if (location.hash) {
        const heading = primaryRef.current?.querySelector<HTMLElement>(`[id="${CSS.escape(readingFragment(location.hash))}"]`);
        if (heading) { revealMarkdownTarget(heading); heading.scrollIntoView({ block: "start" }); return; }
      }
      canvas.scrollTop = positions.get(positionKey) ?? 0;
    });
    const record = () => { if (canvas.isConnected) savePosition(positionKey, canvas.scrollTop); };
    canvas.addEventListener("scroll", record, { passive: true });
    return () => { cancelAnimationFrame(restore); record(); canvas.removeEventListener("scroll", record); };
    // Successful refreshes keep the current reading position.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [active, editing, positionKey, location.hash, positions, savePosition]);
  const openerRef = useRef<HTMLElement | null>(null);
  const restoreFocusFrame = useRef<number | null>(null);
  useLayoutEffect(() => () => { if (restoreFocusFrame.current !== null) cancelAnimationFrame(restoreFocusFrame.current); restoreFocusFrame.current = null; }, [location.key]);
  const [toolbarHost, setToolbarHost] = useState<HTMLElement | null>(null);
  const [contextHeaderHost, setContextHeaderHost] = useState<HTMLElement | null>(null);
  const sideTriggerRef = useRef<HTMLButtonElement>(null);
  useEffect(() => { setPreview(null); setPreviewHistory([]); setSideSheetOpen(false); setContextMode(null); setDetailsOpen(false); setReferencesRequest(undefined); }, [location.pathname, checking, revision]);
  useEffect(() => { setSideHistory([]); }, [checking, revision, user?.user_id]);
  const lastSideRequest = useRef(workspace.sideRequest);
  useEffect(() => {
    if (lastSideRequest.current === workspace.sideRequest) return;
    lastSideRequest.current = workspace.sideRequest;
    setPreview(null); setPreviewHistory([]); setContextMode(null); setSideSheetOpen(sizing.splitNarrow);
  }, [workspace.sideRequest, sizing.splitNarrow]);
  const showReferences = useCallback(() => {
    setPreview(null); setPreviewHistory([]); setSideSheetOpen(false); setContextMode(null);
    setReferencesRequest({ id: ++referencesRequestId.current, ...referenceOrigin.current });
  }, []);
  const openReference = useCallback((target: ReadingDocument, opener?: HTMLElement) => {
    if (target.vault !== name) return;
    if (opener) {
      openerRef.current = opener;
      const source = opener.closest(".document-references");
      referenceOrigin.current = source ? { href: opener.getAttribute("href") ?? undefined, referenceKey: opener.dataset.referenceKey,
        scrollTop: source.querySelector(".document-reference-body")?.scrollTop } : undefined;
    }
    setContextMode("preview"); setSideSheetOpen(false);
    setPreviewHistory(previous => preview ? [...previous, preview].slice(-20) : []); setPreview(target);
  }, [name, preview]);
  const focusPrimary = () => {
    const current = document.activeElement;
    if (current instanceof HTMLElement && current !== document.body && current.isConnected) return;
    primaryRef.current?.querySelector<HTMLElement>("#document-reading-canvas")?.focus({ preventScroll: true });
  };
  const closePreview = useCallback(() => {
    if (openerRef.current?.closest(".document-references")) { showReferences(); return; }
    setPreview(null); setPreviewHistory([]); setContextMode(null);
    if (restoreFocusFrame.current !== null) cancelAnimationFrame(restoreFocusFrame.current);
    restoreFocusFrame.current = requestAnimationFrame(() => {
      restoreFocusFrame.current = null;
      const opener = openerRef.current, focused = document.activeElement;
      if (focused instanceof HTMLElement && focused !== document.body && focused !== opener && focused.isConnected) return;
      if (opener?.isConnected) opener.focus({ preventScroll: true });
      else primaryRef.current?.querySelector<HTMLElement>("#document-reading-canvas")?.focus({ preventScroll: true });
    });
  }, [showReferences]);
  const followLink = (event: MouseEvent<HTMLElement> | KeyboardEvent<HTMLElement>) => {
    if (event.defaultPrevented || event.ctrlKey || event.metaKey || event.shiftKey || event.altKey || checking || editing) return;
    if ("button" in event && event.button !== 0 || "key" in event && event.key !== "Enter") return;
    const anchor = (event.target as Element).closest<HTMLAnchorElement>("a");
    if (!anchor || anchor.hasAttribute("download")) return;
    const href = anchor.getAttribute("data-markdown-reference-runtime-url") || anchor.getAttribute("href");
    if (!href || href.startsWith("#")) return;
    const target = readingReference(href, window.location.origin);
    if (!target || target.vault !== name) return;
    if (target.path === doc?.path && target.version === version && new URL(target.href, window.location.origin).hash) return;
    event.preventDefault(); event.stopPropagation(); openReference(target, anchor);
  };
  const panelId = useId();
  const dockOpen = !editing && !!doc && !checking && contextMode !== null;
  const rightMode = dockOpen ? contextMode : !editing && !checking && sideVisible ? "side" : null;
  const constrained = rightMode === "side" ? sizing.splitNarrow : narrow;
  const rightVisible = Boolean(rightMode) && !constrained;
  const panelControl = <ReaderPanelWidthMenu split={rightMode === "side"} onPreset={sizing.preset} onReset={sizing.reset} />;
  const references = !editing && doc && <DocumentReferences key={active} vault={name} title={doc.title || ""} relations={relations} mode={referenceMode} onModeChange={setReferenceMode}
    loading={relationsQuery.isPending} error={relationsQuery.isError} historical={Boolean(version)} onRetry={() => void relationsQuery.refetch()} onOpen={openReference}
    manageRef={connectionManagerOpener} onManage={opener => { connectionManagerOpener.current = opener; setConnectionManagerRequest(value => value + 1); }} />;
  const sideControl = !editing && workspace.companion && <DocumentIconButton ref={sideTriggerRef} label="Open side document" aria-expanded={sideVisible && (!sizing.splitNarrow || sideSheetOpen)} onClick={() => {
    setContextMode(null); setPreview(null); setSideSheetOpen(value => sizing.splitNarrow ? !value : false);
    if (!sizing.splitNarrow) requestAnimationFrame(() => frameRef.current?.querySelector<HTMLElement>(".reading-side-document .reading-context-label")?.focus({ preventScroll: true }));
  }}><Columns2 size={16} aria-hidden /></DocumentIconButton>;
  const chrome = { toolbarHost, references, referencesRequest, sideControl, detailsOpen, setDetailsOpen, connectionManagerRequest, connectionManagerOpener };
  return <ReadingChromeContext.Provider value={chrome}>
    <div ref={frameRef} className="reading-studio" data-context={rightVisible ? rightMode : "closed"} data-constrained={constrained} data-resizing={sizing.resizing}
      style={{ "--reading-context-width": `${sizing.width}px` } as React.CSSProperties}>
      <header className="reading-studio-header"><div className="reading-primary-toolbar"><div ref={setToolbarHost} className="reading-toolbar-host" /></div><span className="reading-header-divider" hidden={!rightVisible} aria-hidden />
        <div ref={setContextHeaderHost} className="reading-context-header-host" />
      </header>
      <div className="reading-studio-body">
        <div ref={primaryRef} id={panelId} role="tabpanel" aria-label="Active document" aria-labelledby={active ? `${panelId}-tab-${encodeURIComponent(active)}` : undefined} className="reading-primary" onClickCapture={followLink} onKeyDownCapture={followLink}>
          {!editing && <DocumentFindBar />}
          {!!workspace.restoreErrors && <div className="reading-session-notice" role="status">Some saved documents could not be opened.<Button size="sm" variant="ghost" disabled={workspace.restoring} onClick={workspace.retryRestore}>Retry saved tabs</Button></div>}
          <div className="reading-primary-content" style={{ "--reader-font-size": `${(readerTools?.preferences.fontSize ?? 16) / 16}rem`, "--reader-measure": readerTools?.preferences.width === "compact" ? "36rem" : readerTools?.preferences.width === "wide" ? "64rem" : "42rem" } as React.CSSProperties}>{children}</div>
        </div>
        {rightVisible && <div role="separator" tabIndex={0} className="reading-panel-resizer" aria-label={rightMode === "side" ? "Resize side document" : "Resize context panel"}
          aria-orientation="vertical" aria-valuemin={Math.round(sizing.min)} aria-valuemax={Math.round(sizing.max)} aria-valuenow={Math.round(sizing.width)} aria-valuetext={`${Math.round(sizing.width)} pixels wide`}
          title="Drag or use arrow keys to resize · Enter or double-click to reset" {...sizing.handlers} />}
        {contextMode === "preview" && preview && !checking && <ReferencePreview key={`${name}:${revision}`} target={preview} narrow={narrow} headerHost={contextHeaderHost} panelControl={panelControl} canGoBack={previewHistory.length > 0}
          onBack={() => { if (previewHistory.length) { setPreview(previewHistory.at(-1)!); setPreviewHistory(items => items.slice(0, -1)); } else showReferences(); }} onClose={closePreview} onOpen={openReference} onKeepBeside={workspace.openBeside} />}
        {workspace.companion && !checking && !editing && <ReferencePreview key={`side:${user?.user_id}:${revision}`} target={workspace.companion} narrow={sizing.splitNarrow} headerHost={contextHeaderHost} persistent hidden={!sideVisible} modalOpen={sideSheetOpen && sideVisible}
          panelControl={panelControl} returnFocusRef={sideTriggerRef} canGoBack={sideHistory.length > 0} onBack={() => { const previous = sideHistory.at(-1); if (previous) { setSideHistory(items => items.slice(0, -1)); workspace.openBeside(previous); } }}
          onDismiss={() => { setSideSheetOpen(false); sideTriggerRef.current?.focus({ preventScroll: true }); }}
          onClose={() => { workspace.closeBeside(); setSideHistory([]); setSideSheetOpen(false); requestAnimationFrame(focusPrimary); }}
          onOpen={target => { if (workspace.companion) setSideHistory(items => [...items, workspace.companion!].slice(-20)); workspace.openBeside(target); }} onKeepBeside={workspace.openBeside} />}
      </div>
      <div className="reading-workspace-bottom">{workspace.restoring && <span role="status" className="reading-restore-status">Restoring tabs…</span>}<DocumentTabs active={active} panelId={panelId} /></div>

    </div>
  </ReadingChromeContext.Provider>;
}

function ReferencePreview({ target, narrow, headerHost, panelControl, canGoBack, onBack, onClose, onOpen, onKeepBeside, persistent = false, hidden = false, modalOpen = true, onDismiss, returnFocusRef }: {
  target: ReadingDocument; narrow: boolean; headerHost: HTMLElement | null; panelControl: ReactNode; canGoBack: boolean; onBack: () => void; onClose: () => void;
  onOpen: (target: ReadingDocument, opener?: HTMLElement) => void; onKeepBeside: (target: ReadingDocument) => void;
  persistent?: boolean; hidden?: boolean; modalOpen?: boolean; onDismiss?: () => void;
  returnFocusRef?: RefObject<HTMLButtonElement | null>;
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
  const [scrollElement, setScrollElement] = useState<HTMLDivElement | null>(null);
  const attachScroll = useCallback((element: HTMLDivElement | null) => { scrollRef.current = element; setScrollElement(element); }, []);
  const prefix = useId().replaceAll(":", "");
  const wasNarrow = useRef(narrow);
  useLayoutEffect(() => {
    if (persistent && narrow && !wasNarrow.current && !modalOpen && document.activeElement === document.body) returnFocusRef?.current?.focus({ preventScroll: true });
    wasNarrow.current = narrow;
  }, [persistent, narrow, modalOpen, returnFocusRef]);
  const positionKey = readingDocumentKey(target) + (persistent ? ":side" : ":preview");
  const loadedPath = doc?.path;
  const { positions, savePosition } = workspace;
  useLayoutEffect(() => {
    if (!scrollElement || !loadedPath || hidden || (narrow && !modalOpen)) return;
    const frame = requestAnimationFrame(() => {
      const hash = new URL(target.href, window.location.origin).hash;
      const heading = hash ? scrollElement.querySelector<HTMLElement>(`[id="${CSS.escape(prefix + "-" + readingFragment(hash))}"]`) : null;
      if (heading) { revealMarkdownTarget(heading); heading.scrollIntoView({ block: "start" }); }
      else scrollElement.scrollTop = positions.get(positionKey) ?? 0;
    });
    const record = () => { if (scrollElement.isConnected && !scrollElement.closest("[hidden]")) savePosition(positionKey, scrollElement.scrollTop); };
    scrollElement.addEventListener("scroll", record, { passive: true });
    return () => { cancelAnimationFrame(frame); record(); scrollElement.removeEventListener("scroll", record); };
  }, [scrollElement, loadedPath, positionKey, positions, savePosition, narrow, modalOpen, hidden, target.href, prefix]);
  useEffect(() => {
    headingRef.current?.focus({ preventScroll: true });
  }, [target.href]);
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
      if (heading) { event.preventDefault(); event.stopPropagation(); revealMarkdownTarget(heading); heading.scrollIntoView({ block: "start" }); }
      return;
    }
    const next = readingReference(href, window.location.origin);
    if (next?.vault === target.vault) { event.preventDefault(); event.stopPropagation(); onOpen(next); }
  };
  const toolbar = <div role="group" aria-label={persistent ? "Side document tools" : "Reference preview tools"} className="reading-context-toolbar reading-preview-toolbar">
      <button type="button" className="reading-icon-button" aria-label={persistent ? "Previous side document" : canGoBack ? "Previous reference" : "Back to references"} disabled={persistent && !canGoBack} onClick={onBack}><ArrowLeft size={17} aria-hidden /></button>
      <span ref={headingRef} tabIndex={-1} className="reading-context-label">{persistent ? "Side by side" : "Reference preview"}</span>
      {!persistent && <button type="button" className="reading-icon-button" aria-label="Keep side by side" title="Keep side by side" disabled={!resolved} onClick={() => {
        if (resolved) { savePosition(readingDocumentKey(resolved) + ":side", scrollRef.current?.scrollTop ?? 0); onKeepBeside(resolved); }
      }}><Columns2 size={15} aria-hidden /></button>}
      {!narrow && panelControl}
      <button type="button" className="reading-icon-button" aria-label={persistent ? "Close side document" : "Close reference preview"} onClick={onClose}><X size={17} aria-hidden /></button>
    </div>;
  const metadata = doc && <>
    <DocumentByline author={documentAuthorName(doc)} updatedAt={target.version ? undefined : doc.updated_at} status={doc.status} historical={Boolean(target.version)} version={target.version} />
    <p className="reading-preview-path">{target.vault} / {doc.path}</p>
    {!persistent && <div className="reading-preview-actions">
      <button type="button" className="reading-keep-button" disabled={!resolved} onClick={() => resolved && !kept && workspace.remember(resolved)}>{kept ? <Check size={14} aria-hidden /> : <Plus size={14} aria-hidden />}{kept ? "Kept in tabs" : "Keep in tabs"}</button>
    </div>}
  </>;
  const content = <>
    {!hidden && (!narrow && headerHost ? createPortal(toolbar, headerHost) : toolbar)}
    <div ref={attachScroll} className="reading-preview-scroll rail-scroll" onClickCapture={follow} onKeyDownCapture={follow}>
      {isPending ? <LoadingState label="Loading reference" delayMs={200}><div className="space-y-5 p-6"><Skeleton className="h-7 w-3/4" /><Skeleton className="h-4 w-full" /><Skeleton className="h-4 w-5/6" /></div></LoadingState>
        : !doc ? <div className="p-4"><DocumentUnavailableState compact error={error} retrying={isFetching} onRetry={() => void refetch()} retryLabel="Try again" /></div>
          : <div className="reading-preview-identity">{error && <Alert variant="warning" className="m-4">The reference could not be refreshed. Previous content is shown.<Button variant="ghost" size="sm" onClick={() => void refetch()}>Try again</Button></Alert>}
            <MarkdownRender markdown={doc.content || ""} summary={doc.summary} leadingMetadata={metadata} documentTitle={doc.title || "Untitled document"}
              className="document-reading-flow reading-preview-markdown" headingIdPrefix={`${prefix}-`}
              assetContext={{ mode: "authenticated", vault: target.vault, document: doc.path, commit: doc.current_commit }} /></div>}
    </div>
    {resolved && <footer className="reading-preview-footer"><Link to={resolved.href} onClick={event => {
      if (!event.defaultPrevented && event.button === 0 && !event.ctrlKey && !event.metaKey && !event.shiftKey && !event.altKey) onClose();
    }}>Open as main document<ArrowUpRight size={14} aria-hidden /></Link></footer>}
  </>;
  return narrow ? <Dialog open={modalOpen && !hidden} onOpenChange={open => { if (!open) (onDismiss ?? onClose)(); }}>
    <DialogContent hideClose className="reading-studio reading-reference-dialog" aria-describedby={undefined} onCloseAutoFocus={event => { event.preventDefault(); if (persistent) returnFocusRef?.current?.focus({ preventScroll: true }); }}>
      <DialogTitle className="sr-only">{persistent ? "Side document" : "Reference preview"}</DialogTitle>{content}
    </DialogContent>
  </Dialog> : <aside hidden={hidden} className={`reading-reference-preview${persistent ? " reading-side-document" : ""}`} aria-label={persistent ? `Side document: ${doc?.title || "Loading"}` : doc?.title || "Reference preview"} onKeyDown={event => {
    if (event.key === "Escape" && !event.defaultPrevented) { event.preventDefault(); event.stopPropagation(); (onDismiss ?? onClose)(); }
  }}>{content}</aside>;
}
