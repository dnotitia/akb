import { useEffect, useState } from "react";
import { Loader2 } from "lucide-react";
import { createPublication, getDocument, listPublications } from "@/lib/api";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";

interface PublishOptionsDialogProps {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  vault: string;
  docId: string;
  /** Called with the created publication's slug after success. */
  onPublished: (slug: string) => void;
}

const EXPIRY_PRESETS: Array<{ value: string; label: string }> = [
  { value: "", label: "Never" },
  { value: "1d", label: "1 day" },
  { value: "7d", label: "7 days" },
  { value: "30d", label: "30 days" },
  { value: "90d", label: "90 days" },
];

export function PublishOptionsDialog({
  open,
  onOpenChange,
  vault,
  docId,
  onPublished,
}: PublishOptionsDialogProps) {
  const [password, setPassword] = useState("");
  const [requirePassword, setRequirePassword] = useState(false);
  const [expiresIn, setExpiresIn] = useState("");
  const [maxViews, setMaxViews] = useState("");
  const [working, setWorking] = useState(false);
  const [error, setError] = useState("");

  useEffect(() => {
    if (!open) {
      setPassword("");
      setRequirePassword(false);
      setExpiresIn("");
      setMaxViews("");
      setError("");
    }
  }, [open]);

  async function handlePublish() {
    setWorking(true);
    setError("");
    try {
      // Idempotency: if a publication for this doc already exists, surface
      // it without creating another. The doc page treats publication as
      // singular ("/p/<slug>"); avoiding duplicates keeps that contract.
      const doc = await getDocument(vault, docId);
      const { publications } = await listPublications(vault, "document");
      const existing = publications.find((p: any) => p.document_id === doc.id);
      if (existing) {
        onPublished(existing.slug);
        onOpenChange(false);
        return;
      }

      const max = maxViews.trim();
      const result = await createPublication(vault, {
        resource_type: "document",
        doc_id: docId,
        password: requirePassword && password ? password : undefined,
        expires_in: expiresIn || undefined,
        max_views: max ? Number(max) : undefined,
      });
      onPublished(result.slug);
      onOpenChange(false);
    } catch (e: any) {
      setError(e?.message || "Failed to publish");
    } finally {
      setWorking(false);
    }
  }

  return (
    <Dialog open={open} onOpenChange={(o) => !working && onOpenChange(o)}>
      <DialogContent className="max-w-xl">
        <DialogHeader>
          <DialogTitle>Publish to /p/…</DialogTitle>
          <DialogDescription>
            Create a public, read-only link. Optionally lock it down with a
            password, expiry, or view limit.
          </DialogDescription>
        </DialogHeader>

        <div className="space-y-4">
          {/* Password gate */}
          <div className="border border-border p-3">
            <label className="flex items-baseline gap-2 cursor-pointer">
              <input
                type="checkbox"
                checked={requirePassword}
                onChange={(e) => setRequirePassword(e.target.checked)}
                className="cursor-pointer"
              />
              <div className="flex-1 min-w-0">
                <div className="text-sm font-medium">Require password</div>
                <div className="coord">VIEWERS MUST ENTER THIS BEFORE READING</div>
              </div>
            </label>
            {requirePassword && (
              <div className="mt-3">
                <Label htmlFor="pub-pw" className="sr-only">
                  Password
                </Label>
                <Input
                  id="pub-pw"
                  type="text"
                  value={password}
                  onChange={(e) => setPassword(e.target.value)}
                  placeholder="Set a strong shared password"
                  autoComplete="off"
                />
              </div>
            )}
          </div>

          {/* Expiry */}
          <div>
            <Label className="coord-ink mb-1.5 block">EXPIRES</Label>
            <div className="grid grid-cols-5 gap-px border border-border bg-border">
              {EXPIRY_PRESETS.map((p) => {
                const active = expiresIn === p.value;
                return (
                  <button
                    key={p.label}
                    type="button"
                    onClick={() => setExpiresIn(p.value)}
                    aria-pressed={active}
                    className={`px-2 py-2 text-xs font-mono uppercase tracking-wider transition-colors cursor-pointer focus:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-inset ${
                      active
                        ? "bg-foreground text-background"
                        : "bg-surface text-foreground hover:bg-surface-muted"
                    }`}
                  >
                    {p.label}
                  </button>
                );
              })}
            </div>
            <p className="text-xs text-foreground-muted mt-1.5">
              After expiry the link returns 410. Recreate to extend.
            </p>
          </div>

          {/* Max views */}
          <div>
            <Label htmlFor="pub-max" className="coord-ink mb-1.5 block">
              MAX VIEWS
            </Label>
            <Input
              id="pub-max"
              type="number"
              min={1}
              value={maxViews}
              onChange={(e) => setMaxViews(e.target.value)}
              placeholder="Unlimited"
            />
            <p className="text-xs text-foreground-muted mt-1.5">
              Optional. After this many views the link returns 410.
            </p>
          </div>

          {error && (
            <div role="alert" className="border border-destructive p-2 text-xs text-destructive">
              {error}
            </div>
          )}
        </div>

        <DialogFooter>
          <Button
            type="button"
            variant="outline"
            onClick={() => onOpenChange(false)}
            disabled={working}
          >
            Cancel
          </Button>
          <Button
            type="button"
            variant="accent"
            onClick={handlePublish}
            disabled={working || (requirePassword && !password.trim())}
          >
            {working ? (
              <>
                <Loader2 className="h-4 w-4 animate-spin" aria-hidden />
                Publishing…
              </>
            ) : (
              "Publish"
            )}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
