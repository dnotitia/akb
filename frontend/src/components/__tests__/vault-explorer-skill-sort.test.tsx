import { describe, it, expect } from "vitest";
import { sortCollectionItems } from "@/hooks/use-vault-tree";

describe("vault-explorer skill sort", () => {
  it("sorts skill docs first within a collection (doc_type on raw.doc_type)", () => {
    const items = [
      { name: "a.md", raw: { doc_type: "note" } },
      { name: "vault-skill.md", raw: { doc_type: "skill" } },
      { name: "b.md", raw: { doc_type: "note" } },
    ];
    const sorted = sortCollectionItems(items);
    expect(sorted[0].name).toBe("vault-skill.md");
    expect(sorted[0].raw.doc_type).toBe("skill");
  });

  it("falls back to alpha sort within same doc_type", () => {
    const items = [
      { name: "c.md", raw: { doc_type: "note" } },
      { name: "a.md", raw: { doc_type: "note" } },
      { name: "b.md", raw: { doc_type: "note" } },
    ];
    const sorted = sortCollectionItems(items);
    expect(sorted.map(i => i.name)).toEqual(["a.md", "b.md", "c.md"]);
  });
});
