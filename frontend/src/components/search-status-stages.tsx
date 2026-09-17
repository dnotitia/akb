import type { StageObservation, VaultSearchObservation, WorkState } from "@/lib/indexing-status";
import { timeAgo } from "@/lib/utils";

const STATE_LABELS: Record<WorkState, string> = {
  clear: "No updates waiting", updating: "Updating", attention: "Needs attention", paused: "Updates paused", unknown: "Limited status",
};

function stageCaption(stage: StageObservation) {
  if (stage.mode === "not_applicable") return "Not applicable";
  if (stage.mode === "disabled") return stage.state === "paused" ? "Paused by configuration" : "Disabled by configuration";
  if (stage.state === "attention" && (stage.exhausted ?? 0) > 0 && !(stage.abandoned ?? 0)) return "Final retry outcome unconfirmed";
  if (!stage.versioned && stage.state === "updating") return "Reported queued work";
  return STATE_LABELS[stage.state];
}

/** Shared read-only details: units remain separate; retries are never added to pending. */
export function SearchStatusStages({ observation }: { observation?: VaultSearchObservation }) {
  if (!observation) return <p className="text-xs text-foreground-muted">Search-update status has not been verified.</p>;
  const old = observation.observation !== "fresh";
  return <div className="space-y-3 text-xs text-foreground-muted">
    {old && <p>These are last-known observations, not current processing status.</p>}
    {observation.stages.length === 0 && <p>No processing details are available.</p>}
    <dl className="divide-y divide-border">
      {observation.stages.map(stage => <div key={stage.id} className="py-2 first:pt-0 last:pb-0">
        <dt className="flex items-start justify-between gap-3"><span className="font-medium text-foreground">{stage.label}</span><span className="text-right">{old ? `Last reported: ${stageCaption(stage).toLowerCase()}` : stageCaption(stage)}</span></dt>
        <dd className="mt-1 space-y-1 leading-4">
          {stage.mode !== "not_applicable" && <p className="tabular-nums">{stage.pending === undefined ? "Pending count unavailable" : `${stage.pending.toLocaleString()} ${stage.unit.replaceAll("_", " ")} pending`}
            {stage.retrying !== undefined && stage.retrying > 0 ? ` · ${stage.retrying.toLocaleString()} retrying` : ""}</p>}
          {(stage.exhausted ?? 0) > 0 && <p>{stage.exhausted!.toLocaleString()} at the final retry boundary; completion is not yet confirmed.</p>}
          {(stage.abandoned ?? 0) > 0 && <p>{stage.historicalFailure ? "Recorded processing failures" : "Stopped processing"}: {stage.abandoned!.toLocaleString()}{stage.historicalFailure ? ". Current impact is not available." : ". An AKB operator may need to investigate."}</p>}
          {!stage.versioned && <p>Legacy observation; stage configuration and count scope are not fully verified.</p>}
          {stage.mode === "unknown" && stage.versioned && <p>Stage configuration could not be verified.</p>}
        </dd>
      </div>)}
    </dl>
    {observation.receivedAt !== undefined && <p>Last observed <time dateTime={new Date(observation.receivedAt).toISOString()} title={new Date(observation.receivedAt).toLocaleString()}>{timeAgo(new Date(observation.receivedAt).toISOString())}</time>.</p>}
  </div>;
}
