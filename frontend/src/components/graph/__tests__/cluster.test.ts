import { describe, it, expect } from "vitest";
import {
  groupOf,
  groupColor,
  forceCluster,
  forceCollide,
  forceCenterPull,
  COLLIDE_RADIUS,
} from "../cluster";
import type { GraphNode } from "../graph-types";

describe("groupOf", () => {
  it("groups by the first two collection segments (week-level for nested vaults)", () => {
    // weekly-updates/<week>/<topic> → groups by week
    expect(groupOf("akb://gnu-weekly/coll/weekly-updates/2026-05-12/akb/doc/x.md")).toBe(
      "weekly-updates/2026-05-12",
    );
    expect(groupOf("akb://gnu-weekly/coll/weekly-updates/2026-05-19/operations/doc/y.md")).toBe(
      "weekly-updates/2026-05-19",
    );
  });

  it("uses the whole collection path when it has fewer than two segments", () => {
    expect(groupOf("akb://v/coll/guides/doc/intro.md")).toBe("guides");
    expect(groupOf("akb://v/coll/data/warehouse/table/metrics")).toBe("data/warehouse");
  });

  it("returns null for vault-root resources and unparseable URIs", () => {
    expect(groupOf("akb://v/doc/readme.md")).toBeNull();
    expect(groupOf("akb://v")).toBeNull();
    expect(groupOf("not-a-uri")).toBeNull();
  });
});

describe("groupColor", () => {
  // The tokenized categorical scale (--color-cat-1..6) resolved by readColors().
  const cat = ["#1f5a6e", "#2f8f94", "#4f9c7a", "#b9791b", "#c44a1e", "#5e6068"];

  it("is deterministic for a group key", () => {
    expect(groupColor("w/1", cat)).toBe(groupColor("w/1", cat));
  });

  it("returns a color drawn from the provided categorical scale", () => {
    expect(cat).toContain(groupColor("w/1", cat));
  });

  it("usually distinguishes different groups", () => {
    expect(groupColor("w/1", cat)).not.toBe(groupColor("w/2", cat));
  });

  it("falls back to a neutral when the scale is empty", () => {
    expect(groupColor("x", [])).toBe("gray");
  });
});

describe("forceCluster", () => {
  it("nudges same-group nodes toward their shared centroid", () => {
    const a: GraphNode = { uri: "a", name: "a", kind: "document", group: "g", x: 0, y: 0, vx: 0, vy: 0 };
    const b: GraphNode = { uri: "b", name: "b", kind: "document", group: "g", x: 10, y: 0, vx: 0, vy: 0 };
    const f = forceCluster(0.5);
    f.initialize([a, b]);
    f(1); // centroid (5,0): a pulled +x, b pulled -x
    expect(a.vx!).toBeGreaterThan(0);
    expect(b.vx!).toBeLessThan(0);
  });

  it("leaves ungrouped nodes untouched", () => {
    const lone: GraphNode = { uri: "c", name: "c", kind: "document", group: null, x: 99, y: 99, vx: 0, vy: 0 };
    const f = forceCluster(0.5);
    f.initialize([lone]);
    f(1);
    expect(lone.vx).toBe(0);
    expect(lone.vy).toBe(0);
  });
});

describe("forceCenterPull", () => {
  it("pulls a node toward the origin (restoring spring → bounded layout)", () => {
    const a: GraphNode = { uri: "a", name: "a", kind: "document", x: 100, y: -40, vx: 0, vy: 0 };
    const f = forceCenterPull(0.1);
    f.initialize([a]);
    f(1);
    expect(a.vx!).toBeLessThan(0); // +x → pulled back toward 0
    expect(a.vy!).toBeGreaterThan(0); // -y → pulled back toward 0
  });

  it("leaves a node already at the origin untouched", () => {
    const o: GraphNode = { uri: "o", name: "o", kind: "document", x: 0, y: 0, vx: 0, vy: 0 };
    const f = forceCenterPull(0.1);
    f.initialize([o]);
    f(1);
    expect(o.vx).toBe(0);
    expect(o.vy).toBe(0);
  });
});

describe("forceCollide", () => {
  it("pushes overlapping nodes apart", () => {
    const a: GraphNode = { uri: "a", name: "a", kind: "document", x: 0, y: 0 };
    const b: GraphNode = { uri: "b", name: "b", kind: "document", x: 2, y: 0 }; // far closer than 2*radius
    const before = Math.abs(a.x! - b.x!);
    const f = forceCollide(COLLIDE_RADIUS);
    f.initialize([a, b]);
    f(1);
    expect(Math.abs(a.x! - b.x!)).toBeGreaterThan(before);
  });

  it("leaves nodes already farther apart than 2·radius alone", () => {
    const a: GraphNode = { uri: "a", name: "a", kind: "document", x: 0, y: 0 };
    const b: GraphNode = { uri: "b", name: "b", kind: "document", x: COLLIDE_RADIUS * 4, y: 0 };
    const f = forceCollide(COLLIDE_RADIUS);
    f.initialize([a, b]);
    f(1);
    expect(a.x).toBe(0);
    expect(b.x).toBe(COLLIDE_RADIUS * 4);
  });

  it("skips nodes without positions", () => {
    const a: GraphNode = { uri: "a", name: "a", kind: "document" };
    const b: GraphNode = { uri: "b", name: "b", kind: "document", x: 0, y: 0 };
    const f = forceCollide(COLLIDE_RADIUS);
    f.initialize([a, b]);
    expect(() => f(1)).not.toThrow();
    expect(b.x).toBe(0);
  });
});
