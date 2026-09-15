import { authenticatedFetch } from "@/lib/api";

export const WORKSPACE_COUNT_FIELDS = ["vault_count", "document_count", "table_count", "file_count"] as const;
export type WorkspaceCountField = typeof WORKSPACE_COUNT_FIELDS[number];
export type WorkspaceSummary = Partial<Record<WorkspaceCountField, number>> & {
  version: 1;
  scope: "accessible";
  observed_at: string;
};

export class WorkspaceSummaryUnavailable extends Error {}
export class WorkspaceSummaryAccessDenied extends Error {}

export function validCount(value: unknown): value is number {
  return typeof value === "number" && Number.isSafeInteger(value) && value >= 0;
}

export function parseWorkspaceSummary(value: unknown): WorkspaceSummary {
  if (!value || typeof value !== "object") throw new WorkspaceSummaryUnavailable();
  const data = value as Record<string, unknown>;
  if (data.version !== 1 || data.scope !== "accessible" || typeof data.observed_at !== "string"
      || !Number.isFinite(Date.parse(data.observed_at))) {
    throw new WorkspaceSummaryUnavailable();
  }
  const result: WorkspaceSummary = { version: 1, scope: "accessible", observed_at: data.observed_at };
  for (const field of WORKSPACE_COUNT_FIELDS) {
    // Missing/invalid fields are unknown, never synthetic zeros.
    if (validCount(data[field])) result[field] = data[field];
  }
  return result;
}

export async function getWorkspaceSummary(signal?: AbortSignal): Promise<WorkspaceSummary> {
  const timeout = AbortSignal.timeout(8_000);
  const response = await authenticatedFetch("/api/v1/my/workspace-summary", {
    signal: signal ? AbortSignal.any([signal, timeout]) : timeout,
    cache: "no-store",
  });
  if (response.status === 403) throw new WorkspaceSummaryAccessDenied();
  if ([404, 405, 501].includes(response.status)) throw new WorkspaceSummaryUnavailable();
  if (!response.ok) throw new Error("Workspace totals could not be loaded.");
  return parseWorkspaceSummary(await response.json());
}
