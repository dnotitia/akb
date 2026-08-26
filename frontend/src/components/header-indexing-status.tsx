import { AlertTriangle, CircleDashed } from "lucide-react";
import { Badge } from "@/components/ui/badge";
import type { AccessibleIndexingStatus } from "@/hooks/use-accessible-indexing-health";

export function HeaderIndexingStatus({
  status,
}: {
  status: AccessibleIndexingStatus | null;
}) {
  const suffix = status?.incomplete ? "+" : "";
  const abandoned = status?.abandoned ?? 0;
  const pending = status?.pending ?? 0;
  const hasAbandoned = abandoned > 0;
  const hasPending = pending > 0;
  const visibleLabel = hasAbandoned
    ? `${abandoned.toLocaleString()}${suffix} need attention`
    : hasPending
      ? `${pending.toLocaleString()}${suffix} indexing`
      : null;
  const announcement = !status
    ? "Checking indexing across accessible Vaults"
    : hasAbandoned
      ? `${status.abandoned.toLocaleString()}${suffix} items need indexing attention across accessible Vaults`
      : hasPending
        ? `${status.pending.toLocaleString()}${suffix} items are indexing across accessible Vaults`
        : status.incomplete
          ? "Indexing status is temporarily unavailable for some accessible Vaults"
          : `Knowledge indexing is caught up across ${status.vaultCount.toLocaleString()} accessible Vaults`;

  return (
    <div
      className="hidden w-44 shrink-0 items-center justify-end lg:flex"
      role="status"
      aria-live="polite"
      aria-atomic="true"
      data-testid="header-indexing-status"
    >
      <span className="sr-only">{announcement}</span>
      {visibleLabel && (
        <Badge
          aria-hidden="true"
          variant={hasAbandoned ? "error" : "pending"}
          className="max-w-full tabular-nums"
          title={announcement}
        >
          {hasAbandoned ? (
            <AlertTriangle className="h-3 w-3 shrink-0" aria-hidden />
          ) : (
            <CircleDashed className="h-3 w-3 shrink-0 animate-spin" aria-hidden />
          )}
          <span className="min-w-0 truncate">{visibleLabel}</span>
        </Badge>
      )}
    </div>
  );
}
