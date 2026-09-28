import { useCallback, useId, useLayoutEffect, useRef, useState, type FocusEventHandler, type MouseEventHandler, type ReactNode } from "react";
import * as DialogPrimitive from "@radix-ui/react-dialog";
import { DismissableLayer } from "@radix-ui/react-dismissable-layer";
import { History, Info, Link2, ListTree, X } from "lucide-react";
import { Dialog, DialogOverlay, DialogPortal, DialogTitle } from "@/components/ui/dialog";
import { Tooltip, TooltipContent, TooltipProvider, TooltipTrigger } from "@/components/ui/tooltip";
import { cn } from "@/lib/utils";
import { useModalCount } from "@/lib/modal-visibility";

const views = [
  { id: "info", label: "Document info", heading: "Document info", icon: Info },
  { id: "outline", label: "Table of contents", heading: "On this page", icon: ListTree },
  { id: "relations", label: "Relations", heading: "Relations", icon: Link2 },
  { id: "history", label: "Version history", heading: "Version history", icon: History },
] as const;

type ContextView = typeof views[number]["id"];

function focusHeading(heading: HTMLElement) {
  if (!heading.hasAttribute("tabindex")) {
    heading.tabIndex = -1;
    heading.addEventListener("blur", () => heading.removeAttribute("tabindex"), { once: true });
  }
  // scrollIntoView also scrolls overflow:hidden ancestors. A tall collection
  // tree can give the app shell hidden overflow even when this article is
  // short, pulling both sidebars under the header and leaving a bottom gap.
  // Only the reader owns heading navigation; never scroll its outer shells.
  const canvas = heading.closest<HTMLElement>("#document-reading-canvas");
  if (canvas) {
    canvas.scrollTop += heading.getBoundingClientRect().top - canvas.getBoundingClientRect().top - canvas.clientTop;
  }
  heading.focus({ preventScroll: true });
}

/** Context floats above the article without changing its measure. Small or
 * short reading workspaces use a modal so its controls remain reachable. */
export function DocumentContextPanel({ open, onOpenChange, view, onViewChange, children }: {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  view: ContextView;
  onViewChange: (view: ContextView) => void;
  children: ReactNode;
}) {
  const id = useId();
  const railRef = useRef<HTMLDivElement>(null);
  const closeRef = useRef<HTMLButtonElement>(null);
  const headingFocusRef = useRef<HTMLElement | null>(null);
  const triggerRefs = useRef<Partial<Record<ContextView, HTMLButtonElement | null>>>({});
  const [floating, setFloating] = useState(false);
  const previousFloatingOpen = useRef(false);
  const restoreTriggerFocus = useRef(true);
  const latestOpen = useRef(open);
  const current = views.find(item => item.id === view)!;
  const modalCount = useModalCount();
  const supportingDialog = useRef(false);
  const lastPanelFocus = useRef<HTMLElement | null>(null);

  const hasForegroundDialog = useCallback(() => {
    const rail = railRef.current;
    return Boolean(rail && Array.from(rail.ownerDocument.querySelectorAll('[role="dialog"], [role="alertdialog"]'))
      .some(dialog => dialog.id !== id && dialog.getAttribute("data-state") !== "closed" && dialog.getAttribute("aria-hidden") !== "true" && !dialog.contains(rail)));
  }, [id]);

  const restoreFocus = useCallback(() => {
    const heading = headingFocusRef.current;
    headingFocusRef.current = null;
    if (heading) focusHeading(heading);
    else if (restoreTriggerFocus.current) triggerRefs.current[view]?.focus({ preventScroll: true });
  }, [view]);

  useLayoutEffect(() => {
    latestOpen.current = open;
    const floatingOpen = open && floating;
    if (floatingOpen && !previousFloatingOpen.current) closeRef.current?.focus({ preventScroll: true });
    if (!open && previousFloatingOpen.current) restoreFocus();
    previousFloatingOpen.current = floatingOpen;
  }, [open, floating, restoreFocus]);

  useLayoutEffect(() => {
    const layout = railRef.current?.parentElement;
    if (!layout) return;
    const measure = () => {
      // Changing the presentation remounts children. Defer that while a
      // property/relation dialog owns a live draft or keyboard focus.
      if (latestOpen.current && hasForegroundDialog()) {
        supportingDialog.current = true;
        return;
      }
      if (supportingDialog.current) {
        supportingDialog.current = false;
        // Supporting dialogs without a DialogTrigger cannot restore focus
        // themselves. Keep their opener if this presentation survives resize.
        const opener = lastPanelFocus.current;
        requestAnimationFrame(() => {
          if (latestOpen.current && opener?.isConnected && !hasForegroundDialog()) opener.focus({ preventScroll: true });
        });
      }
      const rem = parseFloat(getComputedStyle(document.documentElement).fontSize) || 16;
      const bounds = layout.getBoundingClientRect();
      // Leave visible article space beside the 24rem popover and 3rem rail.
      setFloating(bounds.width >= 48 * rem && bounds.height >= 20 * rem);
    };
    measure();
    if (typeof ResizeObserver === "undefined") return;
    const observer = new ResizeObserver(measure);
    observer.observe(layout);
    // Closing a supporting dialog restores focus even if no resize follows.
    layout.ownerDocument.addEventListener("focusin", measure);
    return () => {
      observer.disconnect();
      layout.ownerDocument.removeEventListener("focusin", measure);
    };
  }, [hasForegroundDialog, modalCount]);

  const controls = (insidePanel = false) => <TooltipProvider delayDuration={250}>
    <div role="group" aria-label="Document context" className={cn("flex gap-1", insidePanel ? "border-b border-border px-3 py-1" : "flex-col items-center py-2")}>
      {views.map(item => {
        const active = open && view === item.id;
        return <Tooltip key={item.id}>
          <TooltipTrigger asChild>
            <button
              ref={insidePanel ? undefined : element => { triggerRefs.current[item.id] = element; }}
              type="button"
              aria-label={item.label}
              aria-expanded={active}
              aria-controls={active ? id : undefined}
              onClick={() => {
                restoreTriggerFocus.current = true;
                onViewChange(item.id);
                onOpenChange(insidePanel || !active);
              }}
              className={cn("relative inline-flex h-11 w-11 shrink-0 items-center justify-center rounded-[var(--radius-sm)] transition-token cursor-pointer focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-inset focus-visible:ring-ring",
                active ? "bg-surface-selected text-surface-selected-foreground" : "text-foreground-muted hover:bg-surface-hover hover:text-foreground")}
            >
              <item.icon className="h-4 w-4" aria-hidden />
            </button>
          </TooltipTrigger>
          <TooltipContent side={insidePanel ? "bottom" : "left"}>{item.label}</TooltipContent>
        </Tooltip>;
      })}
    </div>
  </TooltipProvider>;

  const onPanelClick: MouseEventHandler<HTMLElement> = event => {
    if (!(event.target instanceof Element)) return;
    const href = event.target.closest("a")?.getAttribute("href");
    if (!href?.startsWith("#")) return;
    const heading = railRef.current?.parentElement?.querySelector<HTMLElement>(`[id="${CSS.escape(href.slice(1))}"]`);
    if (!heading) return;
    // Native hash navigation discards the route-backed preview's history
    // state. Navigate within this article without closing its parent preview.
    event.preventDefault();
    headingFocusRef.current = heading;
    onOpenChange(false);
  };
  const closePanel = () => {
    restoreTriggerFocus.current = true;
    onOpenChange(false);
  };
  const rememberPanelFocus: FocusEventHandler<HTMLElement> = event => {
    // React focus events from portalled child dialogs also bubble here.
    if (event.target instanceof HTMLElement && event.currentTarget.contains(event.target)) lastPanelFocus.current = event.target;
  };
  const panelClass = "flex min-h-0 flex-col overflow-hidden bg-surface text-foreground outline-none";
  const panelBody = <>
    <div className="flex h-12 shrink-0 items-center justify-between gap-2 border-b border-border px-4">
      {floating ? <h2 id={`${id}-heading`} className="text-sm font-semibold tracking-normal">{current.heading}</h2>
        : <DialogTitle className="text-sm font-semibold tracking-normal">{current.heading}</DialogTitle>}
      <button ref={closeRef} type="button" aria-label="Close document panel" onClick={closePanel}
        className="inline-flex h-11 w-11 shrink-0 items-center justify-center rounded-[var(--radius-sm)] text-foreground-muted hover:bg-surface-hover hover:text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring">
        <X className="h-4 w-4" aria-hidden />
      </button>
    </div>
    {!floating && controls(true)}
    <div className="min-h-0 flex-1 overflow-y-auto px-4 py-4 rail-scroll rail-scroll-auto">{children}</div>
  </>;

  return <Dialog open={open} onOpenChange={onOpenChange} modal={!floating}>
    {floating ? open && <DismissableLayer asChild onDismiss={() => onOpenChange(false)}
      onEscapeKeyDown={() => { restoreTriggerFocus.current = true; }}
      onInteractOutside={event => {
        const rail = railRef.current;
        // Edge controls switch/toggle without an intermediate dismissal.
        // Properties editing is a sibling portal; a nested modal must not
        // dismiss its supporting inspector when it takes focus.
        if ((event.target instanceof Node && rail?.contains(event.target)) || hasForegroundDialog()) {
          event.preventDefault();
          return;
        }
        // Let outside clicks and Tab keep their intended target. Only explicit
        // Close/Escape should return keyboard focus to the originating icon.
        restoreTriggerFocus.current = false;
      }}>
      <aside id={id} tabIndex={-1} aria-labelledby={`${id}-heading`} data-slot="document-context-panel" data-mode="floating"
        onFocusCapture={rememberPanelFocus} onClickCapture={onPanelClick} className={cn(panelClass, "absolute right-14 top-2 z-[var(--z-overlay)] max-h-[calc(100%-1rem)] w-96 rounded-[var(--radius-md)] border border-border-strong shadow-md")}>
        {panelBody}
      </aside>
    </DismissableLayer> : <DialogPortal><DialogOverlay />
      <DialogPrimitive.Content id={id} data-slot="document-context-panel" data-mode="overlay" aria-modal="true" aria-describedby={undefined}
        onOpenAutoFocus={event => { event.preventDefault(); closeRef.current?.focus({ preventScroll: true }); }}
        onCloseAutoFocus={event => {
          event.preventDefault();
          // Resizing an open overlay into a floating panel is not a dismissal.
          if (latestOpen.current) { closeRef.current?.focus({ preventScroll: true }); return; }
          requestAnimationFrame(restoreFocus);
        }}
        onFocusCapture={rememberPanelFocus} onClickCapture={onPanelClick}
        className={cn(panelClass, "fixed inset-y-0 right-0 z-[var(--z-modal)] w-[calc(100%-2rem)] max-w-96 border-l border-border shadow-lg")}>
        {panelBody}
      </DialogPrimitive.Content>
    </DialogPortal>}
    <div ref={railRef} data-slot="document-context-rail" className="h-full w-12 shrink-0 overflow-y-auto border-l border-border bg-surface rail-scroll rail-scroll-auto">
      {controls()}
    </div>
  </Dialog>;
}
