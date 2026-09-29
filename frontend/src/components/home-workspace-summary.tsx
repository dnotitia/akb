import { useQuery } from "@tanstack/react-query";
import { Box, FileText, Paperclip, Table2, type LucideIcon } from "lucide-react";
import { LoadingState } from "@/components/ui/loading-state";
import { getWorkspaceSummary, validCount, WorkspaceSummaryAccessDenied, type WorkspaceCountField } from "@/lib/workspace-summary";
import { cn } from "@/lib/utils";

const metrics: { field: WorkspaceCountField; label: string; icon: LucideIcon; tone: string }[] = [
  { field: "vault_count", label: "Vaults", icon: Box, tone: "text-cat-1" },
  { field: "document_count", label: "Documents", icon: FileText, tone: "text-cat-1" },
  { field: "table_count", label: "Tables", icon: Table2, tone: "text-cat-3" },
  { field: "file_count", label: "Files", icon: Paperclip, tone: "text-cat-4" },
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
  const summary = <dl className="grid w-full max-w-2xl grid-cols-2 gap-x-4 gap-y-4 sm:grid-cols-4 sm:gap-x-6">
    {(loading ? metrics : shown).map(({ field, label, icon: Icon, tone }, index) => <div key={field} className={cn(
      "min-w-0 border-border",
      index % 2 === 1 && "border-l pl-4 sm:pl-6",
      index === 2 && "sm:border-l sm:pl-6",
    )}>
      <dt className="flex items-center gap-2 text-sm leading-5 text-foreground-muted">
        <Icon className={cn("h-4 w-4 shrink-0", tone)} aria-hidden />
        <span className="min-w-0 break-words">{label}</span>
      </dt>
      <dd className="mt-1 min-h-7 text-xl font-semibold leading-7 tracking-tight wrap-anywhere tabular-nums text-foreground" aria-label={!loading && !validCount(values[field]) ? "Unavailable" : undefined}>
        {loading ? <span className="my-1 block h-5 w-12 rounded-[var(--radius-xs)] bg-surface-2" />
          : validCount(values[field]) ? values[field].toLocaleString() : "—"}
      </dd>
    </div>)}
  </dl>;

  return <section aria-label="Workspace summary" className="mb-6 space-y-3">
    <span className="sr-only">Resources in vaults you can access.</span>
    {loading ? <LoadingState label="Loading workspace totals">{summary}</LoadingState> : summary}
    {unavailable && <p className="text-xs text-foreground-muted" title="Complete workspace totals are unavailable. You can still open your vaults and documents.">Totals unavailable</p>}
  </section>;
}
