import { useEffect, useMemo, useRef, useState } from "react";
import { Link, useNavigate } from "react-router-dom";
import { ArrowLeft, ArrowRight, ChevronRight, GitBranch } from "lucide-react";
import { createVault, listVaultTemplates, type VaultTemplateSummary } from "@/lib/api";
import { Alert } from "@/components/ui/alert";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Select } from "@/components/ui/select";
import {
  Tooltip,
  TooltipContent,
  TooltipProvider,
  TooltipTrigger,
} from "@/components/ui/tooltip";

export default function VaultNewPage() {
  const navigate = useNavigate();
  const [name, setName] = useState("");
  const [description, setDescription] = useState("");
  const [error, setError] = useState("");
  const [creating, setCreating] = useState(false);
  const [templates, setTemplates] = useState<VaultTemplateSummary[]>([]);
  const [selectedTemplate, setSelectedTemplate] = useState<string>("");
  const nameRef = useRef<HTMLInputElement>(null);
  const nameValid = /^[a-z0-9-]+$/.test(name.trim());

  useEffect(() => {
    listVaultTemplates()
      .then(setTemplates)
      .catch((e) => {
        console.warn("Failed to load vault templates; falling back to none-only.", e);
        setTemplates([]);
      });
  }, []);

  const selectedSummary = useMemo(
    () => templates.find((t) => t.name === selectedTemplate) || null,
    [templates, selectedTemplate],
  );

  function handleCancel() {
    // Prefer history-back so users return to where they came from.
    // navigate(-1) is a no-op when there is no prior entry; the browser
    // simply stays put. For deep-link visits with no history we still
    // want to land somewhere sensible — window.history.length > 1
    // signals that we have a prior entry in real browsers.
    if (typeof window !== "undefined" && window.history.length > 1) {
      navigate(-1);
    } else {
      navigate("/");
    }
  }

  useEffect(() => {
    function onKey(e: KeyboardEvent) {
      if (e.key === "Escape" && !creating) handleCancel();
    }
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [creating]);

  async function handleSubmit(e: React.FormEvent) {
    e.preventDefault();
    const trimmed = name.trim();
    if (!trimmed) {
      setError("Vault name is required.");
      nameRef.current?.focus();
      return;
    }
    if (!/^[a-z0-9-]+$/.test(trimmed)) {
      setError("Use lowercase letters, digits, and hyphens only.");
      nameRef.current?.focus();
      return;
    }
    setError("");
    setCreating(true);
    try {
      await createVault(
        trimmed,
        description.trim() || undefined,
        selectedTemplate || undefined,
      );
      navigate(`/vault/${trimmed}`);
    } catch (err: any) {
      setError(err?.message || "Failed to create vault");
      setCreating(false);
    }
  }

  return (
    <div className="max-w-3xl mx-auto fade-up">
      <nav aria-label="Breadcrumb" className="flex items-center gap-2 coord mb-6">
        <Link to="/" className="hover:text-link">Home</Link>
        <ChevronRight className="h-3 w-3 text-foreground-muted" aria-hidden />
        <span className="text-foreground">New vault</span>
      </nav>

      <header className="mb-6">
        <h1 className="font-display text-3xl tracking-tight text-foreground">
          Create a vault
        </h1>
        <p className="mt-3 text-sm text-foreground-muted max-w-prose">
          A vault is a Git-backed knowledge root. Documents, tables, and files live
          inside it. Pick a short, lowercase name — it becomes the URL path and the
          repo identifier.
        </p>
      </header>

      <form
        onSubmit={handleSubmit}
        className="rounded-[var(--radius-lg)] border border-border bg-surface shadow-sm p-8 space-y-5"
      >
        <div className="space-y-1.5">
          <Label htmlFor="vault-name">
            Name <span className="text-destructive normal-case">*</span>
          </Label>
          <Input
            id="vault-name"
            ref={nameRef}
            value={name}
            onChange={(e) => {
              setName(e.target.value);
              if (error) setError("");
            }}
            placeholder="e.g. engineering"
            required
            aria-required="true"
            aria-invalid={error ? true : undefined}
            autoFocus
            className="font-mono"
            aria-describedby={error ? "vault-form-error vault-name-hint" : "vault-name-hint"}
          />
          <div id="vault-name-hint" className="coord">
            Lowercase letters, digits, hyphens · becomes /vault/&lt;name&gt;
          </div>
        </div>

        <div className="space-y-1.5">
          <Label htmlFor="vault-description">
            Description <span className="normal-case tracking-normal text-foreground-muted">(optional)</span>
          </Label>
          <Input
            id="vault-description"
            value={description}
            onChange={(e) => setDescription(e.target.value)}
            placeholder="What this vault is for"
          />
        </div>

        <div className="space-y-1.5">
          <Label htmlFor="vault-template">
            Template <span className="normal-case tracking-normal text-foreground-muted">(optional)</span>
          </Label>
          <Select
            id="vault-template"
            value={selectedTemplate}
            onChange={(e) => setSelectedTemplate(e.target.value)}
            className="font-mono"
          >
            <option value="">None — empty vault</option>
            {templates.map((t) => (
              <option key={t.name} value={t.name}>{t.display_name}</option>
            ))}
          </Select>
          {selectedSummary && (
            <div className="coord">
              {selectedSummary.description}
              <br />
              Will create {selectedSummary.collection_count} collections:{" "}
              {selectedSummary.collections.map((c) => c.path).join(" · ")}
            </div>
          )}
        </div>

        <TooltipProvider>
          <Tooltip>
            <TooltipTrigger asChild>
              <div>
                <Label className="pointer-events-none">
                  Connect external Git <span className="normal-case tracking-normal text-foreground-muted">(coming soon)</span>
                </Label>
                <div className="mt-1.5 flex items-center gap-2 rounded-[var(--radius-md)] border border-border border-dashed px-3 py-2 text-sm text-foreground-muted cursor-not-allowed opacity-60">
                  <GitBranch className="h-4 w-4 shrink-0" aria-hidden />
                  <span>Upstream repo URL · read-only mirror</span>
                </div>
              </div>
            </TooltipTrigger>
            <TooltipContent>
              Available via MCP (akb_create_vault); REST extension pending
            </TooltipContent>
          </Tooltip>
        </TooltipProvider>

        {error && <Alert variant="destructive" id="vault-form-error">{error}</Alert>}

        <div className="flex gap-3 pt-2">
          <Button type="submit" variant="accent" loading={creating} disabled={!nameValid}>
            {!creating && (
              <>
                Create vault
                <ArrowRight className="h-4 w-4" aria-hidden />
              </>
            )}
            {creating && "Creating…"}
          </Button>
          <Button type="button" variant="outline" onClick={handleCancel}>
            <ArrowLeft className="h-4 w-4" aria-hidden /> Cancel
          </Button>
        </div>
      </form>
    </div>
  );
}
