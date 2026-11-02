import {
  MarkdownLocaleProvider,
  MarkdownViewer,
  type MarkdownViewerReadingOptions,
} from "@akb/markdown-editor/react";
import { useCallback, useEffect, useMemo, useRef, useState, type ReactNode, type CSSProperties } from "react";
import { useReaderTools } from "@/contexts/reader-tools-context";
import { DEFAULT_READER_PREFERENCES } from "@/lib/reader-preferences";
import { DocumentCodeToolbar, DocumentImageOpenButton, ReaderImageZoom } from "@/components/document-reader-tools";
import { parseMarkdown } from "@akb/markdown-editor";
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
  fallbackTitle?: string;
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
  fallbackTitle,
  searchable = false,
  readingTools = true,
}: MarkdownRenderProps) {
  const body = useMemo(() => stripFrontmatter(markdown || ""), [markdown]);
  const tools = useReaderTools();
  const preferences = tools?.preferences ?? DEFAULT_READER_PREFERENCES;
  const [codeState, setCodeState] = useState<{ body: string; overrides: Record<number, boolean> }>({ body, overrides: {} });
  const overrides = useMemo(() => codeState.body === body ? codeState.overrides : {}, [body, codeState]);
  const renderCodeToolbar = useCallback((block: Parameters<NonNullable<MarkdownViewerReadingOptions["renderCodeToolbar"]>>[0]) =>
    <DocumentCodeToolbar key={`${body}:${block.index}`} block={block} onToggle={() => setCodeState(previous => ({ body, overrides: { ...(previous.body === body ? previous.overrides : {}), [block.index]: !block.collapsed } }))} />, [body]);
  const report = tools?.report;
  const findQuery = searchable && tools?.findOpen ? tools.query : "";
  const current = tools?.current ?? 0;
  const rootRef = useRef<HTMLDivElement>(null);
  const zoomOpener = useRef<HTMLElement | null>(null);
  const [zoom, setZoom] = useState<{ src: string; alt: string } | null>(null);
  useEffect(() => { setZoom(null); }, [body]);
  const openImage = (event: React.MouseEvent | React.KeyboardEvent) => {
    if (event.defaultPrevented || event.metaKey || event.ctrlKey || event.altKey || event.shiftKey || ("button" in event && event.button !== 0) || ("key" in event && !["Enter", " "].includes(event.key))) return;
    const image = event.target instanceof HTMLImageElement ? event.target : null;
    if (!readingTools || !image || image.closest("a") || !image.complete || !image.naturalWidth) return;
    event.preventDefault(); event.stopPropagation();
    const index = Array.from(rootRef.current?.querySelectorAll("img[data-markdown-image], img[data-markdown-target]") ?? []).indexOf(image);
    zoomOpener.current = rootRef.current?.querySelector<HTMLElement>(`button[data-reader-image-index="${index}"]`) ?? null;
    setZoom({ src: image.currentSrc || image.src, alt: image.alt });
  };
  const enlarge = useCallback((image: { src: string; alt: string }, opener: HTMLElement) => { zoomOpener.current = opener; setZoom(image); }, []);
  const renderImageToolbar = useCallback(({ index, alt }: { index: number; alt: string }) => <DocumentImageOpenButton index={index} alt={alt} rootRef={rootRef} onOpen={enlarge} />, [enlarge]);
  const reading = useMemo<MarkdownViewerReadingOptions>(() => ({ renderCodeToolbar, renderImageToolbar, collapseCodeBlocks: preferences.foldCode,
    collapsedCodeBlocks: Object.keys(overrides).filter(index => overrides[Number(index)]).map(Number),
    expandedCodeBlocks: Object.keys(overrides).filter(index => !overrides[Number(index)]).map(Number),
    search: { query: findQuery, current }, onSearchResults: searchable ? report : undefined,
  }), [renderCodeToolbar, renderImageToolbar, preferences.foldCode, overrides, findQuery, current, searchable, report]);
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
  const headingOptions = useMemo(
    () => ({
      levelOffset: headingLevelOffset,
      ids: parseHeadings(body).map((heading) => `${headingIdPrefix}${heading.slug}`),
    }),
    [body, headingIdPrefix, headingLevelOffset],
  );
  // A leading authored heading remains the title and keeps its original ID.
  // Documents starting with another block receive a UI-only identity instead.
  const structure = useMemo(() => {
    if (!fallbackTitle) return null;
    const blocks = parseMarkdown(body).content || [];
    const first = blocks[0];
    const paragraph = first?.type === "heading" ? blocks[1] : first;
    const textOf = (node: typeof first): string => node?.text ?? node?.content?.map(textOf).join("") ?? "";
    return { heading: first?.type === "heading", opening: paragraph?.type === "paragraph" ? textOf(paragraph) : "" };
  }, [body, fallbackTitle]);
  const summaryRepeatsOpening = Boolean(structure?.opening && summary?.trim().replace(/\s+/g, " ") === structure.opening.trim().replace(/\s+/g, " "));
  const adornment = leadingMetadata || summary?.trim() ? <>
    {!structure?.heading && fallbackTitle && <h2 className="document-fallback-title">{fallbackTitle}</h2>}
    {leadingMetadata}
    {summary?.trim() && !summaryRepeatsOpening && <DocumentSummary key={summary} summary={summary} />}
  </> : undefined;

  return (
    <MarkdownLocaleProvider locale="en"><div ref={rootRef} className="reader-render" data-table-fit={preferences.tableFit} onClickCapture={openImage} onKeyDownCapture={openImage}>
      <MarkdownViewer
        reading={readingTools ? reading : undefined}
        style={{ "--document-reading-measure": preferences.width === "compact" ? "36rem" : preferences.width === "wide" ? "64rem" : className?.includes("document-reading-flow") && tools ? "42rem" : "52rem", "--reader-font-size": `${preferences.fontSize / 16}rem` } as CSSProperties}
        markdown={body}
        afterLeadingHeading={adornment}
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
    </div><ReaderImageZoom image={zoom} onClose={() => setZoom(null)} returnFocusRef={zoomOpener} /></MarkdownLocaleProvider>
  );
}

export default MarkdownRender;
