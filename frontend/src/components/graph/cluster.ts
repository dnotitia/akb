// frontend/src/components/graph/cluster.ts
//
// Cluster grouping + visual encoding for the graph canvas (Tier 1+2+3):
//   1. grouping       — groupOf(): which cluster a node belongs to
//   2. color          — groupPaint(): a stable color per cluster
//   3. cluster force   — forceCluster(): pulls same-group nodes together
//   4. rounded hull    — drawClusterHulls(): the translucent "blob" outline
//
// Everything here is dependency-free (hand-rolled convex hull + Catmull-Rom
// smoothing) so it adds no npm package / lockfile churn.
//
// Grouping seam: the force, hull, and color readers are all grouping-source
// agnostic — they only read `node.group`. Today that field is populated from
// the node's collection (groupOf(), a pure per-URI function: deterministic,
// flicker-free). To switch to link-structure communities (e.g. Louvain),
// replace the ANNOTATION step (where node.group is assigned at data ingest):
// community detection needs the whole edge set, not a single URI, so it can't
// reuse groupOf()'s signature — it would be a `annotate(nodes, edges)` pass.
import { parseUri } from "@/lib/uri";
import type { GraphNode } from "./graph-types";

export type Point = [number, number];

/** Default pull strength of the cluster force (exported for tuning/tests). */
export const CLUSTER_STRENGTH = 0.18;
/** Padding (px) between a cluster's outermost nodes and its hull outline. */
export const HULL_PAD = 26;

/**
 * The cluster a node belongs to: its TOP-LEVEL collection segment, parsed
 * from the canonical URI (`akb://V/coll/<top>/<rest>/...`). Top-level (not
 * full path) keeps clusters few and large, which reads as cleaner blobs.
 * Root-level resources (no collection) are ungrouped → null (no hull, no
 * cluster pull, keep their kind color).
 */
export function groupOf(uri: string): string | null {
  const p = parseUri(uri);
  if (!p || !p.collection) return null;
  return p.collection.split("/")[0] || null;
}

// ── Color ───────────────────────────────────────────────────────────────

/** Deterministic string → hue in [0,360) (FNV-1a). Same group key always
 *  maps to the same hue regardless of how many groups exist, so colors never
 *  reshuffle/flicker as the graph changes. */
function hashHue(s: string): number {
  let h = 2166136261;
  for (let i = 0; i < s.length; i++) {
    h ^= s.charCodeAt(i);
    h = Math.imul(h, 16777619);
  }
  return (h >>> 0) % 360;
}

export interface GroupPaint {
  /** node ring (solid, readable) */
  node: string;
  /** hull outline (translucent) */
  hullStroke: string;
  /** hull fill (very translucent so nodes/edges underneath stay visible) */
  hullFill: string;
}

/** Luminance check on a hex background so colors adapt to the active theme.
 *  Falls back to "dark" for non-hex / unparseable input. */
export function isDarkBg(bg: string): boolean {
  const m = /^#?([0-9a-f]{3}|[0-9a-f]{6})$/i.exec(bg.trim());
  if (!m) return true;
  let hex = m[1];
  if (hex.length === 3) hex = hex.split("").map((c) => c + c).join("");
  const r = parseInt(hex.slice(0, 2), 16);
  const g = parseInt(hex.slice(2, 4), 16);
  const b = parseInt(hex.slice(4, 6), 16);
  return 0.299 * r + 0.587 * g + 0.114 * b < 128;
}

const paintCache = new Map<string, GroupPaint>();

/** Stable, theme-aware color set for a group key. Memoized — called once per
 *  visible node and per group every frame. On a light background the ring +
 *  stroke drop to a lower lightness so they don't wash out. */
export function groupPaint(group: string, dark = true): GroupPaint {
  const key = `${group}|${dark ? 1 : 0}`;
  const cached = paintCache.get(key);
  if (cached) return cached;
  const h = hashHue(group);
  const p: GroupPaint = dark
    ? {
        node: `hsl(${h}, 65%, 62%)`,
        hullStroke: `hsla(${h}, 65%, 60%, 0.45)`,
        hullFill: `hsla(${h}, 65%, 55%, 0.10)`,
      }
    : {
        node: `hsl(${h}, 60%, 42%)`,
        hullStroke: `hsla(${h}, 60%, 42%, 0.5)`,
        hullFill: `hsla(${h}, 65%, 50%, 0.13)`,
      };
  paintCache.set(key, p);
  return p;
}

// ── Geometry ──────────────────────────────────────────────────────────────

/** Andrew's monotone-chain convex hull. Returns hull vertices in order;
 *  fewer than 3 unique points returns the input as-is (caller draws a
 *  circle fallback). */
export function convexHull(points: Point[]): Point[] {
  const pts = points.slice().sort((a, b) => a[0] - b[0] || a[1] - b[1]);
  const n = pts.length;
  if (n < 3) return pts;
  const cross = (o: Point, a: Point, b: Point) =>
    (a[0] - o[0]) * (b[1] - o[1]) - (a[1] - o[1]) * (b[0] - o[0]);
  const lower: Point[] = [];
  for (const p of pts) {
    while (lower.length >= 2 && cross(lower[lower.length - 2], lower[lower.length - 1], p) <= 0)
      lower.pop();
    lower.push(p);
  }
  const upper: Point[] = [];
  for (let i = n - 1; i >= 0; i--) {
    const p = pts[i];
    while (upper.length >= 2 && cross(upper[upper.length - 2], upper[upper.length - 1], p) <= 0)
      upper.pop();
    upper.push(p);
  }
  lower.pop();
  upper.pop();
  return lower.concat(upper);
}

/** Inflate a hull outward from its centroid by `pad` px so the blob clears
 *  the nodes. Centroid-push (vs edge-bisector) is robust and looks smooth
 *  once the curve is rounded. */
export function padHull(hull: Point[], pad: number): Point[] {
  const n = hull.length;
  if (n === 0) return hull;
  let cx = 0;
  let cy = 0;
  for (const [x, y] of hull) {
    cx += x;
    cy += y;
  }
  cx /= n;
  cy /= n;
  return hull.map(([x, y]) => {
    const dx = x - cx;
    const dy = y - cy;
    const m = Math.hypot(dx, dy) || 1;
    return [x + (dx / m) * pad, y + (dy / m) * pad] as Point;
  });
}

/** Trace a smooth closed curve through `pts` onto the canvas using a
 *  Catmull-Rom spline converted to cubic béziers — this is what gives the
 *  rounded blob look. Caller is responsible for beginPath/fill/stroke. */
export function traceSmoothClosedPath(ctx: CanvasRenderingContext2D, pts: Point[]): void {
  const n = pts.length;
  if (n === 0) return;
  if (n < 3) {
    ctx.moveTo(pts[0][0], pts[0][1]);
    for (let i = 1; i < n; i++) ctx.lineTo(pts[i][0], pts[i][1]);
    ctx.closePath();
    return;
  }
  ctx.moveTo(pts[0][0], pts[0][1]);
  for (let i = 0; i < n; i++) {
    const p0 = pts[(i - 1 + n) % n];
    const p1 = pts[i];
    const p2 = pts[(i + 1) % n];
    const p3 = pts[(i + 2) % n];
    const c1x = p1[0] + (p2[0] - p0[0]) / 6;
    const c1y = p1[1] + (p2[1] - p0[1]) / 6;
    const c2x = p2[0] - (p3[0] - p1[0]) / 6;
    const c2y = p2[1] - (p3[1] - p1[1]) / 6;
    ctx.bezierCurveTo(c1x, c1y, c2x, c2y, p2[0], p2[1]);
  }
  ctx.closePath();
}

/** Draw a rounded translucent hull behind each group's nodes. Call from
 *  `onRenderFramePre` (under the nodes/links). Cheap: O(nodes) per frame.
 *  Self-contained (save/restore) so it never leaks ctx state into the node
 *  paint that follows. */
export function drawClusterHulls(
  ctx: CanvasRenderingContext2D,
  nodes: GraphNode[],
  globalScale: number,
  dark = true,
): void {
  const groups = new Map<string, Point[]>();
  for (const n of nodes) {
    if (!n.group || n.x == null || n.y == null) continue;
    const arr = groups.get(n.group);
    if (arr) arr.push([n.x, n.y]);
    else groups.set(n.group, [[n.x, n.y]]);
  }
  // One (or zero) clusters → nothing to distinguish; a single all-enclosing
  // blob is just noise, so skip drawing it.
  if (groups.size < 2) return;
  ctx.save();
  for (const [group, pts] of groups) {
    const paint = groupPaint(group, dark);
    ctx.beginPath();
    const hull = pts.length >= 3 ? convexHull(pts) : pts;
    if (hull.length >= 3) {
      traceSmoothClosedPath(ctx, padHull(hull, HULL_PAD));
    } else {
      traceCircleAround(ctx, pts, HULL_PAD);
    }
    ctx.fillStyle = paint.hullFill;
    ctx.fill();
    ctx.lineWidth = 1.5 / globalScale;
    ctx.strokeStyle = paint.hullStroke;
    ctx.stroke();
  }
  ctx.restore();
}

/** Enclosing circle fallback for 1–2 node (or collinear) groups. */
function traceCircleAround(ctx: CanvasRenderingContext2D, pts: Point[], pad: number): void {
  let cx = 0;
  let cy = 0;
  for (const [x, y] of pts) {
    cx += x;
    cy += y;
  }
  cx /= pts.length;
  cy /= pts.length;
  let r = 0;
  for (const [x, y] of pts) r = Math.max(r, Math.hypot(x - cx, y - cy));
  ctx.arc(cx, cy, r + pad, 0, 2 * Math.PI);
}

// ── Force ───────────────────────────────────────────────────────────────

export interface ClusterForce {
  (alpha: number): void;
  initialize: (nodes: GraphNode[]) => void;
}

/**
 * A d3-force that nudges each node toward its group's live centroid, so
 * same-group nodes clump tighter on top of the default link/charge forces.
 * Ungrouped nodes are left untouched. The pull is scaled by the simulation's
 * decaying `alpha`, so it eases off as the layout settles.
 */
export function forceCluster(strength = CLUSTER_STRENGTH): ClusterForce {
  let nodes: GraphNode[] = [];
  const force = (alpha: number) => {
    const cent = new Map<string, { x: number; y: number; n: number }>();
    for (const n of nodes) {
      if (!n.group || n.x == null || n.y == null) continue;
      const c = cent.get(n.group);
      if (c) {
        c.x += n.x;
        c.y += n.y;
        c.n++;
      } else {
        cent.set(n.group, { x: n.x, y: n.y, n: 1 });
      }
    }
    for (const c of cent.values()) {
      c.x /= c.n;
      c.y /= c.n;
    }
    const k = strength * alpha;
    for (const n of nodes) {
      if (!n.group || n.x == null || n.y == null) continue;
      const c = cent.get(n.group);
      if (!c) continue;
      n.vx = (n.vx ?? 0) + (c.x - n.x) * k;
      n.vy = (n.vy ?? 0) + (c.y - n.y) * k;
    }
  };
  force.initialize = (ns: GraphNode[]) => {
    nodes = ns;
  };
  return force;
}
