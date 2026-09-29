import { describe, expect, it } from "vitest";
import {
  layoutSpatialGraph,
  seedSpatialPositions,
  type SpatialLayoutInput,
  type SpatialPoint,
} from "../graph-spatial-layout";

// A nonzero scalar triple product proves the four points enclose a volume;
// checking z alone would incorrectly accept a tilted two-dimensional plane.
function volume6([a, b, c, d]: SpatialPoint[]) {
  const u = { x: b.x - a.x, y: b.y - a.y, z: b.z - a.z };
  const v = { x: c.x - a.x, y: c.y - a.y, z: c.z - a.z };
  const w = { x: d.x - a.x, y: d.y - a.y, z: d.z - a.z };
  return Math.abs(u.x * (v.y * w.z - v.z * w.y) - u.y * (v.x * w.z - v.z * w.x) + u.z * (v.x * w.y - v.y * w.x));
}

function inputFor(count: number, linked: number): SpatialLayoutInput {
  const ids = Array.from({ length: count }, (_, index) => `akb://fixture/doc/resource-${index}.md`);
  const positions = seedSpatialPositions(ids, new Map());
  return {
    nodes: ids.map(id => ({ id, position: positions.get(id)! })),
    edges: Array.from({ length: Math.max(0, linked - 1) }, (_, index) => ({ source: ids[index], target: ids[index + 1] })),
    pinned: [],
  };
}

function expectFinite(points: SpatialPoint[]) {
  expect(points.every(point => point && Number.isFinite(point.x) && Number.isFinite(point.y) && Number.isFinite(point.z))).toBe(true);
}

describe("seedSpatialPositions", () => {
  it("starts a real volume reproducibly, independent of resource ordering", () => {
    const ids = ["first", "second", "third", "fourth"];
    const seeded = seedSpatialPositions(ids, new Map());
    const reversed = seedSpatialPositions([...ids].reverse(), new Map());
    expect(seeded.size).toBe(4);
    expectFinite([...seeded.values()]);
    expect(volume6(ids.map(id => seeded.get(id)!))).toBeGreaterThan(1);
    for (const id of ids) expect(reversed.get(id)).toEqual(seeded.get(id));
    expect(seedSpatialPositions(ids, new Map())).toEqual(seeded);
  });

  it("preserves existing xyz coordinates when resources are added without mutating the cache", () => {
    const original = { x: 123, y: -456, z: 789 };
    const existing = new Map([["existing", original]]);
    const seeded = seedSpatialPositions(["existing", "new"], existing);
    expect(seeded.get("existing")).toEqual({ x: 123, y: -456, z: 789 });
    expectFinite([seeded.get("new")!]);
    expect(existing.size).toBe(1);
    seeded.get("existing")!.x = 0;
    expect(original.x).toBe(123);
  });

  it("seeds incremental neighbors and chains near real anchors in a deterministic volume", () => {
    const original = { x: 10_000, y: -20_000, z: 30_000 };
    const existing = new Map([["anchor", original]]);
    const ids = ["anchor", "first", "second", "third", "tail", "unrelated"];
    const edges = [
      { source: "tail", target: "first" },
      { source: "anchor", target: "third" },
      { source: "first", target: "anchor" },
      { source: "anchor", target: "second" },
    ];
    const seeded = seedSpatialPositions(ids, existing, edges);
    const distance = (a: string, b: string) => {
      const first = seeded.get(a)!, second = seeded.get(b)!;
      return Math.hypot(first.x - second.x, first.y - second.y, first.z - second.z);
    };
    for (const [a, b] of [["anchor", "first"], ["anchor", "second"], ["anchor", "third"], ["first", "tail"]]) {
      expect(distance(a, b)).toBeGreaterThanOrEqual(70);
      expect(distance(a, b)).toBeLessThanOrEqual(90);
    }
    expect(volume6(["anchor", "first", "second", "third"].map(id => seeded.get(id)!))).toBeGreaterThan(1);
    expect(seeded.get("anchor")).toEqual(original);
    expect(seeded.get("anchor")).not.toBe(original);
    expect(existing).toEqual(new Map([["anchor", { x: 10_000, y: -20_000, z: 30_000 }]]));
    expect(seeded.get("unrelated")).toEqual(seedSpatialPositions(ids, existing).get("unrelated"));
    const reversed = seedSpatialPositions([...ids].reverse(), existing, [...edges].reverse());
    for (const id of ids) expect(reversed.get(id)).toEqual(seeded.get(id));
  });

  it("keeps initial placement volumetric without using links as synthetic anchors", () => {
    const ids = ["first", "second", "third", "fourth"];
    const edges = [{ source: "first", target: "second" }, { source: "second", target: "third" }];
    expect(seedSpatialPositions(ids, new Map(), edges)).toEqual(seedSpatialPositions(ids, new Map()));
  });
});

describe("layoutSpatialGraph", () => {
  it("handles the empty scene without producing synthetic resources", () => {
    expect(layoutSpatialGraph({ nodes: [], edges: [], pinned: [] })).toEqual({});
  });

  it("centers a single free resource with a finite depth coordinate", () => {
    const output = layoutSpatialGraph({
      nodes: [{ id: "only", position: { x: 12, y: -34, z: 56 } }], edges: [], pinned: [],
    });
    expect(Object.keys(output)).toEqual(["only"]);
    expect(output.only).toEqual({ x: 0, y: 0, z: 0 });
  });

  it.each([0, 4])("preserves a noncoplanar volume with %i linked resources", linked => {
    const output = layoutSpatialGraph(inputFor(4, linked));
    expect(Object.keys(output)).toHaveLength(4);
    expectFinite(Object.values(output));
    expect(volume6(Object.values(output))).toBeGreaterThan(1);
  });

  it("separates coincident free resources in all three simulated dimensions", () => {
    const output = layoutSpatialGraph({
      nodes: ["a", "b", "c", "d"].map(id => ({ id, position: { x: 0, y: 0, z: 0 } })),
      edges: [], pinned: [],
    });
    expect(Object.keys(output)).toHaveLength(4);
    expectFinite(Object.values(output));
    expect(volume6(Object.values(output))).toBeGreaterThan(1);
  });

  it("moves free resources while preserving pinned x, y, and z exactly", () => {
    const input = inputFor(8, 8);
    input.nodes[0].position = { x: 400, y: -250, z: 175 };
    input.nodes[3].position = { x: -230, y: 300, z: -110 };
    input.pinned = [input.nodes[0].id, input.nodes[3].id];
    const output = layoutSpatialGraph(input);
    expect(output[input.nodes[0].id]).toEqual({ x: 400, y: -250, z: 175 });
    expect(output[input.nodes[3].id]).toEqual({ x: -230, y: 300, z: -110 });
    expect(output[input.nodes[1].id]).not.toEqual(input.nodes[1].position);
  });

  it("keeps a newly connected resource near a far-away pinned anchor", () => {
    const anchor = { x: 30_000, y: -2_000, z: 5_000 };
    const ids = ["anchor", "neighbor"];
    const edges = [{ source: "anchor", target: "neighbor" }];
    const positions = seedSpatialPositions(ids, new Map([["anchor", anchor]]), edges);
    const output = layoutSpatialGraph({
      nodes: ids.map(id => ({ id, position: positions.get(id)! })),
      edges,
      pinned: ["anchor"],
    });
    expect(output.anchor).toEqual(anchor);
    expect(Math.hypot(
      output.neighbor.x - anchor.x,
      output.neighbor.y - anchor.y,
      output.neighbor.z - anchor.z,
    )).toBeLessThan(300);
  });

  it("does not mutate input positions, edge endpoints, or pin arrays", () => {
    const input = inputFor(6, 6);
    const before = structuredClone(input);
    for (const node of input.nodes) { Object.freeze(node.position); Object.freeze(node); }
    for (const edge of input.edges) Object.freeze(edge);
    Object.freeze(input.nodes); Object.freeze(input.edges); Object.freeze(input.pinned); Object.freeze(input);
    const output = layoutSpatialGraph(input);
    expect(input).toEqual(before);
    expect(Object.keys(output)).toHaveLength(6);
    expectFinite(Object.values(output));
  });

  it("is deterministic for identical topology and starting coordinates", () => {
    const input = inputFor(12, 9);
    expect(layoutSpatialGraph(input)).toEqual(layoutSpatialGraph(input));
  });

  it("tolerates parallel and self links while retaining all isolated resources", () => {
    const input = inputFor(8, 4);
    input.edges.push({ ...input.edges[0] }, { source: input.nodes[1].id, target: input.nodes[1].id });
    const output = layoutSpatialGraph(input);
    expect(Object.keys(output)).toHaveLength(8);
    expectFinite(Object.values(output));
    expect(volume6(input.nodes.slice(4).map(node => output[node.id]))).toBeGreaterThan(1);
  });

  it("uses real links to affect spatial proximity without inventing topology for isolates", () => {
    const input = inputFor(10, 0);
    const a = input.nodes[0].id, b = input.nodes[1].id;
    const disconnected = layoutSpatialGraph(input);
    const connected = layoutSpatialGraph({ ...input, edges: [{ source: a, target: b }] });
    expect(Object.keys(disconnected)).toHaveLength(10);
    expect(Object.keys(connected)).toHaveLength(10);
    const distance = (points: Record<string, SpatialPoint>) => Math.hypot(points[a].x - points[b].x, points[a].y - points[b].y, points[a].z - points[b].z);
    expect(distance(connected)).toBeLessThan(distance(disconnected));
    expect(input.edges).toEqual([]);
  });

  it.each([[700, 200], [1500, 1000]])("returns finite volumetric positions for %i resources (%i connected)", (count, linked) => {
    const output = layoutSpatialGraph(inputFor(count, linked));
    expect(Object.keys(output)).toHaveLength(count);
    expectFinite(Object.values(output));
    expect(volume6(Object.values(output).slice(0, 4))).toBeGreaterThan(1);
    expect(volume6(Object.values(output).slice(-4))).toBeGreaterThan(1);
  }, 20_000);
});
