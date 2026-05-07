import { Link } from "react-router-dom";
import { Plus } from "lucide-react";
import { Button } from "@/components/ui/button";

/**
 * /vault — directory landing. No vault selected yet; the left VaultNav
 * carries the picker. This page just gives the content column a friendly
 * anchor so the URL is reachable without bouncing through Home.
 */
export default function VaultIndexPage() {
  return (
    <div className="h-full flex items-center justify-center p-10">
      <div className="max-w-md text-center">
        <div className="coord-spark mb-4">§ Vaults</div>
        <h1 className="text-3xl font-semibold tracking-tight text-foreground mb-3">
          Pick a vault
        </h1>
        <p className="text-sm text-foreground-muted leading-relaxed mb-6">
          Choose one from the sidebar to browse documents, tables, and files.
          Or create a new vault to start fresh.
        </p>
        <Button asChild variant="outline" size="sm">
          <Link to="/vault/new">
            <Plus className="h-4 w-4" aria-hidden />
            New vault
          </Link>
        </Button>
      </div>
    </div>
  );
}
