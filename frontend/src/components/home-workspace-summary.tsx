import { useQuery } from "@tanstack/react-query";
import { LoadingState } from "@/components/ui/loading-state";
import { getWorkspaceSummary, validCount, WorkspaceSummaryAccessDenied, type WorkspaceCountField } from "@/lib/workspace-summary";

const metrics: { field: WorkspaceCountField; label: string }[] = [
  { field: "vault_count", label: "Vaults" },
  { field: "document_count", label: "Documents" },
  { field: "table_count", label: "Tables" },
  { field: "file_count", label: "Files" },
];

export function HomeWorkspaceSummary({ userId, directoryKey, vaultCount, directoryLoading }: {
  userId: string;
  directoryKey: string;
  vaultCount: number | undefined;
  directoryLoading: boolean;
}) {
  const verified = !!userId && validCount(vaultCount) && !directoryLoading;
  const query = useQuery({
    queryKey: ["workspace-summary", userId, directoryKey],
    queryFn: ({ signal }) => getWorkspaceSummary(signal),
    enabled: verified,
    retry: false,
    staleTime: 30_000,
    gcTime: 60_000,
    // Directory verification precedes this request. Do not flash cached private
    // totals on a new visit or race the shell's foreground identity proof.
    refetchOnMount: "always",
    refetchOnWindowFocus: false,
    refetchOnReconnect: "always",
  });
  const loading = directoryLoading || (verified && query.isFetching);
  const snapshot = verified && !query.isFetching && !query.isError ? query.data : undefined;
  const totalVaults = snapshot?.vault_count ?? (verified && !(query.error instanceof WorkspaceSummaryAccessDenied) ? vaultCount : undefined);
  const shown = totalVaults === 0 ? metrics.slice(0, 1) : metrics;
  const values = { ...snapshot, vault_count: totalVaults };
  const unavailable = !loading && shown.some(({ field }) => !validCount(values[field]));

  return <section aria-label="Workspace summary" className="mb-5 flex flex-wrap items-baseline gap-x-6 gap-y-2">
    <span className="sr-only">Resources in vaults you can access.</span>
    {loading ? <LoadingState label="Loading workspace totals">
      <div className="grid grid-cols-2 gap-x-6 gap-y-2 sm:flex sm:flex-wrap sm:gap-x-6">
        {metrics.map(({ field, label }) => <span key={field} className="inline-flex items-baseline gap-1.5 text-sm text-foreground-muted"><span className="h-4 w-8 self-center rounded-[var(--radius-sm)] bg-surface-2" />{label}</span>)}
      </div>
    </LoadingState> : <>
      <dl className="grid grid-cols-2 gap-x-6 gap-y-2 sm:flex sm:flex-wrap sm:gap-x-6">
        {shown.map(({ field, label }) => <div key={field} className="flex items-baseline gap-1.5">
          <dt className="order-2 text-sm text-foreground-muted">{label}</dt>
          <dd className="text-base font-semibold tabular-nums text-foreground" aria-label={validCount(values[field]) ? undefined : "Unavailable"}>
            {validCount(values[field]) ? values[field].toLocaleString() : "—"}
          </dd>
        </div>)}
      </dl>
      {unavailable && <span className="text-xs text-foreground-muted" title="Complete workspace totals are unavailable. You can still open your vaults and documents.">Totals unavailable</span>}
    </>}
  </section>;
}
