import {
  EditorContent,
  useMarkdownEditor,
  useMarkdownTargetResolutions,
} from "@akb/markdown-editor/react";
import { useEffect, useLayoutEffect, useMemo, useRef } from "react";
import "katex/dist/katex.min.css";
import type { AssetContext } from "@/components/asset-image";
import {
  getAssetBlob,
  publicationAssetUrl,
  refreshPublicationViewGrant,
} from "@/lib/api";
import {
  canonicalAkbMarkdownTarget,
  classifyAkbMarkdownTarget,
  createAkbMarkdownAdapters,
  type AkbMarkdownTargetResolution,
} from "@/lib/markdown-adapters";
import { assetIdFromUrl } from "@/lib/image-assets";
import { parseHeadings, stripFrontmatter } from "@/lib/markdown";
import { cn } from "@/lib/utils";

function applyViewerTargetResolutions(
  root: HTMLElement,
  resolutions: ReadonlyMap<string, AkbMarkdownTargetResolution>,
  resolving: boolean,
): void {
  root.querySelectorAll<HTMLAnchorElement>("a[href]").forEach((link) => {
    const rawTarget =
      link.dataset.markdownTarget ?? link.getAttribute("href") ?? "";
    const target = canonicalAkbMarkdownTarget(rawTarget);
    const kind = target ? classifyAkbMarkdownTarget(target) : null;
    if (!target || !kind || kind === "attachment") return;

    link.dataset.markdownTarget = target;
    const resolution = resolutions.get(target);
    if (resolution?.status === "available" && resolution.runtimeUrl) {
      link.href = resolution.runtimeUrl;
      link.dataset.markdownResolution = "available";
      link.removeAttribute("aria-disabled");
      link.removeAttribute("aria-label");
      link.removeAttribute("title");
      return;
    }

    link.dataset.markdownResolution = resolution ? "unavailable" : "pending";
    link.href = "#";
    link.setAttribute("aria-disabled", "true");
    if (resolution || !resolving)
      link.setAttribute("title", "Reference unavailable");
  });
}

function normalizeViewerTopLevelImages(
  editor: NonNullable<ReturnType<typeof useMarkdownEditor>>,
): void {
  const document = editor.getJSON();
  if (!document.content?.some((node) => node.type === "image")) return;
  editor.commands.setContent({
    ...document,
    content: document.content.map((node) =>
      node.type === "image" ? { type: "paragraph", content: [node] } : node,
    ),
  });
}

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

function useViewerTargetDom(
  rootRef: React.RefObject<HTMLDivElement | null>,
  resolutions: ReadonlyMap<string, AkbMarkdownTargetResolution>,
  resolving: boolean,
  editor: ReturnType<typeof useMarkdownEditor>,
): void {
  useLayoutEffect(() => {
    const host = rootRef.current;
    if (!host) return;

    const apply = () => {
      const root =
        (editor?.view.dom as HTMLElement | undefined) ??
        host.querySelector<HTMLElement>(".ProseMirror");
      if (root) applyViewerTargetResolutions(root, resolutions, resolving);
    };
    apply();
    const timer = window.setTimeout(apply, 25);
    const observer =
      typeof MutationObserver === "undefined"
        ? null
        : new MutationObserver(apply);
    observer?.observe(host, { childList: true, subtree: true });
    return () => {
      window.clearTimeout(timer);
      observer?.disconnect();
    };
  }, [editor, resolutions, resolving, rootRef]);
}

function useViewerResources(
  rootRef: React.RefObject<HTMLDivElement | null>,
  assetContext?: AssetContext,
  editor: ReturnType<typeof useMarkdownEditor> = null,
  markdown = "",
): void {
  const assetMode = assetContext?.mode;
  const authenticatedContext =
    assetMode === "authenticated" ? assetContext : undefined;
  const publicationContext =
    assetMode === "publication" ? assetContext : undefined;
  const assetVault = authenticatedContext?.vault;
  const assetDocument = authenticatedContext?.document;
  const assetCommit = authenticatedContext?.commit;
  const publicationSlug = publicationContext?.slug;

  useLayoutEffect(() => {
    const host = rootRef.current;
    if (!host) return;

    const objectUrls = new Map<string, string>();
    const pending = new Set<string>();
    const controllers = new Map<string, AbortController>();
    let disposed = false;
    const placeholders = new Map<
      string,
      { element: HTMLSpanElement; alt: string; title: string | null }
    >();
    const publicationRefreshes = new Set<string>();
    const publicationListeners: Array<{
      image: HTMLImageElement;
      listener: () => void;
    }> = [];

    const sync = () => {
      const root =
        (editor?.view.dom as HTMLElement | undefined) ??
        host.querySelector<HTMLElement>(".ProseMirror");
      if (!root) return;
      normalizeViewerHeadings(root, markdown);

      root.querySelectorAll<HTMLImageElement>("img").forEach((image) => {
        image.setAttribute("referrerpolicy", "no-referrer");
        const rawTarget =
          image.dataset.markdownTarget ?? image.getAttribute("src") ?? "";
        const target = canonicalAkbMarkdownTarget(rawTarget, "attachment");
        const assetId = target ? assetIdFromUrl(target) : null;
        if (!target || !assetId) return;
        image.dataset.markdownTarget = target;

        if (assetMode === "publication" && publicationSlug) {
          const publicationKey = `${publicationSlug}:${assetId}`;
          image.src = publicationAssetUrl(publicationSlug, assetId);
          if (!image.dataset.publicationRefreshListener) {
            const listener = () => {
              if (publicationRefreshes.has(publicationKey)) return;
              publicationRefreshes.add(publicationKey);
              void refreshPublicationViewGrant(publicationSlug)
                .then(() => {
                  image.src = publicationAssetUrl(publicationSlug, assetId);
                })
                .finally(() => publicationRefreshes.delete(publicationKey));
            };
            image.addEventListener("error", listener);
            image.dataset.publicationRefreshListener = "true";
            publicationListeners.push({ image, listener });
          }
          return;
        }

        if (assetMode !== "authenticated" || !assetVault) return;
        const key = `${assetVault}\u0000${assetDocument ?? ""}\u0000${assetCommit ?? ""}\u0000${assetId}`;
        const existing = objectUrls.get(key);
        if (existing) {
          image.src = existing;
          return;
        }
        if (pending.has(key)) return;
        pending.add(key);
        const placeholder = window.document.createElement("span");
        placeholder.role = "status";
        placeholder.setAttribute(
          "aria-label",
          image.alt ? `Loading image: ${image.alt}` : "Loading image",
        );
        placeholder.dataset.markdownTarget = target;
        placeholder.className =
          "block min-h-28 rounded-[var(--radius-lg)] border border-border bg-surface-2 p-4 text-sm text-foreground-muted";
        placeholders.set(key, {
          element: placeholder,
          alt: image.alt,
          title: image.getAttribute("title"),
        });
        image.replaceWith(placeholder);
        const controller = new AbortController();
        controllers.set(key, controller);
        void getAssetBlob(
          assetId,
          assetVault,
          controller.signal,
          assetDocument && assetCommit
            ? { document: assetDocument, commit: assetCommit }
            : undefined,
        )
          .then((blob) => {
            if (controller.signal.aborted || disposed) return;
            const url =
              typeof URL.createObjectURL === "function"
                ? URL.createObjectURL(blob)
                : target;
            objectUrls.set(key, url);
            const current = placeholders.get(key);
            if (current) {
              const element = window.document.createElement("img");
              element.src = url;
              element.alt = current.alt;
              if (current.title) element.title = current.title;
              element.dataset.markdownTarget = target;
              element.dataset.markdownResolution = "available";
              element.setAttribute("referrerpolicy", "no-referrer");
              current.element.replaceWith(element);
              placeholders.delete(key);
            }
          })
          .catch(() => {
            if (controller.signal.aborted || disposed) return;
            const current = placeholders.get(key);
            if (current) {
              current.element.role = "img";
              current.element.setAttribute(
                "aria-label",
                current.alt
                  ? `Image unavailable: ${current.alt}`
                  : "Image unavailable",
              );
              current.element.dataset.markdownResolution = "unavailable";
            }
          })
          .finally(() => {
            pending.delete(key);
            controllers.delete(key);
          });
      });

      root.querySelectorAll<HTMLTableElement>("table").forEach((table) => {
        table.classList.add("w-max", "min-w-full");
        if (table.parentElement?.classList.contains("akb-md-table")) return;
        const wrapper = window.document.createElement("div");
        wrapper.className =
          "akb-md-table my-5 overflow-x-auto rounded-[var(--radius-lg)] border border-border";
        table.replaceWith(wrapper);
        wrapper.append(table);
      });
    };

    sync();
    const observer =
      typeof MutationObserver === "undefined"
        ? null
        : new MutationObserver(sync);
    observer?.observe(host, { childList: true, subtree: true });

    return () => {
      disposed = true;
      observer?.disconnect();
      controllers.forEach((controller) => controller.abort());
      publicationListeners.forEach(({ image, listener }) =>
        image.removeEventListener("error", listener),
      );
      placeholders.clear();
      objectUrls.forEach((url) => {
        if (
          url.startsWith("blob:") &&
          typeof URL.revokeObjectURL === "function"
        ) {
          URL.revokeObjectURL(url);
        }
      });
    };
  }, [
    assetCommit,
    assetDocument,
    assetMode,
    assetVault,
    publicationSlug,
    editor,
    markdown,
    rootRef,
  ]);
}

export interface MarkdownRenderProps {
  markdown: string;
  className?: string;
  assetContext?: AssetContext;
}

function CommonMarkdownViewer({
  markdown,
  className,
  assetContext,
  adapters,
  resolverContext,
}: {
  markdown: string;
  className?: string;
  assetContext?: AssetContext;
  adapters?: ReturnType<typeof createAkbMarkdownAdapters>;
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
    adapters?.targetResolver,
    resolverContext,
  );

  useLayoutEffect(() => {
    if (editor) normalizeViewerTopLevelImages(editor);
  }, [editor]);

  useEffect(() => {
    if (!editor || editor.getMarkdown() === markdown) return;
    editor.commands.setContent(markdown, { contentType: "markdown" });
  }, [editor, markdown]);

  useViewerTargetDom(
    rootRef,
    resolutions,
    Boolean(adapters?.targetResolver),
    editor,
  );
  useViewerResources(rootRef, assetContext, editor, markdown);

  return (
    <div
      ref={rootRef}
      className={cn(
        "akb-md min-w-0 text-[15px] text-foreground prose dark:prose-invert !max-w-none",
        className,
      )}
    >
      {editor ? <EditorContent editor={editor} /> : null}
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
  return (
    <CommonMarkdownViewer
      markdown={body}
      className={className}
      assetContext={assetContext}
      adapters={adapters}
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
