import { describe, expect, it } from "vitest";
import { filterByText, MENU_FILTER_THRESHOLD } from "@/components/ui/menu-filter-utils";

const items = [
  { name: "akb-platform" },
  { name: "gnu-weekly" },
  { name: "Design-System" },
];

describe("filterByText", () => {
  it("returns all items for an empty query", () => {
    expect(filterByText(items, "", (i) => i.name)).toHaveLength(3);
  });

  it("returns all items for a whitespace-only query", () => {
    expect(filterByText(items, "   ", (i) => i.name)).toHaveLength(3);
  });

  it("filters case-insensitively via the accessor", () => {
    expect(filterByText(items, "DESIGN", (i) => i.name)).toEqual([{ name: "Design-System" }]);
  });

  it("matches a substring anywhere in the text", () => {
    expect(filterByText(items, "weekly", (i) => i.name)).toEqual([{ name: "gnu-weekly" }]);
  });

  it("returns an empty array when nothing matches", () => {
    expect(filterByText(items, "zzz", (i) => i.name)).toEqual([]);
  });
});

describe("MENU_FILTER_THRESHOLD", () => {
  it("is a positive integer shared across pickers", () => {
    expect(Number.isInteger(MENU_FILTER_THRESHOLD)).toBe(true);
    expect(MENU_FILTER_THRESHOLD).toBeGreaterThan(0);
  });
});
