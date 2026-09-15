import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { HeaderIndexingStatus } from "../header-indexing-status";
import { VaultIndexingStatus } from "../pending-indexing-badge";
import { normalizeIndexingResponse, pendingIndexingCount, summarizeIndexing, type VaultSearchObservation } from "@/lib/indexing-status";
import type { useSearchStatus } from "@/hooks/use-search-status";

let context: ReturnType<typeof useSearchStatus>;
vi.mock("@/hooks/use-search-status", () => ({
  useSearchStatus: () => context,
  useOptionalSearchStatus: () => context,
}));

function row(name: string, pending: number): VaultSearchObservation {
  return normalizeIndexingResponse({ search_update_status: { version: 1, stages: {
    search_index: { mode: "enabled", unit: "chunks", scope: "stored_chunks", pending, retrying: 0, abandoned: 0 },
    metadata: { mode: "enabled", unit: "documents", scope: "external_git_documents", pending: 20, retrying: 2, abandoned: 1 },
  } } }, { id: name, name }, Date.now());
}

beforeEach(() => {
  context = { summary: summarizeIndexing([], { verified: true, loading: false }), observations: [],
    verified: true, loading: false, refresh: vi.fn() };
});
afterEach(cleanup);

describe("Compact indexing counts", () => {
  it("counts chunks across distinct Vaults, not Vaults or other queue units", () => {
    const a = row("alpha", 3);
    a.stages[0].retrying = 2;
    a.stages[0].abandoned = 5;
    a.stages[1].pending = 40;
    const b = row("beta", 7);
    expect(pendingIndexingCount([a, a, b], true)).toEqual({ pending: 10, incomplete: false });
    context.observations = [a, b];
    render(<HeaderIndexingStatus />);
    expect(screen.getByText("10 indexing")).toBeTruthy();
    expect(screen.getByRole("status")).toHaveTextContent("10 chunks waiting for search indexing across accessible vaults.");
    expect(screen.queryByRole("button")).toBeNull();
    expect(screen.queryByText(/Search status|updating.*vaults/i)).toBeNull();
  });

  it("renders no badge for idle, initial, failed-only, or metadata-only work", () => {
    const view = render(<HeaderIndexingStatus />);
    expect(screen.getByRole("status")).toBeEmptyDOMElement();
    context.observations = [row("alpha", 0)];
    context.observations[0].stages[0].abandoned = 4;
    view.rerender(<HeaderIndexingStatus />);
    expect(screen.getByRole("status")).toBeEmptyDOMElement();
    expect(screen.queryByText(/caught up|healthy|attention/i)).toBeNull();
  });

  it("uses only the selected Vault's fresh indexing count in Overview", () => {
    context.observations = [row("alpha", 3), row("beta", 17)];
    const view = render(<VaultIndexingStatus vaultName="alpha" />);
    expect(screen.getByText("3 indexing")).toBeTruthy();
    expect(screen.getByText("3 chunks waiting for search indexing in alpha.")).toBeTruthy();
    expect(screen.queryByRole("status")).toBeNull();
    view.rerender(<VaultIndexingStatus vaultName="beta" />);
    expect(screen.getByText("17 indexing")).toBeTruthy();
    view.rerender(<VaultIndexingStatus vaultName="missing" />);
    expect(view.container).toBeEmptyDOMElement();
  });

  it("omits stale/missing/paused counts and qualifies the known subtotal", () => {
    const stale = { ...row("stale", 99), observation: "stale" as const };
    const missing = row("missing", 4); missing.stages[0].pending = undefined;
    const paused = row("paused", 9); paused.stages[0].mode = "disabled";
    context.observations = [row("fresh", 3), stale, missing, paused];
    render(<HeaderIndexingStatus />);
    expect(screen.getByText("3+ indexing")).toBeTruthy();
    expect(screen.getByRole("status")).toHaveTextContent("At least 3 chunks");
    expect(screen.getByRole("status")).toHaveTextContent("Some indexing counts are unavailable.");
    expect(pendingIndexingCount([stale, missing, paused], true)).toEqual({ pending: 0, incomplete: true });
  });

  it("retains the legacy pending count without adding retries or subtracting failures", () => {
    const legacy = normalizeIndexingResponse({
      vector_store: { backfill: { upsert: { pending: 12, retrying: 3, abandoned: 5 } } },
      metadata_backfill: { pending: 300 },
    }, { id: "legacy", name: "legacy" }, Date.now());
    expect(pendingIndexingCount([legacy], true)).toEqual({ pending: 12, incomplete: false });
    expect(pendingIndexingCount([legacy], false)).toEqual({ pending: 12, incomplete: true });
  });

  it("rejects wrong units, unverified configuration, invalid numbers and unknown scopes", () => {
    for (const patch of [
      { unit: "documents" }, { scope: "unknown" }, { mode: "unknown" as const },
      { pending: -1 }, { pending: NaN }, { pending: 1.5 },
    ]) {
      const invalid = row("invalid", 2);
      Object.assign(invalid.stages[0], patch);
      expect(pendingIndexingCount([invalid], true)).toEqual({ pending: 0, incomplete: true });
    }
  });

  it("quietly disappears on completion without adding a success control", () => {
    context.observations = [row("alpha", 21)];
    const view = render(<HeaderIndexingStatus />);
    expect(screen.getByText("21 indexing")).toBeTruthy();
    context.observations = [row("alpha", 0)];
    view.rerender(<HeaderIndexingStatus />);
    expect(screen.getByRole("status")).toBeEmptyDOMElement();
    expect(screen.queryByRole("button")).toBeNull();
  });
});
