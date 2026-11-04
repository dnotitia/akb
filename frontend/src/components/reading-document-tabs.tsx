import { useEffect, useRef, useState, type KeyboardEvent } from "react";
import { useLocation, useNavigate } from "react-router-dom";
import { FileText, History, X } from "lucide-react";
import { Portal as TooltipPortal } from "@radix-ui/react-tooltip";
import { useReadingWorkspace } from "@/contexts/reading-workspace-context";
import { readingDocumentKey } from "@/lib/reading-workspace";
import { ReadingTabManager } from "@/components/reading-tab-manager";
import { Tooltip, TooltipContent, TooltipProvider, TooltipTrigger } from "@/components/ui/tooltip";

export function DocumentTabs({ active, panelId }: { active: string; panelId: string }) {
  const workspace = useReadingWorkspace()!;
  const navigate = useNavigate();
  const location = useLocation();
  const [focused, setFocused] = useState<string | null>(null);
  const [dragging, setDragging] = useState<string | null>(null);
  const [dropTarget, setDropTarget] = useState<string | null>(null);
  const listRef = useRef<HTMLDivElement>(null);
  const activeButtonRef = useRef<HTMLButtonElement>(null);
  const [notice, setNotice] = useState("");
  useEffect(() => { activeButtonRef.current?.scrollIntoView?.({ block: "nearest", inline: "nearest" }); }, [active]);
  useEffect(() => {
    setFocused(active || null);
    const closed = (location.state as { readingCloseTab?: string } | null)?.readingCloseTab;
    if (closed && active && active !== closed) activeButtonRef.current?.focus({ preventScroll: true });
  }, [active, location.state]);
  useEffect(() => {
    const strip = listRef.current;
    if (!strip) return;
    const wheel = (event: WheelEvent) => {
      if (event.ctrlKey || Math.abs(event.deltaX) > Math.abs(event.deltaY)) return;
      const delta = event.deltaY * (event.deltaMode === 1 ? 24 : event.deltaMode === 2 ? strip.clientWidth : 1);
      const next = Math.max(0, Math.min(strip.scrollWidth - strip.clientWidth, strip.scrollLeft + delta));
      if (next !== strip.scrollLeft) { event.preventDefault(); strip.scrollLeft = next; }
    };
    strip.addEventListener("wheel", wheel, { passive: false });
    return () => strip.removeEventListener("wheel", wheel);
  }, []);
  useEffect(() => {
    const keydown = (event: globalThis.KeyboardEvent) => {
      if (event.defaultPrevented || event.isComposing || event.repeat || !event.altKey || event.ctrlKey || event.metaKey
        || document.querySelector('[role="dialog"][data-state="open"], [role="alertdialog"]')
        || event.target instanceof HTMLElement && (event.target.isContentEditable || event.target.closest("input,textarea,select"))) return;
      const index = workspace.tabs.findIndex(tab => readingDocumentKey(tab) === active);
      if (event.shiftKey && ["ArrowLeft", "ArrowRight"].includes(event.key) && workspace.tabs.length) {
        event.preventDefault(); navigate(workspace.tabs[(index + (event.key === "ArrowRight" ? 1 : -1) + workspace.tabs.length) % workspace.tabs.length].href);
      } else if (event.shiftKey && event.code === "KeyT" && workspace.closed[0]) { event.preventDefault(); navigate(workspace.closed[0].href); }
      else if (!event.shiftKey && /^Digit[1-9]$/.test(event.code)) {
        const tab = workspace.tabs[Number(event.code.slice(-1)) - 1]; if (tab) { event.preventDefault(); navigate(tab.href); }
      } else if (!event.shiftKey && event.code === "KeyW" && active) { event.preventDefault(); workspace.close(active, active); }
    };
    window.addEventListener("keydown", keydown); return () => window.removeEventListener("keydown", keydown);
  }, [workspace, active, navigate]);
  const onKey = (event: KeyboardEvent, index: number) => {
    const buttons = listRef.current?.querySelectorAll<HTMLButtonElement>('[role="tab"]');
    if (!buttons?.length || event.altKey || event.ctrlKey || event.metaKey || event.nativeEvent.isComposing) return;
    const next = event.key === "ArrowRight" ? (index + 1) % buttons.length : event.key === "ArrowLeft" ? (index - 1 + buttons.length) % buttons.length : event.key === "Home" ? 0 : event.key === "End" ? buttons.length - 1 : null;
    if (next !== null) { event.preventDefault(); buttons[next].focus(); }
    if (event.key === "Delete") {
      event.preventDefault(); const key = readingDocumentKey(workspace.tabs[index]); workspace.close(key, active);
      if (key !== active) { setFocused(active); activeButtonRef.current?.focus({ preventScroll: true }); }
    }
    if (event.key === "Escape") { setDragging(null); setDropTarget(null); }
  };
  return <TooltipProvider delayDuration={300}>
    <div ref={listRef} className="reading-tabs rail-scroll" role="tablist" aria-label="Open documents" aria-orientation="horizontal">
      {workspace.tabs.map((tab, index) => {
        const key = readingDocumentKey(tab), selected = key === active;
        const title = tab.title.trim();
        const label = title && title !== tab.path ? title : tab.path.split("/").at(-1) || tab.path;
        const details = `${tab.vault} / ${tab.path}${tab.version ? ` · Version ${tab.version}` : ""}`;
        const Icon = tab.version ? History : FileText;
        return <div key={key} className="reading-tab-wrap" role="presentation" data-selected={selected} data-drop-target={dragging && key !== dragging && dropTarget === key}
          data-drop-direction={workspace.tabs.findIndex(item => readingDocumentKey(item) === dragging) < index ? "after" : "before"}
          onDragOver={event => { if (!dragging) return; event.preventDefault(); setDropTarget(key); if (listRef.current) { const bounds = listRef.current.getBoundingClientRect(); if (event.clientX < bounds.left + 32) listRef.current.scrollLeft -= 20; else if (event.clientX > bounds.right - 32) listRef.current.scrollLeft += 20; } }}
          onDrop={event => { if (dragging) { event.preventDefault(); workspace.reorder(dragging, key); setNotice("Document tabs reordered"); } setDragging(null); setDropTarget(null); }}>
          <Tooltip>
            <TooltipTrigger asChild>
              <button ref={selected ? activeButtonRef : undefined} type="button" role="tab" id={`${panelId}-tab-${encodeURIComponent(key)}`}
                draggable onDragStart={event => { setDragging(key); event.dataTransfer.effectAllowed = "move"; event.dataTransfer.setData("application/x-akb-reading-tab", key); }} onDragEnd={() => { setDragging(null); setDropTarget(null); }}
                aria-selected={selected} aria-controls={selected ? panelId : undefined} aria-description={details} tabIndex={(focused ?? active) === key ? 0 : -1}
                className="reading-tab"
                onMouseDown={event => { if (event.button === 1) event.preventDefault(); }} onAuxClick={event => { if (event.button === 1) { event.preventDefault(); workspace.close(key, active); } }}
                onFocus={() => setFocused(key)} onKeyDown={event => onKey(event, index)} onClick={() => navigate(tab.href)}>
                <Icon size={14} aria-hidden /><span>{label}</span>
              </button>
            </TooltipTrigger>
            <TooltipPortal>
              <TooltipContent side="top" aria-label={details} className="max-w-[min(28rem,90vw)] wrap-anywhere tracking-normal">
                <span className="block">{label}</span>
                <span className="block font-normal text-foreground-muted">{details}</span>
              </TooltipContent>
            </TooltipPortal>
          </Tooltip>
          <button type="button" className="reading-icon-button reading-tab-close" aria-label={`Close ${label}`} aria-description={details} tabIndex={selected ? 0 : -1} onClick={() => workspace.close(key, active)}><X size={14} aria-hidden /></button>
        </div>;
      })}
    </div>
    <ReadingTabManager active={active} />
    <span role="status" className="sr-only">{notice}</span>
  </TooltipProvider>;
}
