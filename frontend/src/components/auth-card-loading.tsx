import { LoadingState } from "@/components/ui/loading-state";
import { Skeleton } from "@/components/ui/skeleton";

interface AuthCardLoadingProps {
  label?: string;
  compact?: boolean;
}

/** Mirrors the final authentication card so policy discovery never flashes an empty panel. */
export function AuthCardLoading({
  label = "Loading authentication options",
  compact = false,
}: AuthCardLoadingProps) {
  return (
    <LoadingState label={label}>
      <div className={compact ? "space-y-4" : "space-y-5"}>
        {!compact && (
          <div className="grid grid-cols-2 gap-1 rounded-[var(--radius-md)] bg-surface-2 p-1">
            <Skeleton className="h-9 rounded-[var(--radius-sm)]" />
            <Skeleton className="h-9 rounded-[var(--radius-sm)]" />
          </div>
        )}
        <div className="space-y-2">
          <Skeleton className="h-3 w-24 rounded-[var(--radius-sm)]" />
          <Skeleton className="h-10 w-full rounded-[var(--radius-md)]" />
        </div>
        <div className="space-y-2">
          <Skeleton className="h-3 w-20 rounded-[var(--radius-sm)]" />
          <Skeleton className="h-10 w-full rounded-[var(--radius-md)]" />
        </div>
        <Skeleton className="h-10 w-full rounded-[var(--radius-md)]" />
        <Skeleton className="mx-auto h-3 w-40 rounded-[var(--radius-sm)]" />
      </div>
    </LoadingState>
  );
}
