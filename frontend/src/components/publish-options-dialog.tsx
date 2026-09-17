import { useEffect, useRef, useState, type FormEvent } from "react";
import { Eye, EyeOff, Globe2 } from "lucide-react";
import {
  createPublication,
  getDocument,
  listPublications,
  type Publication,
} from "@/lib/api";
import {
  emptyPublicationAccessOptions,
  publicationAccessError,
  publicationAccessPayload,
  type PublicationAccessOptions,
} from "@/lib/publication-options";
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
  /** Required for the existing document publication flow. */
  docId?: string;
  /** File publications pass their canonical URI directly. Defaults to document. */
  resourceType?: "document" | "file";
  resourceUri?: string;
  resourceName?: string;
  /** Called with the ready public link after success or exact-URI reuse. */
  onPublished: (slug: string, publication?: Publication) => void;
}

const EXPIRY_PRESETS: Array<{ value: string; label: string }> = [
  { value: "", label: "Never" },
  { value: "1d", label: "1 day" },
  { value: "7d", label: "7 days" },
  { value: "30d", label: "30 days" },
  { value: "90d", label: "90 days" },
];

export function PublicationAccessFields({
  value,
  onChange,
  idPrefix,
  disabled = false,
}: {
  value: PublicationAccessOptions;
  onChange: (next: PublicationAccessOptions) => void;
  idPrefix: string;
  disabled?: boolean;
}) {
  const [showPassword, setShowPassword] = useState(false);
  const passwordId = `${idPrefix}-password`;
  const passwordHelpId = `${idPrefix}-password-help`;
  const maxViewsId = `${idPrefix}-max-views`;
  const maxViewsHelpId = `${idPrefix}-max-views-help`;
  const passwordInvalid =
    value.requirePassword && value.password.length > 0 && value.password.trim().length < 8;
  const max = value.maxViews.trim();
  const maxViewsInvalid = Boolean(
    max && (!/^\d+$/.test(max) || !Number.isSafeInteger(Number(max)) || Number(max) < 1),
  );

  useEffect(() => {
    if (!value.requirePassword) setShowPassword(false);
  }, [value.requirePassword]);

  return (
    <div className="space-y-4">
      <div className="rounded-[var(--radius-md)] border border-border p-3">
        <label className="flex min-h-9 cursor-pointer items-center gap-2">
          <input
            type="checkbox"
            checked={value.requirePassword}
            disabled={disabled}
            onChange={(event) =>
              onChange({ ...value, requirePassword: event.target.checked })
            }
            className="h-4 w-4 shrink-0 cursor-pointer rounded-[var(--radius-sm)] accent-[var(--color-primary)] focus:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2 focus-visible:ring-offset-surface disabled:cursor-not-allowed"
          />
          <span className="min-w-0 flex-1">
            <span className="block text-sm font-medium text-foreground">Require password</span>
            <span className="block text-xs text-foreground-muted">
              Viewers enter a shared password before access.
            </span>
          </span>
        </label>
        {value.requirePassword && (
          <div className="mt-3">
            <Label htmlFor={passwordId} className="mb-1.5 block text-xs text-foreground">
              Publication password
            </Label>
            <div className="relative">
              <Input
                id={passwordId}
                type={showPassword ? "text" : "password"}
                autoComplete="new-password"
                value={value.password}
                disabled={disabled}
                onChange={(event) => onChange({ ...value, password: event.target.value })}
                placeholder="At least 8 characters"
                aria-invalid={passwordInvalid || undefined}
                aria-describedby={passwordHelpId}
                className="pr-11 font-mono"
              />
              <button
                type="button"
                onClick={() => setShowPassword((current) => !current)}
                aria-label={showPassword ? "Hide password" : "Show password"}
                disabled={disabled}
                className="absolute right-1 top-1/2 inline-flex h-9 w-9 -translate-y-1/2 cursor-pointer items-center justify-center rounded-[var(--radius-sm)] text-foreground-muted transition-token hover:bg-surface-hover hover:text-primary focus:outline-none focus-visible:ring-2 focus-visible:ring-ring disabled:cursor-not-allowed disabled:opacity-50"
              >
                {showPassword ? (
                  <EyeOff className="h-4 w-4" aria-hidden />
                ) : (
                  <Eye className="h-4 w-4" aria-hidden />
                )}
              </button>
            </div>
            <p
              id={passwordHelpId}
              className={passwordInvalid ? "mt-1 text-xs text-destructive" : "mt-1 text-xs text-foreground-muted"}
            >
              {passwordInvalid ? "Use at least 8 characters." : "Store this password somewhere safe; AKB cannot reveal it later."}
            </p>
          </div>
        )}
      </div>

      <fieldset disabled={disabled}>
        <legend className="mb-1.5 text-xs font-medium text-foreground">Expires</legend>
        <div className="grid grid-cols-2 gap-px overflow-hidden rounded-[var(--radius-md)] border border-border bg-border sm:grid-cols-5">
          {EXPIRY_PRESETS.map((preset) => {
            const active = value.expiresIn === preset.value;
            return (
              <button
                key={preset.label}
                type="button"
                onClick={() => onChange({ ...value, expiresIn: preset.value })}
                aria-pressed={active}
                className={`min-h-9 cursor-pointer px-2 py-2 text-xs font-medium transition-token focus:outline-none focus-visible:ring-2 focus-visible:ring-inset focus-visible:ring-ring disabled:cursor-not-allowed disabled:opacity-50 ${
                  active
                    ? "bg-primary text-primary-foreground"
                    : "bg-surface text-foreground hover:bg-surface-hover"
                }`}
              >
                {preset.label}
              </button>
            );
          })}
        </div>
        <p className="mt-1.5 text-xs text-foreground-muted">
          Expired links stop responding and must be recreated.
        </p>
      </fieldset>

      <div>
        <Label htmlFor={maxViewsId} className="mb-1.5 block text-xs text-foreground">
          Max views
        </Label>
        <Input
          id={maxViewsId}
          type="number"
          min={1}
          step={1}
          inputMode="numeric"
          value={value.maxViews}
          disabled={disabled}
          onChange={(event) => onChange({ ...value, maxViews: event.target.value })}
          placeholder="Unlimited"
          aria-invalid={maxViewsInvalid || undefined}
          aria-describedby={maxViewsHelpId}
        />
        <p
          id={maxViewsHelpId}
          className={maxViewsInvalid ? "mt-1.5 text-xs text-destructive" : "mt-1.5 text-xs text-foreground-muted"}
        >
          {maxViewsInvalid
            ? "Enter a positive whole number."
            : "Each public view counts toward this limit."}
        </p>
      </div>
    </div>
  );
}

export function PublishOptionsDialog({
  open,
  onOpenChange,
  vault,
  docId,
  resourceType = "document",
  resourceUri,
  resourceName,
  onPublished,
}: PublishOptionsDialogProps) {
  const [access, setAccess] = useState<PublicationAccessOptions>(
    emptyPublicationAccessOptions,
  );
  const [working, setWorking] = useState(false);
  const [error, setError] = useState("");
  const errorRef = useRef<HTMLDivElement | null>(null);
  const accessError = publicationAccessError(access);
  const resourceLabel = resourceType === "file" ? "file" : "document";

  useEffect(() => {
    if (!open) {
      setAccess(emptyPublicationAccessOptions());
      setError("");
    }
  }, [open]);

  useEffect(() => {
    if (error) errorRef.current?.focus();
  }, [error]);

  async function handlePublish(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (accessError) {
      setError(accessError);
      return;
    }

    setWorking(true);
    setError("");
    try {
      let uri = resourceUri;
      let title = resourceName;
      if (resourceType === "document") {
        if (!docId) throw new Error("Document identity is unavailable.");
        // Documents expose one representative /p/ link from their viewer, so
        // keep the existing singular/idempotent contract. File publications
        // intentionally do not reuse: the same file can have multiple links
        // with different passwords, expiry dates, and view limits.
        const [document, { publications }] = await Promise.all([
          getDocument(vault, docId),
          listPublications(vault, "document"),
        ]);
        uri = document.uri;
        title = document.title || document.path;
        const existing = publications.find(
          (publication) => publication.resource_uri === uri,
        );
        if (existing) {
          onPublished(existing.slug, existing);
          onOpenChange(false);
          return;
        }
      }
      if (!uri) throw new Error(`${resourceLabel} identity is unavailable.`);

      const result = await createPublication(vault, {
        resource_type: resourceType,
        uri,
        title: title || undefined,
        ...publicationAccessPayload(access),
      });
      onPublished(result.slug, result);
      onOpenChange(false);
    } catch (caught: unknown) {
      setError(caught instanceof Error ? caught.message : `Failed to publish ${resourceLabel}.`);
    } finally {
      setWorking(false);
    }
  }

  return (
    <Dialog open={open} onOpenChange={(next) => !working && onOpenChange(next)}>
      <DialogContent className="max-w-xl">
        <form onSubmit={handlePublish} className="contents">
          <DialogHeader>
            <DialogTitle>Publish {resourceLabel}</DialogTitle>
            <DialogDescription>
              Create a public, read-only link with optional access limits.
            </DialogDescription>
          </DialogHeader>

          <Alert variant="info" title="Public link">
            <span className="inline-flex items-start gap-2">
              <Globe2 className="mt-0.5 h-4 w-4 shrink-0" aria-hidden />
              Anyone with the link can open this {resourceLabel} without signing in.
            </span>
          </Alert>

          {error && (
            <div ref={errorRef} tabIndex={-1}>
              <Alert variant="destructive" title="Publication failed">
                {error}
              </Alert>
            </div>
          )}

          <PublicationAccessFields
            value={access}
            onChange={setAccess}
            idPrefix={`${resourceType}-publication`}
            disabled={working}
          />

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
              type="submit"
              variant="accent"
              loading={working}
              disabled={Boolean(accessError)}
            >
              {working ? "Publishing…" : "Publish"}
            </Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  );
}
