import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { isFresh, recencyTone, timeAgo } from "@/lib/utils";

// timeAgo / isFresh are time-relative, so pin "now" to a fixed instant and
// build each input as an offset back from it. Locks the bucket boundaries
// (incl. the dormant tail w/mo/y added so directories don't show "247d ago",
// and the 360–364d window that must read "12mo", never "0y").

const NOW = Date.UTC(2026, 5, 11, 12, 0, 0); // 2026-06-11T12:00:00Z
const MIN = 60_000;
const HOUR = 60 * MIN;
const DAY = 24 * HOUR;
const ago = (ms: number) => new Date(NOW - ms).toISOString();

beforeEach(() => {
  vi.useFakeTimers();
  vi.setSystemTime(NOW);
});
afterEach(() => {
  vi.useRealTimers();
});

describe("timeAgo", () => {
  it("recent grammar holds for the first week (just now / m / h / d)", () => {
    expect(timeAgo(ago(30 * 1000))).toBe("just now");
    expect(timeAgo(ago(59 * MIN))).toBe("59m ago");
    expect(timeAgo(ago(HOUR))).toBe("1h ago");
    expect(timeAgo(ago(23 * HOUR))).toBe("23h ago");
    expect(timeAgo(ago(DAY))).toBe("1d ago");
    expect(timeAgo(ago(6 * DAY))).toBe("6d ago");
  });

  it("collapses the dormant tail into coarse buckets past a week", () => {
    expect(timeAgo(ago(7 * DAY))).toBe("1w ago"); // 6d → 1w flip
    expect(timeAgo(ago(28 * DAY))).toBe("4w ago");
    expect(timeAgo(ago(30 * DAY))).toBe("1mo ago");
    expect(timeAgo(ago(364 * DAY))).toBe("12mo ago");
    expect(timeAgo(ago(365 * DAY))).toBe("1y ago");
    expect(timeAgo(ago(730 * DAY))).toBe("2y ago");
  });

  it("has no 360–364d gap (must be 12mo, never 0y)", () => {
    expect(timeAgo(ago(360 * DAY))).toBe("12mo ago");
    expect(timeAgo(ago(364 * DAY))).toBe("12mo ago");
  });

  it("falls back to '-' for missing or non-ISO input (never 'NaNm ago')", () => {
    expect(timeAgo(null)).toBe("-");
    expect(timeAgo(undefined)).toBe("-");
    expect(timeAgo("not-a-date")).toBe("-");
  });
});

describe("isFresh", () => {
  it("is true within the default 1h window, false outside", () => {
    expect(isFresh(ago(30 * MIN))).toBe(true);
    expect(isFresh(ago(2 * HOUR))).toBe(false);
    expect(isFresh(null)).toBe(false);
    expect(isFresh("not-a-date")).toBe(false);
  });
});

describe("recencyTone", () => {
  it("cools from spark through the warm ramp to muted with age", () => {
    expect(recencyTone(ago(30 * MIN))).toBe("var(--color-spark)"); // <1h
    expect(recencyTone(ago(5 * HOUR))).toBe("var(--color-recency-h)"); // <1d
    expect(recencyTone(ago(3 * DAY))).toBe("var(--color-recency-d)"); // <1w
    expect(recencyTone(ago(20 * DAY))).toBe("var(--color-recency-w)"); // <1mo
    expect(recencyTone(ago(90 * DAY))).toBe("var(--color-foreground-muted)"); // ≥1mo
  });
  it("falls back to muted for missing/invalid input", () => {
    expect(recencyTone(null)).toBe("var(--color-foreground-muted)");
    expect(recencyTone("not-a-date")).toBe("var(--color-foreground-muted)");
  });
});
