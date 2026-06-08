import { describe, it, expect } from "vitest";
import {
  groupOf,
  groupPaint,
  isDarkBg,
  convexHull,
  padHull,
  traceSmoothClosedPath,
  forceCluster,
  drawClusterHulls,
  type Point,
} from "../cluster";
import type { GraphNode } from "../graph-types";

/** Minimal recording stub for CanvasRenderingContext2D used by the hull/path
 *  tests — captures method-call names and tolerates property assignment. */
function recordingCtx(): { ctx: CanvasRenderingContext2D; calls: string[] } {
  const calls: string[] = [];
  const ctx = new Proxy(
    {},
    {
      get: (_t, prop) => {
        if (prop === "fillStyle" || prop === "strokeStyle" || prop === "lineWidth") return "";
        return (..._args: unknown[]) => calls.push(String(prop));
      },
      set: () => true,
    },
  ) as unknown as CanvasRenderingContext2D;
  return { ctx, calls };
}

describe("groupOf", () => {
  it("groups a collection doc by its TOP-LEVEL collection segment", () => {
    expect(groupOf("akb://v/coll/specs/2026/doc/api.md")).toBe("specs");
    expect(groupOf("akb://v/coll/guides/doc/intro.md")).toBe("guides");
  });

  it("groups tables/files by their top-level collection too", () => {
    expect(groupOf("akb://v/coll/data/warehouse/table/metrics")).toBe("data");
    expect(groupOf("akb://v/coll/assets/file/8f1c.png")).toBe("assets");
  });

  it("returns null for vault-root resources (no collection)", () => {
    expect(groupOf("akb://v/doc/readme.md")).toBeNull();
    expect(groupOf("akb://v/table/metrics")).toBeNull();
  });

  it("returns null for unparseable / vault / collection URIs", () => {
    expect(groupOf("not-a-uri")).toBeNull();
    expect(groupOf("akb://v")).toBeNull();
  });
});

describe("groupPaint", () => {
  it("is deterministic for a given group key", () => {
    expect(groupPaint("specs")).toEqual(groupPaint("specs"));
  });

  it("returns the three color roles as CSS color strings", () => {
    const p = groupPaint("specs");
    expect(p.node).toMatch(/^hsl/);
    expect(p.hullStroke).toMatch(/^hsla/);
    expect(p.hullFill).toMatch(/^hsla/);
  });

  it("usually distinguishes different groups by hue", () => {
    // Not a hard guarantee (hash collisions exist) but should hold for these.
    expect(groupPaint("specs").node).not.toBe(groupPaint("guides").node);
  });

  it("uses a different (darker) ring on a light background", () => {
    expect(groupPaint("specs", false).node).not.toBe(groupPaint("specs", true).node);
  });
});

describe("isDarkBg", () => {
  it("classifies dark vs light hex backgrounds", () => {
    expect(isDarkBg("#0f172a")).toBe(true); // app dark bg
    expect(isDarkBg("#faf9f5")).toBe(false); // app light bg
    expect(isDarkBg("#000")).toBe(true);
    expect(isDarkBg("#fff")).toBe(false);
  });

  it("falls back to dark for unparseable input", () => {
    expect(isDarkBg("")).toBe(true);
    expect(isDarkBg("oklch(0.2 0 0)")).toBe(true);
  });
});

describe("convexHull", () => {
  it("returns the 4 corners for a filled square of points", () => {
    const pts: Point[] = [
      [0, 0], [10, 0], [10, 10], [0, 10], // corners
      [5, 5], [3, 7], [8, 2], // interior — must be dropped
    ];
    const hull = convexHull(pts);
    expect(hull).toHaveLength(4);
    for (const corner of [[0, 0], [10, 0], [10, 10], [0, 10]]) {
      expect(hull).toContainEqual(corner);
    }
  });

  it("returns the input unchanged for fewer than 3 points", () => {
    expect(convexHull([[1, 1]])).toEqual([[1, 1]]);
    expect(convexHull([[1, 1], [2, 2]])).toEqual([[1, 1], [2, 2]]);
  });

  it("collapses 3+ collinear points to < 3 (so the caller draws a circle)", () => {
    expect(convexHull([[0, 0], [1, 1], [2, 2]]).length).toBeLessThan(3);
    expect(convexHull([[0, 0], [1, 0], [2, 0], [3, 0]]).length).toBeLessThan(3);
  });
});

describe("traceSmoothClosedPath", () => {
  it("emits one bézier per vertex for a closed smoothed loop (n>=3)", () => {
    const { ctx, calls } = recordingCtx();
    const pts: Point[] = [[0, 0], [10, 0], [10, 10], [0, 10]];
    traceSmoothClosedPath(ctx, pts);
    expect(calls.filter((c) => c === "bezierCurveTo")).toHaveLength(pts.length);
    expect(calls).toContain("moveTo");
    expect(calls).toContain("closePath");
  });

  it("falls back to straight segments for < 3 points (no bézier)", () => {
    const { ctx, calls } = recordingCtx();
    traceSmoothClosedPath(ctx, [[0, 0], [10, 0]]);
    expect(calls).toContain("moveTo");
    expect(calls).toContain("lineTo");
    expect(calls).not.toContain("bezierCurveTo");
  });
});

describe("padHull", () => {
  it("pushes every vertex outward from the centroid", () => {
    const hull: Point[] = [[0, 0], [10, 0], [10, 10], [0, 10]]; // centroid (5,5)
    const padded = padHull(hull, 5);
    // every padded vertex is farther from the centroid than the original
    for (let i = 0; i < hull.length; i++) {
      const d0 = Math.hypot(hull[i][0] - 5, hull[i][1] - 5);
      const d1 = Math.hypot(padded[i][0] - 5, padded[i][1] - 5);
      expect(d1).toBeGreaterThan(d0);
    }
  });
});

describe("forceCluster", () => {
  it("nudges same-group nodes toward their shared centroid", () => {
    const a: GraphNode = { uri: "a", name: "a", kind: "document", group: "g", x: 0, y: 0, vx: 0, vy: 0 };
    const b: GraphNode = { uri: "b", name: "b", kind: "document", group: "g", x: 10, y: 0, vx: 0, vy: 0 };
    const f = forceCluster(0.5);
    f.initialize([a, b]);
    f(1); // centroid = (5,0): a pulled +x, b pulled -x
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

describe("drawClusterHulls", () => {
  it("draws one hull per group and skips ungrouped/position-less nodes (smoke)", () => {
    const { ctx, calls } = recordingCtx();
    const nodes: GraphNode[] = [
      { uri: "a", name: "a", kind: "document", group: "g1", x: 0, y: 0 },
      { uri: "b", name: "b", kind: "document", group: "g1", x: 10, y: 0 },
      { uri: "c", name: "c", kind: "document", group: "g1", x: 5, y: 10 },
      { uri: "d", name: "d", kind: "document", group: "g2", x: 100, y: 100 }, // singleton -> circle
      { uri: "e", name: "e", kind: "document", group: null, x: 0, y: 0 }, // ungrouped -> skipped
      { uri: "f", name: "f", kind: "document", group: "g1" }, // no position -> skipped
    ];

    expect(() => drawClusterHulls(ctx, nodes, 1)).not.toThrow();
    // two groups -> two fills + two strokes
    expect(calls.filter((c) => c === "fill")).toHaveLength(2);
    expect(calls.filter((c) => c === "stroke")).toHaveLength(2);
    // the 3-node group produces a smoothed path (bezier), the singleton a circle (arc)
    expect(calls).toContain("bezierCurveTo");
    expect(calls).toContain("arc");
    // self-contained: must save/restore so it never leaks ctx state to nodes
    expect(calls).toContain("save");
    expect(calls).toContain("restore");
  });

  it("draws nothing when fewer than two groups are present", () => {
    const { ctx, calls } = recordingCtx();
    const nodes: GraphNode[] = [
      { uri: "a", name: "a", kind: "document", group: "only", x: 0, y: 0 },
      { uri: "b", name: "b", kind: "document", group: "only", x: 10, y: 0 },
      { uri: "c", name: "c", kind: "document", group: "only", x: 5, y: 10 },
    ];
    drawClusterHulls(ctx, nodes, 1);
    expect(calls).not.toContain("fill");
    expect(calls).not.toContain("stroke");
  });
});
