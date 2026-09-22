import {
  MarkdownViewer,
} from "@akb/markdown-editor/react";
import { useMemo } from "react";
import "katex/dist/katex.min.css";
import {
  createAkbMarkdownAdapters,
  createAkbMarkdownPublicationTargetResolver,
} from "@/lib/markdown-adapters";
import { parseHeadings, stripFrontmatter } from "@/lib/markdown";
import { cn } from "@/lib/utils";

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
  ariaLabel: "Scrollable table",
} as const;

export interface MarkdownRenderProps {
  markdown: string;
  className?: string;
  assetContext?: AssetContext;
}

export function MarkdownRender({
  markdown,
  className,
  assetContext,
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
      levelOffset: 1,
      ids: parseHeadings(body).map((heading) => heading.slug),
    }),
    [body],
  );

  return (
    <MarkdownViewer
      markdown={body}
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
  );
}

export default MarkdownRender;
