import { useState } from "react";
import { useQueryClient } from "@tanstack/react-query";

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { ConfirmDialog } from "@/components/ui/confirm-dialog";
import { ownershipQueryKey, type useProviderOwnership } from "@/lib/admin-sso-queries";
import { ApiError, setAdminSsoOwnership, type AdminSsoOwnership } from "@/lib/api";

type Action = { kind: "release" | "clear"; shown: AdminSsoOwnership };

const STALE_SCREEN =
  "The provider changed since this screen loaded, so nothing was changed. Review the refreshed state before trying again.";

/**
 * Owner, revision, emergency latch, drift and secret-revision evidence for one
 * provider, with the two human-only ways back: releasing ownership and clearing
 * the emergency latch. Each is a compare-and-set on exactly what is shown.
 */
export function ProviderOwnershipDetails({
  alias,
  query,
  onChanged,
}: {
  alias: string;
  query: ReturnType<typeof useProviderOwnership>;
  onChanged: (message: string) => void;
}) {
  const queryClient = useQueryClient();
  const [action, setAction] = useState<Action | null>(null);
  const accessDenied = [401, 403].includes((query.error as { status?: number } | null)?.status ?? 0);
  const ownership = accessDenied ? undefined : query.data;

  if (query.isPending) {
    return <p className="mt-2 text-xs leading-5 text-foreground-muted">Reading ownership…</p>;
  }
  if (!ownership) {
    return (
      <p className="mt-2 text-xs leading-5 text-foreground-muted">
        <Badge variant="warning">Ownership unknown</Badge>{" "}
        Ownership could not be read. The server still refuses ordinary changes to an owned provider.
      </p>
    );
  }

  async function confirm(current: Action) {
    const { shown } = current;
    try {
      const updated = await setAdminSsoOwnership(alias, {
        owner_id: current.kind === "release" ? null : shown.owner_id,
        expected_revision: shown.revision,
        expected_fingerprint: shown.fingerprint,
        clear_emergency: current.kind === "clear",
      });
      queryClient.setQueryData(ownershipQueryKey(alias), updated);
      onChanged(current.kind === "release"
        ? `Ownership of ${alias} was released.`
        : `The emergency latch on ${alias} was cleared. The provider stays as it is.`);
    } catch (caught) {
      if (caught instanceof ApiError && caught.status === 409) {
        await query.refetch();
        throw new Error(STALE_SCREEN, { cause: caught });
      }
      throw caught;
    }
  }

  const owned = ownership.owner_id !== null;
  return (
    <div className="mt-2 space-y-2">
      <div className="flex flex-wrap items-center gap-2 text-xs leading-5 text-foreground-muted">
        {owned ? (
          <span>Owner <span className="coord break-all">{ownership.owner_id}</span></span>
        ) : (
          <span>No external owner</span>
        )}
        <span aria-hidden>·</span>
        <span>Revision {ownership.revision}</span>
        {ownership.emergency_disabled && <Badge variant="warning">Emergency latch set</Badge>}
        {ownership.drifted && <Badge variant="error">Drift detected</Badge>}
      </div>
      <p className="text-xs leading-5 text-foreground-muted">
        {ownership.provider_secret_revision === null
          ? "No secret revision recorded."
          : <>Secret revision recorded: <span className="coord break-all">{ownership.provider_secret_revision}</span></>}
      </p>
      {ownership.drifted && (
        <p className="text-xs leading-5 text-foreground-muted">
          The live provider no longer matches the last recorded change. Inspect it before releasing or approving anything.
        </p>
      )}
      {(owned || ownership.emergency_disabled) && (
        <div className="flex flex-wrap gap-2">
          {owned && (
            <Button type="button" size="sm" variant="outline" onClick={() => setAction({ kind: "release", shown: ownership })}>
              Release ownership
            </Button>
          )}
          {ownership.emergency_disabled && (
            <Button type="button" size="sm" variant="outline" onClick={() => setAction({ kind: "clear", shown: ownership })}>
              Clear emergency latch
            </Button>
          )}
        </div>
      )}
      <ConfirmDialog
        open={action !== null}
        onOpenChange={(open) => { if (!open) setAction(null); }}
        title={action?.kind === "release" ? `Release ownership of ${alias}?` : `Clear the emergency latch on ${alias}?`}
        variant={action?.kind === "release" ? "destructive" : "default"}
        confirmLabel={action?.kind === "release" ? "Release ownership" : "Clear emergency latch"}
        description={action && (action.kind === "release"
          ? `The external owner ${action.shown.owner_id} can no longer operate this provider, and it returns to ordinary management here. Releasing clears the recorded secret revision${action.shown.provider_secret_revision ? ` (${action.shown.provider_secret_revision})` : ""}. Applies only if the provider is still at revision ${action.shown.revision}.`
          : `This records that the emergency is over. It does not enable the provider. Applies only if the provider is still at revision ${action.shown.revision}.`)}
        onConfirm={() => (action ? confirm(action) : undefined)}
      />
    </div>
  );
}
