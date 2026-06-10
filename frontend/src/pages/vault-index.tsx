import { useEffect, useMemo, useState } from "react";
import { Link } from "react-router-dom";
import { Plus } from "lucide-react";
import { listVaults } from "@/lib/api";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { EmptyState } from "@/components/empty-state";
import { VaultList, type VaultRow } from "@/components/vault-list";

/**
 * /vault — the "no vault selected" state of the workspace shell. The shell keeps
 * the same chrome as a vault (title bar with the switcher + the left sidebar),
 * so moving in and out of a vault never shifts layout. The body is the vault
 * DIRECTORY: a visible, scannable list so the user's vaults are right there (not
 * hidden behind the switcher dropdown). No duplication — the sidebar at /vault
 * is a quiet prompt, not a second list.
 */
export default function VaultIndexPage() {
  const [vaults, setVaults] = useState<VaultRow[] | null>(null);
  const [error, setError] = useState(false);
  const [filter, setFilter] = useState("");

  useEffect(() => {
    let cancelled = false;
    listVaults()
      .then((d) => {
        if (!cancelled) setVaults(d.vaults || []);
      })
      .catch(() => {
        if (!cancelled) setError(true);
      });
    return () => {
      cancelled = true;
    };
  }, []);

  const q = filter.trim().toLowerCase();
  const filtered = useMemo(
    () =>
      !vaults
        ? []
        : q
          ? vaults.filter(
              (v) =>
                v.name?.toLowerCase().includes(q) ||
                v.description?.toLowerCase().includes(q),
            )
          : vaults,
    [vaults, q],
  );

  return (
    <div className="fade-up">
      <header className="flex items-baseline justify-between gap-4 flex-wrap pb-3 border-b border-border">
        <div className="flex items-baseline gap-3">
          <h1 className="font-display text-2xl font-semibold tracking-tight text-foreground">
            Vaults
          </h1>
          {vaults && (
            <Badge variant="default" className="tabular-nums">{vaults.length}</Badge>
          )}
        </div>
        <div className="flex items-center gap-3">
          {vaults && vaults.length > 5 && (
            <Input
              type="search"
              placeholder="Filter vaults"
              value={filter}
              onChange={(e) => setFilter(e.target.value)}
              aria-label="Filter vaults"
              className="h-9 w-48"
            />
          )}
          <Button asChild variant="accent" size="sm">
            <Link to="/vault/new">
              <Plus className="h-4 w-4" aria-hidden />
              New vault
            </Link>
          </Button>
        </div>
      </header>

      {q && vaults && vaults.length > 0 && (
        <p className="coord mt-3" aria-live="polite">
          Showing {filtered.length} of {vaults.length}
        </p>
      )}

      {error ? (
        <EmptyState
          title="Couldn't load vaults"
          description="Something went wrong fetching your vaults."
        />
      ) : vaults === null ? (
        <div className="coord py-8" role="status" aria-live="polite">
          Loading…
        </div>
      ) : vaults.length === 0 ? (
        <EmptyState
          icon={
            <span className="feature-tile feat-knowledge h-14 w-14">
              <Plus className="h-6 w-6" aria-hidden />
            </span>
          }
          title="No vaults yet"
          description="Create your first vault to start collecting documents, tables, and files for your agents."
          action={
            <Button asChild variant="accent" size="sm">
              <Link to="/vault/new">
                <Plus className="h-4 w-4" aria-hidden />
                Create first vault
              </Link>
            </Button>
          }
        />
      ) : filtered.length === 0 ? (
        <EmptyState
          title={`No matches for "${filter}"`}
          action={
            <Button variant="outline" size="sm" onClick={() => setFilter("")}>
              Clear filter
            </Button>
          }
        />
      ) : (
        <VaultList vaults={filtered} />
      )}
    </div>
  );
}
