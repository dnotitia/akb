import { CircleDashed } from "lucide-react";
import { Badge } from "@/components/ui/badge";
import { useOptionalSearchStatus } from "@/hooks/use-search-status";
import { pendingIndexingCount } from "@/lib/indexing-status";

/** A queue indicator, not a button, completion estimate, or health assertion. */
export function PendingIndexingBadge({ pending, incomplete = false, vaultName }: {
  pending: number;
  incomplete?: boolean;
  vaultName?: string;
}) {
  if (pending <= 0) return null;
  const count = pending.toLocaleString();
  const scope = vaultName ? `in ${vaultName}` : "across accessible vaults";
  const description = `${incomplete ? "At least " : ""}${count} ${pending === 1 ? "chunk" : "chunks"} waiting for search indexing ${scope}.${incomplete ? " Some indexing counts are unavailable." : ""}`;

  return (
    <Badge variant="pending" className="max-w-full gap-1.5 whitespace-nowrap tabular-nums" title={description}>
      <CircleDashed className="h-3 w-3 shrink-0 animate-spin motion-reduce:animate-none" aria-hidden />
      <span aria-hidden="true" className="truncate">{count}{incomplete ? "+" : ""} indexing</span>
      <span className="sr-only">{description}</span>
    </Badge>
  );
}

export function VaultIndexingStatus({ vaultName }: { vaultName: string }) {
  const status = useOptionalSearchStatus();
  const observation = status?.observations.find(row => row.vaultName === vaultName);
  const { pending, incomplete } = pendingIndexingCount(observation ? [observation] : [], Boolean(observation));
  // The global header owns live announcements; the scoped badge is ordinary text.
  return <PendingIndexingBadge pending={pending} incomplete={incomplete} vaultName={vaultName} />;
}
