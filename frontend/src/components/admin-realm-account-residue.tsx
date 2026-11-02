import { useQuery, type UseQueryResult } from "@tanstack/react-query";

import { Alert } from "@/components/ui/alert";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { LoadingState } from "@/components/ui/loading-state";
import { Panel, PanelHeader } from "@/components/ui/panel";
import { Skeleton } from "@/components/ui/skeleton";
import { StatTile } from "@/components/ui/stat-tile";
import {
  getAdminRealmAccountResidue,
  type AdminRealmAccountResidue,
  type AdminRealmResidueAccount,
} from "@/lib/api";
import { formatDate } from "@/lib/utils";

const REALM_ACCOUNT_RESIDUE_QUERY_KEY = ["admin-realm-account-residue", 1] as const;

/**
 * Read-only reconciliation of the installation realm against AKB: accounts
 * with neither an exact binding nor a recorded arrival. Nothing here deletes
 * or expires an account; AKB's management client cannot, by design.
 */
export function RealmAccountResidue() {
  const query = useQuery<AdminRealmAccountResidue>({
    queryKey: REALM_ACCOUNT_RESIDUE_QUERY_KEY,
    queryFn: getAdminRealmAccountResidue,
    staleTime: 30_000,
  });

  return (
    <section className="space-y-4" aria-labelledby="realm-residue-heading" aria-busy={query.isFetching}>
      <div>
        <h2 id="realm-residue-heading" className="font-display text-xl font-semibold tracking-tight">
          Realm account residue
        </h2>
        <p className="mt-1 text-sm leading-6 text-foreground-muted">
          Accounts in this installation&apos;s realm that have no AKB binding and no recorded
          arrival. AKB only reads them. Removing a realm account is a separate Keycloak operator
          action.
        </p>
      </div>
      <RealmAccountResidueBody query={query} />
    </section>
  );
}

function RealmAccountResidueBody({
  query,
}: {
  query: UseQueryResult<AdminRealmAccountResidue>;
}) {
  const retry = (
    <Button variant="outline" size="sm" loading={query.isFetching} onClick={() => void query.refetch()}>
      Retry
    </Button>
  );

  if (query.isPending) {
    return <RealmAccountResidueLoading />;
  }
  // A refused read (the admin session ended) hides what an earlier read showed.
  const accessDenied = [401, 403].includes((query.error as { status?: number } | null)?.status ?? 0);
  const residue = accessDenied ? undefined : query.data;
  if (!residue) {
    return (
      <Alert variant="destructive" title="Realm accounts could not be reconciled">
        No residue count is shown because the reconciliation did not complete. {retry}
      </Alert>
    );
  }
  if (residue.state === "unavailable" && residue.unavailable_code === "keycloak_provider_control_delegated") {
    return (
      <Alert variant="info" title="The realm is managed by the deployment operator">
        This installation holds no realm management credential, so it cannot read the realm&apos;s
        accounts. No residue count is shown.
      </Alert>
    );
  }
  if (residue.state === "unavailable") {
    return (
      <Alert variant="warning" title="Realm accounts are unavailable">
        The installation realm could not be read, so no residue count is shown.{" "}
        <span className="coord">{residue.unavailable_code}</span> {retry}
      </Alert>
    );
  }

  const unbound = residue.pending_arrival_count + residue.residue_count;
  return (
    <div className="space-y-4">
      {query.isError && (
        <Alert variant="warning">
          Could not refresh the reconciliation. The previous read is shown. {retry}
        </Alert>
      )}
      {residue.truncated && (
        <Alert variant="warning" title="Realm listing truncated">
          The realm holds more than {residue.scan_limit.toLocaleString()} accounts. These counts
          cover only the first {residue.realm_account_count.toLocaleString()} read.
        </Alert>
      )}
      <div className="grid gap-3 sm:grid-cols-3">
        <StatTile label="Residue" value={residue.residue_count} kind="No binding, no arrival" />
        <StatTile label="Pending arrivals" value={residue.pending_arrival_count} kind="Awaiting approval" dimZero />
        <StatTile label="Realm accounts read" value={residue.realm_account_count} kind={`${residue.bound_count} bound`} dimZero />
      </div>
      {unbound === 0 ? (
        <Alert variant="success">Every realm account that was read has an AKB binding.</Alert>
      ) : (
        <Panel flush>
          <PanelHeader label="Unbound realm accounts" count={unbound} />
          {residue.accounts.length < unbound && (
            <p className="border-b border-border px-4 py-2 text-xs leading-5 text-foreground-muted">
              Showing {residue.accounts.length} of {unbound}, residue first and oldest first.
            </p>
          )}
          <ul className="divide-y divide-border">
            {residue.accounts.map((account) => (
              <ResidueRow key={account.realm_user_id} account={account} />
            ))}
          </ul>
        </Panel>
      )}
    </div>
  );
}

function ResidueRow({ account }: { account: AdminRealmResidueAccount }) {
  return (
    <li className="flex flex-col gap-2 p-4 sm:flex-row sm:items-start sm:justify-between">
      <div className="min-w-0">
        <p className="break-all font-medium">{account.username}</p>
        <p className="coord mt-1 break-all">{account.realm_user_id}</p>
        <p className="mt-1 text-xs leading-5 text-foreground-muted">
          Created {account.created_at ? formatDate(account.created_at) : "at an unknown time"}
        </p>
      </div>
      <div className="shrink-0 sm:text-right">
        {account.arrival ? (
          <>
            <Badge variant="pending">Arrival pending</Badge>
            <p className="mt-1 text-xs leading-5 text-foreground-muted">
              Last arrived {formatDate(account.arrival.last_seen_at)}
            </p>
            <p className="coord mt-1 break-all">{account.arrival.admission_id}</p>
          </>
        ) : (
          <Badge variant="outline">No record in AKB</Badge>
        )}
      </div>
    </li>
  );
}

function RealmAccountResidueLoading() {
  return (
    <LoadingState label="Reconciling realm accounts" className="space-y-3">
      <div className="grid gap-3 sm:grid-cols-3">
        {[0, 1, 2].map((item) => (
          <Skeleton key={item} className="h-24 w-full rounded-[var(--radius-lg)]" />
        ))}
      </div>
      <Skeleton className="h-16 w-full rounded-[var(--radius-lg)]" />
    </LoadingState>
  );
}
