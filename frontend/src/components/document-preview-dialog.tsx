import { useRef, useState, type CSSProperties } from "react";
import { useLocation, useNavigate } from "react-router-dom";
import DocumentPage from "@/pages/document";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogTitle,
} from "@/components/ui/dialog";
import { documentPreviewReturnFocusId } from "@/lib/document-preview-navigation";

function readWorkspaceLeftOffset() {
  if (typeof document === "undefined" || typeof window === "undefined") return 32;
  if (!window.matchMedia("(min-width: 1024px)").matches) return 16;

  const navigation = document.getElementById("vault-workspace-navigation");
  if (!navigation) return 32;
  return Math.max(32, Math.round(navigation.getBoundingClientRect().right + 16));
}

/**
 * Route-backed reading surface launched by search results. The background
 * route remains mounted, so closing the dialog restores its exact query,
 * filters, scroll position, and focused result.
 */
export function DocumentPreviewDialog() {
  const navigate = useNavigate();
  const location = useLocation();
  const contentRef = useRef<HTMLDivElement | null>(null);
  const closingRef = useRef(false);
  const [desktopLeftOffset] = useState(readWorkspaceLeftOffset);
  const returnFocusId = documentPreviewReturnFocusId(location);

  function closePreview() {
    // Radix can report the same outside interaction through both the overlay
    // and onOpenChange. Keep route-backed dismissal to a single history step.
    if (closingRef.current) return;
    closingRef.current = true;
    navigate(-1);
    if (!returnFocusId) return;
    window.requestAnimationFrame(() => {
      window.requestAnimationFrame(() => {
        document.getElementById(returnFocusId)?.focus();
      });
    });
  }

  return (
    <Dialog
      open
      onOpenChange={(open) => {
        if (!open) closePreview();
      }}
    >
      <DialogContent
        ref={contentRef}
        data-testid="document-preview-dialog"
        className="flex h-dvh max-h-none w-full max-w-none flex-col gap-0 !overflow-hidden rounded-none border-0 p-0 sm:h-[calc(100dvh-1rem)] sm:w-[calc(100%-1rem)] sm:rounded-[var(--radius-xl)] sm:border lg:left-[min(var(--document-preview-left),calc(100vw-57rem))] lg:right-8 lg:h-[calc(100dvh-4rem)] lg:w-auto lg:translate-x-0"
        style={
          {
            "--document-preview-left": `${desktopLeftOffset}px`,
          } as CSSProperties
        }
        overlayProps={{
          className: "cursor-pointer",
          onClick: (event) => {
            if (event.target !== event.currentTarget) return;
            closePreview();
          },
        }}
        onOpenAutoFocus={(event) => {
          event.preventDefault();
          contentRef.current?.focus();
        }}
        onCloseAutoFocus={(event) => event.preventDefault()}
      >
        <DialogTitle className="sr-only">Document preview</DialogTitle>
        <DialogDescription className="sr-only">
          Read this document without leaving the current search results.
        </DialogDescription>
        <DocumentPage presentation="preview" />
      </DialogContent>
    </Dialog>
  );
}
