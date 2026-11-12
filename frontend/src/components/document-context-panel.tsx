import { useLayoutEffect, useRef, type ReactNode, type RefObject } from "react";
import { X } from "lucide-react";
import { Dialog, DialogContent, DialogTitle } from "@/components/ui/dialog";
import { Tabs, TabsList, TabsTrigger, TabsContent } from "@/components/ui/tabs";
import { Button } from "@/components/ui/button";
import { useModalCount } from "@/lib/modal-visibility";

export type DocumentContextView = "info" | "history" | "relations";

/** Document information has its own stable surface; reading navigation stays in Contents. */
export function DocumentContextPanel({ id, open, onOpenChange, view, onViewChange, triggerRef, info, history, relations, focusRequest = 0, focusTargetRef, onFocusTarget }: {
  id?: string; open: boolean; onOpenChange: (open: boolean) => void;
  view: DocumentContextView; onViewChange: (view: DocumentContextView) => void;
  triggerRef: RefObject<HTMLButtonElement | null>;
  info: ReactNode; history: ReactNode; relations: ReactNode; focusRequest?: number;
  focusTargetRef?: RefObject<HTMLElement | null>; onFocusTarget?: () => void;
}) {
  const panel = useRef<HTMLDivElement>(null);
  const activeTab = useRef<HTMLButtonElement>(null);
  const close = useRef<HTMLButtonElement>(null);
  const previousRequest = useRef(0);
  const modalCount = useModalCount();
  const lastInsideFocus = useRef<HTMLElement | null>(null);
  const supportingOpener = useRef<HTMLElement | null>(null);
  const supportingOpen = useRef(false);
  const connections = view === "relations";
  const restoreTarget = () => {
    const target = focusTargetRef?.current;
    if (!target?.isConnected || !panel.current?.contains(target)) return false;
    target.focus({ preventScroll: true }); onFocusTarget?.(); return true;
  };
  useLayoutEffect(() => {
    if (!open) { previousRequest.current = focusRequest; return; }
    const frame = requestAnimationFrame(() => {
      if (restoreTarget()) return;
      if (focusRequest && previousRequest.current !== focusRequest && !focusTargetRef) (activeTab.current ?? close.current)?.focus({ preventScroll: true });
      previousRequest.current = focusRequest;
    });
    return () => cancelAnimationFrame(frame);
  });
  useLayoutEffect(() => {
    if (!open) { supportingOpen.current = false; return; }
    // Modal registration precedes the portal commit. Inspect after that commit
    // so a sibling property/relation editor can return to its real opener.
    const frame = requestAnimationFrame(() => {
      const dialogs = [...document.querySelectorAll<HTMLElement>('[role="dialog"], [role="alertdialog"]')]
        .filter(dialog => dialog.getAttribute("data-state") !== "closed" && dialog.getAttribute("aria-hidden") !== "true");
      const foreground = dialogs.at(-1);
      if (foreground && foreground !== panel.current && !foreground.contains(panel.current)) {
        if (!supportingOpen.current) supportingOpener.current = lastInsideFocus.current;
        supportingOpen.current = true; return;
      }
      if (!supportingOpen.current) return;
      supportingOpen.current = false;
      const target = supportingOpener.current;
      if (target?.isConnected && panel.current?.contains(target)) target.focus({ preventScroll: true });
    });
    return () => cancelAnimationFrame(frame);
  }, [modalCount, open]);
  return <Dialog open={open} onOpenChange={onOpenChange}>
    <DialogContent ref={panel} id={id} hideClose aria-modal="true" aria-describedby={undefined} className="document-info-dialog"
      data-slot="document-context-panel" data-mode="dialog"
      onFocusCapture={event => { if (event.target instanceof HTMLElement && panel.current?.contains(event.target)) lastInsideFocus.current = event.target; }}
      onEscapeKeyDown={event => { event.preventDefault(); event.stopPropagation(); onOpenChange(false); }}
      onOpenAutoFocus={event => { event.preventDefault(); if (!restoreTarget()) (activeTab.current ?? close.current)?.focus({ preventScroll: true }); }}
      onCloseAutoFocus={event => { event.preventDefault(); if (triggerRef.current?.isConnected) triggerRef.current.focus({ preventScroll: true }); }}>
      <div className="document-info-heading">
        <DialogTitle>{connections ? "Connections" : "Details"}</DialogTitle>
        <Button ref={close} type="button" variant="ghost" size="icon" aria-label={connections ? "Close connections" : "Close details"} onClick={() => onOpenChange(false)}><X size={16} aria-hidden /></Button>
      </div>
      {connections ? <div className="document-info-content rail-scroll">{relations}</div> :
        <Tabs value={view} onValueChange={value => onViewChange(value as DocumentContextView)} activationMode="manual" className="document-info-tabs">
          <TabsList aria-label="Document information" className="document-info-tab-list">
            <TabsTrigger ref={view === "info" ? activeTab : undefined} value="info">Properties</TabsTrigger>
            <TabsTrigger ref={view === "history" ? activeTab : undefined} value="history">Version history</TabsTrigger>
          </TabsList>
          <TabsContent value="info" className="document-info-content rail-scroll">{info}</TabsContent>
          <TabsContent value="history" className="document-info-content rail-scroll">{history}</TabsContent>
        </Tabs>}
    </DialogContent>
  </Dialog>;
}
