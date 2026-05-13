// frontend/src/components/graph/__tests__/graph-state.test.ts
import { describe, it, expect } from "vitest";
import { viewToQuery, queryToView } from "../graph-state";
import { DEFAULT_VIEW, ALL_NODE_KINDS, ALL_RELATIONS, type GraphView } from "../graph-types";

describe("graph-state codec", () => {
  it("returns empty query for default view", () => {
    expect(viewToQuery(DEFAULT_VIEW)).toBe("");
  });

  it("roundtrips entry + depth", () => {
    const v: GraphView = { ...DEFAULT_VIEW, entry: "d-94d8657f", depth: 3 };
    const q = viewToQuery(v);
    expect(q).toContain("entry=d-94d8657f");
    expect(q).toContain("depth=3");
    const back = queryToView(new URLSearchParams(q));
    expect(back.entry).toBe("d-94d8657f");
    expect(back.depth).toBe(3);
  });

  it("omits types when all are selected", () => {
    const v: GraphView = { ...DEFAULT_VIEW, types: new Set(ALL_NODE_KINDS) };
    expect(viewToQuery(v)).not.toContain("types=");
  });

  it("encodes a partial types subset", () => {
    const v: GraphView = { ...DEFAULT_VIEW, types: new Set(["document"]) };
    expect(viewToQuery(v)).toContain("types=document");
  });

  it("omits rel when all are selected", () => {
    const v: GraphView = { ...DEFAULT_VIEW, relations: new Set(ALL_RELATIONS) };
    expect(viewToQuery(v)).not.toContain("rel=");
  });

  it("roundtrips a partial relation subset (order-insensitive)", () => {
    const v: GraphView = { ...DEFAULT_VIEW, relations: new Set(["depends_on", "implements"]) };
    const q = viewToQuery(v);
    const back = queryToView(new URLSearchParams(q));
    expect(back.relations).toEqual(new Set(["depends_on", "implements"]));
  });

  it("roundtrips selected", () => {
    const uri = "akb://akb/doc/specs/2026/foo.md";
    const v: GraphView = { ...DEFAULT_VIEW, selected: uri };
    const back = queryToView(new URLSearchParams(viewToQuery(v)));
    expect(back.selected).toBe(uri);
  });

  it("ignores unknown depths and clamps to 2", () => {
    const back = queryToView(new URLSearchParams("depth=7"));
    expect(back.depth).toBe(2);
  });

  it("ignores unknown node kinds in types", () => {
    const back = queryToView(new URLSearchParams("types=document,bogus"));
    expect(back.types).toEqual(new Set(["document"]));
  });
});
