// frontend/src/components/graph/GraphCanvas.tsx
import { forwardRef, useCallback, useEffect, useImperativeHandle, useMemo, useRef, useState } from "react";
import ForceGraph2D, { type ForceGraphMethods } from "react-force-graph-2d";
import { Pause, Play, Maximize2, Minus, Plus, Boxes } from "lucide-react";
import { useTheme } from "@/hooks/use-theme";
import {
  RELATION_DASH,
  RELATION_CLASS,
  readColors,
  type GraphEdge,
  type GraphNode,
  type GraphColors,
} from "./graph-types";
import { endpointUri } from "./use-graph-data";
import {
  groupColor,
  forceCluster,
  forceCollide,
  forceCenterPull,
  CLUSTER_STRENGTH,
  COLLIDE_RADIUS,
  CHARGE_STRENGTH,
  DEFAULT_CHARGE,
} from "./cluster";
import {
  computeViewportRect,
  inViewportPoint,
  nextBand,
  bandTargetCount,
  floorFromDegrees,
  MARGIN_PX,
  CULL_MIN_NODES,
  LABEL_MIN_NODE_PX,
  LABEL_CAP,
  type ViewportRect,
} from "./lod";

/** True when the OS asks for reduced motion — used to snap the force layout
 *  instead of animating it (the canvas rAF sim is unreachable by CSS). */
function prefersReducedMotion(): boolean {
  return typeof window !== "undefined" && !!window.matchMedia?.("(prefers-reduced-motion: reduce)").matches;
}

/** The app's sans stack (`--font-sans`, Pretendard) for canvas labels. Read
 *  once — it's theme-independent — so paintNode never touches getComputedStyle
 *  on the hot path. Labels match the title as rendered in the detail panel;
 *  the design system reserves mono for ids/code. */
function readFontSans(): string {
  if (typeof document === "undefined") return "sans-serif";
  return getComputedStyle(document.documentElement).getPropertyValue("--font-sans").trim() || "sans-serif";
}

/** Trace a node's kind-specific outline into the current path (the caller fills
 *  + strokes). Documents — the 90% case — are a plain degree-sized circle so
 *  the graph reads as dots like every reference tool; tables a rounded square
 *  (a distinct silhouette); files a circle the caller strokes dashed. Kind is
 *  thus carried by shape, freeing the old per-node D/T/F glyph (illegible at
 *  overview zoom, redundant with the legend). */
function traceNode(ctx: CanvasRenderingContext2D, kind: GraphNode["kind"], x: number, y: number, r: number) {
  if (kind === "table") {
    const s = r * 1.8;
    const rad = r * 0.4;
    if (typeof ctx.roundRect === "function") ctx.roundRect(x - s / 2, y - s / 2, s, s, rad);
    else ctx.rect(x - s / 2, y - s / 2, s, s);
  } else {
    ctx.arc(x, y, r, 0, 2 * Math.PI);
  }
}

export interface GraphCanvasHandle {
  centerOnNode: (uri: string) => void;
}

interface Props {
  nodes: GraphNode[];
  edges: GraphEdge[];
  selected?: string;
  pinned: Set<string>;
  hidden: Set<string>;
  onSelect: (uri: string | undefined) => void;
  /** Double-click / Enter on a node — expand its neighborhood (NOT navigate;
   *  navigation lives in the context menu + detail panel). */
  onExpand: (node: GraphNode) => void;
  /** Drag-release pins the node in place (Obsidian-style); the page adds it to
   *  the `pinned` set so the marker shows and it survives relayout. */
  onPinNode: (uri: string) => void;
  onContextMenu: (
    node: GraphNode,
    screenX: number,
    screenY: number,
  ) => void;
}

// react-force-graph mutates extra fields onto the node objects at
// runtime; we keep the alias so call sites read self-documenting,
// but it's structurally identical to GraphNode.
type RenderNode = GraphNode;
// RenderEdge cannot extend GraphEdge directly because react-force-graph
// resolves source/target from string IDs to node objects during simulation.
interface RenderEdge {
  source: string | RenderNode;
  target: string | RenderNode;
  relation: GraphEdge["relation"];
}

/** Node RADIUS (graph units, pre-clamp): a base plus a sqrt(degree) term so
 *  topology is the primary visual channel — hubs read clearly bigger than
 *  leaves — while sqrt compresses the tail so one giant hub doesn't dwarf the
 *  graph (the ForceAtlas2 degree-sizing convention). */
const NODE_BASE_R = 5;
const DEGREE_R_GAIN = 1.9;
/** The drawn radius is CLAMPED to a screen-pixel range so a node neither
 *  balloons when a small graph fits-to-view (zoomed way in) nor vanishes on a
 *  big graph (zoomed out). Drawn at `clamp(baseR·zoom, MIN, MAX) / zoom` graph
 *  units → a constant-ish on-screen size that still grows with degree. */
const NODE_MIN_R_PX = 4;
const NODE_MAX_R_PX = 20;

/**
 * Layout effort by node count. `warmup` ticks run SYNCHRONOUSLY off-screen
 * before the first paint, so node positions ALWAYS exist even when the animated
 * `cooldown` is short or zero — this is what prevents the old origin-stacking
 * blank on large graphs. Bigger graphs warm up more (so they're laid out) but
 * animate less (so the tab stays responsive); `collide` (the only O(n²) force)
 * is dropped past ~1200 nodes.
 */
function layoutTier(n: number) {
  if (n > 3000) return { warmup: 250, cooldownTicks: 0, collide: false };
  if (n > 1200) return { warmup: 150, cooldownTicks: 60, collide: false };
  if (n > 400) return { warmup: 60, cooldownTicks: 120, collide: true };
  return { warmup: 0, cooldownTicks: 200, collide: true };
}

export const GraphCanvas = forwardRef<GraphCanvasHandle, Props>(function GraphCanvas(
  {
    nodes,
    edges,
    selected,
    pinned,
    hidden,
    onSelect,
    onExpand,
    onPinNode,
    onContextMenu,
  },
  ref,
) {
  const fgRef = useRef<ForceGraphMethods | undefined>(undefined);
  const fittedRef = useRef(false);
  const wrapRef = useRef<HTMLDivElement>(null);
  // Latest node array (the live objects react-force-graph mutates in place),
  // read inside the pin-sync effect without keying it on `nodes` — so the
  // physics reconciliation runs on a pin/unpin, not on every data change.
  const nodesRef = useRef(nodes);
  nodesRef.current = nodes;
  const { theme } = useTheme();
  const [colors, setColors] = useState<GraphColors>(() => readColors());
  // App sans stack for canvas labels — read once (theme-independent).
  const [fontSans] = useState(() => readFontSans());
  // Snap (don't animate) the layout when the OS asks for reduced motion.
  const [frozen, setFrozen] = useState(() => prefersReducedMotion());
  // Latest frozen flag, read inside the cluster/expand reheat path without
  // making those effects depend on it — so Freeze/Resume never reinstalls the
  // forces, yet a reheat still respects the live state (reduced-motion Resume).
  const frozenRef = useRef(frozen);
  frozenRef.current = frozen;
  const [clustered, setClustered] = useState(true);
  const [hovered, setHovered] = useState<string>();
  const [size, setSize] = useState({ w: 0, h: 0 });

  // ── Viewport culling + LOD (refs so the per-frame visibility predicates stay
  //    O(1) and never trigger a React re-render). The cull layer skips paint +
  //    hit-test for off-screen elements; the LOD layer raises a degree floor at
  //    overview zoom so only hubs draw, revealing leaves as you zoom in. ──
  const sizeRef = useRef(size);
  sizeRef.current = size;
  const transformRef = useRef({ k: 1, cx: 0, cy: 0 });
  const viewportRef = useRef<ViewportRect | null>(null);
  const degreeFloorRef = useRef(0);
  const lodBandRef = useRef(0);
  const fitZoomRef = useRef(0); // captured post-fit; 0 until then (relZoom→1)
  const degreeRef = useRef<Map<string, number>>(new Map());
  const sortedDegreesRef = useRef<number[]>([]);
  const labelCountRef = useRef(0);
  // Forced elements always paint (selection / hover / pin / focus-neighbor),
  // even below the LOD floor or off-screen — read via refs by stable predicates.
  const selectedRef = useRef(selected);
  selectedRef.current = selected;
  const hoveredRef = useRef(hovered);
  hoveredRef.current = hovered;
  const pinnedRef = useRef(pinned);
  pinnedRef.current = pinned;
  const focusUriRef = useRef<string | undefined>(undefined);
  const adjacencyRef = useRef<Map<string, Set<string>>>(new Map());
  // Bumped on NON-camera triggers (resize / zoom-settle / fit-capture) to force
  // one repaint so the cull re-runs; camera moves already repaint on their own.
  const [cullEpoch, setCullEpoch] = useState(0);

  // Recompute the visible world rect + the LOD degree floor from the current
  // camera. Cheap (a few ops) — called on zoom/resize, never per frame. Reads
  // refs only, so its identity is stable.
  const recomputeCull = useCallback(() => {
    const { k, cx, cy } = transformRef.current;
    if (!k) return;
    const { w, h } = sizeRef.current;
    viewportRef.current = computeViewportRect({ w, h, k, cx, cy, marginPx: MARGIN_PX });
    const relZoom = fitZoomRef.current ? k / fitZoomRef.current : 1;
    const band = nextBand(relZoom, lodBandRef.current);
    lodBandRef.current = band;
    degreeFloorRef.current = floorFromDegrees(sortedDegreesRef.current, bandTargetCount(band));
  }, []);

  // react-force-graph-2d defaults to window.innerWidth/Height with no resize
  // handling — measure the actual wrapper so the canvas fills its grid cell and
  // reflows when the sidebar/detail panel toggles.
  useEffect(() => {
    const el = wrapRef.current;
    if (!el) return;
    const ro = new ResizeObserver(([e]) => {
      const w = e.contentRect.width;
      const h = e.contentRect.height;
      setSize({ w, h });
      sizeRef.current = { w, h };
      recomputeCull();
      setCullEpoch((x) => x + 1);
    });
    ro.observe(el);
    return () => ro.disconnect();
  }, [recomputeCull]);

  useImperativeHandle(
    ref,
    (): GraphCanvasHandle => ({
      centerOnNode: (uri) => {
        const n = nodes.find((node) => node.uri === uri);
        if (!n || n.x == null || n.y == null) {
          fgRef.current?.zoomToFit(400, 60);
          return;
        }
        fgRef.current?.centerAt(n.x, n.y, 400);
        fgRef.current?.zoom(Math.max(fgRef.current?.zoom() || 1, 1.5), 400);
      },
    }),
    [nodes],
  );

  useEffect(() => {
    setColors(readColors());
  }, [theme]);

  useEffect(() => {
    const fg = fgRef.current;
    if (!fg) return;
    // Pause only for Freeze / reduced-motion — NEVER for node count. Suspending
    // the sim on a large graph was the old blank bug. The built-in 'center'
    // force stays installed; pausing already stops all ticks.
    if (frozen) fg.pauseAnimation();
    else fg.resumeAnimation();
  }, [frozen]);

  // On first settle: fit the view once. We do NOT pin nodes here.
  //
  // The old code pinned EVERY node (fx/fy = x/y) on every settle to stop a
  // "click makes clusters drift apart" ratchet — but that froze the layout
  // permanently, so overlaps never relaxed and expand/cluster-toggle had to
  // un-pin and reheat into the same frozen mess. The real cause of the ratchet
  // was an unbounded layout (charge repulsion with no restoring force); it's
  // fixed properly by forceCenterPull (a weak spring to the origin → a real
  // equilibrium the live sim settles back to after any reheat). So the sim
  // stays alive and only USER drags pin nodes (onNodeDragEnd) — the
  // Obsidian/d3 model.
  const handleEngineStop = useCallback(() => {
    const fg = fgRef.current;
    if (!fg) return;
    if (!fittedRef.current) {
      fg.zoomToFit(400, 60);
      fittedRef.current = true;
      // Capture the FITTED zoom (after the 400ms animation) as the relZoom=1
      // anchor — the whole-graph overview. Until then relZoom defaults to 1, so
      // a graph that never settles still gets the overview LOD. Then run the
      // first cull + force one repaint so the overview opens decluttered.
      setTimeout(() => {
        const live = fgRef.current;
        if (!live) return;
        const z = live.zoom();
        const c = live.centerAt();
        if (z) {
          fitZoomRef.current = z;
          transformRef.current = { k: z, cx: c?.x ?? 0, cy: c?.y ?? 0 };
          recomputeCull();
          setCullEpoch((x) => x + 1);
        }
      }, 450);
    }
  }, [recomputeCull]);

  // node.group is assigned at data ingest (use-graph-data.ts), so it travels
  // with the node and this stays a pure filter.
  const visibleNodes = useMemo(
    () => nodes.filter((n) => !hidden.has(n.uri)),
    [nodes, hidden],
  );
  // Layout effort scales with the rendered node count (see layoutTier).
  const tier = useMemo(() => layoutTier(visibleNodes.length), [visibleNodes.length]);
  const visibleEdges = useMemo(
    () =>
      edges.filter(
        (e) => !hidden.has(endpointUri(e.source)) && !hidden.has(endpointUri(e.target)),
      ),
    [edges, hidden],
  );

  // Node degree (visible edges) — drives node sizing AND which labels surface
  // first at overview zoom (hubs before leaves) via the paint order below.
  const degree = useMemo(() => {
    const d = new Map<string, number>();
    for (const e of visibleEdges) {
      const s = endpointUri(e.source);
      const t = endpointUri(e.target);
      d.set(s, (d.get(s) ?? 0) + 1);
      d.set(t, (d.get(t) ?? 0) + 1);
    }
    return d;
  }, [visibleEdges]);

  // Descending degree list backs the LOD count→floor mapping; both it and the
  // degree map are mirrored to refs so the ref-only visibility predicates +
  // recomputeCull can read them. Recompute the floor when the distribution
  // changes (a filter/expand) so the overview stays calibrated.
  const sortedDegrees = useMemo(() => [...degree.values()].sort((a, b) => b - a), [degree]);
  degreeRef.current = degree;
  sortedDegreesRef.current = sortedDegrees;
  // Cull + LOD only engage past a node-count threshold; smaller graphs show
  // everything at every zoom (mirrors layoutTier's count-gating).
  const cullActive = visibleNodes.length > CULL_MIN_NODES;
  useEffect(() => {
    recomputeCull();
  }, [sortedDegrees, recomputeCull]);

  // Paint hubs FIRST so a high-degree node's label claims its collision box
  // before a leaf's (the label cull is first-come, in onRenderFramePre order).
  // Node order doesn't affect the simulation — react-force-graph treats nodes
  // as a set — only paint/label priority.
  const graphData = useMemo(
    () => ({
      nodes: [...visibleNodes].sort(
        (a, b) => (degree.get(b.uri) ?? 0) - (degree.get(a.uri) ?? 0),
      ) as RenderNode[],
      links: visibleEdges as RenderEdge[],
    }),
    [visibleNodes, visibleEdges, degree],
  );

  // Adjacency (uri → neighbor uris) for hover/selection neighbor-highlighting.
  // Cheap O(E) rebuild only when the visible edge set changes.
  const adjacency = useMemo(() => {
    const adj = new Map<string, Set<string>>();
    const link = (a: string, b: string) => {
      let s = adj.get(a);
      if (!s) adj.set(a, (s = new Set()));
      s.add(b);
    };
    for (const e of visibleEdges) {
      const s = endpointUri(e.source);
      const t = endpointUri(e.target);
      link(s, t);
      link(t, s);
    }
    return adj;
  }, [visibleEdges]);

  // Hover takes precedence over selection for the highlight focus, so moving the
  // pointer previews a node's neighborhood without committing a selection. When
  // neither is active, nothing dims (the plain overview).
  const focusUri = hovered ?? selected;
  adjacencyRef.current = adjacency;
  focusUriRef.current = focusUri;
  const isRelated = useCallback(
    (uri: string) => !focusUri || uri === focusUri || (adjacency.get(focusUri)?.has(uri) ?? false),
    [focusUri, adjacency],
  );

  // ── Visibility predicates: O(1), ref-only, so force-graph can call them once
  //    per element per frame cheaply (it skips paint AND hit-test for a false
  //    return). `forced` elements always show; otherwise a node shows iff it
  //    clears the LOD degree floor AND sits in the viewport rect; an edge shows
  //    iff both endpoints clear the floor and at least one is on-screen (so
  //    edges follow their nodes — overview = a hub skeleton). Identity bumps on
  //    cullEpoch (non-camera repaints); camera moves repaint + re-run on their
  //    own against the updated refs. ──
  const isForced = useCallback((uri: string): boolean => {
    if (uri === selectedRef.current || uri === hoveredRef.current || pinnedRef.current.has(uri)) {
      return true;
    }
    const f = focusUriRef.current;
    return f != null && (uri === f || (adjacencyRef.current.get(f)?.has(uri) ?? false));
  }, []);
  const nodeVisible = useCallback(
    (n: RenderNode): boolean => {
      if (isForced(n.uri)) return true;
      const v = viewportRef.current;
      if (!v) return true;
      if ((degreeRef.current.get(n.uri) ?? 0) < degreeFloorRef.current) return false;
      return inViewportPoint(n.x ?? 0, n.y ?? 0, v);
    },
    [cullEpoch, isForced],
  );
  const linkVisible = useCallback(
    (l: RenderEdge): boolean => {
      const src = typeof l.source === "object" ? l.source : null;
      const tgt = typeof l.target === "object" ? l.target : null;
      if (!src || !tgt) return true;
      const f = focusUriRef.current;
      if (f != null && (src.uri === f || tgt.uri === f)) return true; // incident always
      const floor = degreeFloorRef.current;
      if ((degreeRef.current.get(src.uri) ?? 0) < floor) return false;
      if ((degreeRef.current.get(tgt.uri) ?? 0) < floor) return false;
      const v = viewportRef.current;
      if (!v) return true;
      return inViewportPoint(src.x ?? 0, src.y ?? 0, v) || inViewportPoint(tgt.x ?? 0, tgt.y ?? 0, v);
    },
    [cullEpoch],
  );

  // Accepted-label boxes for the greedy collision cull in paintNode's label
  // step (reset each frame in onRenderFramePre). A non-forced label whose box
  // overlaps one already drawn this frame is skipped — so labels never overlap.
  const labelBoxes = useRef<Array<{ x: number; y: number; w: number; h: number }>>([]);
  const overlaps = useCallback((b: { x: number; y: number; w: number; h: number }) => {
    for (const o of labelBoxes.current) {
      if (b.x < o.x + o.w && b.x + b.w > o.x && b.y < o.y + o.h && b.y + b.h > o.y) return true;
    }
    return false;
  }, []);

  // Install/remove the clustering forces on the three STATE inputs only — not
  // on graphData. force-graph already re-feeds nodes + reheats to alpha(1) on
  // every data change, so depending on graphData here would double-reheat and
  // jolt the layout on every selection/filter. Cleanup nulls the forces so a
  // StrictMode double-mount stays symmetric.
  //
  // While clustering: forceCluster pulls each group together, forceCollide
  // keeps nodes spaced (so clumps aren't cramped), and the global charge is
  // softened so separate clusters sit closer together rather than flying
  // apart. All restored/removed when clustering is off.
  // Install the restoring centering spring once, on mount. Always active
  // (clustered or not) so the layout has a bounded equilibrium and can't
  // ratchet outward on a reheat — the root-cause fix that lets us drop
  // pin-on-settle and keep a live simulation.
  useEffect(() => {
    const fg = fgRef.current;
    if (!fg) return;
    fg.d3Force("centerPull", forceCenterPull() as never);
    return () => {
      fg.d3Force("centerPull", null);
    };
  }, []);

  useEffect(() => {
    const fg = fgRef.current;
    if (!fg) return;
    const on = clustered; // forceCluster (centroid pass, O(n)) — fine at every tier
    const collideOn = clustered && tier.collide; // forceCollide (O(n²)) only ≤1200
    fg.d3Force("cluster", on ? (forceCluster(CLUSTER_STRENGTH) as never) : null);
    fg.d3Force("collide", collideOn ? (forceCollide(COLLIDE_RADIUS) as never) : null);
    const charge = fg.d3Force("charge") as { strength?: (v: number) => unknown } | undefined;
    charge?.strength?.(on ? CHARGE_STRENGTH : DEFAULT_CHARGE);
    // The sim is live (no pin-on-settle): just reheat to a low alpha and let
    // forceCenterPull + the changed forces re-settle. User pins survive (their
    // fx/fy are reconciled by the pin-sync effect). Gate on the LIVE `frozen`
    // flag — not a fresh media query — so a later reheat (cluster-toggle/expand)
    // by a now-resumed reduced-motion user takes effect instead of staying
    // permanently blocked.
    if (!frozenRef.current) fg.d3ReheatSimulation();
    return () => {
      fg.d3Force("cluster", null);
      fg.d3Force("collide", null);
    };
  }, [clustered, tier.collide]);

  // Reconcile node physics to the `pinned` Set. A pinned node is fixed at its
  // current spot (fx/fy = x/y) so it survives relayout; an unpinned node is
  // released (fx/fy = undefined) so the live sim can move it. This is what makes
  // a context-menu / detail-panel Pin actually FREEZE the node and Unpin
  // actually RELEASE it — a drag already sets fx/fy (onNodeDragEnd) before
  // adding to the Set, but the menu/panel toggles only ever touched the Set.
  // Without this, removing pin-on-settle left those two paths cosmetic. The
  // node objects are the live ones force-graph mutates in place, so x/y are
  // current here. Skip the reheat on the first run (the sim is already warming
  // up on mount); on later pin/unpin, reheat so the layout settles around it.
  const pinSyncInit = useRef(true);
  useEffect(() => {
    for (const n of nodesRef.current) {
      if (pinned.has(n.uri)) {
        n.fx = n.x;
        n.fy = n.y;
      } else {
        n.fx = undefined;
        n.fy = undefined;
      }
    }
    if (pinSyncInit.current) {
      pinSyncInit.current = false;
      return;
    }
    if (!frozenRef.current) fgRef.current?.d3ReheatSimulation();
  }, [pinned]);

  const paintNode = useCallback(
    (n: RenderNode, ctx: CanvasRenderingContext2D, scale: number) => {
      const x = n.x || 0;
      const y = n.y || 0;
      const isSelected = n.uri === selected;
      const isPinned = pinned.has(n.uri);
      const related = isRelated(n.uri);
      const deg = degree.get(n.uri) ?? 0;
      // Degree-driven RADIUS (topology as the primary channel) clamped to a
      // screen-pixel range, then back to graph units, so a node is a
      // constant-ish on-screen size at any zoom while hubs read larger.
      const baseR = NODE_BASE_R + Math.sqrt(deg) * DEGREE_R_GAIN;
      const r = Math.max(NODE_MIN_R_PX, Math.min(NODE_MAX_R_PX, baseR * scale)) / scale;
      // Cluster membership re-tints the node outline; selection (teal) always
      // wins; ungrouped nodes keep their kind stroke.
      const groupStroke = clustered && n.group ? groupColor(n.group, colors.cat) : null;
      // Far-overview LOD (band 0, large graphs only): every kind draws as a
      // plain circle — the rounded-square/dashed-ring kind silhouettes are
      // illegible at that scale and redundant with the legend.
      const simplified = cullActive && lodBandRef.current === 0;

      ctx.save();
      // Hover/selection neighbor-highlight: dim everything not adjacent to the
      // focused node so its neighborhood pops out of a dense graph.
      if (focusUri && !related) ctx.globalAlpha = 0.18;

      // Kind → fill + default stroke + dash. Shape (traceNode) carries the kind:
      // document = circle, table = rounded square, file = dashed-ring circle.
      let fill: string;
      let kindStroke: string;
      let dashed = false;
      switch (n.kind) {
        case "table":
          fill = colors.surface;
          kindStroke = colors.foreground;
          break;
        case "file":
          fill = colors.background; // hollow ring look
          kindStroke = colors.foregroundMuted;
          dashed = true;
          break;
        default: // document
          fill = colors.surfaceMuted;
          kindStroke = colors.foreground;
          break;
      }
      if (simplified) dashed = false; // plain circle at far overview
      ctx.beginPath();
      traceNode(ctx, simplified ? "document" : n.kind, x, y, r);
      ctx.fillStyle = fill;
      ctx.fill();
      ctx.strokeStyle = isSelected ? colors.primary : (groupStroke ?? kindStroke);
      ctx.lineWidth = isSelected ? 2.5 : 1.5;
      ctx.setLineDash(dashed ? [3, 2] : []);
      ctx.stroke();
      ctx.setLineDash([]);

      // Selected node: a glowing TEAL halo ring so the selection is
      // unmistakable (the inner border alone read too subtly).
      if (isSelected) {
        const pad = 4 / scale;
        ctx.save();
        ctx.shadowColor = colors.primary;
        ctx.shadowBlur = 12;
        ctx.strokeStyle = colors.primary;
        ctx.lineWidth = 2;
        ctx.beginPath();
        ctx.arc(x, y, r + pad, 0, 2 * Math.PI);
        ctx.stroke();
        ctx.restore();
      }

      // Pinned: a small ORANGE marker dot — the one place orange survives (a
      // user-applied pin, never selection), so the two signals never collide.
      if (isPinned) {
        const m = Math.max(2 / scale, r * 0.32);
        ctx.beginPath();
        ctx.arc(x + r * 0.72, y - r * 0.72, m, 0, 2 * Math.PI);
        ctx.fillStyle = colors.accent;
        ctx.fill();
      }

      // Label — readability rules synthesized from Obsidian (zoom restraint),
      // sigma/Cytoscape (declutter + rendered-size gate), Gephi (degree
      // priority), G6/yFiles (pill background):
      //  • constant ON-SCREEN size: divide the graph-space font by the zoom, so
      //    labels don't balloon when a small graph fits-to-view zoomed-in;
      //  • restraint: forced (selected/hovered/pinned/focus-neighbor) always;
      //    otherwise only when the node's ON-SCREEN radius is big enough
      //    (degree-driven → hubs label first, leaves as you zoom in), under a
      //    per-frame hard cap, and in focus mode only the focused neighborhood;
      //  • collision culling: a non-forced label that would overlap one already
      //    drawn this frame is skipped (labelBoxes resets in onRenderFramePre);
      //  • translucent pill behind the text for legibility over edges/nodes.
      const forced = isSelected || n.uri === hovered || isPinned || (focusUri != null && related);
      const rPx = r * scale;
      const wantLabel =
        forced ||
        (focusUri == null && rPx >= LABEL_MIN_NODE_PX && labelCountRef.current < LABEL_CAP);
      if (wantLabel && n.name) {
        const fontSize = 12 / scale;
        const lp = 3 / scale;
        ctx.font = `${fontSize}px ${fontSans}`;
        // Title as-authored — never .toUpperCase() (corrupts acronyms/non-Latin).
        // Truncate harder at overview/mid LOD bands, full 24 chars zoomed in.
        const maxLen = cullActive && lodBandRef.current < 3 ? 12 : 24;
        const label = n.name.length > maxLen ? n.name.slice(0, maxLen) + "…" : n.name;
        const w = ctx.measureText(label).width;
        const ly = y + r + lp;
        const box = { x: x - w / 2 - lp, y: ly, w: w + lp * 2, h: fontSize + lp * 2 };
        if (forced || !overlaps(box)) {
          labelBoxes.current.push(box);
          if (!forced) labelCountRef.current++;
          ctx.globalAlpha = 0.82;
          ctx.fillStyle = colors.background;
          ctx.fillRect(box.x, box.y, box.w, box.h);
          ctx.globalAlpha = 1;
          ctx.textAlign = "center";
          ctx.textBaseline = "top";
          ctx.fillStyle = isSelected ? colors.primary : colors.foreground;
          ctx.fillText(label, x, ly + lp);
        }
      }
      ctx.restore();
    },
    [colors, selected, pinned, clustered, hovered, focusUri, isRelated, degree, overlaps, fontSans, cullActive],
  );

  const paintLink = useCallback(
    (l: RenderEdge, ctx: CanvasRenderingContext2D) => {
      const src = typeof l.source === "object" ? l.source : null;
      const tgt = typeof l.target === "object" ? l.target : null;
      if (!src || !tgt) return;
      const dash = RELATION_DASH[l.relation];
      const structural = RELATION_CLASS[l.relation] === "structural";
      // Edges touching the focused node (hovered, else selected) light up
      // (teal + thicker); the rest dim so the focused node's connections stand
      // out of a dense graph. No shadowBlur — a per-pixel gaussian blur is the
      // costliest canvas op and a focused mega-hub would queue dozens of them
      // per frame; teal + weight already reads as "incident".
      const incident = focusUri != null && (src.uri === focusUri || tgt.uri === focusUri);
      ctx.save();
      ctx.beginPath();
      ctx.moveTo(src.x || 0, src.y || 0);
      ctx.lineTo(tgt.x || 0, tgt.y || 0);
      if (incident) {
        ctx.strokeStyle = colors.primary;
        ctx.lineWidth = 2.5;
      } else {
        // Structural ties (depends_on/implements/derived_from/attached_to) read
        // darker + thicker; associative ties (references/related_to/links_to)
        // muted + thinner — color/weight the primary channel, dash the secondary.
        ctx.strokeStyle = structural ? colors.foreground : colors.foregroundMuted;
        ctx.lineWidth = structural ? 1.6 : 1.1;
        if (focusUri != null) ctx.globalAlpha = 0.15;
      }
      ctx.setLineDash(dash);
      ctx.stroke();
      ctx.restore();
    },
    [colors, focusUri],
  );

  // Arrowheads inherit their edge's encoding (teal when incident, else
  // structural/associative) instead of a single uniform gray, so direction
  // reads with the same vocabulary as the line.
  const arrowColor = useCallback(
    (l: RenderEdge) => {
      const src = typeof l.source === "object" ? l.source : null;
      const tgt = typeof l.target === "object" ? l.target : null;
      const incident = focusUri != null && (src?.uri === focusUri || tgt?.uri === focusUri);
      if (incident) return colors.primary;
      return RELATION_CLASS[l.relation] === "structural" ? colors.foreground : colors.foregroundMuted;
    },
    [colors, focusUri],
  );

  // Single click selects; a second click on the same node within 250 ms
  // expands its neighborhood (the standard graph gesture). Navigation moved to
  // the context menu / detail panel so double-click no longer yanks you off the
  // canvas. Emulated here because react-force-graph-2d has no onNodeDoubleClick.
  const lastClick = useRef<{ uri: string; at: number } | null>(null);
  const handleNodeClick = useCallback(
    (n: RenderNode) => {
      const prev = lastClick.current;
      if (prev && prev.uri === n.uri && Date.now() - prev.at < 250) {
        lastClick.current = null;
        onExpand(n);
        return;
      }
      lastClick.current = { uri: n.uri, at: Date.now() };
      onSelect(n.uri);
    },
    [onSelect, onExpand],
  );

  const handleBackgroundClick = useCallback(() => {
    onSelect(undefined);
  }, [onSelect]);

  const handleNodeRightClick = useCallback(
    (n: RenderNode, ev: MouseEvent) => {
      ev.preventDefault(); // the floating menu is wired in the page shell
      onContextMenu(n, ev.clientX, ev.clientY);
    },
    [onContextMenu],
  );

  return (
    <div
      ref={wrapRef}
      className="absolute inset-0"
      role="img"
      aria-label={`Knowledge graph: ${nodes.length} nodes, ${edges.length} edges`}
    >
      <div className="absolute top-3 left-3 z-[var(--z-raised)] flex gap-1">
        <CanvasButton
          onClick={() => setClustered((c) => !c)}
          label={clustered ? "Hide clusters" : "Show clusters"}
          icon={Boxes}
          active={clustered}
        />
        <CanvasButton
          onClick={() => setFrozen((f) => !f)}
          label={frozen ? "Resume" : "Freeze"}
          icon={frozen ? Play : Pause}
        />
        <CanvasButton
          onClick={() => fgRef.current?.zoomToFit(400, 60)}
          label="Fit"
          icon={Maximize2}
        />
        <CanvasButton
          onClick={() => fgRef.current?.zoom((fgRef.current?.zoom() || 1) * 0.8, 200)}
          label="Zoom out"
          icon={Minus}
        />
        <CanvasButton
          onClick={() => fgRef.current?.zoom((fgRef.current?.zoom() || 1) * 1.25, 200)}
          label="Zoom in"
          icon={Plus}
        />
      </div>
      <GraphLegend />
      <ForceGraph2D
        ref={fgRef as never}
        graphData={graphData as never}
        width={size.w || undefined}
        height={size.h || undefined}
        backgroundColor={colors.background}
        // Clamp the zoom range. Without an upper bound, zooming in keeps
        // scaling the graph-unit decorations (selection halo, arrowheads, edge
        // spacing) until the view falls apart; the lower bound stops zooming out
        // into an unusable speck.
        minZoom={0.2}
        maxZoom={4}
        nodeId="uri"
        linkSource="source"
        linkTarget="target"
        // Viewport culling + LOD: force-graph evaluates these once per element
        // per frame and skips BOTH paint and hit-test for a false return, while
        // the d3 sim keeps the full set — so this is a pure paint/LOD win that
        // never disturbs layout. Disabled (always-true) below the count gate.
        nodeVisibility={cullActive ? (nodeVisible as never) : true}
        linkVisibility={cullActive ? (linkVisible as never) : true}
        // onZoom fires continuously through a wheel/drag/programmatic zoom and
        // each fire repaints, so updating the refs here is enough — the last
        // frame paints the settled cull/LOD. No setState in these handlers:
        // force-graph can fire them synchronously during its own render, and a
        // setState there warns "update while rendering". Idle re-culls that DO
        // need a forced repaint (resize, fit-capture) bump cullEpoch from async
        // callbacks instead.
        onZoom={(t) => {
          transformRef.current = { k: t.k, cx: t.x, cy: t.y };
          recomputeCull();
        }}
        nodeCanvasObject={paintNode as never}
        linkCanvasObject={paintLink as never}
        linkDirectionalArrowLength={5}
        linkDirectionalArrowRelPos={1}
        linkDirectionalArrowColor={arrowColor as never}
        warmupTicks={tier.warmup}
        cooldownTicks={frozen ? 0 : tier.cooldownTicks}
        // Steadier, quicker settle: more friction + faster cooling so the
        // layout reaches equilibrium in fewer ticks and overshoots less
        // (less jiggle on a small/medium graph).
        d3VelocityDecay={0.5}
        d3AlphaDecay={0.04}
        onRenderFramePre={() => {
          // Reset the per-frame label accumulators before nodes paint: the
          // collision boxes and the non-forced label hard-cap counter.
          labelBoxes.current = [];
          labelCountRef.current = 0;
        }}
        onEngineStop={handleEngineStop}
        onNodeHover={(n) => setHovered((n as RenderNode | undefined)?.uri)}
        onNodeClick={handleNodeClick as never}
        onBackgroundClick={handleBackgroundClick}
        onNodeRightClick={handleNodeRightClick as never}
        onNodeDragEnd={(n) => {
          // Drag-release pins the node where the user dropped it (fx/fy fix the
          // position); the page records it in `pinned` so the marker shows and
          // it survives relayout.
          const node = n as unknown as RenderNode;
          node.fx = node.x;
          node.fy = node.y;
          onPinNode(node.uri);
        }}
      />
    </div>
  );
});

/** Static legend: node kinds + the structural-vs-associative edge encoding.
 *  Collapsible so it never competes with the graph; tokens only (no hex). */
function GraphLegend() {
  const [open, setOpen] = useState(true);
  return (
    <div className="absolute bottom-3 right-3 z-[var(--z-raised)]">
      {open ? (
        <div className="rounded-[var(--radius-md)] border border-border bg-surface/90 backdrop-blur px-3 py-2 shadow-sm text-[11px] text-foreground-muted">
          <div className="flex items-center justify-between gap-4 mb-1.5">
            <span className="font-medium text-foreground">Legend</span>
            <button
              type="button"
              onClick={() => setOpen(false)}
              aria-label="Hide legend"
              className="text-foreground-muted hover:text-foreground cursor-pointer rounded-[var(--radius-sm)] focus:outline-none focus-visible:ring-2 focus-visible:ring-ring"
            >
              <Minus className="h-3 w-3" aria-hidden />
            </button>
          </div>
          <ul className="space-y-1">
            <li className="flex items-center gap-2">
              <span className="inline-block h-3 w-3 border border-foreground rounded-full bg-surface-muted" aria-hidden />
              Document
            </li>
            <li className="flex items-center gap-2">
              <span className="inline-block h-3 w-3 border border-foreground rounded-[var(--radius-xs)] bg-surface" aria-hidden />
              Table
            </li>
            <li className="flex items-center gap-2">
              <span className="inline-block h-3 w-3 border border-dashed border-foreground-muted rounded-full" aria-hidden />
              File
            </li>
            <li className="flex items-center gap-2 pt-1">
              <svg width="22" height="6" aria-hidden className="text-foreground"><line x1="0" y1="3" x2="22" y2="3" stroke="currentColor" strokeWidth="1.6" /></svg>
              Structural
            </li>
            <li className="flex items-center gap-2">
              <svg width="22" height="6" aria-hidden><line x1="0" y1="3" x2="22" y2="3" stroke="currentColor" strokeWidth="1.1" strokeDasharray="3 3" /></svg>
              Associative
            </li>
          </ul>
        </div>
      ) : (
        <button
          type="button"
          onClick={() => setOpen(true)}
          aria-label="Show legend"
          title="Legend"
          className="inline-flex items-center justify-center h-8 w-8 rounded-[var(--radius-md)] border border-border bg-surface shadow-sm text-foreground-muted hover:text-foreground hover:bg-surface-hover transition-colors cursor-pointer focus:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2 focus-visible:ring-offset-background"
        >
          <Boxes className="h-3 w-3" aria-hidden />
        </button>
      )}
    </div>
  );
}

function CanvasButton({
  onClick,
  label,
  icon: Icon,
  active = false,
}: {
  onClick: () => void;
  label: string;
  icon: React.ComponentType<{ className?: string; "aria-hidden"?: boolean }>;
  /** Renders a pressed/accent state for toggle buttons. */
  active?: boolean;
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      title={label}
      aria-label={label}
      aria-pressed={active}
      className={`inline-flex items-center justify-center h-8 w-8 rounded-[var(--radius-md)] border shadow-sm focus:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2 focus-visible:ring-offset-background transition-colors cursor-pointer ${
        active
          ? "bg-surface-selected border-primary text-surface-selected-foreground"
          : "bg-surface border-border text-foreground-muted hover:text-foreground hover:bg-surface-hover"
      }`}
    >
      <Icon className="h-3 w-3" aria-hidden />
    </button>
  );
}
