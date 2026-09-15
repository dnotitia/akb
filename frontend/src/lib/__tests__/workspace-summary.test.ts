import { afterEach, describe, expect, it, vi } from "vitest";
import { authenticatedFetch } from "@/lib/api";
import { getWorkspaceSummary, parseWorkspaceSummary, WorkspaceSummaryAccessDenied, WorkspaceSummaryUnavailable } from "../workspace-summary";

vi.mock("@/lib/api", () => ({ authenticatedFetch: vi.fn() }));
afterEach(() => vi.clearAllMocks());
const base = { version: 1, scope: "accessible", observed_at: "2026-09-14T01:00:00Z" };

describe("workspace summary contract", () => {
  it("retains exact counts, including zero, and omits unrelated metadata", () => {
    expect(parseWorkspaceSummary({ ...base, vault_count: 8, document_count: 0, table_count: 12, file_count: 86, private: "ignored" }))
      .toEqual({ ...base, vault_count: 8, document_count: 0, table_count: 12, file_count: 86 });
  });
  it.each([null, undefined, "0", -1, 1.5, Infinity, NaN, Number.MAX_SAFE_INTEGER + 1])("does not coerce invalid counts (%s) into zero", value => {
    expect(parseWorkspaceSummary({ ...base, vault_count: 4, document_count: value })).toEqual({ ...base, vault_count: 4 });
  });
  it.each([null, [], {}, { ...base, version: 2 }, { ...base, scope: "instance" }, { ...base, observed_at: "invalid" }])("rejects unverifiable envelopes", value => {
    expect(() => parseWorkspaceSummary(value)).toThrow(WorkspaceSummaryUnavailable);
  });
  it.each([404, 405, 501])("supports a legacy server without inventing totals (%i)", async status => {
    vi.mocked(authenticatedFetch).mockResolvedValue(new Response("", { status }));
    await expect(getWorkspaceSummary()).rejects.toBeInstanceOf(WorkspaceSummaryUnavailable);
  });
  it("distinguishes denied scope from unsupported counts", async () => {
    vi.mocked(authenticatedFetch).mockResolvedValue(new Response("", { status: 403 }));
    await expect(getWorkspaceSummary()).rejects.toBeInstanceOf(WorkspaceSummaryAccessDenied);
  });
  it("uses cancellable authenticated count-only transport with no persistent HTTP cache", async () => {
    vi.mocked(authenticatedFetch).mockResolvedValue(new Response(JSON.stringify(base)));
    const controller = new AbortController();
    await expect(getWorkspaceSummary(controller.signal)).resolves.toEqual(base);
    expect(authenticatedFetch).toHaveBeenCalledTimes(1);
    expect(authenticatedFetch).toHaveBeenCalledWith("/api/v1/my/workspace-summary", expect.objectContaining({ cache: "no-store" }));
    const signal = vi.mocked(authenticatedFetch).mock.calls[0][1]?.signal;
    controller.abort();
    expect(signal?.aborted).toBe(true);
  });
});
