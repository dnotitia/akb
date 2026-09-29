import { useRef } from "react";
import { useLocation, useNavigate } from "react-router-dom";
import { useQuery } from "@tanstack/react-query";
import { getMe } from "@/lib/api";
import { CurrentUserProvider } from "@/contexts/current-user-context";
import DocumentPage from "@/pages/document";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogTitle,
} from "@/components/ui/dialog";
import { X } from "lucide-react";
import { DocumentPreviewEditorContext, type PreviewEditorSession } from "@/contexts/document-preview-editor-context";
import { ResourceNavigationProvider } from "@/contexts/resource-navigation-context";

/**
 * Route-backed reading surface launched by search results or notifications. The background
 * route remains mounted, so closing the dialog restores its exact query,
 * filters, scroll position, and focused result.
 */
export function DocumentPreviewDialog() {
  const navigate = useNavigate();
  const location = useLocation();
  // Read/Edit changes replace the history entry. Do not reset the identity
  // provider (and its editor/draft/scroll state) for every mode change.
  const sessionKey = useRef(location.key);
  const user = useQuery({ queryKey: ["document-preview-user", sessionKey.current], queryFn: () => getMe(), retry: false });
  const contentRef = useRef<HTMLDivElement | null>(null);
  const closingRef = useRef(false);
  const editorSessionRef = useRef<PreviewEditorSession | null>(null);

  function closePreview() {
    // Radix can report the same outside interaction through both the overlay
    // and onOpenChange. Keep route-backed dismissal to a single history step.
    if (closingRef.current) return;
    closingRef.current = true;
    navigate(-1);
  }

  function requestClose() {
    if (editorSessionRef.current) editorSessionRef.current.requestExit(closePreview);
    else closePreview();
  }

  return (
    <Dialog
      open
      // Native file-picker focus transitions can request false. Only explicit
      // close, Escape, or a reading-mode backdrop click may dismiss this route.
      onOpenChange={() => {}}
    >
      <DialogContent
        ref={contentRef}
        hideClose
        data-testid="document-preview-dialog"
        className="document-preview-surface flex max-h-none max-w-none flex-col gap-0 !overflow-hidden rounded-none border-0 p-0 sm:rounded-[var(--radius-xl)] sm:border"
        overlayProps={{
          className: "cursor-pointer",
          onClick: (event) => {
            if (event.target !== event.currentTarget) return;
            if (!editorSessionRef.current) requestClose();
          },
        }}
        onInteractOutside={(event) => event.preventDefault()}
        onEscapeKeyDown={(event) => {
          event.preventDefault();
          if (editorSessionRef.current?.dismissMenu()) return;
          requestClose();
        }}
        onOpenAutoFocus={(event) => {
          event.preventDefault();
          contentRef.current?.focus();
        }}
        onCloseAutoFocus={(event) => {
          event.preventDefault();
          // AppRoutes restores the committed source entry after this focus
          // scope releases, including history Back and resumed quick search.
        }}
      >
        <DialogTitle className="sr-only">Document preview</DialogTitle>
        <DialogDescription className="sr-only">
          Read this document without leaving the page you opened it from.
        </DialogDescription>
        <button type="button" aria-label="Close dialog" onClick={requestClose} className="absolute right-2 top-2 inline-flex h-9 w-9 items-center justify-center rounded-[var(--radius-sm)] text-foreground-muted transition-colors hover:bg-surface-hover hover:text-foreground focus-ring-instant">
          <X className="h-4 w-4" aria-hidden />
        </button>
        <DocumentPreviewEditorContext.Provider value={editorSessionRef}>
          <ResourceNavigationProvider>
            <CurrentUserProvider user={user.data ?? null}><DocumentPage presentation="preview" /></CurrentUserProvider>
          </ResourceNavigationProvider>
        </DocumentPreviewEditorContext.Provider>
      </DialogContent>
    </Dialog>
  );
}
