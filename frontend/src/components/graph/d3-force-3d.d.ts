/** The subset of d3-force-3d 3.x used by the isolated spatial layout. */
declare module "d3-force-3d" {
  export interface SimulationNodeDatum {
    index?: number;
    x?: number; y?: number; z?: number;
    vx?: number; vy?: number; vz?: number;
    fx?: number | null; fy?: number | null; fz?: number | null;
  }

  export interface Force<Node extends SimulationNodeDatum> {
    (alpha: number): void;
    initialize?: (nodes: Node[], random: () => number, dimensions: number) => void;
  }

  export interface Simulation<Node extends SimulationNodeDatum> {
    stop(): this;
    randomSource(source: () => number): this;
    alphaDecay(decay: number): this;
    velocityDecay(decay: number): this;
    force(name: string, force: Force<Node>): this;
    tick(iterations: number): this;
  }

  export interface LinkForce<Node extends SimulationNodeDatum> extends Force<Node> {
    id(accessor: (node: Node) => string): this;
    distance(distance: number): this;
    strength(strength: number): this;
  }

  export interface ManyBodyForce<Node extends SimulationNodeDatum> extends Force<Node> {
    strength(strength: number): this;
    distanceMax(distance: number): this;
  }

  export interface CollisionForce<Node extends SimulationNodeDatum> extends Force<Node> {
    strength(strength: number): this;
  }

  export function forceSimulation<Node extends SimulationNodeDatum>(nodes: Node[], dimensions: 3): Simulation<Node>;
  export function forceLink<Node extends SimulationNodeDatum>(links: Array<{ source: string | Node; target: string | Node }>): LinkForce<Node>;
  export function forceManyBody<Node extends SimulationNodeDatum>(): ManyBodyForce<Node>;
  export function forceCollide<Node extends SimulationNodeDatum>(radius: number): CollisionForce<Node>;
  export function forceCenter<Node extends SimulationNodeDatum>(x: number, y: number, z: number): Force<Node>;
}
