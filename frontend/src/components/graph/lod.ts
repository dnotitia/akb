// frontend/src/components/graph/lod.ts
//
// Pure (React-free) helpers for VIEWPORT CULLING + LEVEL-OF-DETAIL rendering,
// so the canvas paints proportional to the visible screen ratio: at the
// whole-graph overview only the strongest hubs draw; zooming into a region
// progressively reveals leaves, labels, and edges, and off-screen elements are
// culled. Dependency-free + deterministic → unit-tested like cluster.ts.
//
// react-force-graph evaluates nodeVisibility/linkVisibility once per element
// per frame (a plain `.filter`) and skips BOTH the paint AND the hit-test for
// culled elements, while the d3 simulation keeps the full node/link set — so
// culling is a pure paint/LOD win that never disturbs layout.

export interface ViewportRect {
  minX: number;
  maxX: number;
  minY: number;
  maxY: number;
}

/** Screen-pixel slack added around the viewport so a node (and its label) that
 *  is near the edge doesn't pop in/out abruptly: max node radius (20) + a
 *  ~60px label-pill band. Converted to world units (÷ zoom) at use. */
export const MARGIN_PX = 80;
/** Below this rendered-node count, culling + LOD are OFF — small graphs show
 *  everything at every zoom (mirrors layoutTier's count-gating). */
export const CULL_MIN_NODES = 300;
/** A non-forced label only draws once the node's on-screen radius reaches this,
 *  so labels turn on hub-first as you zoom in. */
export const LABEL_MIN_NODE_PX = 7.5;
/** Hard cap on non-forced labels painted per frame (forced labels bypass it). */
export const LABEL_CAP = 150;
/** Hysteresis fraction around band boundaries so a wheel nudge near a boundary
 *  can't flicker the detail band in/out. */
export const LOD_HYST = 0.08;

/** Detail bands keyed on RELATIVE zoom (currentZoom / fitZoom, where 1.0 = the
 *  fit / whole-graph overview). Each band renders the top `target` highest-
 *  degree nodes (ranked over the whole graph, then intersected with the viewport
 *  cull) in full — a COUNT target, not an absolute degree, so the policy adapts
 *  to any vault's degree distribution (an absolute floor would blank an overview
 *  whose max degree is below it). Ordered far → near. */
export const LOD_BANDS: ReadonlyArray<{ maxRel: number; target: number }> = [
  { maxRel: 1.5, target: 30 },            // overview: a readable hub constellation
  { maxRel: 2.5, target: 80 },            // mid: hubs + secondary connectors
  { maxRel: 4.0, target: 200 },           // region: nearly everything in view
  { maxRel: Infinity, target: Infinity }, // zoomed in: all leaves incl. isolates
];

/** Visible world-space rectangle from the camera. `cx,cy` are the graph-space
 *  viewport center (force-graph's onZoom {x,y} / centerAt()), `k` the zoom,
 *  `w,h` the canvas size. `marginPx` is inflated in screen px (÷ k → world). */
export function computeViewportRect(args: {
  w: number;
  h: number;
  k: number;
  cx: number;
  cy: number;
  marginPx?: number;
}): ViewportRect {
  const { w, h, k, cx, cy, marginPx = 0 } = args;
  const halfW = w / 2 / k;
  const halfH = h / 2 / k;
  const m = marginPx / k;
  return {
    minX: cx - halfW - m,
    maxX: cx + halfW + m,
    minY: cy - halfH - m,
    maxY: cy + halfH + m,
  };
}

export function inViewportPoint(x: number, y: number, r: ViewportRect): boolean {
  return x >= r.minX && x <= r.maxX && y >= r.minY && y <= r.maxY;
}

/** Whether a segment (x1,y1)-(x2,y2) might cross the rect — tested via its
 *  bounding box overlapping the rect. Conservative (admits a few near-miss
 *  diagonals), but it catches the case BOTH endpoints are off-screen yet the
 *  edge crosses the visible area — which a per-endpoint test wrongly culls. A
 *  few extra edge paints are far cheaper than an exact segment-rect intersect. */
export function segmentOverlapsRect(
  x1: number,
  y1: number,
  x2: number,
  y2: number,
  r: ViewportRect,
): boolean {
  return (
    Math.min(x1, x2) <= r.maxX &&
    Math.max(x1, x2) >= r.minX &&
    Math.min(y1, y2) <= r.maxY &&
    Math.max(y1, y2) >= r.minY
  );
}

/** Stateless band index for a relative zoom (no hysteresis). */
export function lodBand(relZoom: number): number {
  for (let i = 0; i < LOD_BANDS.length; i++) {
    if (relZoom < LOD_BANDS[i].maxRel) return i;
  }
  return LOD_BANDS.length - 1;
}

/** Next band from the current one with hysteresis: only step UP past a
 *  boundary·(1+HYST) and DOWN below boundary/(1+HYST). The while-loops handle a
 *  deliberate multi-band zoom jump while the dead-band kills boundary flicker. */
export function nextBand(relZoom: number, currentBand: number): number {
  let band = Math.max(0, Math.min(LOD_BANDS.length - 1, currentBand));
  while (band < LOD_BANDS.length - 1 && relZoom >= LOD_BANDS[band].maxRel * (1 + LOD_HYST)) band++;
  while (band > 0 && relZoom < LOD_BANDS[band - 1].maxRel / (1 + LOD_HYST)) band--;
  return band;
}

export function bandTargetCount(band: number): number {
  return LOD_BANDS[Math.max(0, Math.min(LOD_BANDS.length - 1, band))].target;
}

/** The set of node URIs visible at a band: the top `targetCount` by degree
 *  (rankedUris is the URI list sorted DESCENDING by degree). Returns null when
 *  targetCount covers everything → "all visible", skip the membership test.
 *  EXACT-count (rank-based) on purpose: a degree FLOOR over-admits when many
 *  nodes tie at the threshold degree (e.g. 24 hubs at degree 13, target 30 →
 *  the 30th-ranked node has degree 1 → a floor of 1 admits every leaf). */
export function lodVisibleSet(rankedUris: string[], targetCount: number): Set<string> | null {
  if (targetCount >= rankedUris.length) return null;
  return new Set(rankedUris.slice(0, Math.max(0, targetCount)));
}

/** The zoom at which the whole graph would fit the viewport: min(w/bboxW,
 *  h/bboxH) × a padding factor. relZoom (currentZoom / this) anchors the LOD
 *  bands. Derived from the LIVE graph bbox on every recompute — NOT captured
 *  once after the fit animation — so it self-calibrates after an expand / filter
 *  / cluster change, with no settle-timing race and no stale anchor. Returns 0
 *  (→ relZoom falls back to 1, the overview band) for a missing/degenerate bbox. */
export function fitZoomFromBbox(
  bbox: { x: [number, number]; y: [number, number] } | null | undefined,
  w: number,
  h: number,
  padding = 0.92,
): number {
  if (!bbox || w <= 0 || h <= 0) return 0;
  const bw = bbox.x[1] - bbox.x[0];
  const bh = bbox.y[1] - bbox.y[0];
  if (!(bw > 0) || !(bh > 0)) return 0;
  return Math.min(w / bw, h / bh) * padding;
}
