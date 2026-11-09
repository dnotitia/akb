import { useEffect, useState } from "react";
import { FolderPlus, Plus } from "lucide-react";
import { listVaults } from "@/lib/api";
import type { VaultSummary } from "@/hooks/use-vaults";
import { Alert } from "@/components/ui/alert";
import { Button } from "@/components/ui/button";
import { LoadingState } from "@/components/ui/loading-state";
import { Skeleton } from "@/components/ui/skeleton";
import { VaultDirectory } from "@/components/vault-directory";
import { useOpenVaultCreateDialog } from "@/contexts/vault-create-dialog-context";
import { useAccessVerification, useCurrentUser } from "@/contexts/current-user-context";
import { useVaultDirectoryView } from "@/hooks/use-vault-directory-view";

export default function VaultIndexPage() {
  const user = useCurrentUser();
  const { checking, revision } = useAccessVerification();
  return <VaultIndexWorkspace key={JSON.stringify([user?.user_id, revision, checking])} checking={checking} />;
}

function VaultIndexWorkspace({ checking }: { checking: boolean }) {
  const [vaults, setVaults] = useState<VaultSummary[] | null>(null);
  const [loadError, setLoadError] = useState(false);
  const [reloadKey, setReloadKey] = useState(0);

  useEffect(() => {
    if (checking) return;
    let cancelled = false;
    setVaults(null);
    setLoadError(false);
    listVaults()
      .then((data) => {
        if (!cancelled) setVaults((data.vaults as VaultSummary[]) || []);
      })
      .catch(() => {
        if (!cancelled) setLoadError(true);
      });
    return () => {
      cancelled = true;
    };
  }, [reloadKey, checking]);

  if (loadError) {
    return (
      <div className="vault-page">
        <Alert variant="destructive" title="Vaults could not be loaded" className="w-full">
          The workspace is still available, but the vault directory did not respond.
          <div className="mt-3">
            <Button variant="outline" size="sm" onClick={() => setReloadKey((key) => key + 1)}>
              Try again
            </Button>
          </div>
        </Alert>
      </div>
    );
  }

  if (vaults === null) return <VaultIndexLoading />;
  if (vaults.length === 0) return <FirstVaultState />;
  return <VaultSelectionState vaults={vaults} />;
}

function VaultIndexLoading() {
  const { view } = useVaultDirectoryView();
  return <div className="vault-page vault-directory @container/directory">
    <LoadingState label="Loading available vaults">
      <div className="vault-directory-heading" aria-hidden><div className="min-w-0 flex-1"><Skeleton className="vault-directory-title-skeleton" /><Skeleton className="vault-directory-description-skeleton mt-1 h-5 w-72 max-w-full" /></div><Skeleton className="vault-control-skeleton w-28" /></div>
      <div className="vault-directory-tools" aria-hidden><Skeleton className="vault-control-skeleton w-full" /></div>
      <div className="vault-directory-filter-row" aria-hidden>
        <div className="vault-directory-filters"><Skeleton className="vault-control-skeleton w-52 max-w-full" /></div>
        <p><Skeleton className="h-4 w-20" /></p>
        <div className="vault-view-switch"><Skeleton className="vault-view-placeholder" /><Skeleton className="vault-view-placeholder" /></div>
      </div>
      <div className={view === "grid" ? "vault-library-grid" : "vault-library-list"} aria-hidden>
        {Array.from({ length: 6 }, (_, index) => <div key={index} className="vault-library-card vault-library-card-skeleton">
          <Skeleton className="h-7 w-7" />
          <div className="w-full min-w-0"><Skeleton className="h-4 w-3/4" /><Skeleton className="mt-2 h-3 w-full" /></div>
        </div>)}
      </div>
    </LoadingState>
  </div>;
}

function VaultSelectionState({ vaults }: { vaults: VaultSummary[] }) {
  return <VaultDirectory vaults={vaults} />;
}

function FirstVaultState() {
  const create = useOpenVaultCreateDialog();
  return <section aria-labelledby="first-vault-heading" className="vault-page">
    <div className="vault-first-space">
    <span className="vault-library-mark mb-5"><FolderPlus className="h-6 w-6" aria-hidden /></span>
    <h1 id="first-vault-heading" className="text-xl font-semibold">Create your first vault.</h1>
    <p className="mt-2 max-w-xl text-sm leading-relaxed text-foreground-muted">A vault keeps related documents, tables, and files together. Add collections as your knowledge grows.</p>
    <Button variant="accent" className="vault-primary-action mt-5 shadow-none" onClick={create}><Plus className="h-4 w-4" aria-hidden />Create first vault</Button>
    <p className="mt-4 text-sm text-foreground-muted">Joining an existing team? Ask a vault owner to invite you.</p>
    </div>
  </section>;
}
