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
      <div className="mx-auto w-full max-w-6xl p-3 sm:p-4 lg:p-6">
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
  return <div className="@container/directory mx-auto w-full max-w-6xl p-3 sm:p-4 lg:p-6">
    <LoadingState label="Loading available vaults">
      <Skeleton className="h-12 w-56" />
      <Skeleton className="mt-4 h-10 w-full" />
      <div className="my-3 flex h-[2.625rem] items-center justify-between max-sm:h-[3.125rem] [@media(pointer:coarse)]:h-[3.125rem]"><Skeleton className="h-4 w-24" /><Skeleton className="h-9 w-20 max-sm:h-11 max-sm:w-24 [@media(pointer:coarse)]:h-11 [@media(pointer:coarse)]:w-24" /></div>
      <div className={view === "grid" ? "grid auto-rows-fr grid-cols-1 gap-3 @min-[30rem]/directory:grid-cols-2 @min-[48rem]/directory:grid-cols-3 @min-[64rem]/directory:grid-cols-4" : "divide-y divide-border overflow-hidden rounded-[var(--radius-md)] border border-border"} aria-hidden>
        {Array.from({ length: 6 }, (_, index) => <div key={index} className={view === "grid" ? "vault-box vault-box-loading relative flex h-32 flex-col p-3" : "flex h-16 flex-col bg-surface p-3 pr-14"}>
          {view === "grid" && <div className="vault-box-front" />}
          {view === "grid" && <div className="mb-2 flex h-8 shrink-0 items-center"><Skeleton className="h-4 w-4" /></div>}
          <Skeleton className="h-4 w-3/4" />
          <Skeleton className="mt-auto h-3 w-full" />
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
  return <section aria-labelledby="first-vault-heading" className="mx-auto w-full max-w-4xl p-6 sm:p-8">
    <FolderPlus className="mb-4 h-6 w-6 text-link" aria-hidden />
    <h1 id="first-vault-heading" className="text-xl font-semibold">Create your first vault.</h1>
    <p className="mt-2 max-w-xl text-sm leading-relaxed text-foreground-muted">A vault keeps related documents, tables, and files together. Add collections as your knowledge grows.</p>
    <Button variant="accent" className="mt-5" onClick={create}><Plus className="h-4 w-4" aria-hidden />Create first vault</Button>
    <p className="mt-4 text-sm text-foreground-muted">Joining an existing team? Ask a vault owner to invite you.</p>
  </section>;
}
