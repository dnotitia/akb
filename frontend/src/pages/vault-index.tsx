import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { Library, Plus } from "lucide-react";
import { listVaults } from "@/lib/api";
import { Button } from "@/components/ui/button";
import { EmptyState } from "@/components/empty-state";

/**
 * /vault — the "no vault selected" state of the workspace shell. The shell
 * keeps the same chrome as a vault (title bar with the vault switcher + the
 * left sidebar), so moving in and out of a vault never shifts the layout; this
 * body is just a calm guidance message. Vaults are browsed and picked from the
 * switcher in the title bar, so there's no directory list here (it would
 * duplicate the switcher).
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
  // loading (count === null) keep the common "select a vault" message so the
  // text doesn't flash the wrong guidance.
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
            : "Pick a vault from the switcher in the title bar to open its collections, search, and graph."
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
