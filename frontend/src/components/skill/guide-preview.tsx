import { useId, useMemo, useRef, useState, type MouseEvent } from "react";
import { ChevronDown } from "lucide-react";
import { parseMarkdown, serializeMarkdown, type MarkdownNode } from "@akb/markdown-editor";
import { MarkdownRender } from "@/components/markdown-render";
import { Button } from "@/components/ui/button";
import { stripFrontmatter } from "@/lib/markdown";
import { vaultGuideHref } from "@/lib/vault-guide-navigation";
import { VAULT_SKILL_PATH } from "@/lib/skill";
import { cn } from "@/lib/utils";

/** Keep complete Markdown blocks readable and focusable, without clipping links. */
export default function GuidePreview({ vault, content, onNavigate }: {
  vault: string;
  content: string;
  onNavigate?: (event: MouseEvent<HTMLDivElement>, url: URL) => void;
}) {
  const id = useId();
  const previewRef = useRef<HTMLDivElement>(null);
  const headingPrefix = `guide-preview-${id}-`;
  const guideHref = vaultGuideHref(vault);
  const toggleRef = useRef<HTMLButtonElement>(null);
  const [showAll, setShowAll] = useState(false);
  const { hasMore, markdown } = useMemo(() => {
    const body = stripFrontmatter(content);
    const document = parseMarkdown(body);
    const blocks = document.content ?? [];
    const last = blocks.at(-1);
    const trailingEmpty = last?.type === "paragraph" && !last.content?.length;
    let fragments = false;
    const visit = (node: MarkdownNode) => {
      for (const mark of node.marks ?? []) {
        const href = mark.attrs?.href;
        if (mark.type === "link" && typeof href === "string" && href.startsWith("#")) {
          // Modified clicks still open the guide, rather than a Settings hash.
          mark.attrs = { ...mark.attrs, href: `${guideHref}${href}` };
          fragments = true;
        }
      }
      node.content?.forEach(visit);
    };
    blocks.forEach(visit);
    return {
      hasMore: blocks.length - (trailingEmpty ? 1 : 0) > 4,
      markdown: fragments ? serializeMarkdown(document) : body,
    };
  }, [content, guideHref]);

  function followLink(event: MouseEvent<HTMLDivElement>) {
    if (event.defaultPrevented || event.button !== 0 || event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return;
    const link = event.target instanceof Element ? event.target.closest("a[href]") : null;
    if (!link) return;
    const url = new URL(link.getAttribute("href")!, window.location.href);
    if (url.origin === window.location.origin && url.pathname === guideHref && !url.search && url.hash) {
      let fragment: string;
      try { fragment = decodeURIComponent(url.hash.slice(1)); } catch { return; }
      const target = document.getElementById(`${headingPrefix}${fragment}`);
      if (target && previewRef.current?.contains(target)) {
        event.preventDefault();
        setShowAll(true);
        requestAnimationFrame(() => {
          if (!target.isConnected) return;
          target.tabIndex = -1;
          target.focus({ preventScroll: true });
          target.scrollIntoView({ block: "nearest" });
        });
        return;
      }
    }
    onNavigate?.(event, url);
  }

  return (
    <div className="mt-4 border-t border-border pt-4" onClick={followLink}>
      <div ref={previewRef} id={id} role="region" aria-label="Vault guide preview"
        className={cn("max-w-prose", !showAll && "[&_.ProseMirror>:nth-child(n+5)]:hidden")}>
        <MarkdownRender markdown={markdown}
          assetContext={{ mode: "authenticated", vault, document: VAULT_SKILL_PATH }}
          headingIdPrefix={headingPrefix} headingLevelOffset={3}
          className="text-ui leading-relaxed prose-headings:mb-1.5 prose-headings:mt-3 prose-headings:text-ui prose-headings:font-semibold prose-p:my-1.5 prose-li:my-0.5 prose-pre:my-3 [&_.ProseMirror>:first-child]:mt-0 [&_.ProseMirror>:last-child]:mb-0 [&_img]:max-h-40 [&_img]:object-contain" />
      </div>
      {hasMore && <Button ref={toggleRef} variant="ghost" size="sm"
        className="mt-3 min-h-11 gap-1.5 text-link sm:min-h-8"
        aria-expanded={showAll} aria-controls={id} onClick={() => {
          setShowAll(value => !value);
          if (showAll) requestAnimationFrame(() => {
            if (toggleRef.current?.isConnected) toggleRef.current.scrollIntoView({ block: "nearest" });
          });
        }}>
        {showAll ? "Show less" : "Show more"}
        <ChevronDown className={cn("h-3.5 w-3.5", showAll && "rotate-180")} aria-hidden />
      </Button>}
    </div>
  );
}
