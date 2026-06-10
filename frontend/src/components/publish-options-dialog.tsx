import { useEffect, useState } from "react";
import { Eye, EyeOff } from "lucide-react";
import { createPublication, getDocument, listPublications } from "@/lib/api";
import { Alert } from "@/components/ui/alert";
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
  const [showPw, setShowPw] = useState(false);
  const [requirePassword, setRequirePassword] = useState(false);
  const [expiresIn, setExpiresIn] = useState("");
  const [maxViews, setMaxViews] = useState("");
  const [working, setWorking] = useState(false);
  const [error, setError] = useState("");

  // A shared publication password is a real secret — require a floor length so
  // a one-character "password" can't be set by accident.
  const pwTooShort =
    requirePassword && password.length > 0 && password.length < 8;
  const cannotPublish = working || (requirePassword && password.trim().length < 8);

  useEffect(() => {
    if (!open) {
      setPassword("");
      setShowPw(false);
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
      // The two reads are independent — run them concurrently.
      const [doc, { publications }] = await Promise.all([
        getDocument(vault, docId),
        listPublications(vault, "document"),
      ]);
      const existing = publications.find((p: any) => p.resource_uri === doc.uri);
      if (existing) {
        onPublished(existing.slug);
        onOpenChange(false);
        return;
      }

      const max = maxViews.trim();
      const result = await createPublication(vault, {
        resource_type: "document",
        uri: doc.uri,
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
          <div className="rounded-[var(--radius-md)] border border-border p-3">
            <label className="flex items-baseline gap-2 cursor-pointer">
              <input
                type="checkbox"
                checked={requirePassword}
                onChange={(e) => setRequirePassword(e.target.checked)}
                className="cursor-pointer accent-[var(--color-primary)] focus:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2 focus-visible:ring-offset-surface rounded-[var(--radius-sm)]"
              />
              <div className="flex-1 min-w-0">
                <div className="text-sm font-medium">Require password</div>
                <div className="coord">Viewers must enter this before reading</div>
              </div>
            </label>
            {requirePassword && (
              <div className="mt-3">
                <Label htmlFor="pub-pw" className="coord-ink mb-1.5 block">
                  Publication password
                </Label>
                <div className="relative">
                  <Input
                    id="pub-pw"
                    type={showPw ? "text" : "password"}
                    autoComplete="new-password"
                    value={password}
                    onChange={(e) => setPassword(e.target.value)}
                    placeholder="Set a strong shared password"
                    aria-invalid={pwTooShort ? true : undefined}
                    aria-describedby={pwTooShort ? "pub-pw-help" : undefined}
                    className="pr-10 font-mono"
                  />
                  <button
                    type="button"
                    onClick={() => setShowPw((s) => !s)}
                    aria-label={showPw ? "Hide password" : "Show password"}
                    className="absolute right-2 top-1/2 -translate-y-1/2 inline-flex items-center justify-center h-7 w-7 text-foreground-muted hover:text-primary cursor-pointer rounded-[var(--radius-sm)] focus:outline-none focus-visible:ring-2 focus-visible:ring-ring"
                  >
                    {showPw ? (
                      <EyeOff className="h-4 w-4" aria-hidden />
                    ) : (
                      <Eye className="h-4 w-4" aria-hidden />
                    )}
                  </button>
                </div>
                {pwTooShort && (
                  <p id="pub-pw-help" className="text-destructive text-xs mt-1">
                    Use at least 8 characters.
                  </p>
                )}
              </div>
            )}
          </div>

          {/* Expiry */}
          <div>
            <Label className="coord-ink mb-1.5 block">Expires</Label>
            <div className="grid grid-cols-5 gap-px border border-border bg-border rounded-[var(--radius-md)] overflow-hidden">
              {EXPIRY_PRESETS.map((p) => {
                const active = expiresIn === p.value;
                return (
                  <button
                    key={p.label}
                    type="button"
                    onClick={() => setExpiresIn(p.value)}
                    aria-pressed={active}
                    className={`px-2 py-2 text-xs font-medium transition-colors cursor-pointer focus:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-inset ${
                      active
                        ? "bg-primary text-primary-foreground"
                        : "bg-surface text-foreground hover:bg-surface-hover"
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
              Max views
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

          {error && <Alert variant="destructive" className="text-xs">{error}</Alert>}
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
            loading={working}
            disabled={cannotPublish}
          >
            {working ? "Publishing…" : "Publish"}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
