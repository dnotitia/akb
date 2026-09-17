import { describe, expect, it } from "vitest";
import { visibleNavigationItems } from "../navigation-overflow";

describe("visibleNavigationItems", () => {
  const widths = [100, 100, 90, 120, 110, 100];
  it("keeps all destinations when they fit, without reserving More", () => {
    expect(visibleNavigationItems(widths, 640, 80, 4)).toEqual([0, 1, 2, 3, 4, 5]);
  });
  it("keeps Overview and the visible prefix when reading a resource", () => {
    expect(visibleNavigationItems(widths, 375, 80, 4)).toEqual([0, 1]);
  });
  it("prioritizes the actual current destination without changing order", () => {
    expect(visibleNavigationItems(widths, 375, 80, 4, 5)).toEqual([0, 5]);
    expect(visibleNavigationItems(widths, 240, 80, 4, 3)).toEqual([3]);
  });
  it("does not create a missing-link flash before layout is available", () => {
    expect(visibleNavigationItems(widths, 0, 80, 4)).toEqual([0, 1, 2, 3, 4, 5]);
  });
  it("budgets measured text scaling and accounts for gaps", () => {
    expect(visibleNavigationItems(widths.map(width => width * 2), 600, 160, 8, 5)).toEqual([0, 5]);
    expect(visibleNavigationItems([100, 100], 203, 80, 4)).toEqual([0]);
  });
});
