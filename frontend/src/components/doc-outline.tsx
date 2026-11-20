import { useMemo, useState } from "react";
import { Portal as TooltipPortal } from "@radix-ui/react-tooltip";
import { ChevronDown, ChevronRight, ChevronsDownUp, ChevronsUpDown, Link2, Search } from "lucide-react";
import { useDocOutline } from "@/hooks/use-doc-outline";
import { TooltipText } from "@/components/ui/tooltip-text";
import { Button } from "@/components/ui/button";
import { Tooltip, TooltipContent, TooltipProvider, TooltipTrigger } from "@/components/ui/tooltip";
import { revealMarkdownTarget } from "@akb/markdown-editor/react";

export function DocumentOutline({ markdown, articleEl, documentHref }: { markdown: string; articleEl: HTMLElement | null; documentHref: string }) {
  const { headings, activeSlug } = useDocOutline(markdown, { root: articleEl });
  const [query, setQuery] = useState("");
  const [foldState, setFoldState] = useState<{ markdown: string; ids: Set<string> }>({ markdown, ids: new Set() });
  const collapsed = foldState.markdown === markdown ? foldState.ids : new Set<string>();
  const [notice, setNotice] = useState("");
  const tree = useMemo(() => {
    const stack: number[] = [];
    return headings.map((heading, index) => {
      while (stack.length && headings[stack.at(-1)!].level >= heading.level) stack.pop();
      const ancestors = [...stack];
      stack.push(index);
      return { ...heading, ancestors, children: (headings[index + 1]?.level ?? 0) > heading.level };
    });
  }, [headings]);
  const normalized = query.trim().toLocaleLowerCase();
  const included = new Set<number>();
  tree.forEach((heading, index) => {
    if (normalized && heading.text.toLocaleLowerCase().includes(normalized)) { included.add(index); heading.ancestors.forEach(parent => included.add(parent)); }
  });
  const toggle = (slug: string) => setFoldState(previous => {
    const ids = new Set(previous.markdown === markdown ? previous.ids : []);
    if (ids.has(slug)) ids.delete(slug); else ids.add(slug);
    return { markdown, ids };
  });
  const foldLabel = collapsed.size ? "Expand all" : "Collapse all";
  if (!headings.length) return <p className="text-sm leading-relaxed text-foreground-muted">No headings in this document yet.</p>;
  return <nav aria-label="Document outline" className="reader-outline">
    <div className="reader-outline-toolbar">
      <label className="reader-outline-search"><Search size={14} aria-hidden /><input type="search" placeholder="Find section…" aria-label="Find a section" value={query} onChange={event => setQuery(event.target.value)} /></label>
      {tree.some(heading => heading.children) && <TooltipProvider delayDuration={300}><Tooltip>
        <TooltipTrigger asChild><Button type="button" variant="ghost" size="icon" className="reader-outline-fold-all" aria-label={foldLabel} disabled={Boolean(normalized)}
          onClick={() => setFoldState({ markdown, ids: collapsed.size ? new Set() : new Set(tree.filter(heading => heading.children).map(heading => heading.slug)) })}>
          {collapsed.size ? <ChevronsUpDown size={15} aria-hidden /> : <ChevronsDownUp size={15} aria-hidden />}
        </Button></TooltipTrigger>
        <TooltipPortal><TooltipContent side="top">{foldLabel}</TooltipContent></TooltipPortal>
      </Tooltip></TooltipProvider>}
    </div>
    <span role="status" className="sr-only">{notice}</span>
    <ol>{tree.map((heading, index) => {
      const visible = normalized ? included.has(index) : !heading.ancestors.some(parent => collapsed.has(tree[parent].slug));
      if (!visible) return null;
      const isCollapsed = !normalized && collapsed.has(heading.slug);
      return <li key={heading.slug} className="reader-outline-row" style={{ paddingLeft: `${heading.ancestors.length * 12}px` }}>
        {heading.children ? <button type="button" className="reader-outline-fold" aria-label={`${isCollapsed ? "Expand" : "Collapse"} ${heading.text}`} aria-expanded={!isCollapsed} disabled={Boolean(normalized)} onClick={() => toggle(heading.slug)}>{isCollapsed ? <ChevronRight size={12} aria-hidden /> : <ChevronDown size={12} aria-hidden />}</button> : <span className="reader-outline-fold" />}
        <a href={`#${heading.slug}`} aria-current={activeSlug === heading.slug ? "location" : undefined} onClick={event => {
          if (event.defaultPrevented) return;
          const target = articleEl && Array.from(articleEl.querySelectorAll<HTMLElement>("[id]")).find(element => element.id === heading.slug);
          if (target) { revealMarkdownTarget(target); target.scrollIntoView?.({ block: "start" }); }
        }}><TooltipText className="truncate block">{heading.text}</TooltipText></a>
        <button type="button" className="reader-outline-copy" aria-label={`Copy link to ${heading.text}`} onClick={async () => {
          const url = new URL(documentHref, window.location.origin); url.hash = heading.slug;
          try { await navigator.clipboard.writeText(url.href); setNotice(`Link to ${heading.text} copied`); }
          catch { setNotice("Link could not be copied. Open the section and copy its address."); }
        }}><Link2 size={12} aria-hidden /></button>
      </li>;
    })}</ol>
    {normalized && !included.size && <p className="py-4 text-xs text-foreground-muted" role="status">No matching sections.</p>}
  </nav>;
}
