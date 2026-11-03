import { useEffect, useId, useRef, useState } from "react";
import { Code2, ZoomIn } from "lucide-react";
import type { MarkdownViewerCodeBlock } from "@akb/markdown-editor/react";
import { DocumentCodeToolbar } from "@/components/document-reader-tools";
import { Button } from "@/components/ui/button";

let queue: Promise<unknown> = Promise.resolve();
function renderDiagram(id: string, source: string, valid: () => boolean) {
  const next = queue.catch(() => {}).then(async () => {
    const { default: mermaid } = await import("mermaid");
    if (!valid()) return null;
    const style = getComputedStyle(document.documentElement);
    const color = (token: string) => style.getPropertyValue(`--color-${token}`).trim();
    mermaid.initialize({ startOnLoad: false, securityLevel: "strict", suppressErrorRendering: true, look: "classic",
      maxTextSize: 40_000, maxEdges: 500, htmlLabels: false, theme: "base",
      fontFamily: style.getPropertyValue("--font-sans").trim(),
      themeVariables: { fontSize: "14px", background: color("surface"), primaryColor: color("surface-2"), primaryTextColor: color("foreground"), primaryBorderColor: color("border-strong"), lineColor: color("foreground-muted"), secondaryColor: color("surface"), tertiaryColor: color("surface-2") },
      secure: ["secure", "securityLevel", "startOnLoad", "maxTextSize", "maxEdges", "suppressErrorRendering", "htmlLabels", "theme", "themeVariables", "themeCSS", "fontFamily"],
    });
    const host = document.createElement("div");
    host.style.cssText = "position:fixed;left:-100000px;top:0;pointer-events:none";
    host.setAttribute("aria-hidden", "true"); document.body.append(host);
    try {
      const { svg } = await mermaid.render(`diagram-${id}`, source, host);
      const parsed = new DOMParser().parseFromString(svg, "image/svg+xml");
      const picture = parsed.documentElement, bounds = picture.getAttribute("viewBox")?.split(/[ ,]+/).map(Number);
      if (bounds?.length === 4 && bounds.slice(2).every(value => Number.isFinite(value) && value > 0 && value < 20_000)) {
        picture.setAttribute("width", String(bounds[2])); picture.setAttribute("height", String(bounds[3]));
      }
      return new XMLSerializer().serializeToString(picture);
    }
    finally { host.remove(); }
  });
  queue = next; return next;
}

export function ReaderMermaid({ block, onToggle, onEnlarge }: { block: MarkdownViewerCodeBlock; onToggle: () => void; onEnlarge: (image: { src: string; alt: string }, opener: HTMLElement) => void }) {
  const id = useId().replace(/[^a-z0-9_-]/gi, "");
  const root = useRef<HTMLDivElement>(null);
  const imageUrls = useRef<string[]>([]);
  const [visible, setVisible] = useState(false);
  const [theme, setTheme] = useState(0);
  const [sourceView, setSourceView] = useState(false);
  const [result, setResult] = useState<{ src?: string; error?: string }>({});
  useEffect(() => () => imageUrls.current.forEach(url => URL.revokeObjectURL(url)), []);
  useEffect(() => {
    const observer = new MutationObserver(() => setTheme(value => value + 1));
    observer.observe(document.documentElement, { attributes: true, attributeFilter: ["class"] });
    return () => observer.disconnect();
  }, []);
  useEffect(() => {
    if (!root.current || typeof IntersectionObserver === "undefined") { setVisible(true); return; }
    const observer = new IntersectionObserver(entries => { if (entries.some(entry => entry.isIntersecting)) { setVisible(true); observer.disconnect(); } }, { rootMargin: "200px" });
    observer.observe(root.current); return () => observer.disconnect();
  }, []);
  useEffect(() => {
    if (!visible) return;
    let alive = true;
    setResult(previous => previous.src ? previous : {});
    // Source-specific config and external resources must not activate a new page policy.
    if (block.code.length > 40_000 || /(?:https?|data|javascript|file):|url\s*\(|<\s*(?:img|iframe|script|object|link)\b|%%\s*\{|^---\s*\n/im.test(block.code)) {
      setResult({ error: "This diagram cannot be rendered here. Its source remains available." }); return;
    }
    void renderDiagram(id, block.code, () => alive).then(svg => {
      if (!alive || !svg) return;
      if (svg.length > 2_000_000) throw new Error("Diagram exceeds the display budget");
      const objectUrl = URL.createObjectURL(new Blob([svg], { type: "image/svg+xml" }));
      imageUrls.current.push(objectUrl);
      if (imageUrls.current.length > 3) URL.revokeObjectURL(imageUrls.current.shift()!);
      setResult({ src: objectUrl });
    }).catch(() => { if (alive) setResult({ error: "Diagram could not be rendered. View the source or try again after editing it." }); });
    return () => { alive = false; };
  }, [block.code, id, visible, theme]);
  const diagram = !sourceView && !block.searchMatch && Boolean(result.src);
  const alt = /^\s*accTitle:\s*(.+)$/m.exec(block.code)?.[1] || "Mermaid diagram";
  return <div ref={root} className="reader-mermaid-block" data-diagram-mode={diagram ? "diagram" : "source"}>
    <DocumentCodeToolbar block={block} onToggle={onToggle}>
      <Button variant="ghost" size="icon" disabled={!result.src} aria-label={sourceView ? "Show Mermaid diagram" : "Show Mermaid source"} aria-pressed={sourceView} title={sourceView ? "Show diagram" : "Show source"} onClick={() => setSourceView(value => !value)}><Code2 size={14} aria-hidden /></Button>
      <Button variant="ghost" size="icon" disabled={!result.src} data-reader-diagram-enlarge aria-label="Enlarge Mermaid diagram" title="Enlarge diagram" onClick={event => result.src && onEnlarge({ src: result.src, alt }, event.currentTarget)}><ZoomIn size={14} aria-hidden /></Button>
    </DocumentCodeToolbar>
    {!block.collapsed && diagram && <figure className="reader-mermaid-figure"><img src={result.src} alt={alt} /></figure>}
    {!block.collapsed && !result.src && !sourceView && <p className="reader-mermaid-status" role="status">{result.error || "Loading diagram…"}</p>}
  </div>;
}
