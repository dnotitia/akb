import { useId } from "react";
import { AlertTriangle, CircleDashed, CircleHelp, PauseCircle } from "lucide-react";
import { Portal as TooltipPortal } from "@radix-ui/react-tooltip";
import { Button } from "@/components/ui/button";
import { Popover, PopoverContent, PopoverTrigger } from "@/components/ui/popover";
import { Tooltip, TooltipContent, TooltipProvider, TooltipTrigger } from "@/components/ui/tooltip";
import { SearchStatusStages } from "@/components/search-status-stages";
import { useOptionalSearchStatus } from "@/hooks/use-search-status";
import { pendingIndexingCount, summarizeIndexing } from "@/lib/indexing-status";
import { cn } from "@/lib/utils";

/** Operational observations live beside Overview's queue badge, not in settings. */
export function VaultProcessingDetails({ vaultName }: { vaultName: string }) {
  const titleId = useId();
  const status = useOptionalSearchStatus();
  const observation = status?.verified
    ? status.observations.find(row => row.vaultName === vaultName)
    : undefined;
  if (!status) return null;
  const rows = observation ? [observation] : [];
  const summary = summarizeIndexing(rows, { verified: !!observation, loading: status.loading && !status.verified });
  const { pending, incomplete } = pendingIndexingCount(rows, !!observation);
  const fresh = observation?.observation === "fresh";
  const warning = fresh && summary.tone === "warning";
  const paused = fresh && summary.pausedIds.length > 0;
  // Only verified, fully observed, idle work can disappear. Missing/stale
  // evidence is never silently presented as an empty queue.
  if (fresh && summary.complete && pending === 0 && summary.label === "Search status") return null;
  const label = warning ? "Updates need attention"
    : paused ? "Updates paused"
    : pending > 0 ? `${pending.toLocaleString()}${incomplete ? "+" : ""} indexing`
    : fresh && summary.updatingIds.length > 0 ? "Search updates pending"
    : fresh && summary.label === "Details updating" ? "Metadata updating"
    : status.loading && !status.verified ? "Checking updates…"
    : !fresh ? "Status unavailable" : "Limited status";
  const description = pending > 0
    ? `${pending.toLocaleString()}${incomplete ? "+" : ""} chunks waiting for search indexing in ${vaultName}. ${summary.description}`
    : `${vaultName}: ${summary.description}`;
  const Icon = warning ? AlertTriangle : paused ? PauseCircle : pending > 0 ? CircleDashed : CircleHelp;
  return (
    <Popover>
      <TooltipProvider delayDuration={300}><Tooltip>
        <TooltipTrigger asChild><PopoverTrigger asChild>
          <Button variant="ghost" size="sm" aria-label={`${label} in ${vaultName}`}
            className={cn("min-h-11 max-w-full gap-1.5 px-2 text-xs sm:min-h-8 [@media(pointer:coarse)]:min-h-11", warning ? "text-warning" : "text-foreground-muted")}>
            <Icon className="h-3.5 w-3.5 shrink-0" aria-hidden />{label}
          </Button>
        </PopoverTrigger></TooltipTrigger>
        <TooltipPortal><TooltipContent side="bottom" className="max-w-80 font-normal tracking-normal">{description}</TooltipContent></TooltipPortal>
      </Tooltip></TooltipProvider>
      <PopoverContent align="start" aria-labelledby={titleId}>
        <h2 id={titleId} className="mb-3 break-words text-sm font-semibold">Search updates · {vaultName}</h2>
        <SearchStatusStages observation={observation} />
      </PopoverContent>
    </Popover>
  );
}
