import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";
import { SearchStatusStages } from "../search-status-stages";
import { normalizeIndexingResponse } from "@/lib/indexing-status";

afterEach(cleanup);
describe("Settings processing details", () => {
  it("keeps stage units and retry subsets separate", () => {
    const row = normalizeIndexingResponse({
      vector_store: { backfill: { upsert: { pending: 3, retrying: 2, abandoned: 0 } } },
      metadata_backfill: { pending: 2, retrying: 0 },
    }, { id: "one", name: "one" }, Date.now());
    render(<SearchStatusStages observation={row} />);
    expect(screen.getByText("3 chunks pending · 2 retrying")).toBeTruthy();
    expect(screen.getByText("2 documents pending")).toBeTruthy();
    expect(screen.queryByText(/5 (documents|chunks)/)).toBeNull();
  });
  it("does not present historical failures or unavailable counts as current work", () => {
    const row = normalizeIndexingResponse({ native_derived: { abandoned: 3 } },
      { id: "one", name: "one" }, Date.now());
    row.observation = "stale";
    render(<SearchStatusStages observation={row} />);
    expect(screen.getByText("Pending count unavailable")).toBeTruthy();
    expect(screen.getByText(/Recorded processing failures: 3. Current impact is not available./)).toBeTruthy();
    expect(screen.getByText(/last-known observations/)).toBeTruthy();
  });
  it("renders an explicit unknown state before observation", () => {
    render(<SearchStatusStages />);
    expect(screen.getByText(/has not been verified/)).toBeTruthy();
  });
});
