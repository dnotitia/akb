import { useCallback, useId, useLayoutEffect, useRef, useState, type FocusEventHandler, type MouseEventHandler, type ReactNode, type RefObject } from "react";
import * as DialogPrimitive from "@radix-ui/react-dialog";
import { DismissableLayer } from "@radix-ui/react-dismissable-layer";
import { ChevronDown, X } from "lucide-react";
import { Dialog, DialogOverlay, DialogPortal, DialogTitle } from "@/components/ui/dialog";
import { Button } from "@/components/ui/button";
import { cn } from "@/lib/utils";
import { useModalCount } from "@/lib/modal-visibility";

export type DocumentContextView = "info" | "outline" | "relations" | "history";
const sections = [{ id: "info", label: "Properties" }, { id: "history", label: "Version history" }, { id: "relations", label: "Related documents" }] as const;

/** One inspector beside a wide article, or a modal sheet in a constrained reader. */
export function DocumentContextPanel({ id: suppliedId, open, onOpenChange, view, onViewChange, triggerRef, outline, info, history, relations, focusRequest = 0, focusTargetRef, onFocusTarget }: {
  id?: string; open: boolean; onOpenChange: (open: boolean) => void;
  view: DocumentContextView; onViewChange: (view: DocumentContextView) => void;
  triggerRef: RefObject<HTMLButtonElement | null>; outline: ReactNode; info: ReactNode; history: ReactNode; relations: ReactNode; focusRequest?: number;
  /** An explicit return target takes precedence over opening/disclosure focus. */
  focusTargetRef?: RefObject<HTMLElement | null>; onFocusTarget?: () => void;
}) {
  const generatedId = useId();
  const id = suppliedId ?? generatedId;
  const markerRef = useRef<HTMLSpanElement>(null);
  const panelRef = useRef<HTMLElement | null>(null);
  const closeRef = useRef<HTMLButtonElement>(null);
  const [docked, setDocked] = useState(false);
  const latestOpen = useRef(open);
  const previousDockedOpen = useRef(false);
  const lastPanelFocus = useRef<HTMLElement | null>(null);
  const pendingInsideFocus = useRef<HTMLElement | null>(null);
  const supportingDialog = useRef(false);
  const modalCount = useModalCount();
  const sectionRefs = useRef<Partial<Record<DocumentContextView, HTMLElement | null>>>({});
  const previousRequest = useRef(0);
  const hasForegroundDialog = useCallback(() => {
    const marker = markerRef.current;
    return Boolean(marker && Array.from(marker.ownerDocument.querySelectorAll('[role="dialog"], [role="alertdialog"]'))
      .some(dialog => dialog.id !== id && dialog.getAttribute("data-state") !== "closed" && dialog.getAttribute("aria-hidden") !== "true" && !dialog.contains(marker)));
  }, [id]);
  const restoreTrigger = useCallback(() => triggerRef.current?.focus({ preventScroll: true }), [triggerRef]);
  const focusRequestedTarget = useCallback(() => {
    const target = focusTargetRef?.current;
    if (!open || !target?.isConnected || !panelRef.current?.contains(target)) return false;
    target.focus({ preventScroll: true });
    if (target.ownerDocument.activeElement !== target) return false;
    onFocusTarget?.();
    return true;
  }, [focusTargetRef, onFocusTarget, open]);
  const restoreInsideFocus = useCallback(() => {
    // A presentation change remounts the panel. Its autofocus must not replace
    // the supporting dialog's opener before we can find its new DOM instance.
    const previous = pendingInsideFocus.current ?? lastPanelFocus.current;
    const candidate = previous?.isConnected ? previous : previous && Array.from(panelRef.current?.querySelectorAll<HTMLElement>('button, a[href], summary, input, textarea, select') ?? [])
      .find(element => element.tagName === previous.tagName && element.getAttribute("aria-label") === previous.getAttribute("aria-label") && element.textContent === previous.textContent);
    pendingInsideFocus.current = null;
    (candidate ?? closeRef.current)?.focus({ preventScroll: true });
  }, []);
  useLayoutEffect(() => {
    latestOpen.current = open;
    const dockedOpen = open && docked;
    if (dockedOpen && !previousDockedOpen.current && !focusRequestedTarget()) {
      if (pendingInsideFocus.current) restoreInsideFocus();
      else closeRef.current?.focus({ preventScroll: true });
    }
    if (!open && previousDockedOpen.current) restoreTrigger();
    previousDockedOpen.current = dockedOpen;
  }, [open, docked, focusRequestedTarget, restoreInsideFocus, restoreTrigger]);
  useLayoutEffect(() => {
    const layout = markerRef.current?.parentElement;
    if (!layout) return;
    const measure = () => {
      // Do not remount a relation/property editor during a child dialog's draft.
      if (latestOpen.current && hasForegroundDialog()) { supportingDialog.current = true; return; }
      const returningFromDialog = supportingDialog.current;
      supportingDialog.current = false;
      if (returningFromDialog) pendingInsideFocus.current = lastPanelFocus.current;
      const rem = parseFloat(getComputedStyle(document.documentElement).fontSize) || 16;
      const bounds = layout.getBoundingClientRect();
      setDocked(bounds.width >= 48 * rem && bounds.height >= 20 * rem);
      if (returningFromDialog) requestAnimationFrame(() => { if (latestOpen.current && !hasForegroundDialog()) restoreInsideFocus(); });
    };
    measure();
    const observer = typeof ResizeObserver === "undefined" ? null : new ResizeObserver(measure);
    observer?.observe(layout);
    layout.ownerDocument.addEventListener("focusin", measure);
    return () => { observer?.disconnect(); layout.ownerDocument.removeEventListener("focusin", measure); };
  }, [hasForegroundDialog, modalCount, restoreInsideFocus]);
  useLayoutEffect(() => {
    // History can arrive after the panel opens. Keep the request in its caller
    // until the requested control is mounted and successfully receives focus.
    focusRequestedTarget();
  }, [focusRequestedTarget, docked, history]);
  useLayoutEffect(() => {
    if (!open || focusTargetRef) {
      // Closing or an explicit return target cancels deferred section focus.
      // Reopening after a comparison must respect the caller's focus restore;
      // only a newly incremented request should focus a disclosure again.
      previousRequest.current = focusRequest;
      return;
    }
    if (!focusRequest || previousRequest.current === focusRequest) return;
    let frame = 0;
    const focusSection = (attempt = 0) => {
      const target = sectionRefs.current[view];
      if (!target) { if (attempt < 3) frame = requestAnimationFrame(() => focusSection(attempt + 1)); return; }
      previousRequest.current = focusRequest;
      target.focus({ preventScroll: true });
      const scroll = target.closest<HTMLElement>('[data-slot="document-context-scroll"]');
      if (scroll) scroll.scrollTop += target.getBoundingClientRect().top - scroll.getBoundingClientRect().top;
    };
    frame = requestAnimationFrame(() => focusSection());
    return () => cancelAnimationFrame(frame);
  }, [focusRequest, focusTargetRef, open, view, docked]);
  const onPanelClick: MouseEventHandler<HTMLElement> = event => {
    if (!(event.target instanceof Element)) return;
    const href = event.target.closest("a")?.getAttribute("href");
    if (!href?.startsWith("#")) return;
    const canvas = markerRef.current?.parentElement?.querySelector<HTMLElement>("#document-reading-canvas");
    const heading = canvas?.querySelector<HTMLElement>(`[id="${CSS.escape(href.slice(1))}"]`);
    if (!canvas || !heading) return;
    event.preventDefault();
    canvas.scrollTop += heading.getBoundingClientRect().top - canvas.getBoundingClientRect().top - canvas.clientTop;
  };
  const rememberFocus: FocusEventHandler<HTMLElement> = event => { if (event.target instanceof HTMLElement && event.currentTarget.contains(event.target)) lastPanelFocus.current = event.target; };
  const body = <>
    <div className="document-panel-heading">
      {docked ? <h2 id={`${id}-heading`}>Document details</h2> : <DialogTitle>Document details</DialogTitle>}
      <Button ref={closeRef} type="button" variant="ghost" size="icon" aria-label="Close document panel" onClick={() => onOpenChange(false)}><X className="h-4 w-4" aria-hidden /></Button>
    </div>
    <div data-slot="document-context-scroll" className="document-panel-scroll rail-scroll rail-scroll-auto">
      <section className="document-panel-outline" aria-labelledby={`${id}-outline`}><h3 id={`${id}-outline`}>On this page</h3>{outline}</section>
      <div className="document-panel-sections">{sections.map(section => <details key={section.id} data-context-section={section.id} open={view === section.id}
        onToggle={event => { if (event.currentTarget.open && view !== section.id) onViewChange(section.id); else if (!event.currentTarget.open && view === section.id) onViewChange("outline"); }}>
        <summary ref={element => { sectionRefs.current[section.id] = element; }}><span>{section.label}</span><ChevronDown className="h-3.5 w-3.5" aria-hidden /></summary>
        {view === section.id && <div className="document-panel-section-body">{({ info, history, relations })[section.id]}</div>}
      </details>)}</div>
    </div>
  </>;
  return <><span ref={markerRef} hidden /><Dialog open={open} onOpenChange={onOpenChange} modal={!docked}>
    {docked ? open && <DismissableLayer asChild
      onEscapeKeyDown={event => {
        // Register above an enclosing preview's capture listener so Escape
        // closes this inspector before it can dismiss the document route.
        event.preventDefault();
        if (!hasForegroundDialog()) onOpenChange(false);
      }}
      onInteractOutside={event => event.preventDefault()}>
      <aside ref={panelRef} id={id} tabIndex={-1} aria-labelledby={`${id}-heading`} data-slot="document-context-panel" data-mode="docked"
      className="document-details-panel document-details-docked" onFocusCapture={rememberFocus} onClickCapture={onPanelClick}
      >{body}</aside>
    </DismissableLayer>
      : <DialogPortal><DialogOverlay /><DialogPrimitive.Content ref={element => { panelRef.current = element; }} id={id}
        data-slot="document-context-panel" data-mode="overlay" aria-modal="true" aria-describedby={undefined} className={cn("document-details-panel", "document-details-overlay")}
        onOpenAutoFocus={event => { event.preventDefault(); if (focusRequestedTarget()) return; if (pendingInsideFocus.current) restoreInsideFocus(); else if (!panelRef.current?.contains(document.activeElement)) closeRef.current?.focus({ preventScroll: true }); }}
        onCloseAutoFocus={event => { event.preventDefault(); requestAnimationFrame(() => { if (latestOpen.current) restoreInsideFocus(); else restoreTrigger(); }); }}
        onFocusCapture={rememberFocus} onClickCapture={onPanelClick}>{body}</DialogPrimitive.Content></DialogPortal>}
  </Dialog></>;
}
