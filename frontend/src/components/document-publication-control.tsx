import { useEffect, useId, useImperativeHandle, useRef, useState, type Ref } from "react";
import { Link } from "react-router-dom";
import { Copy, ExternalLink, Globe2, X } from "lucide-react";
import { Alert } from "@/components/ui/alert";
import { Button } from "@/components/ui/button";
import { ConfirmDialog } from "@/components/ui/confirm-dialog";
import { PublishOptionsForm } from "@/components/publish-options-dialog";
import type { Publication } from "@/lib/api";
import { Popover, PopoverClose, PopoverContent, PopoverTrigger } from "@/components/ui/popover";
import { Label } from "@/components/ui/label";
import { Textarea } from "@/components/ui/textarea";
import { Tooltip, TooltipContent, TooltipProvider, TooltipTrigger } from "@/components/ui/tooltip";

interface DocumentPublicationControlProps {
  vault: string;
  docId: string;
  publicSlug?: string | null;
  disabledReason?: string;
  onPublished: (slug: string, publication?: Publication) => void;
  onUnpublish: () => Promise<void>;
  triggerRef?: Ref<HTMLButtonElement>;
}

export function DocumentPublicationControl({
  vault,
  docId,
  publicSlug,
  disabledReason,
  onPublished,
  onUnpublish,
  triggerRef,
}: DocumentPublicationControlProps) {
  const [open, setOpen] = useState(false);
  const [confirmOpen, setConfirmOpen] = useState(false);
  const [publishing, setPublishing] = useState(false);
  const [portalContainer, setPortalContainer] = useState<HTMLElement | null>(null);
  const [copying, setCopying] = useState(false);
  const [copyResult, setCopyResult] = useState<"copied" | "error" | null>(null);
  const buttonRef = useRef<HTMLButtonElement>(null);
  const unpublishRef = useRef<HTMLButtonElement>(null);
  const confirmReturnFocusRef = useRef<HTMLElement | null>(null);
  const urlRef = useRef<HTMLTextAreaElement>(null);
  const publishedRef = useRef(false);
  const urlId = useId();
  const reasonId = useId();
  const titleId = useId();
  const descriptionId = useId();
  const publishDisabled = !publicSlug && Boolean(disabledReason);
  const publicUrl = publicSlug
    ? `${window.location.origin}/p/${encodeURIComponent(publicSlug)}`
    : "";

  useImperativeHandle(triggerRef, () => buttonRef.current!, []);

  useEffect(() => {
    if (publishDisabled) setOpen(false);
  }, [publishDisabled]);

  useEffect(() => {
    if (publicSlug && publishedRef.current) {
      publishedRef.current = false;
      urlRef.current?.focus({ preventScroll: true });
    }
  }, [publicSlug]);

  async function copyLink() {
    setCopyResult(null);
    setCopying(true);
    try {
      if (!navigator.clipboard?.writeText) throw new Error("Clipboard unavailable");
      await navigator.clipboard.writeText(publicUrl);
      setCopyResult("copied");
    } catch {
      setCopyResult("error");
      urlRef.current?.focus();
      urlRef.current?.select();
    } finally {
      setCopying(false);
    }
  }

  async function unpublish() {
    // Recheck the current permission if access changes while confirming.
    if (disabledReason) throw new Error(disabledReason);
    await onUnpublish();
    confirmReturnFocusRef.current = buttonRef.current;
    setOpen(false);
  }

  const header = (
    <div className={`space-y-1${publicSlug ? "" : " pt-4"}`}>
      <div className="flex items-center justify-between gap-3">
        <h2 id={titleId} className="text-sm font-semibold">{publicSlug ? "Public link" : "Publish document"}</h2>
        <PopoverClose asChild>
          <Button type="button" variant="ghost" size="icon" className="h-8 w-8 -mr-1" aria-label={publicSlug ? "Close public link" : "Close publish options"} disabled={publishing}>
            <X className="h-4 w-4" aria-hidden />
          </Button>
        </PopoverClose>
      </div>
      <p id={descriptionId} className="text-xs leading-relaxed text-foreground-muted">
        {publicSlug
          ? "Share a read-only view of this document. Access depends on the link’s limits."
          : "Anyone with the link can read this document without signing in. Add access limits below if needed."}
      </p>
    </div>
  );

  return (
    <>
      <Popover
        modal={false}
        open={open && !publishDisabled}
        onOpenChange={(nextOpen) => {
          if (confirmOpen || publishing || (nextOpen && publishDisabled)) return;
          if (nextOpen) {
            setCopyResult(null);
            // Keep the popover inside a preview dialog's focus boundary.
            setPortalContainer(buttonRef.current?.closest<HTMLElement>('[role="dialog"]') ?? null);
          }
          setOpen(nextOpen);
        }}
      >
        <TooltipProvider delayDuration={300}>
          <Tooltip>
            <TooltipTrigger asChild>
              <PopoverTrigger asChild>
                <Button
                  ref={buttonRef}
                  type="button"
                  variant="outline"
                  size="sm"
                  data-reader-control
                  aria-haspopup="dialog"
                  aria-expanded={open && !publishDisabled}
                  aria-disabled={publishDisabled || undefined}
                  aria-describedby={publishDisabled ? reasonId : undefined}
                  className={`text-sm @max-[32rem]/resource-commands:px-2${publishDisabled ? " opacity-50" : ""}`}
                  onClick={(event) => {
                    if (publishDisabled) event.preventDefault();
                  }}
                >
                  <Globe2 className="h-4 w-4 @max-[32rem]/resource-commands:hidden" aria-hidden />
                  {publicSlug ? "Public link" : "Publish"}
                </Button>
              </PopoverTrigger>
            </TooltipTrigger>
            {publishDisabled && <TooltipContent>{disabledReason}</TooltipContent>}
          </Tooltip>
        </TooltipProvider>
        {disabledReason && <span id={reasonId} className="sr-only">{disabledReason}</span>}

        <PopoverContent
          portalContainer={portalContainer}
          collisionBoundary={portalContainer}
          aria-labelledby={titleId}
          aria-describedby={descriptionId}
          className={`w-[21.25rem] ${publicSlug ? "grid gap-4" : "flex flex-col overflow-hidden p-0"}${confirmOpen ? " invisible" : ""}`}
          onEscapeKeyDown={(event) => {
            if (publishing) event.preventDefault();
          }}
          onInteractOutside={(event) => {
            // The confirmation is a separate portal; retain this panel so
            // cancelling can return to the action that opened it.
            if (confirmOpen || publishing) event.preventDefault();
          }}
        >
          {publicSlug && header}
          {!publicSlug ? <PublishOptionsForm
            vault={vault}
            docId={docId}
            compact
            working={publishing}
            onWorkingChange={setPublishing}
            disabledReason={disabledReason}
            header={header}
            onCancel={() => setOpen(false)}
            onPublished={(slug, publication) => {
              publishedRef.current = true;
              onPublished(slug, publication);
            }}
          /> : <>
          <div className="space-y-2">
            <Label htmlFor={urlId}>Public URL</Label>
            <Textarea
              ref={urlRef}
              id={urlId}
              readOnly
              value={publicUrl}
              rows={2}
              className="min-h-0 resize-none font-mono text-xs"
              onFocus={(event) => event.currentTarget.select()}
            />
            <div className="flex flex-wrap gap-2">
              <Button type="button" variant="outline" size="sm" onClick={copyLink} loading={copying}>
                <Copy className="h-4 w-4" aria-hidden />
                Copy link
              </Button>
              <Button variant="outline" size="sm" asChild>
                <a href={publicUrl} target="_blank" rel="noopener noreferrer">
                  <ExternalLink className="h-4 w-4" aria-hidden />
                  Open link
                </a>
              </Button>
            </div>
            {copyResult === "copied" && <p role="status" className="text-xs text-foreground-muted">Link copied.</p>}
            {copyResult === "error" && (
              <Alert variant="destructive">Could not copy the link. Select the URL above and copy it manually.</Alert>
            )}
          </div>
          <Link
            to={`/vault/${encodeURIComponent(vault)}/publications`}
            className="text-sm text-link hover:text-link-hover hover:underline rounded-[var(--radius-sm)] focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2 focus-visible:ring-offset-surface"
          >
            Manage links and access limits
          </Link>
          <div className="border-t border-border pt-4">
            <TooltipProvider delayDuration={300}>
              <Tooltip>
                <TooltipTrigger asChild>
                  <Button
                    ref={unpublishRef}
                    type="button"
                    variant="outline"
                    size="sm"
                    aria-haspopup="dialog"
                    aria-expanded={confirmOpen}
                    aria-disabled={Boolean(disabledReason) || undefined}
                    aria-describedby={disabledReason ? reasonId : undefined}
                    className={disabledReason ? "opacity-50" : undefined}
                    onClick={() => {
                      if (disabledReason) return;
                      confirmReturnFocusRef.current = unpublishRef.current;
                      setConfirmOpen(true);
                    }}
                  >
                    Unpublish
                  </Button>
                </TooltipTrigger>
                {disabledReason && <TooltipContent>{disabledReason}</TooltipContent>}
              </Tooltip>
            </TooltipProvider>
          </div>
          </>}
        </PopoverContent>
      </Popover>
      <ConfirmDialog
        open={confirmOpen}
        onOpenChange={setConfirmOpen}
        title="Unpublish this document?"
        description="All public links to this document will stop working. The document will remain in this Vault."
        confirmLabel="Unpublish"
        variant="destructive"
        onConfirm={unpublish}
        returnFocusRef={confirmReturnFocusRef}
      />
    </>
  );
}
