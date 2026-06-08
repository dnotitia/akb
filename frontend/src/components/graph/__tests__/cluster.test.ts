import { describe, it, expect } from "vitest";
import {
  groupOf,
  groupColor,
  isDarkBg,
  forceCluster,
  forceCollide,
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
  it("is deterministic and theme-aware", () => {
    expect(groupColor("w/1")).toBe(groupColor("w/1"));
    expect(groupColor("w/1", false)).not.toBe(groupColor("w/1", true));
    expect(groupColor("w/1")).toMatch(/^hsl\(/);
  });

  it("usually distinguishes different groups by hue", () => {
    expect(groupColor("w/1")).not.toBe(groupColor("w/2"));
  });
});

describe("isDarkBg", () => {
  it("classifies dark vs light hex backgrounds, defaulting to dark", () => {
    expect(isDarkBg("#0f172a")).toBe(true);
    expect(isDarkBg("#faf9f5")).toBe(false);
    expect(isDarkBg("#fff")).toBe(false);
    expect(isDarkBg("oklch(0.2 0 0)")).toBe(true);
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
