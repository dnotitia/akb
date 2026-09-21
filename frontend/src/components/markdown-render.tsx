import {
  MarkdownSurface,
  useMarkdownEditor,
  useMarkdownTargetResolutions,
} from "@akb/markdown-editor/react";
import type { MarkdownTargetResolver } from "@akb/markdown-editor";
import { useEffect, useLayoutEffect, useMemo, useRef } from "react";
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

function normalizeViewerHeadings(root: HTMLElement, markdown: string): void {
  const headings = parseHeadings(markdown);
  root
    .querySelectorAll<HTMLElement>("h1, h2, h3, h4, h5, h6")
    .forEach((heading, index) => {
      const source = headings[index];
      const level = source
        ? Math.min(source.level + 1, 6)
        : Math.min(Number(heading.tagName.slice(1)) + 1, 6);
      const tagName = `h${level}`;
      if (heading.tagName.toLowerCase() !== tagName) {
        const replacement = window.document.createElement(tagName);
        [...heading.attributes].forEach((attribute) =>
          replacement.setAttribute(attribute.name, attribute.value),
        );
        replacement.innerHTML = heading.innerHTML;
        heading.replaceWith(replacement);
        heading = replacement;
      }
      if (source?.slug) heading.id = source.slug;
    });
}

function useViewerLayout(
  rootRef: React.RefObject<HTMLDivElement | null>,
  editor: ReturnType<typeof useMarkdownEditor>,
  markdown: string,
): void {
  useLayoutEffect(() => {
    const host = rootRef.current;
    const root = editor?.view.dom as HTMLElement | undefined;
    if (!host || !root) return;

    const apply = () => {
      normalizeViewerHeadings(root, markdown);
      root.querySelectorAll<HTMLImageElement>("img").forEach((image) => {
        image.setAttribute("referrerpolicy", "no-referrer");
      });
      root.querySelectorAll<HTMLTableElement>("table").forEach((table) => {
        table.classList.add("w-max", "min-w-full");
        if (table.parentElement?.classList.contains("akb-md-table")) return;
        const wrapper = window.document.createElement("div");
        wrapper.className =
          "akb-md-table my-5 overflow-x-auto rounded-[var(--radius-lg)] border border-border";
        wrapper.tabIndex = 0;
        wrapper.setAttribute("role", "region");
        wrapper.setAttribute("aria-label", "Scrollable table");
        table.replaceWith(wrapper);
        wrapper.append(table);
      });
      root.querySelectorAll<HTMLElement>("pre").forEach((block) => {
        block.tabIndex = 0;
        block.setAttribute("role", "region");
        block.setAttribute("aria-label", "Scrollable code block");
      });
    };

    apply();
    const observer =
      typeof MutationObserver === "undefined"
        ? null
        : new MutationObserver(apply);
    observer?.observe(host, { childList: true, subtree: true });
    return () => observer?.disconnect();
  }, [editor, markdown, rootRef]);
}

export interface MarkdownRenderProps {
  markdown: string;
  className?: string;
  assetContext?: AssetContext;
}

function CommonMarkdownViewer({
  markdown,
  className,
  resolver,
  resolverContext,
}: {
  markdown: string;
  className?: string;
  resolver?: MarkdownTargetResolver;
  resolverContext?: { vault?: string; document?: string; commit?: string };
}) {
  const rootRef = useRef<HTMLDivElement>(null);
  const editor = useMarkdownEditor({
    initialMarkdown: markdown,
    profile: "preserve",
    editable: false,
  });
  const resolutions = useMarkdownTargetResolutions(
    markdown,
    resolver,
    resolverContext,
  );

  useEffect(() => {
    if (!editor || editor.getMarkdown() === markdown) return;
    editor.commands.setContent(markdown, { contentType: "markdown" });
  }, [editor, markdown]);

  useViewerLayout(rootRef, editor, markdown);

  return (
    <div
      ref={rootRef}
      className={cn(
        "akb-md min-w-0 text-[15px] text-foreground prose dark:prose-invert !max-w-none",
        className,
      )}
    >
      <MarkdownSurface
        editor={editor}
        editable={false}
        resolutions={resolutions}
        resolvingTargets={Boolean(resolver)}
      />
    </div>
  );
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

  return (
    <CommonMarkdownViewer
      markdown={body}
      className={className}
      resolver={resolver}
      resolverContext={
        authenticated
          ? {
              vault: authenticatedVault,
              document: authenticatedDocument,
              commit: authenticatedCommit,
            }
          : {}
      }
    />
  );
}

export default MarkdownRender;
