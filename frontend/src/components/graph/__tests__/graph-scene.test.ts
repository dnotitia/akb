import { describe, expect, it } from "vitest";
import { graphCoverage } from "../graph-scene";

describe("graphCoverage", () => {
  it("never compares combined resources to connected-only totals", () => {
    const coverage = graphCoverage({ nodesTotal: 300, returned: 200, edgesTotal: 600, truncated: true, orphanReturned: 500, orphanTruncated: false });
    expect(coverage.label).toBe("Partial map");
    expect(coverage.detail).toContain("200 of 300 connected resources");
    expect(coverage.detail).toContain("500 unconnected resources loaded");
    expect(coverage.detail).not.toContain("700 of 300");
  });
  it("treats legacy and neighborhood completeness as unknown", () => {
    const coverage = graphCoverage(undefined);
    expect(coverage.label).toBe("Loaded map");
    expect(coverage.detail).toContain("Connected-resource coverage is not supplied by this server");
    expect(coverage.detail).toContain("Unconnected-resource coverage is unknown");
    expect(graphCoverage({ nodesTotal: 20, returned: 20, edgesTotal: 12, truncated: false }).label).toBe("Loaded map");
  });
  it("marks the map partial when only the unconnected resources were limited", () => {
    const coverage = graphCoverage({ nodesTotal: 3, returned: 3, truncated: false, orphanReturned: 500, orphanTruncated: true });
    expect(coverage.label).toBe("Partial map");
    expect(coverage.detail).toContain("3 of 3 connected resources loaded");
    expect(coverage.detail).toContain("500 unconnected resources loaded (limited)");
  });
  it("preserves explicit zero counts without converting them to unknown coverage", () => {
    const coverage = graphCoverage({ nodesTotal: 0, returned: 0, truncated: false, orphanReturned: 0, orphanTruncated: false });
    expect(coverage.detail).toContain("0 of 0 connected resources loaded");
    expect(coverage.detail).toContain("0 unconnected resources loaded");
  });
});
