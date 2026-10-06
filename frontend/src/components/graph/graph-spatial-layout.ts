import { forceCenter, forceCollide, forceLink, forceManyBody, forceSimulation } from "d3-force-3d";

export interface SpatialPoint { x: number; y: number; z: number }

export interface SpatialLayoutInput {
  nodes: Array<{ id: string; position: SpatialPoint }>;
  edges: Array<{ source: string; target: string }>;
  pinned: string[];
}

function hashId(id: string): number {
  let hash = 2166136261;
  for (let index = 0; index < id.length; index += 1) hash = Math.imul(hash ^ id.charCodeAt(index), 16777619);
  return hash >>> 0;
}

function seededRandom(seed: number): () => number {
  let state = seed;
  return () => {
    state = (state + 0x6d2b79f5) | 0;
    let value = Math.imul(state ^ state >>> 15, state | 1);
    value ^= value + Math.imul(value ^ value >>> 7, value | 61);
    return ((value ^ value >>> 14) >>> 0) / 4294967296;
  };
}

/** New resources start in a deterministic volume; no row, plane, or synthetic
 *  connection encodes meaning. During expansion, real neighbors start near
 *  cached resources, whose coordinates remain untouched. */
export function seedSpatialPositions(
  ids: string[],
  existing: Map<string, SpatialPoint>,
  edges: SpatialLayoutInput["edges"] = [],
): Map<string, SpatialPoint> {
  const positions = new Map([...existing].map(([id, point]) => [id, { ...point }]));
  const extent = 70 * Math.cbrt(Math.max(1, ids.length));
  for (const id of ids) {
    if (positions.has(id)) continue;
    const random = seededRandom(hashId(id));
    const depth = random() * 2 - 1;
    const angle = random() * Math.PI * 2;
    const radius = extent * Math.cbrt(random());
    const planar = Math.sqrt(1 - depth * depth);
    positions.set(id, {
      x: radius * planar * Math.cos(angle),
      y: radius * planar * Math.sin(angle),
      z: radius * depth,
    });
  }
  if (existing.size === 0 || edges.length === 0) return positions;

  const visible = new Set(ids);
  const neighbors = new Map<string, Set<string>>();
  for (const { source, target } of edges) {
    if (source === target || !visible.has(source) || !visible.has(target)) continue;
    if (!neighbors.has(source)) neighbors.set(source, new Set());
    if (!neighbors.has(target)) neighbors.set(target, new Set());
    neighbors.get(source)!.add(target);
    neighbors.get(target)!.add(source);
  }
  // Breadth-first placement also reaches new chain neighbors. Sorted roots
  // and neighbors make the chosen anchor independent of payload ordering.
  const queue = [...visible].filter(id => existing.has(id)).sort();
  const anchored = new Set(queue);
  for (let index = 0; index < queue.length; index += 1) {
    const anchorId = queue[index];
    const anchor = positions.get(anchorId)!;
    for (const id of [...(neighbors.get(anchorId) ?? [])].sort()) {
      if (anchored.has(id)) continue;
      const random = seededRandom(hashId(id));
      const depth = random() * 2 - 1;
      const angle = random() * Math.PI * 2;
      const radius = 70 + random() * 20;
      const planar = Math.sqrt(1 - depth * depth);
      positions.set(id, {
        x: anchor.x + radius * planar * Math.cos(angle),
        y: anchor.y + radius * planar * Math.sin(angle),
        z: anchor.z + radius * depth,
      });
      anchored.add(id);
      queue.push(id);
    }
  }
  return positions;
}

interface SimulationNode extends SpatialPoint {
  id: string;
  fx?: number;
  fy?: number;
  fz?: number;
}

/** Bounded, synchronous computation for the worker. All three coordinates take
 *  part in the force simulation; only real input links supply spring forces.
 *  D3 may mutate nodes and endpoints, so it receives private working copies. */
export function layoutSpatialGraph(input: SpatialLayoutInput): Record<string, SpatialPoint> {
  if (input.nodes.length === 0) return {};
  const pinned = new Set(input.pinned);
  const nodes: SimulationNode[] = input.nodes.map(({ id, position }) => ({
    id, ...position,
    ...(pinned.has(id) ? { fx: position.x, fy: position.y, fz: position.z } : {}),
  }));
  const ids = new Set(nodes.map(node => node.id));
  // A self relation has no spatial length to enforce. Leave it in the scene,
  // but omit its zero-length spring; ignore endpoints absent from this slice.
  const links = input.edges
    .filter(edge => edge.source !== edge.target && ids.has(edge.source) && ids.has(edge.target))
    .map(edge => ({ source: edge.source, target: edge.target }));
  const simulation = forceSimulation(nodes, 3).stop();
  try {
    simulation
      .randomSource(seededRandom(0x51a7))
      .alphaDecay(1 - Math.pow(0.001, 1 / 200))
      .velocityDecay(0.35)
      .force("links", forceLink<SimulationNode>(links).id(node => node.id).distance(130).strength(0.16))
      .force("charge", forceManyBody<SimulationNode>().strength(-220).distanceMax(1200))
      .force("collision", forceCollide<SimulationNode>(28).strength(0.8));
    // Centering translates positions before fixed coordinates are restored.
    // With pins, that would move only free neighbors away from their anchors.
    if (pinned.size === 0) simulation.force("center", forceCenter<SimulationNode>(0, 0, 0));
    simulation.tick(200);
    return Object.fromEntries(nodes.map(({ id, x, y, z }) => {
      if (![x, y, z].every(Number.isFinite)) throw new Error("Spatial layout produced an invalid position");
      return [id, { x, y, z }];
    }));
  } finally {
    simulation.stop();
  }
}
