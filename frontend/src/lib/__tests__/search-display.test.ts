import { describe, expect, it } from "vitest";
import { cleanSearchContext, safeSearchTags } from "@/lib/search-display";

describe("search display helpers", () => {
  it("removes indexed chunk headers and markdown list noise from result context", () => {
    expect(
      cleanSearchContext(
        "[# Worker split > ## Results] * The worker stays isolated.\n* API remains available.",
      ),
    ).toBe("The worker stays isolated. API remains available.");
  });

  it("normalizes optional tags without assuming a new backend field", () => {
    expect(safeSearchTags(["infra", " infra ", "", 3, "security"])).toEqual([
      "infra",
      "security",
    ]);
    expect(safeSearchTags(undefined)).toEqual([]);
  });
});
