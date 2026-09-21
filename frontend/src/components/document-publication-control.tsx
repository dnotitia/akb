import { useId, useImperativeHandle, useRef, useState, type Ref } from "react";
import { Link } from "react-router-dom";
import { Copy, ExternalLink, Globe2 } from "lucide-react";
import { Alert } from "@/components/ui/alert";
import { Button } from "@/components/ui/button";
import { ConfirmDialog } from "@/components/ui/confirm-dialog";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Label } from "@/components/ui/label";
import { Textarea } from "@/components/ui/textarea";
import { Tooltip, TooltipContent, TooltipProvider, TooltipTrigger } from "@/components/ui/tooltip";

interface DocumentPublicationControlProps {
  vault: string;
  publicSlug?: string | null;
  disabledReason?: string;
  onPublish: () => void;
  onUnpublish: () => Promise<void>;
  triggerRef?: Ref<HTMLButtonElement>;
}

export function DocumentPublicationControl({
  vault,
  publicSlug,
  disabledReason,
  onPublish,
  onUnpublish,
  triggerRef,
}: DocumentPublicationControlProps) {
  const [open, setOpen] = useState(false);
  const [confirmOpen, setConfirmOpen] = useState(false);
  const [copying, setCopying] = useState(false);
  const [copyResult, setCopyResult] = useState<"copied" | "error" | null>(null);
  const buttonRef = useRef<HTMLButtonElement>(null);
  const unpublishRef = useRef<HTMLButtonElement>(null);
  const confirmReturnFocusRef = useRef<HTMLElement | null>(null);
  const urlRef = useRef<HTMLTextAreaElement>(null);
  const urlId = useId();
  const reasonId = useId();
  const publishDisabled = !publicSlug && Boolean(disabledReason);
  const publicUrl = publicSlug
    ? `${window.location.origin}/p/${encodeURIComponent(publicSlug)}`
    : "";

  useImperativeHandle(triggerRef, () => buttonRef.current!, []);

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

  return (
    <>
      <TooltipProvider delayDuration={300}>
        <Tooltip>
          <TooltipTrigger asChild>
            <Button
              ref={buttonRef}
              type="button"
              variant="outline"
              size="sm"
              data-reader-control
              aria-haspopup="dialog"
              aria-expanded={publicSlug ? open : undefined}
              aria-disabled={publishDisabled || undefined}
              aria-describedby={publishDisabled ? reasonId : undefined}
              className={`text-sm${publishDisabled ? " opacity-50" : ""}`}
              onClick={() => {
                if (publishDisabled) return;
                if (!publicSlug) {
                  onPublish();
                  return;
                }
                setCopyResult(null);
                setOpen(true);
              }}
            >
              <Globe2 className="h-4 w-4" aria-hidden />
              {publicSlug ? "Public link" : "Publish"}
            </Button>
          </TooltipTrigger>
          {publishDisabled && <TooltipContent>{disabledReason}</TooltipContent>}
        </Tooltip>
      </TooltipProvider>
      {disabledReason && <span id={reasonId} className="sr-only">{disabledReason}</span>}

      <Dialog open={open && Boolean(publicSlug)} onOpenChange={setOpen}>
        <DialogContent
          className="max-w-sm"
          onCloseAutoFocus={(event) => {
            event.preventDefault();
            buttonRef.current?.focus();
          }}
        >
          <DialogHeader>
            <DialogTitle>Public link</DialogTitle>
            <DialogDescription>
              Share a read-only view of this document. Access depends on the link’s limits.
            </DialogDescription>
          </DialogHeader>
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
        </DialogContent>
      </Dialog>
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
