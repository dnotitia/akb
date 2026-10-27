import {
  MarkdownLocaleProvider,
  MarkdownViewer,
} from "@akb/markdown-editor/react";
import { useMemo, type ReactNode } from "react";
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
}: MarkdownRenderProps) {
  const body = useMemo(() => stripFrontmatter(markdown || ""), [markdown]);
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
    <MarkdownLocaleProvider locale="en">
      <MarkdownViewer
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
    </MarkdownLocaleProvider>
  );
}

export default MarkdownRender;
