import { useLayoutEffect, useRef, useState, type MouseEvent, type ReactNode } from "react";
import { ListTree, ChevronDown, X } from "lucide-react";
import { revealMarkdownTarget } from "@akb/markdown-editor/react";
import { Button } from "@/components/ui/button";
import { Dialog, DialogContent, DialogTitle } from "@/components/ui/dialog";
import type { ReferenceNavigationRequest } from "@/contexts/reading-chrome-context";

function fitsNavigationRail(frame: HTMLElement) {
  const rem = parseFloat(getComputedStyle(document.documentElement).fontSize) || 16;
  const bounds = frame.getBoundingClientRect();
  return bounds.width >= 50 * rem && bounds.height >= 20 * rem;
}

/** One Contents location: a persistent right rail, or the same content in a narrow reader sheet. */
export function DocumentContents({ children, identity, references, referencesRequest, locked = false }: {
  children: ReactNode; identity: string; references?: ReactNode;
  referencesRequest?: ReferenceNavigationRequest; locked?: boolean;
}) {
  const marker = useRef<HTMLSpanElement>(null);
  const trigger = useRef<HTMLButtonElement>(null);
  const destination = useRef<HTMLElement | null>(null);
  const mode = useRef(false);
  const restoreNavigation = useRef(false);
  const referencesHost = useRef<HTMLDivElement>(null);
  const handledReferencesRequest = useRef(0);
  const skipCloseFocus = useRef(false);
  const [wide, setWide] = useState(false);
  const [opened, setOpened] = useState<string | null>(null);
  useLayoutEffect(() => {
    const frame = marker.current?.parentElement;
    if (!frame) return;
    const measure = () => {
      if (locked) return;
      const next = fitsNavigationRail(frame);
      if (mode.current !== next && frame.contains(document.activeElement) && document.activeElement?.closest(".document-contents-rail")) restoreNavigation.current = true;
      mode.current = next; setWide(next);
    };
    measure(); const observer = new ResizeObserver(measure); observer.observe(frame);
    window.addEventListener("resize", measure);
    return () => { observer.disconnect(); window.removeEventListener("resize", measure); };
  }, [locked]);
  useLayoutEffect(() => {
    if (!restoreNavigation.current) return;
    restoreNavigation.current = false;
    trigger.current?.focus({ preventScroll: true });
  }, [wide]);
  useLayoutEffect(() => {
    if (!referencesRequest || handledReferencesRequest.current === referencesRequest.id) return;
    const layout = marker.current?.parentElement;
    // Closing a preview expands the article before ResizeObserver catches up.
    // Reconcile that geometry before opening a sheet or restoring its focus.
    if (layout) {
      const next = fitsNavigationRail(layout);
      if (next !== wide) { mode.current = next; setWide(next); return; }
    }
    if (!wide && opened !== identity) { setOpened(identity); return; }
    const frame = requestAnimationFrame(() => {
      const target = [...(referencesHost.current?.querySelectorAll<HTMLAnchorElement>("a[data-reference-link]") ?? [])]
        .find(link => referencesRequest.referenceKey ? link.dataset.referenceKey === referencesRequest.referenceKey : link.getAttribute("href") === referencesRequest.href)
        ?? referencesHost.current?.querySelector<HTMLElement>("h2");
      if (!target?.isConnected) return;
      const scroll = referencesHost.current?.querySelector<HTMLElement>(".document-reference-body");
      if (scroll && target.tagName === "A") {
        scroll.scrollTop = referencesRequest.scrollTop ?? scroll.scrollTop;
        const item = target.getBoundingClientRect(), bounds = scroll.getBoundingClientRect();
        if (item.top < bounds.top) scroll.scrollTop += item.top - bounds.top;
        else if (item.bottom > bounds.bottom) scroll.scrollTop += item.bottom - bounds.bottom;
      }
      target.focus({ preventScroll: true }); handledReferencesRequest.current = referencesRequest.id;
    });
    return () => cancelAnimationFrame(frame);
  }, [referencesRequest, wide, opened, identity]);
  const navigate = (event: MouseEvent<HTMLElement>) => {
    if (event.button || event.metaKey || event.ctrlKey || event.altKey || event.shiftKey || !(event.target instanceof Element)) return;
    const href = event.target.closest("a")?.getAttribute("href");
    if (event.target.closest("a[data-reference-link]")) {
      if (!wide) { skipCloseFocus.current = true; setOpened(null); }
      return;
    }
    if (!href?.startsWith("#")) return;
    const canvas = marker.current?.parentElement?.querySelector<HTMLElement>("#document-reading-canvas");
    const heading = canvas?.querySelector<HTMLElement>(`[id="${CSS.escape(decodeURIComponent(href.slice(1)))}"]`);
    if (!canvas || !heading) return;
    event.preventDefault(); revealMarkdownTarget(heading);
    canvas.scrollTop += heading.getBoundingClientRect().top - canvas.getBoundingClientRect().top - 16;
    if (!wide) { destination.current = heading; setOpened(null); }
  };
  return <><span ref={marker} hidden />{wide ?
    <aside className="document-contents-rail" aria-label="Contents" onClickCapture={navigate}>
      <h2>Contents</h2><div className="document-contents-scroll rail-scroll">{children}</div>
      {references && <div ref={referencesHost} className="document-contents-references">{references}</div>}
    </aside> : <>
      <div className="document-contents-compact"><Button ref={trigger} variant="ghost" size="sm" aria-haspopup="dialog" aria-expanded={opened === identity} aria-label="Open contents" onClick={() => { destination.current = null; setOpened(identity); }}><ListTree size={14} aria-hidden />Contents<ChevronDown size={13} aria-hidden /></Button></div>
      <Dialog open={opened === identity} onOpenChange={open => { if (!open) setOpened(null); }}>
        <DialogContent hideClose aria-modal="true" aria-describedby={undefined} className="document-contents-sheet left-auto top-0 translate-x-0 translate-y-0" onClickCapture={navigate}
          onCloseAutoFocus={event => {
            event.preventDefault();
            if (skipCloseFocus.current) { skipCloseFocus.current = false; return; }
            const target = destination.current; destination.current = null;
            if (target?.isConnected) { target.tabIndex = -1; target.focus({ preventScroll: true }); }
            else (trigger.current ?? marker.current?.parentElement?.querySelector<HTMLElement>(".document-contents-rail a"))?.focus({ preventScroll: true });
          }}>
          <div className="document-info-heading"><DialogTitle>Contents</DialogTitle><Button variant="ghost" size="icon" aria-label="Close contents" onClick={() => setOpened(null)}><X size={16} aria-hidden /></Button></div>
          <div className="document-contents-scroll rail-scroll">{children}</div>
          {references && <div ref={referencesHost} className="document-contents-references">{references}</div>}
        </DialogContent>
      </Dialog>
    </>}</>;
}
