import { describe, expect, it } from "vitest";
import { edgeFor, hrefFor } from "@/components/relations/relation-row-utils";
import type { RelationRow } from "@/lib/api";

const SRC = "akb://v1/coll/notes/doc/alpha.md";

function row(partial: Partial<RelationRow> & { uri: string }): RelationRow {
  return { direction: "outgoing", relation: "references", ...partial };
}

describe("edgeFor", () => {
  const other = "akb://v1/coll/notes/doc/beta.md";

  it("outgoing: this document is the source", () => {
    expect(edgeFor(row({ direction: "outgoing", uri: other }), SRC)).toEqual({
      source: SRC,
      target: other,
    });
  });

  it("incoming: this document is the target (source/target swapped)", () => {
    expect(edgeFor(row({ direction: "incoming", uri: other }), SRC)).toEqual({
      source: other,
      target: SRC,
    });
  });

  it("treats any non-incoming direction as outgoing", () => {
    // Defensive: the type is required, but a stray value must not invert the edge.
    const r = { direction: "both", relation: "references", uri: other } as unknown as RelationRow;
    expect(edgeFor(r, SRC)).toEqual({ source: SRC, target: other });
  });
});

describe("hrefFor", () => {
  it("routes a collection doc to /doc with the FULL vault-relative ref (the historical blank-screen bug)", () => {
    expect(hrefFor(row({ uri: "akb://v1/coll/notes/doc/beta.md" }), "v1")).toBe(
      `/vault/v1/doc/${encodeURIComponent("notes/beta.md")}`,
    );
  });

  it("routes a root-level doc to /doc", () => {
    expect(hrefFor(row({ uri: "akb://v1/doc/readme.md" }), "v1")).toBe("/vault/v1/doc/readme.md");
  });

  it("routes a table URI to /table (basename id)", () => {
    expect(hrefFor(row({ uri: "akb://v1/coll/data/table/sales" }), "v1")).toBe("/vault/v1/table/sales");
  });

  it("routes a file URI to /file", () => {
    expect(hrefFor(row({ uri: "akb://v1/coll/x/file/uuid-123" }), "v1")).toBe("/vault/v1/file/uuid-123");
  });

  it("uses the URI's own vault, not the fallback", () => {
    expect(hrefFor(row({ uri: "akb://other/coll/n/doc/z.md" }), "v1")).toBe(
      `/vault/other/doc/${encodeURIComponent("n/z.md")}`,
    );
  });

  it("returns '#' for a ref-less URI (vault root) rather than a broken half-path", () => {
    expect(hrefFor(row({ uri: "akb://v1" }), "v1")).toBe("#");
  });

  it("returns '#' for an unparseable URI", () => {
    expect(hrefFor(row({ uri: "not-a-uri" }), "v1")).toBe("#");
  });
});
