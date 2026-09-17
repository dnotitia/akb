import { useEffect, useId, useMemo, useRef, useState } from "react";
import { ArrowRight, GitBranch } from "lucide-react";
import {
  createVault,
  listVaults,
  listVaultTemplates,
  type VaultTemplateSummary,
} from "@/lib/api";
import { Alert } from "@/components/ui/alert";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { SelectMenu } from "@/components/ui/select-menu";
import { cn } from "@/lib/utils";

export function VaultCreateForm({
  onCreated,
  onOpenExisting,
  onCancel,
  onBusyChange,
  className,
}: {
  onCreated: (name: string) => void;
  onOpenExisting?: (name: string) => void;
  onCancel?: () => void;
  onBusyChange?: (busy: boolean) => void;
  className?: string;
}) {
  const id = useId();
  const nameId = `${id}-vault-name`;
  const descriptionId = `${id}-vault-description`;
  const templateId = `${id}-vault-template`;
  const nameHintId = `${id}-vault-name-hint`;
  const errorId = `${id}-vault-form-error`;
  const [name, setName] = useState("");
  const [description, setDescription] = useState("");
  const [error, setError] = useState("");
  const [accessibleConflict, setAccessibleConflict] = useState<string | null>(null);
  const [creating, setCreating] = useState(false);
  const [templates, setTemplates] = useState<VaultTemplateSummary[]>([]);
  const [selectedTemplate, setSelectedTemplate] = useState("");
  const nameRef = useRef<HTMLInputElement>(null);
  const conflictCheckGeneration = useRef(0);
  const nameValid = /^[a-z0-9]+(?:-[a-z0-9]+)*$/.test(name.trim());

  useEffect(() => {
    listVaultTemplates()
      .then(setTemplates)
      .catch((cause) => {
        console.warn("Failed to load vault templates; falling back to none-only.", cause);
        setTemplates([]);
      });
  }, []);

  useEffect(
    () => () => {
      conflictCheckGeneration.current += 1;
    },
    [],
  );

  const selectedSummary = useMemo(
    () => templates.find((template) => template.name === selectedTemplate) || null,
    [templates, selectedTemplate],
  );

  async function handleSubmit(event: React.FormEvent) {
    event.preventDefault();
    const trimmed = name.trim();
    if (!trimmed) {
      setError("Vault name is required.");
      nameRef.current?.focus();
      return;
    }
    if (!/^[a-z0-9]+(?:-[a-z0-9]+)*$/.test(trimmed)) {
      setError("Use lowercase letters and digits, with single hyphens between words.");
      nameRef.current?.focus();
      return;
    }

    setError("");
    setAccessibleConflict(null);
    conflictCheckGeneration.current += 1;
    setCreating(true);
    onBusyChange?.(true);
    try {
      await createVault(
        trimmed,
        description.trim() || undefined,
        selectedTemplate || undefined,
      );
      onCreated(trimmed);
    } catch (cause: unknown) {
      const detail =
        typeof cause === "object" && cause !== null && "detail" in cause
          ? (cause as { detail?: unknown }).detail
          : null;
      const code =
        typeof detail === "object" && detail !== null && "code" in detail
          ? (detail as { code?: unknown }).code
          : null;

      if (code === "vault_name_unavailable") {
        setError("Vault name is unavailable. Choose a different name.");
        if (onOpenExisting) {
          const generation = ++conflictCheckGeneration.current;
          void listVaults()
            .then((result) => {
              if (conflictCheckGeneration.current !== generation) return;
              const isAccessible = result.vaults.some(
                (vault) =>
                  typeof vault === "object" &&
                  vault !== null &&
                  "name" in vault &&
                  vault.name === trimmed,
              );
              if (isAccessible) setAccessibleConflict(trimmed);
            })
            .catch(() => {
              // The recovery action is optional. Keep the original conflict
              // visible if the access-filtered list cannot be refreshed.
            });
        }
      } else {
        setError(cause instanceof Error ? cause.message : "Failed to create vault");
      }
      setCreating(false);
      onBusyChange?.(false);
    }
  }

  return (
    <form onSubmit={handleSubmit} className={cn("space-y-5", className)}>
      <div className="space-y-1.5">
        <Label htmlFor={nameId}>
          Name <span className="text-foreground-muted normal-case">*</span>
        </Label>
        <Input
          id={nameId}
          ref={nameRef}
          value={name}
          onChange={(event) => {
            setName(event.target.value);
            conflictCheckGeneration.current += 1;
            if (error) setError("");
            if (accessibleConflict) setAccessibleConflict(null);
          }}
          placeholder="e.g. engineering"
          required
          aria-required="true"
          aria-invalid={error ? true : undefined}
          autoFocus
          disabled={creating}
          className="font-mono"
          aria-describedby={error ? `${errorId} ${nameHintId}` : nameHintId}
        />
        <div id={nameHintId} className="coord">
          Lowercase letters, digits, single hyphens · unique across this AKB installation · becomes akb://&lt;name&gt;
        </div>
      </div>

      <div className="space-y-1.5">
        <Label htmlFor={descriptionId}>
          Description <span className="normal-case tracking-normal text-foreground-muted">(optional)</span>
        </Label>
        <Input
          id={descriptionId}
          value={description}
          onChange={(event) => setDescription(event.target.value)}
          placeholder="What this vault is for"
          disabled={creating}
        />
      </div>

      <div className="space-y-1.5">
        <Label htmlFor={templateId}>
          Template <span className="normal-case tracking-normal text-foreground-muted">(optional)</span>
        </Label>
        <SelectMenu
          id={templateId}
          aria-label="Vault template"
          value={selectedTemplate}
          onValueChange={setSelectedTemplate}
          disabled={creating}
          options={[
            { value: "", label: "None — empty vault" },
            ...templates.map((template) => ({
              value: template.name,
              label: template.display_name,
              hint: `${template.collection_count} collection${template.collection_count === 1 ? "" : "s"}`,
            })),
          ]}
        />
        {selectedSummary && (
          <div className="coord">
            {selectedSummary.description}
            <br />
            Will create {selectedSummary.collection_count} collections:{" "}
            {selectedSummary.collections.map((collection) => collection.path).join(" · ")}
          </div>
        )}
      </div>

      <div>
        <Label className="pointer-events-none">
          Connect external Git{" "}
          <span className="normal-case tracking-normal text-foreground-muted">(coming soon)</span>
        </Label>
        <div className="mt-1.5 flex items-center gap-2 rounded-[var(--radius-md)] border border-dashed border-border px-3 py-2 text-sm text-foreground-muted opacity-60">
          <GitBranch className="h-4 w-4 shrink-0" aria-hidden />
          <span>Upstream repo URL · read-only mirror</span>
        </div>
        <p className="coord mt-1.5">Available via MCP (akb_create_vault); REST extension pending.</p>
      </div>

      {error && (
        <Alert variant="destructive" id={errorId}>
          <p>{error}</p>
        </Alert>
      )}
      {accessibleConflict && onOpenExisting && (
        <>
          <p role="status" className="sr-only">
            An accessible vault with this name can be opened.
          </p>
          <div className="flex justify-end">
            <Button
              type="button"
              variant="link"
              onClick={() => onOpenExisting(accessibleConflict)}
            >
              Open existing vault
            </Button>
          </div>
        </>
      )}

      <div className="flex flex-col-reverse gap-2 pt-2 sm:flex-row sm:justify-end">
        {onCancel && (
          <Button type="button" variant="outline" onClick={onCancel} disabled={creating}>
            Cancel
          </Button>
        )}
        <Button type="submit" variant="accent" loading={creating} disabled={!nameValid}>
          {!creating && (
            <>
              Create vault
              <ArrowRight className="h-4 w-4" aria-hidden />
            </>
          )}
          {creating && "Creating…"}
        </Button>
      </div>
    </form>
  );
}
