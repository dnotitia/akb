import { useEffect, useMemo, useState } from "react";
import { Link, useNavigate } from "react-router-dom";
import { ArrowLeft, ArrowRight, ChevronRight, GitBranch, Loader2 } from "lucide-react";
import { createVault, listVaultTemplates, type VaultTemplateSummary } from "@/lib/api";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
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
      setError("Vault name is required");
      return;
    }
    if (!/^[a-z0-9-]+$/.test(trimmed)) {
      setError("Use lowercase letters, digits, and hyphens only");
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
        <Link to="/" className="hover:text-accent">HOME</Link>
        <ChevronRight className="h-3 w-3 text-foreground-muted" aria-hidden />
        <span className="text-foreground">NEW VAULT</span>
      </nav>

      <header className="border-b border-border pb-4 mb-0">
        <div className="coord-spark mb-2">§ NEW VAULT</div>
        <h1 className="text-3xl font-semibold tracking-tight text-foreground">
          Create a vault.
        </h1>
        <p className="mt-3 text-sm text-foreground-muted max-w-prose">
          A vault is a Git-backed knowledge root. Documents, tables, and files live
          inside it. Pick a short, lowercase name — it becomes the URL path and the
          repo identifier.
        </p>
      </header>

      <form
        onSubmit={handleSubmit}
        className="border border-border border-t-0 bg-surface p-8 space-y-5"
      >
        <div className="space-y-1.5">
          <Label htmlFor="vault-name">
            Name <span className="text-destructive normal-case">*</span>
          </Label>
          <Input
            id="vault-name"
            value={name}
            onChange={(e) => setName(e.target.value)}
            placeholder="e.g. engineering"
            autoFocus
            className="font-mono"
            aria-describedby="vault-name-hint"
          />
          <div id="vault-name-hint" className="coord">
            LOWERCASE LETTERS, DIGITS, HYPHENS · BECOMES /vault/&lt;name&gt;
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
          <select
            id="vault-template"
            value={selectedTemplate}
            onChange={(e) => setSelectedTemplate(e.target.value)}
            className="w-full bg-surface border border-border px-3 py-2 text-sm font-mono focus:outline-none focus:border-accent transition-colors"
          >
            <option value="">None — empty vault</option>
            {templates.map((t) => (
              <option key={t.name} value={t.name}>{t.display_name}</option>
            ))}
          </select>
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
                <div className="mt-1.5 flex items-center gap-2 border border-border border-dashed px-3 py-2 text-sm text-foreground-muted cursor-not-allowed opacity-60">
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

        {error && (
          <div
            role="alert"
            aria-live="polite"
            className="border border-destructive px-3 py-2 text-xs font-mono uppercase tracking-wider text-destructive"
          >
            ⚠ {error.toUpperCase()}
          </div>
        )}

        <div className="flex gap-3 pt-2">
          <Button type="submit" variant="accent" disabled={creating}>
            {creating ? (
              <>
                <Loader2 className="h-4 w-4 animate-spin" aria-hidden />
                Creating…
              </>
            ) : (
              <>
                Create vault
                <ArrowRight className="h-4 w-4" aria-hidden />
              </>
            )}
          </Button>
          <Button type="button" variant="outline" onClick={handleCancel}>
            <ArrowLeft className="h-4 w-4" aria-hidden /> Cancel
          </Button>
        </div>
      </form>
    </div>
  );
}
