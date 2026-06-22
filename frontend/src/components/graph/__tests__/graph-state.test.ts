// frontend/src/components/graph/__tests__/graph-state.test.ts
import { describe, it, expect } from "vitest";
import { viewToQuery, queryToView } from "../graph-state";
import { DEFAULT_VIEW, ALL_NODE_KINDS, ALL_RELATIONS, type GraphView } from "../graph-types";

describe("graph-state codec", () => {
  it("returns empty query for default view", () => {
    expect(viewToQuery(DEFAULT_VIEW)).toBe("");
  });

  it("roundtrips entry + hops", () => {
    const v: GraphView = { ...DEFAULT_VIEW, entry: "d-94d8657f", hops: 3 };
    const q = viewToQuery(v);
    expect(q).toContain("entry=d-94d8657f");
    expect(q).toContain("hops=3");
    const back = queryToView(new URLSearchParams(q));
    expect(back.entry).toBe("d-94d8657f");
    expect(back.hops).toBe(3);
  });

  // Pre-0.3.0 URLs used `depth=N` for the same field. The decoder
  // honours the legacy name so bookmarked graph URLs keep working
  // after the upgrade.
  it("legacy `depth=` URL param maps to hops", () => {
    const back = queryToView(new URLSearchParams("depth=3"));
    expect(back.hops).toBe(3);
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

  it("ignores unknown hops values and clamps to 2", () => {
    const back = queryToView(new URLSearchParams("hops=7"));
    expect(back.hops).toBe(2);
  });

  // Back-compat: the doc page's "Open in graph" link historically emitted
  // `?focus=<akb-uri>`, but the graph only consumes `entry=<doc-id>`. queryToView
  // normalizes a focus URI to its doc-id so already-shipped links land focused
  // instead of silently dumping the whole vault graph.
  it("normalizes a legacy ?focus=<uri> into the entry doc-id", () => {
    const uri = "akb://akb/doc/specs/2026/foo.md";
    const back = queryToView(new URLSearchParams(`focus=${encodeURIComponent(uri)}`));
    expect(back.entry).toBe("specs/2026/foo.md");
  });

  it("prefers entry over focus when both are present", () => {
    const back = queryToView(
      new URLSearchParams(`entry=d-123&focus=${encodeURIComponent("akb://akb/doc/x.md")}`),
    );
    expect(back.entry).toBe("d-123");
  });

  it("falls through to the whole graph for an unparseable focus URI", () => {
    const back = queryToView(new URLSearchParams("focus=not-a-uri"));
    expect(back.entry).toBeUndefined();
  });

  it("ignores unknown node kinds in types", () => {
    const back = queryToView(new URLSearchParams("types=document,bogus"));
    expect(back.types).toEqual(new Set(["document"]));
  });
});
