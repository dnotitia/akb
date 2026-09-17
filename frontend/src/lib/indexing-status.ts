export type StageId =
  | "search_index"
  | "file_projection"
  | "content_preparation"
  | "metadata";

export type WorkState = "clear" | "updating" | "attention" | "paused" | "unknown";

export interface StageObservation {
  id: StageId;
  label: string;
  mode: "enabled" | "disabled" | "not_applicable" | "unknown";
  unit: string;
  scope: string;
  versioned: boolean;
  pending?: number;
  retrying?: number;
  exhausted?: number;
  abandoned?: number;
  state: WorkState;
  historicalFailure: boolean;
}

export interface VaultSearchObservation {
  vaultId: string;
  vaultName: string;
  observation: "checking" | "fresh" | "stale" | "unavailable";
  coverage: "complete" | "partial" | "unsupported";
  receivedAt?: number;
  attemptedAt?: number;
  stages: StageObservation[];
  core: WorkState;
  auxiliary: WorkState;
  historicalFailureReported: boolean;
  error?: string;
}

export interface IndexingSummary {
  label: string;
  description: string;
  tone: "neutral" | "info" | "warning";
  attentionIds: string[];
  updatingIds: string[];
  pausedIds: string[];
  unknownIds: string[];
  affectedIds: string[];
  complete: boolean;
  checkedCount: number;
  totalCount: number;
}

const STAGES = {
  search_index: {
    label: "Search index",
    unit: "chunks",
    scope: "stored_chunks",
    native: false,
  },
  file_projection: {
    label: "File updates",
    unit: "file_updates",
    scope: "latest_file_intents",
    native: true,
  },
  content_preparation: {
    label: "Content preparation",
    unit: "revision_updates",
    scope: "current_heads",
    native: true,
  },
  metadata: {
    label: "Automatic metadata",
    unit: "documents",
    scope: "external_git_documents",
    native: false,
  },
} as const;

const STAGE_IDS = Object.keys(STAGES) as StageId[];
const COUNTERS = ["pending", "retrying", "exhausted", "abandoned"] as const;
type Counters = Partial<Pick<StageObservation, (typeof COUNTERS)[number]>>;

function object(value: unknown): Record<string, unknown> | undefined {
  return value !== null && typeof value === "object" && !Array.isArray(value)
    ? (value as Record<string, unknown>)
    : undefined;
}

function count(value: unknown): number | undefined {
  return typeof value === "number" && Number.isSafeInteger(value) && value >= 0
    ? value
    : undefined;
}

function counters(source: Record<string, unknown>): {
  values: Counters;
  invalid: boolean;
} {
  const values: Counters = {};
  let invalid = false;
  for (const key of COUNTERS) {
    const value = count(source[key]);
    if (value !== undefined) values[key] = value;
    else if (source[key] !== null && source[key] !== undefined) invalid = true;
  }
  return { values, invalid };
}

function unknownStage(id: StageId, versioned: boolean): StageObservation {
  return {
    id,
    label: STAGES[id].label,
    mode: "unknown",
    unit: "unknown",
    scope: "unknown",
    versioned,
    state: "unknown",
    historicalFailure: false,
  };
}

function hasWork(values: Counters): boolean {
  return COUNTERS.some((key) => (values[key] ?? 0) > 0);
}

function decodeVersionedStage(
  id: StageId,
  value: unknown,
): { stage: StageObservation; complete: boolean } {
  const stage = unknownStage(id, true);
  const source = object(value);
  if (!source) return { stage, complete: false };
  const definition = STAGES[id];
  const { values, invalid } = counters(source);
  if (
    source.mode === "enabled" ||
    source.mode === "disabled" ||
    source.mode === "not_applicable"
  ) {
    stage.mode = source.mode;
  }
  if (source.unit === definition.unit) stage.unit = definition.unit;
  if (source.scope === definition.scope) stage.scope = definition.scope;
  // A count with the wrong evidence scope or unit cannot inform this stage.
  if (stage.unit === "unknown" || stage.scope === "unknown") {
    return { stage, complete: false };
  }
  const { pending, retrying, exhausted, abandoned } = values;
  if (
    (!definition.native && source.exhausted !== undefined && source.exhausted !== null) ||
    (pending !== undefined && retrying !== undefined && retrying > pending) ||
    (pending !== undefined && exhausted !== undefined && exhausted > pending) ||
    (definition.native && pending !== undefined && retrying !== undefined &&
      exhausted !== undefined && retrying > pending - exhausted) ||
    (stage.mode === "not_applicable" && hasWork(values))
  ) {
    // Do not repair contradictory subset counts, or display their sum as work.
    return { stage, complete: false };
  }
  Object.assign(stage, values);
  const requiredKnown = pending !== undefined && abandoned !== undefined &&
    (!definition.native || exhausted !== undefined);
  const declaredEmpty = stage.mode === "not_applicable";
  const optionalDisabled = id === "metadata" && stage.mode === "disabled";
  const complete = stage.mode !== "unknown" && !invalid &&
    (requiredKnown || declaredEmpty || optionalDisabled);

  if (declaredEmpty || optionalDisabled) {
    stage.state = invalid ? "unknown" : "clear";
  } else if (stage.mode === "disabled" && hasWork(values)) {
    stage.state = "paused";
  } else if ((abandoned ?? 0) > 0 || (exhausted ?? 0) > 0) {
    stage.state = "attention";
  } else if ((pending ?? 0) > 0 || (retrying ?? 0) > 0) {
    stage.state = "updating";
  } else if (complete) {
    stage.state = "clear";
  }
  return { stage, complete };
}

function legacySource(body: Record<string, unknown>, id: StageId): unknown {
  if (id === "search_index") {
    return object(object(body.vector_store)?.backfill)?.upsert;
  }
  if (id === "file_projection") return body.native_file_projection;
  if (id === "content_preparation") return body.native_derived;
  return body.metadata_backfill;
}

function recognizedLegacySource(value: unknown): value is Record<string, unknown> {
  const source = object(value);
  return source !== undefined &&
    [...COUNTERS, "indexed", "status", "applied", "superseded", "deleted", "direct_grep"]
      .some((key) => Object.hasOwn(source, key));
}

function decodeLegacyStage(id: StageId, source: Record<string, unknown>): StageObservation {
  const definition = STAGES[id];
  const { values } = counters(source);
  // Exhausted is a Native retry-boundary observation, never a terminal total.
  if (!definition.native) delete values.exhausted;
  const historicalFailure = id === "content_preparation" && (values.abandoned ?? 0) > 0;
  const stage: StageObservation = {
    ...unknownStage(id, false),
    unit: definition.unit,
    // Only the v1 decoder can assign current_heads; raw totals include old revisions.
    scope: id === "content_preparation" ? "revision_intents" : definition.scope,
    ...values,
    historicalFailure,
  };
  if (id !== "content_preparation" &&
    ((values.abandoned ?? 0) > 0 || (values.exhausted ?? 0) > 0)) {
    stage.state = "attention";
  } else if ((values.pending ?? 0) > 0 || (values.retrying ?? 0) > 0 ||
    (values.exhausted ?? 0) > 0) {
    stage.state = "updating";
  }
  // All-zero legacy data cannot establish worker mode or a clear pipeline.
  return stage;
}

function workState(stages: StageObservation[]): WorkState {
  for (const state of ["attention", "paused", "updating", "unknown"] as const) {
    if (stages.some((stage) => stage.state === state)) return state;
  }
  return stages.length ? "clear" : "unknown";
}

/** Normalize only reader-scoped queue evidence; never infer retrieval health. */
export function normalizeIndexingResponse(
  body: unknown,
  vault: { id: string; name: string },
  now: number,
): VaultSearchObservation {
  const source = object(body);
  if (!source) throw new Error("Invalid vault search-status response");
  const envelope = object(source.search_update_status);
  const versionedStages = envelope?.version === 1 ? object(envelope.stages) : undefined;
  let stages: StageObservation[];
  let complete = false;
  if (envelope?.version === 1) {
    if (!versionedStages || !STAGE_IDS.some((id) => object(versionedStages[id]))) {
      throw new Error("Invalid version 1 vault search-status stages");
    }
    const decoded = STAGE_IDS.map((id) => decodeVersionedStage(id, versionedStages[id]));
    stages = decoded.map(({ stage }) => stage);
    complete = decoded.every((item) => item.complete) &&
      Object.keys(versionedStages).every((id) => Object.hasOwn(STAGES, id));
  } else {
    stages = STAGE_IDS.flatMap((id) => {
      const value = legacySource(source, id);
      return recognizedLegacySource(value) ? [decodeLegacyStage(id, value)] : [];
    });
    if (!stages.length) throw new Error("No recognized vault search-status stages");
  }

  const coreStages = stages.filter((stage) => stage.id !== "metadata");
  const core = workState(coreStages);
  const rawDerived = object(source.native_derived);
  const historicalFailureReported = (count(rawDerived?.abandoned) ?? 0) > 0 ||
    stages.some((stage) => stage.historicalFailure);
  return {
    vaultId: vault.id,
    vaultName: vault.name,
    observation: "fresh",
    coverage: complete ? "complete" : "partial",
    receivedAt: now,
    attemptedAt: now,
    stages,
    core,
    auxiliary: workState(stages.filter((stage) => stage.id === "metadata")),
    historicalFailureReported,
  };
}

function vaultCount(count: number): string {
  return `${count} ${count === 1 ? "vault" : "vaults"}`;
}

/** Sum only fresh search-index chunks; retries are already included in pending.
 * Other stages have different units and must never inflate the indexing badge.
 * Legacy backends expose this same queue without declaring a worker mode.
 */
export function pendingIndexingCount(
  observations: VaultSearchObservation[],
  verified: boolean,
): { pending: number; incomplete: boolean } {
  let pending = 0;
  let incomplete = !verified;
  const rows = new Map(observations.map(row => [row.vaultId, row]));
  for (const row of rows.values()) {
    const stage = row.stages.find(item => item.id === "search_index");
    if (row.observation !== "fresh" || !stage ||
      stage.unit !== "chunks" || stage.scope !== "stored_chunks" ||
      (stage.versioned && stage.mode !== "enabled") ||
      stage.pending === undefined || !Number.isSafeInteger(stage.pending) || stage.pending < 0 ||
      !Number.isSafeInteger(pending + stage.pending)) {
      incomplete = true;
      continue;
    }
    pending += stage.pending;
  }
  return { pending, incomplete };
}

/** Current affected totals use fresh core-work sets, never stage-counter sums. */
export function summarizeIndexing(
  observations: VaultSearchObservation[],
  scope: { verified: boolean; loading: boolean },
): IndexingSummary {
  const rows = [...new Map(observations.map((row) => [row.vaultId, row])).values()];
  const attention = new Set<string>();
  const updating = new Set<string>();
  const paused = new Set<string>();
  const unknown = new Set<string>();
  let checkedCount = 0;
  for (const row of rows) {
    if (row.observation !== "fresh") {
      unknown.add(row.vaultId);
      continue;
    }
    checkedCount += 1;
    const coreStages = row.stages.filter((stage) => stage.id !== "metadata");
    if (row.core === "attention" || coreStages.some((stage) => stage.state === "attention")) {
      attention.add(row.vaultId);
    }
    if (row.core === "paused" || coreStages.some((stage) => stage.state === "paused")) {
      paused.add(row.vaultId);
    }
    if (row.core === "updating" || coreStages.some((stage) =>
      stage.state === "updating" ||
      (stage.state === "attention" && (stage.pending ?? 0) > 0))) {
      updating.add(row.vaultId);
    }
    if (row.coverage !== "complete" || row.core === "unknown") unknown.add(row.vaultId);
  }
  // A background observation does not invalidate still-fresh evidence or emit
  // a new live announcement. Loading controls expose the request separately.
  const complete = scope.verified && unknown.size === 0;
  const affected = new Set([...attention, ...paused, ...updating]);
  const summary: IndexingSummary = {
    label: "Search status",
    description: "No search updates waiting in the reported queues.",
    tone: "neutral",
    attentionIds: [...attention],
    updatingIds: [...updating],
    pausedIds: [...paused],
    unknownIds: [...unknown],
    affectedIds: [...affected],
    complete,
    checkedCount,
    totalCount: rows.length,
  };
  const secondary: string[] = [];
  if (attention.size) {
    summary.label = `Needs attention · ${attention.size}`;
    summary.description = `Reported search updates need attention in ${vaultCount(attention.size)}.`;
    summary.tone = "warning";
    if (paused.size) secondary.push(`Updates are paused in ${vaultCount(paused.size)}.`);
    if (updating.size) secondary.push(`Queued work is reported in ${vaultCount(updating.size)}.`);
  } else if (paused.size) {
    summary.label = "Updates paused";
    summary.description = `Search updates are paused by configuration in ${vaultCount(paused.size)}.`;
    summary.tone = "info";
    if (updating.size) secondary.push(`Queued work is reported in ${vaultCount(updating.size)}.`);
  } else if (updating.size) {
    summary.label = `Updating · ${vaultCount(updating.size)}`;
    summary.description = `Search-update queues report work in ${vaultCount(updating.size)}.`;
    summary.tone = "info";
  } else if ((scope.loading && !scope.verified) || (rows.length > 0 && rows.every((row) => row.observation === "checking"))) {
    summary.label = "Checking…";
    summary.description = "Checking search-update status in accessible vaults.";
  } else if (!scope.verified || (rows.length > 0 && checkedCount === 0)) {
    summary.label = "Status unavailable";
    summary.description = rows.length > 0 && rows.every((row) => row.coverage === "unsupported")
      ? "This server does not provide detailed status."
      : "Current search-update status could not be verified.";
  } else if (!complete) {
    summary.label = "Limited status";
    summary.description = "No pending updates reported, but some status could not be verified.";
  } else if (rows.some((row) => row.observation === "fresh" && row.auxiliary === "attention")) {
    summary.label = "Details need attention";
    summary.description = "Automatic metadata needs attention; no core search updates are reported waiting.";
    summary.tone = "warning";
  } else if (rows.some((row) => row.observation === "fresh" && row.auxiliary === "updating")) {
    summary.label = "Details updating";
    summary.description = "Automatic metadata is updating; no core search updates are reported waiting.";
    summary.tone = "info";
  } else if (rows.length === 0) {
    summary.description = "No vaults to check.";
  }
  if (unknown.size && affected.size) {
    secondary.push(`Status could not be fully verified for ${vaultCount(unknown.size)}.`);
  }
  if (!scope.verified && affected.size) secondary.push("Accessible vault scope could not be verified.");
  summary.description = [summary.description, ...secondary].join(" ");
  return summary;
}
