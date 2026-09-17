import { describe, expect, it } from "vitest";
import {
  normalizeIndexingResponse,
  summarizeIndexing,
  type StageId,
  type VaultSearchObservation,
} from "../indexing-status";

const NOW = 1_800_000_000_000;
const VAULT = { id: "vault-a", name: "Research" };
const VERIFIED = { verified: true, loading: false };

function payload(overrides: Partial<Record<StageId, Record<string, unknown>>> = {}) {
  const stages: Record<StageId, Record<string, unknown>> = {
    search_index: {
      mode: "enabled", unit: "chunks", scope: "stored_chunks",
      pending: 0, retrying: 0, abandoned: 0,
    },
    file_projection: {
      mode: "enabled", unit: "file_updates", scope: "latest_file_intents",
      pending: 0, retrying: 0, exhausted: 0, abandoned: 0,
    },
    content_preparation: {
      mode: "enabled", unit: "revision_updates", scope: "current_heads",
      pending: 0, retrying: 0, exhausted: 0, abandoned: 0,
    },
    metadata: {
      mode: "enabled", unit: "documents", scope: "external_git_documents",
      pending: 0, retrying: 0, abandoned: 0,
    },
  };
  for (const id of Object.keys(overrides) as StageId[]) Object.assign(stages[id], overrides[id]);
  return { search_update_status: { version: 1, observed_at: "2026-09-14T03:00:00Z", stages } };
}

function normalize(body: unknown = payload(), id = VAULT.id) {
  return normalizeIndexingResponse(body, { ...VAULT, id }, NOW);
}

function stage(row: VaultSearchObservation, id: StageId) {
  return row.stages.find((item) => item.id === id)!;
}

function legacy() {
  return {
    vector_store: { backfill: { upsert: { pending: 0, retrying: 0, abandoned: 0, indexed: 800 } } },
    native_file_projection: { status: "ok", pending: 0, retrying: 0, exhausted: 0, abandoned: 0 },
    native_derived: { status: "ok", pending: 0, retrying: 0, exhausted: 0, abandoned: 0, applied: 900 },
    metadata_backfill: { pending: 0, retrying: 0, abandoned: 0 },
  };
}

describe("normalizeIndexingResponse", () => {
  it("keeps four stages, their distinct units and scopes, and client receipt time", () => {
    const row = normalize();
    expect(row).toMatchObject({
      vaultId: VAULT.id, vaultName: VAULT.name, observation: "fresh", coverage: "complete",
      core: "clear", auxiliary: "clear", receivedAt: NOW, attemptedAt: NOW,
      historicalFailureReported: false,
    });
    expect(row.stages.map(({ id, unit, scope, versioned }) => ({ id, unit, scope, versioned })))
      .toEqual([
        { id: "search_index", unit: "chunks", scope: "stored_chunks", versioned: true },
        { id: "file_projection", unit: "file_updates", scope: "latest_file_intents", versioned: true },
        { id: "content_preparation", unit: "revision_updates", scope: "current_heads", versioned: true },
        { id: "metadata", unit: "documents", scope: "external_git_documents", versioned: true },
      ]);
  });

  it("never adds retrying to pending or subtracts abandoned", () => {
    const row = normalize(payload({ search_index: { pending: 3, retrying: 2, abandoned: 4 } }));
    expect(stage(row, "search_index")).toMatchObject({
      pending: 3, retrying: 2, abandoned: 4, state: "attention",
    });
    expect(row.coverage).toBe("complete");
  });

  it("retains exhausted as outstanding retry-boundary work distinct from terminal abandonment", () => {
    const row = normalize(payload({ content_preparation: { pending: 2, retrying: 1, exhausted: 1 } }));
    expect(stage(row, "content_preparation")).toMatchObject({
      pending: 2, retrying: 1, exhausted: 1, abandoned: 0, state: "attention",
    });
  });

  it.each([undefined, null, -1, 0.1, Number.NaN, Infinity, "3", true, {}, [], Number.MAX_SAFE_INTEGER + 1])(
    "omits invalid or unavailable required pending value %j without inventing zero",
    (value) => {
      const row = normalize(payload({ search_index: { pending: value } }));
      expect(stage(row, "search_index")).not.toHaveProperty("pending");
      expect(stage(row, "search_index").state).toBe("unknown");
      expect(row.coverage).toBe("partial");
    },
  );

  it("accepts the largest safe integer without rounding it", () => {
    const row = normalize(payload({ search_index: { pending: Number.MAX_SAFE_INTEGER } }));
    expect(stage(row, "search_index").pending).toBe(Number.MAX_SAFE_INTEGER);
    expect(row.coverage).toBe("complete");
  });

  it.each([
    { pending: 2, retrying: 3, exhausted: 0 },
    { pending: 2, retrying: 0, exhausted: 3 },
    { pending: 2, retrying: 2, exhausted: 1 },
    { pending: Number.MAX_SAFE_INTEGER, retrying: Number.MAX_SAFE_INTEGER, exhausted: 1 },
  ])("rejects contradictory Native subsets independently: %j", (counts) => {
    const row = normalize(payload({ file_projection: counts, search_index: { pending: 5 } }));
    expect(stage(row, "file_projection")).toMatchObject({ state: "unknown" });
    expect(stage(row, "file_projection")).not.toHaveProperty("pending");
    expect(stage(row, "search_index")).toMatchObject({ state: "updating", pending: 5 });
    expect(row).toMatchObject({ coverage: "partial", core: "updating" });
  });

  it("rejects the retry subset invariant for search chunks too", () => {
    const row = normalize(payload({ search_index: { pending: 0, retrying: 1 } }));
    expect(stage(row, "search_index").state).toBe("unknown");
    expect(row.coverage).toBe("partial");
  });

  it.each(["search_index", "metadata"] as const)("rejects exhausted on non-Native %s", (id) => {
    const row = normalize(payload({ [id]: { exhausted: 0 } }));
    expect(stage(row, id).state).toBe("unknown");
    expect(row.coverage).toBe("partial");
  });

  it.each([
    ["search_index", "unit", "documents"],
    ["search_index", "scope", "current_heads"],
    ["file_projection", "unit", "chunks"],
    ["file_projection", "scope", "revision_intents"],
    ["content_preparation", "unit", "documents"],
    ["content_preparation", "scope", "revision_intents"],
    ["metadata", "unit", "file_updates"],
    ["metadata", "scope", "stored_chunks"],
  ] as const)("does not use mismatched %s %s %s as queue evidence", (id, key, value) => {
    const row = normalize(payload({ [id]: { [key]: value, pending: 9 } }));
    expect(stage(row, id).state).toBe("unknown");
    expect(stage(row, id)).not.toHaveProperty("pending");
    expect(row.coverage).toBe("partial");
  });

  it("shows known work while required counters are unobserved", () => {
    const row = normalize(payload({ content_preparation: { pending: 3, abandoned: null, exhausted: null } }));
    expect(row).toMatchObject({ core: "updating", coverage: "partial" });
    expect(stage(row, "content_preparation")).toMatchObject({ pending: 3, state: "updating" });
    expect(stage(row, "content_preparation")).not.toHaveProperty("abandoned");
  });

  it("requires Native exhausted observations before declaring an empty stage clear", () => {
    const row = normalize(payload({ file_projection: { exhausted: undefined } }));
    expect(row.coverage).toBe("partial");
    expect(stage(row, "file_projection").state).toBe("unknown");
  });

  it("allows an omitted optional retrying counter", () => {
    const row = normalize(payload({ file_projection: { retrying: undefined } }));
    expect(row.coverage).toBe("complete");
    expect(stage(row, "file_projection")).not.toHaveProperty("retrying");
  });

  it.each(["unknown", "sleeping", null, undefined])("does not infer liveness from mode %j", (mode) => {
    const row = normalize(payload({ search_index: { mode, pending: 2 } }));
    expect(stage(row, "search_index")).toMatchObject({ mode: "unknown", state: "updating" });
    expect(row.coverage).toBe("partial");
  });

  it.each([
    { pending: 3 },
    { abandoned: 2 },
    { pending: 2, exhausted: 1 },
  ])("reports a disabled core consumer with retained work as paused: %j", (counts) => {
    const row = normalize(payload({ file_projection: { ...counts, mode: "disabled" } }));
    expect(stage(row, "file_projection").state).toBe("paused");
    expect(row.core).toBe("paused");
  });

  it("keeps disabled optional metadata informational even with retained failures", () => {
    const row = normalize(payload({ metadata: { mode: "disabled", pending: 4, abandoned: 8 } }));
    expect(row).toMatchObject({ core: "clear", auxiliary: "clear", coverage: "complete" });
    expect(stage(row, "metadata")).toMatchObject({ pending: 4, abandoned: 8, mode: "disabled" });
  });

  it.each(["disabled", "not_applicable"])("allows explicitly %s metadata with unobserved counters", (mode) => {
    const row = normalize(payload({ metadata: { mode, pending: null, retrying: null, abandoned: null } }));
    expect(row).toMatchObject({ coverage: "complete", auxiliary: "clear" });
    expect(stage(row, "metadata")).not.toHaveProperty("pending");
  });

  it("accepts inapplicable Native stages with no counter read", () => {
    const row = normalize(payload({ file_projection: {
      mode: "not_applicable", pending: undefined, retrying: undefined, exhausted: undefined, abandoned: undefined,
    } }));
    expect(row).toMatchObject({ coverage: "complete", core: "clear" });
  });

  it("rejects inapplicable stages with retained work", () => {
    const row = normalize(payload({ file_projection: { mode: "not_applicable", pending: 1 } }));
    expect(stage(row, "file_projection").state).toBe("unknown");
    expect(row.coverage).toBe("partial");
  });

  it("limits coverage when a new stage extends the catalog", () => {
    const body = payload();
    Object.assign(body.search_update_status.stages, { new_stage: { pending: 0 } });
    expect(normalize(body)).toMatchObject({ coverage: "partial", core: "clear" });
  });

  it("retains an absent required stage as unknown", () => {
    const body = payload();
    Reflect.deleteProperty(body.search_update_status.stages, "content_preparation");
    const row = normalize(body);
    expect(row).toMatchObject({ coverage: "partial", core: "unknown" });
    expect(stage(row, "content_preparation")).toMatchObject({ versioned: true, scope: "unknown" });
  });

  it("does not fill a failed current-head read from the historical ledger", () => {
    const body = payload({ content_preparation: { pending: null, abandoned: null, exhausted: null } });
    const row = normalize({ ...body, native_derived: { pending: 0, abandoned: 5, exhausted: 0 } });
    expect(row).toMatchObject({ core: "unknown", coverage: "partial", historicalFailureReported: true });
    expect(stage(row, "content_preparation")).toMatchObject({ scope: "current_heads", state: "unknown" });
    expect(stage(row, "content_preparation")).not.toHaveProperty("abandoned");
  });

  it("allows a successful current Head to clear while retaining recorded old failures", () => {
    const row = normalize({ ...payload(), native_derived: { abandoned: 5 } });
    expect(row).toMatchObject({ core: "clear", coverage: "complete", historicalFailureReported: true });
    expect(stage(row, "content_preparation")).toMatchObject({ abandoned: 0, historicalFailure: false });
  });

  it("normalizes all recognizable legacy stages conservatively", () => {
    const row = normalize(legacy());
    expect(row).toMatchObject({ coverage: "partial", core: "unknown" });
    expect(row.stages).toHaveLength(4);
    expect(row.stages.every((item) => item.mode === "unknown" && !item.versioned)).toBe(true);
    expect(stage(row, "content_preparation").scope).toBe("revision_intents");
    expect(row.stages.some((item) => item.scope === "current_heads")).toBe(false);
  });

  it("retains old chunk/metadata responses without inventing absent Native support", () => {
    const row = normalize({
      vector_store: { backfill: { upsert: { pending: 2, abandoned: 4, indexed: 100 } } },
      metadata_backfill: { pending: 3 },
    });
    expect(row.stages.map((item) => item.id)).toEqual(["search_index", "metadata"]);
    expect(stage(row, "search_index")).toMatchObject({ pending: 2, abandoned: 4 });
    expect(row.coverage).toBe("partial");
    expect(row.auxiliary).toBe("updating");
  });

  it("does not apply v1 subset arithmetic to unknown legacy semantics", () => {
    const row = normalize({ native_file_projection: { pending: 2, retrying: 4, exhausted: 3, abandoned: 10 } });
    expect(stage(row, "file_projection")).toMatchObject({ pending: 2, retrying: 4, exhausted: 3, abandoned: 10 });
    expect(row.coverage).toBe("partial");
  });

  it("treats raw Native historical abandonment as recorded-only", () => {
    const row = normalize({ native_derived: { pending: 0, exhausted: 0, abandoned: 9, status: "degraded" } });
    expect(row).toMatchObject({ core: "unknown", historicalFailureReported: true });
    expect(stage(row, "content_preparation")).toMatchObject({ historicalFailure: true, abandoned: 9 });
    expect(summarizeIndexing([row], VERIFIED).attentionIds).toEqual([]);
  });

  it("does not promote raw derived exhausted revisions to confirmed current-head failure", () => {
    const row = normalize({ native_derived: { pending: 1, exhausted: 1, abandoned: 0 } });
    expect(stage(row, "content_preparation")).toMatchObject({
      pending: 1, exhausted: 1, abandoned: 0, state: "updating", scope: "revision_intents",
    });
    expect(summarizeIndexing([row], VERIFIED).attentionIds).toEqual([]);
  });

  it("does not interpret legacy status text as fresh queue evidence", () => {
    const row = normalize({ native_file_projection: { status: "degraded" } });
    expect(row).toMatchObject({ core: "unknown", coverage: "partial" });
  });

  it("ignores unknown future envelope semantics and uses recognizable raw data", () => {
    const row = normalize({
      ...legacy(),
      search_update_status: { version: 2, stages: payload({ content_preparation: { abandoned: 50 } }).search_update_status.stages },
    });
    expect(row).toMatchObject({ core: "unknown", coverage: "partial" });
    expect(stage(row, "content_preparation")).toMatchObject({ abandoned: 0, versioned: false, scope: "revision_intents" });
  });

  it.each([
    null, undefined, [], "<html>Sign in</html>", {}, { status: "ok" }, { vector_store: {} },
    { search_update_status: { version: 2, stages: {} } },
    { search_update_status: { version: 1, stages: {} } },
    { search_update_status: { version: 1, stages: [] } },
    { search_update_status: { version: 1, stages: { future: {} } } },
  ])("throws for an unrecognizable response rather than publishing idle: %j", (body) => {
    expect(() => normalizeIndexingResponse(body, VAULT, NOW)).toThrow(Error);
  });

  it("rejects a malformed known-version envelope even if raw fallback fields exist", () => {
    expect(() => normalize({ ...legacy(), search_update_status: { version: 1, stages: "bad" } })).toThrow(Error);
  });

  it("does not mutate a source response", () => {
    const body = payload({ search_index: { pending: 3, retrying: 2 } });
    const before = structuredClone(body);
    normalize(body);
    expect(body).toEqual(before);
  });
});

describe("summarizeIndexing", () => {
  it("distinguishes a verified empty directory from scope failure and initial checking", () => {
    expect(summarizeIndexing([], VERIFIED)).toMatchObject({
      label: "Search status", description: "No vaults to check.", complete: true, totalCount: 0,
    });
    expect(summarizeIndexing([], { verified: false, loading: false })).toMatchObject({
      label: "Status unavailable", complete: false,
    });
    expect(summarizeIndexing([], { verified: false, loading: true })).toMatchObject({
      label: "Checking…", complete: false,
    });
  });

  it("describes clear reported queues without claiming search health or full indexing", () => {
    const result = summarizeIndexing([normalize()], VERIFIED);
    expect(result).toMatchObject({
      label: "Search status", description: "No search updates waiting in the reported queues.",
      tone: "neutral", complete: true, totalCount: 1, checkedCount: 1, affectedIds: [],
    });
    expect(result.description).not.toMatch(/healthy|all documents|100%|fully indexed|up to date/i);
  });

  it("uses overlapping Vault sets across mixed stages and units", () => {
    const row = normalize(payload({
      search_index: { pending: 500, abandoned: 4 },
      file_projection: { mode: "disabled", pending: 10 },
      content_preparation: { pending: 30 },
    }));
    const result = summarizeIndexing([row], VERIFIED);
    expect(result).toMatchObject({
      attentionIds: [VAULT.id], updatingIds: [VAULT.id], pausedIds: [VAULT.id],
      affectedIds: [VAULT.id], label: "Needs attention · 1", totalCount: 1,
    });
    expect(result.description).toContain("paused");
    expect(result.description).toContain("Queued work");
    expect(result.description).not.toContain("540");
  });

  it("deduplicates repeated Vault membership rather than double-counting", () => {
    const row = normalize(payload({ search_index: { pending: 4 } }));
    expect(summarizeIndexing([row, row], VERIFIED)).toMatchObject({
      totalCount: 1, checkedCount: 1, updatingIds: [VAULT.id], affectedIds: [VAULT.id],
    });
  });

  it("preserves independent affected Vaults and gives attention display priority", () => {
    const a = normalize(payload({ search_index: { abandoned: 1 } }), "a");
    const b = normalize(payload({ file_projection: { pending: 3 } }), "b");
    const result = summarizeIndexing([a, b], VERIFIED);
    expect(result).toMatchObject({ attentionIds: ["a"], updatingIds: ["b"], affectedIds: ["a", "b"] });
    expect(result.label).toBe("Needs attention · 1");
    expect(result.description).toContain("Queued work is reported in 1 vault.");
  });

  it("gives paused core work priority over other updating core stages", () => {
    const a = normalize(payload({ search_index: { mode: "disabled", pending: 1 } }), "a");
    const b = normalize(payload({ file_projection: { pending: 3 } }), "b");
    expect(summarizeIndexing([a, b], VERIFIED)).toMatchObject({
      label: "Updates paused", pausedIds: ["a"], updatingIds: ["b"], affectedIds: ["a", "b"],
    });
  });

  it.each(["stale", "unavailable", "checking"] as const)("excludes %s historical work from every current affected set", (observation) => {
    const row = { ...normalize(payload({ search_index: { pending: 2, abandoned: 3 } })), observation };
    const result = summarizeIndexing([row], VERIFIED);
    expect(result).toMatchObject({
      attentionIds: [], updatingIds: [], pausedIds: [], affectedIds: [], unknownIds: [VAULT.id],
      checkedCount: 0, totalCount: 1, complete: false,
    });
    expect(row.core).toBe("attention");
    expect(stage(row, "search_index").abandoned).toBe(3);
  });

  it("retains positive known work and qualifies partial coverage nearby", () => {
    const row = normalize(payload({ search_index: { pending: 2 }, file_projection: { abandoned: undefined } }));
    const result = summarizeIndexing([row], VERIFIED);
    expect(result).toMatchObject({
      label: "Updating · 1 vault", updatingIds: [VAULT.id], unknownIds: [VAULT.id], complete: false,
    });
    expect(result.description).toContain("could not be fully verified");
  });

  it("does not let stale attention override fresh work elsewhere", () => {
    const a: VaultSearchObservation = {
      ...normalize(payload({ search_index: { abandoned: 5 } }), "a"), observation: "stale",
    };
    const b = normalize(payload({ file_projection: { pending: 2 } }), "b");
    expect(summarizeIndexing([a, b], VERIFIED)).toMatchObject({
      label: "Updating · 1 vault", attentionIds: [], affectedIds: ["b"], unknownIds: ["a"], complete: false,
    });
  });

  it("keeps recorded historical failures out of a current Home attention set", () => {
    const row = normalize({ ...payload(), native_derived: { abandoned: 9 } });
    expect(summarizeIndexing([row], VERIFIED)).toMatchObject({
      label: "Search status", attentionIds: [], affectedIds: [], complete: true,
    });
  });

  it.each([
    [{ pending: 2 }, "Details updating", "info"],
    [{ abandoned: 2 }, "Details need attention", "warning"],
  ] as const)("keeps optional metadata outside core affected counts: %j", (counts, label, tone) => {
    const result = summarizeIndexing([normalize(payload({ metadata: counts }))], VERIFIED);
    expect(result).toMatchObject({ label, tone, affectedIds: [], updatingIds: [], attentionIds: [], complete: true });
    expect(result.description).toContain("Automatic metadata");
  });

  it("keeps disabled metadata informational without warning on retained abandoned totals", () => {
    const result = summarizeIndexing([normalize(payload({ metadata: { mode: "disabled", abandoned: 7 } }))], VERIFIED);
    expect(result).toMatchObject({ label: "Search status", tone: "neutral", affectedIds: [] });
  });

  it("prioritizes incomplete core coverage ahead of optional metadata work", () => {
    const row = normalize(payload({ search_index: { abandoned: null }, metadata: { pending: 2 } }));
    expect(summarizeIndexing([row], VERIFIED).label).toBe("Limited status");
  });

  it("qualifies all-zero legacy responses as limited verification", () => {
    expect(summarizeIndexing([normalize(legacy())], VERIFIED)).toMatchObject({
      label: "Limited status", complete: false,
      description: "No pending updates reported, but some status could not be verified.",
    });
  });

  it("loses complete scope without hiding still-observed work", () => {
    const result = summarizeIndexing([normalize(payload({ search_index: { abandoned: 1 } }))], {
      verified: false, loading: false,
    });
    expect(result).toMatchObject({ label: "Needs attention · 1", complete: false, attentionIds: [VAULT.id] });
    expect(result.description).toContain("scope could not be verified");
  });

  it("does not change live copy during a background refresh of fresh observations", () => {
    for (const rows of [[], [normalize()], [normalize(payload({ search_index: { pending: 3 } }))]]) {
      expect(summarizeIndexing(rows, { verified: true, loading: true }))
        .toEqual(summarizeIndexing(rows, VERIFIED));
    }
  });

  it("uses unsupported copy only for confirmed unsupported endpoints", () => {
    const row: VaultSearchObservation = {
      ...normalize(), observation: "unavailable", coverage: "unsupported",
    };
    expect(summarizeIndexing([row], VERIFIED)).toMatchObject({
      label: "Status unavailable", description: "This server does not provide detailed status.", complete: false,
    });
    expect(summarizeIndexing([{ ...row, coverage: "partial" }], VERIFIED).description)
      .not.toContain("does not provide");
  });
});
