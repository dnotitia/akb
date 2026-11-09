import {
  MarkdownLocaleProvider,
  MarkdownViewer,
  type MarkdownViewerReadingOptions,
  revealMarkdownTarget,
} from "@akb/markdown-editor/react";
import { useCallback, useEffect, useMemo, useRef, useState, type ReactNode, type CSSProperties } from "react";
import { useReaderTools } from "@/contexts/reader-tools-context";
import { useReaderPreferences } from "@/lib/reader-preferences";
import { DocumentCodeToolbar, DocumentImageOpenButton, ReaderImageZoom } from "@/components/document-reader-tools";
import { parseMarkdown, serializeMarkdown } from "@akb/markdown-editor";
import { ChevronDown, ChevronRight } from "lucide-react";
import { ReaderMermaid } from "@/components/reader-mermaid";
import { Popover, PopoverAnchor, PopoverContent } from "@/components/ui/popover";
import { Button } from "@/components/ui/button";
import { useReaderSectionControls } from "@/hooks/use-reader-section-controls";
import "katex/dist/katex.min.css";
import {
  createAkbMarkdownAdapters,
  createAkbMarkdownPublicationTargetResolver,
} from "@/lib/markdown-adapters";
import { parseHeadings, stripFrontmatter } from "@/lib/markdown";
import { cn } from "@/lib/utils";
import { DocumentSummary } from "@/components/document-summary";

export type AssetContext =
  | {
      mode: "authenticated";
      vault: string;
      document?: string;
      commit?: string;
    }
  | { mode: "publication"; slug: string };

const AKB_MARKDOWN_VIEWER_IMAGE_OPTIONS = {
  referrerPolicy: "no-referrer",
} as const;
const AKB_MARKDOWN_VIEWER_TABLE_LAYOUT = {
  className: "w-max min-w-full",
  wrapperClassName:
    "akb-md-table my-5 overflow-x-auto rounded-[var(--radius-lg)] border border-border",
} as const;

export interface MarkdownRenderProps {
  markdown: string;
  className?: string;
  /** Read-only document metadata; never part of Markdown or its anchor sequence. */
  summary?: string | null;
  assetContext?: AssetContext;
  /** Embedded readers need unique anchors and headings below their section. */
  headingIdPrefix?: string;
  headingLevelOffset?: number;
  leadingMetadata?: ReactNode;
  /** Canonical document identity; authored headings remain part of the body. */
  documentTitle?: string;
  documentTitleLevel?: 1 | 2;
  /** Named read-only document surface for embedded previews. */
  contentLabel?: string;
  searchable?: boolean;
  readingTools?: boolean;
}

export function MarkdownRender({
  markdown,
  className,
  summary,
  assetContext,
  headingIdPrefix = "",
  headingLevelOffset = 1,
  leadingMetadata,
  documentTitle,
  documentTitleLevel = 2,
  contentLabel,
  searchable = false,
  readingTools = true,
}: MarkdownRenderProps) {
  const body = useMemo(() => stripFrontmatter(markdown || ""), [markdown]);
  const structure = useMemo(() => {
    if (!documentTitle) return null;
    const blocks = parseMarkdown(body).content || [];
    const first = blocks[0];
    const paragraph = first?.type === "heading" ? blocks[1] : first;
    const textOf = (node: typeof first): string => node?.text ?? node?.content?.map(textOf).join("") ?? "";
    const normalize = (text: string) => text.normalize("NFC").trim().replace(/\s+/g, " ");
    const headingLevels: number[] = [];
    const visit = (node: typeof first) => {
      if (node.type === "heading") headingLevels.push(Number(node.attrs?.level ?? 1));
      node.content?.forEach(visit);
    };
    blocks.forEach(visit);
    return {
      titleMatches: first?.type === "heading" && first.attrs?.level === 1
        && normalize(textOf(first)) === normalize(documentTitle),
      opening: paragraph?.type === "paragraph" ? textOf(paragraph) : "",
      headingLevels,
    };
  }, [body, documentTitle]);
  const usesLeadingTitle = Boolean(structure?.titleMatches);
  const tools = useReaderTools();
  const { preferences } = useReaderPreferences();
  const [codeState, setCodeState] = useState<{ body: string; overrides: Record<number, boolean> }>({ body, overrides: {} });
  const overrides = useMemo(() => codeState.body === body ? codeState.overrides : {}, [body, codeState]);
  const [sectionState, setSectionState] = useState<{ body: string; collapsed: number[] }>({ body, collapsed: [] });
  const rootRef = useRef<HTMLDivElement>(null);
  const sectionPositions = useReaderSectionControls(rootRef, body);
  const collapsedSections = useMemo(() => sectionState.body === body
    ? sectionState.collapsed.filter(index => !usesLeadingTitle || index !== 0) : [], [body, sectionState, usesLeadingTitle]);
  const renderSectionToggle = useCallback((section: Parameters<NonNullable<MarkdownViewerReadingOptions["renderSectionToggle"]>>[0]) => usesLeadingTitle && section.index === 0 ? null : <button type="button" className="reader-section-toggle" style={sectionPositions[section.index]} aria-label={`${section.collapsed ? "Expand" : "Collapse"} section: ${section.title}`} aria-expanded={!section.collapsed} title={section.collapsed ? "Expand section" : "Collapse section"} onClick={() => setSectionState(previous => {
    const before = previous.body === body ? previous.collapsed : [];
    return { body, collapsed: section.collapsed ? before.filter(index => index !== section.index) : [...before, section.index] };
  })}>{section.collapsed ? <ChevronRight size={15} aria-hidden /> : <ChevronDown size={15} aria-hidden />}</button>, [body, sectionPositions, usesLeadingTitle]);
  const onRevealSections = useCallback((indices: readonly number[]) => setSectionState(previous => {
    if (previous.body !== body || !previous.collapsed.some(index => indices.includes(index))) return previous;
    return { body, collapsed: previous.collapsed.filter(index => !indices.includes(index)) };
  }), [body]);
  const report = tools?.report;
  const findQuery = searchable && tools?.findOpen ? tools.query : "";
  const current = tools?.current ?? 0;
  const zoomOpener = useRef<HTMLElement | null>(null);
  const [zoom, setZoom] = useState<{ src: string; alt: string } | null>(null);
  useEffect(() => { setZoom(null); }, [body]);
  const openImage = (event: React.MouseEvent | React.KeyboardEvent) => {
    if (event.defaultPrevented || event.metaKey || event.ctrlKey || event.altKey || event.shiftKey || ("button" in event && event.button !== 0) || ("key" in event && !["Enter", " "].includes(event.key))) return;
    const image = event.target instanceof HTMLImageElement ? event.target : null;
    if (!readingTools || !image || image.closest("a") || !image.complete || !image.naturalWidth) return;
    event.preventDefault(); event.stopPropagation();
    const index = Array.from(rootRef.current?.querySelectorAll("img[data-markdown-image], img[data-markdown-target]") ?? []).indexOf(image);
    zoomOpener.current = rootRef.current?.querySelector<HTMLElement>(`button[data-reader-image-index="${index}"]`) ?? image.closest('[data-diagram-mode]')?.querySelector<HTMLElement>('[data-reader-diagram-enlarge]') ?? null;
    setZoom({ src: image.currentSrc || image.src, alt: image.alt });
  };
  const enlarge = useCallback((image: { src: string; alt: string }, opener: HTMLElement) => { zoomOpener.current = opener; setZoom(image); }, []);
  const renderCodeToolbar = useCallback((block: Parameters<NonNullable<MarkdownViewerReadingOptions["renderCodeToolbar"]>>[0]) => {
    const onToggle = () => setCodeState(previous => ({ body, overrides: { ...(previous.body === body ? previous.overrides : {}), [block.index]: !block.collapsed } }));
    return block.language.toLowerCase() === "mermaid" ? <ReaderMermaid key={`${body}:${block.index}`} block={block} onToggle={onToggle} onEnlarge={enlarge} /> : <DocumentCodeToolbar key={`${body}:${block.index}`} block={block} onToggle={onToggle} />;
  }, [body, enlarge]);
  const renderImageToolbar = useCallback(({ index, alt }: { index: number; alt: string }) => <DocumentImageOpenButton index={index} alt={alt} rootRef={rootRef} onOpen={enlarge} />, [enlarge]);
  const reading = useMemo<MarkdownViewerReadingOptions>(() => ({ renderCodeToolbar, renderImageToolbar, renderSectionToggle, collapsedSections, onRevealSections, collapseCodeBlocks: preferences.foldCode,
    collapsedCodeBlocks: Object.keys(overrides).filter(index => overrides[Number(index)]).map(Number),
    expandedCodeBlocks: Object.keys(overrides).filter(index => !overrides[Number(index)]).map(Number),
    search: { query: findQuery, current }, onSearchResults: searchable ? report : undefined,
  }), [renderCodeToolbar, renderImageToolbar, renderSectionToggle, collapsedSections, onRevealSections, preferences.foldCode, overrides, findQuery, current, searchable, report]);
  const definitions = useMemo(() => {
    const result = new Map<string, string>();
    if (readingTools && body.includes("[^")) for (const node of parseMarkdown(body).content ?? []) {
      if (node.type === "footnoteDefinition") {
        const label = String(node.attrs?.label ?? "").normalize("NFC").toLocaleLowerCase();
        if (!result.has(label)) result.set(label, serializeMarkdown({ type: "doc", content: node.content }));
      }
    }
    return result;
  }, [body, readingTools]);
  const [footnote, setFootnote] = useState<{ body: string; source: string; number: string; opener: HTMLAnchorElement; peek: boolean } | null>(null);
  const note = footnote?.body === body ? footnote : null;
  const footnoteContent = useRef<HTMLDivElement>(null);
  const lastFootnote = useRef<typeof footnote>(null);
  const skipFootnoteReturn = useRef(false);
  const footnoteTimer = useRef<ReturnType<typeof setTimeout> | undefined>(undefined);
  useEffect(() => () => clearTimeout(footnoteTimer.current), []);
  const noteAnchor = useMemo(() => ({ current: { getBoundingClientRect: () => note?.opener.getBoundingClientRect() ?? new DOMRect() } }), [note]);
  const showFootnote = (target: EventTarget | null, peek: boolean) => {
    const opener = target instanceof Element ? target.closest<HTMLAnchorElement>("a[data-footnote-link][href]") : null;
    if (!opener || !rootRef.current?.contains(opener)) return false;
    const source = definitions.get((opener.dataset.footnoteLink || "").normalize("NFC").toLocaleLowerCase());
    if (source === undefined) return false;
    clearTimeout(footnoteTimer.current); const next = { body, source, number: opener.dataset.footnoteNumber || "", opener, peek };
    lastFootnote.current = next; skipFootnoteReturn.current = false; setFootnote(next);
    if (!peek) requestAnimationFrame(() => footnoteContent.current?.focus({ preventScroll: true }));
    return true;
  };
  const leaveFootnote = () => {
    clearTimeout(footnoteTimer.current);
    footnoteTimer.current = setTimeout(() => setFootnote(previous => previous?.peek && document.activeElement !== previous.opener && !footnoteContent.current?.contains(document.activeElement) ? null : previous), 180);
  };
  const authenticated =
    assetContext?.mode === "authenticated" ? assetContext : undefined;
  const authenticatedVault = authenticated?.vault;
  const authenticatedDocument = authenticated?.document;
  const authenticatedCommit = authenticated?.commit;
  const adapters = useMemo(
    () =>
      authenticatedVault
        ? createAkbMarkdownAdapters({
            vault: authenticatedVault,
            document: authenticatedDocument,
            commit: authenticatedCommit,
          })
        : undefined,
    [authenticatedCommit, authenticatedDocument, authenticatedVault],
  );
  const publicationSlug =
    assetContext?.mode === "publication" ? assetContext.slug : undefined;
  const publicationResolver = useMemo(
    () =>
      publicationSlug
        ? createAkbMarkdownPublicationTargetResolver(publicationSlug)
        : undefined,
    [publicationSlug],
  );
  const resolver = publicationResolver ?? adapters?.targetResolver;
  const headingOptions = useMemo(() => {
    const headings = parseHeadings(body);
    const ancestors: { level: number; index: number }[] = [];
    const levels = structure?.headingLevels.map((sourceLevel, index) => {
      while (ancestors.length && ancestors.at(-1)!.level >= sourceLevel) ancestors.pop();
      const titleAncestor = usesLeadingTitle && ancestors[0]?.index === 0;
      const level = usesLeadingTitle && index === 0 ? documentTitleLevel
        : Math.min(6, documentTitleLevel + 1 + ancestors.length - (titleAncestor ? 1 : 0));
      ancestors.push({ level: sourceLevel, index });
      return level;
    });
    return {
      levelOffset: headingLevelOffset,
      levels,
      ids: headings.map((heading) => `${headingIdPrefix}${heading.slug}`),
    };
  }, [body, headingIdPrefix, headingLevelOffset, usesLeadingTitle, structure, documentTitleLevel]);
  const summaryRepeatsOpening = Boolean(structure?.opening && summary?.trim().replace(/\s+/g, " ") === structure.opening.trim().replace(/\s+/g, " "));
  const Title = documentTitleLevel === 1 ? "h1" : "h2";
  const separateTitle = Boolean(documentTitle && !usesLeadingTitle);
  const adornment = documentTitle || leadingMetadata || summary?.trim() ? <>
    {separateTitle && <Title className="document-reading-title">{documentTitle}</Title>}
    {leadingMetadata}
    {summary?.trim() && !summaryRepeatsOpening && <DocumentSummary key={summary} summary={summary} />}
  </> : undefined;

  return (
    <MarkdownLocaleProvider locale="en"><div ref={rootRef} className="reader-render" data-table-fit={preferences.tableFit}
      data-document-title-layout={documentTitle ? usesLeadingTitle ? "heading" : "separate" : undefined}
      onClickCapture={event => { if (!event.ctrlKey && !event.metaKey && !event.altKey && !event.shiftKey && !event.button && showFootnote(event.target, false)) { event.preventDefault(); event.stopPropagation(); } else openImage(event); }} onKeyDownCapture={openImage}
      onPointerOverCapture={event => { if (event.pointerType === "mouse" && event.target instanceof Element && event.target.closest("a[data-footnote-link][href]")) { const target = event.target; clearTimeout(footnoteTimer.current); footnoteTimer.current = setTimeout(() => showFootnote(target, true), 250); } }} onPointerOutCapture={leaveFootnote}>
      <MarkdownViewer
        reading={readingTools ? reading : undefined}
        style={{ "--document-reading-measure": preferences.width === "compact" ? "36rem" : preferences.width === "wide" ? "64rem" : "var(--reader-standard-measure, 52rem)", "--reader-font-size": `${preferences.fontSize / 16}rem` } as CSSProperties}
        markdown={body}
        contentAttributes={contentLabel ? { role: "document", "aria-label": contentLabel } : undefined}
        beforeContent={separateTitle ? adornment : undefined}
        afterLeadingHeading={separateTitle ? undefined : adornment}
        className={cn(
          "akb-md min-w-0 text-[15px] text-foreground prose dark:prose-invert !max-w-none",
          className,
        )}
        adapters={{
          targetResolver: resolver,
          reference: adapters?.reference,
        }}
        resolverContext={authenticated ? {
          vault: authenticatedVault,
          document: authenticatedDocument,
          commit: authenticatedCommit,
        } : {}}
        headings={headingOptions}
        image={AKB_MARKDOWN_VIEWER_IMAGE_OPTIONS}
        tableLayout={AKB_MARKDOWN_VIEWER_TABLE_LAYOUT}
      />
    </div><ReaderImageZoom image={zoom} onClose={() => setZoom(null)} returnFocusRef={zoomOpener} />
    <Popover open={Boolean(note)} onOpenChange={open => { if (!open) setFootnote(null); }}><PopoverAnchor virtualRef={noteAnchor} /><PopoverContent ref={footnoteContent} tabIndex={-1} align="start" className="reader-footnote-preview" aria-label={`Footnote ${note?.number}`} onOpenAutoFocus={event => { if (note?.peek) event.preventDefault(); }} onCloseAutoFocus={event => { event.preventDefault(); if (!skipFootnoteReturn.current && lastFootnote.current?.opener.isConnected && !lastFootnote.current.peek) lastFootnote.current.opener.focus({ preventScroll: true }); }} onPointerEnter={() => clearTimeout(footnoteTimer.current)} onPointerLeave={leaveFootnote}>
      <p className="text-xs font-medium text-foreground-muted">Footnote {note?.number}</p>
      {note && <MarkdownRender markdown={note.source} readingTools={false} assetContext={assetContext} headingIdPrefix={`${headingIdPrefix}footnote-`} className="reader-footnote-body" />}
      <Button variant="ghost" size="sm" onClick={() => {
        const id = note?.opener.getAttribute("href")?.slice(1), target = [...(rootRef.current?.querySelectorAll<HTMLElement>("[id]") ?? [])].find(element => element.id === id);
        skipFootnoteReturn.current = true; setFootnote(null); if (target) { revealMarkdownTarget(target); target.scrollIntoView({ block: "center" }); target.focus({ preventScroll: true }); }
      }}>Go to footnote</Button>
    </PopoverContent></Popover>
    </MarkdownLocaleProvider>
  );
}

export default MarkdownRender;
