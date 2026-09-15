import { useSearchStatus } from "@/hooks/use-search-status";
import { pendingIndexingCount } from "@/lib/indexing-status";
import { PendingIndexingBadge } from "@/components/pending-indexing-badge";

export function HeaderIndexingStatus() {
  const { observations, verified } = useSearchStatus();
  const { pending, incomplete } = pendingIndexingCount(observations, verified);
  return (
    <div data-testid="header-indexing-status" className="hidden max-w-36 shrink-0 items-center lg:flex"
      role="status" aria-live="polite" aria-atomic="true">
      <PendingIndexingBadge pending={pending} incomplete={incomplete} />
    </div>
  );
}
