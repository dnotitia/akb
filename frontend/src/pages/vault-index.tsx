import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { Library, Plus } from "lucide-react";
import { listVaults } from "@/lib/api";
import { Button } from "@/components/ui/button";
import { EmptyState } from "@/components/empty-state";

/**
 * /vault — the "no vault selected" state of the workspace shell. The shell keeps
 * the same chrome as a vault (title bar + the left sidebar, whose top section is
 * the always-visible vault list), so the user's vaults are right there on the
 * left; this body is a calm guidance message pointing at them. No directory list
 * here — it would duplicate the sidebar vault list.
 */
export default function VaultIndexPage() {
  const [count, setCount] = useState<number | null>(null);

  useEffect(() => {
    let cancelled = false;
    listVaults()
      .then((d) => {
        if (!cancelled) setCount((d.vaults || []).length);
      })
      .catch(() => {
        if (!cancelled) setCount(0);
      });
    return () => {
      cancelled = true;
    };
  }, []);

  // Only branch to the "first vault" copy once we KNOW the count is 0 — while
  // loading keep the common "select a vault" message so it doesn't flash.
  const noVaults = count === 0;

  return (
    <div className="fade-up flex min-h-[60vh] items-center justify-center">
      <EmptyState
        icon={
          <span className="feature-tile feat-knowledge h-14 w-14">
            <Library className="h-6 w-6" aria-hidden />
          </span>
        }
        title={noVaults ? "No vaults yet" : "Select a vault"}
        description={
          noVaults
            ? "Create your first vault to start collecting documents, tables, and files for your agents."
            : "Pick a vault from the list on the left to open its collections, search, and graph."
        }
        action={
          <Button asChild variant="accent" size="md">
            <Link to="/vault/new">
              <Plus className="h-4 w-4" aria-hidden />
              {noVaults ? "Create first vault" : "New vault"}
            </Link>
          </Button>
        }
      />
    </div>
  );
}
