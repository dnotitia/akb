import { useCallback, useEffect, useState } from "react";
import { Link, useNavigate } from "react-router-dom";
import { ChevronRight } from "lucide-react";
import { VaultCreateForm } from "@/components/vault-create-form";
import { PageHeader } from "@/components/ui/page-header";
import { PageShell } from "@/components/ui/page-shell";

export default function VaultNewPage() {
  const navigate = useNavigate();
  const [creating, setCreating] = useState(false);

  const handleCancel = useCallback(() => {
    if (typeof window !== "undefined" && window.history.length > 1) {
      navigate(-1);
    } else {
      navigate("/");
    }
  }, [navigate]);

  useEffect(() => {
    function onKeyDown(event: KeyboardEvent) {
      if (event.key === "Escape" && !creating) handleCancel();
    }
    window.addEventListener("keydown", onKeyDown);
    return () => window.removeEventListener("keydown", onKeyDown);
  }, [creating, handleCancel]);

  return (
    <PageShell
      header={
        <>
          <nav aria-label="Breadcrumb" className="mb-6 flex items-center gap-2 coord">
            <Link to="/" className="hover:text-link">
              Home
            </Link>
            <ChevronRight className="h-3 w-3 text-foreground-muted" aria-hidden />
            <span className="text-foreground">New vault</span>
          </nav>
          <PageHeader
            title="Create a vault"
            subtitle="A vault is a Git-backed knowledge root. Documents, tables, and files live inside it. Pick a short, lowercase name — it becomes the URL path and the repo identifier."
            className="mb-6"
          />
        </>
      }
      contentWidth="narrow"
    >
      <VaultCreateForm
        onCreated={(name) => navigate(`/vault/${name}`)}
        onCancel={handleCancel}
        onBusyChange={setCreating}
        className="rounded-[var(--radius-lg)] border border-border bg-surface p-8 shadow-sm"
      />
    </PageShell>
  );
}
