// frontend/src/components/graph/GraphCanvas.tsx
import { forwardRef, useCallback, useEffect, useImperativeHandle, useMemo, useRef, useState } from "react";
import ForceGraph2D, { type ForceGraphMethods } from "react-force-graph-2d";
import { Pause, Play, Maximize2, Minus, Plus, Boxes } from "lucide-react";
import { useTheme } from "@/hooks/use-theme";
import {
  RELATION_DASH,
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
  CLUSTER_STRENGTH,
  COLLIDE_RADIUS,
  CHARGE_STRENGTH,
  DEFAULT_CHARGE,
} from "./cluster";

/** True when the OS asks for reduced motion — used to snap the force layout
 *  instead of animating it (the canvas rAF sim is unreachable by CSS). */
function prefersReducedMotion(): boolean {
  return typeof window !== "undefined" && window.matchMedia?.("(prefers-reduced-motion: reduce)").matches;
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

// react-force-graph-2d's typed ForceGraphMethods omits graphData(); the live
// node array (with simulation-mutated x/y/fx/fy) is reachable through it at
// runtime. Narrow cast so we can pin / read positions without `any`.
type FgData = { graphData: () => { nodes: RenderNode[]; links: RenderEdge[] } };
const liveNodes = (fg: ForceGraphMethods | undefined): RenderNode[] => {
  const gd = (fg as unknown as Partial<FgData> | undefined)?.graphData;
  return typeof gd === "function" ? gd().nodes : [];
};

const NODE_SIZE = 16;
/** Above this zoom every node labels; below it only hovered/selected/pinned do,
 *  so a 600-node graph reads as glyphed squares instead of a smear of names. */
const LABEL_ALL_ZOOM = 1.2;
/** Node draw size is CLAMPED to a screen-pixel range so it neither balloons
 *  when a small graph fits-to-view (zoomed way in) nor vanishes on a big graph
 *  (zoomed out). Drawn at `clamp(baseSize·zoom, MIN, MAX) / zoom` graph units →
 *  a constant-ish on-screen size that still grows a little with degree. */
const NODE_MIN_PX = 7;
const NODE_MAX_PX = 26;

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
  // Latest user-pin set, read inside effects without making them depend on it
  // (so a pin/unpin never reheats the simulation).
  const pinnedRef = useRef(pinned);
  pinnedRef.current = pinned;
  const { theme } = useTheme();
  const [colors, setColors] = useState<GraphColors>(() => readColors());
  // Snap (don't animate) the layout when the OS asks for reduced motion.
  const [frozen, setFrozen] = useState(() => prefersReducedMotion());
  const [clustered, setClustered] = useState(true);
  const [hovered, setHovered] = useState<string>();
  const [size, setSize] = useState({ w: 0, h: 0 });

  // react-force-graph-2d defaults to window.innerWidth/Height with no resize
  // handling — measure the actual wrapper so the canvas fills its grid cell and
  // reflows when the sidebar/detail panel toggles.
  useEffect(() => {
    const el = wrapRef.current;
    if (!el) return;
    const ro = new ResizeObserver(([e]) =>
      setSize({ w: e.contentRect.width, h: e.contentRect.height }),
    );
    ro.observe(el);
    return () => ro.disconnect();
  }, []);

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

  // On settle: PIN every node where it landed (fx/fy = x/y), then fit once.
  //
  // Pinning is the cure for the "click makes nodes drift apart" bug: d3-drag
  // reheats the simulation on every click (a click is a zero-distance drag),
  // and the cluster/collide forces have no stable equilibrium, so each reheat
  // nudges nodes farther out — ratcheting. A pinned node has x=fx every tick,
  // so no reheat (click, hover, expand) can move it. This runs on EVERY settle,
  // so a deliberate re-layout (cluster toggle / expand) re-pins afterward.
  // User pins (drag/context-menu) and auto-pins are both fx/fy here; they're
  // told apart by the `pinned` Set, which the cluster re-layout consults.
  const handleEngineStop = useCallback(() => {
    const fg = fgRef.current;
    if (!fg) return;
    for (const n of liveNodes(fg)) {
      n.fx = n.x;
      n.fy = n.y;
    }
    if (!fittedRef.current) {
      fg.zoomToFit(400, 60);
      fittedRef.current = true;
    }
  }, []);

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

  const graphData = useMemo(
    () => ({ nodes: visibleNodes as RenderNode[], links: visibleEdges as RenderEdge[] }),
    [visibleNodes, visibleEdges],
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

  // Node degree (visible edges) — drives both node sizing and which labels
  // surface first at overview zoom (hubs before leaves).
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

  // Hover takes precedence over selection for the highlight focus, so moving the
  // pointer previews a node's neighborhood without committing a selection. When
  // neither is active, nothing dims (the plain overview).
  const focusUri = hovered ?? selected;
  const isRelated = useCallback(
    (uri: string) => !focusUri || uri === focusUri || (adjacency.get(focusUri)?.has(uri) ?? false),
    [focusUri, adjacency],
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
  useEffect(() => {
    const fg = fgRef.current;
    if (!fg) return;
    const on = clustered; // forceCluster (centroid pass, O(n)) — fine at every tier
    const collideOn = clustered && tier.collide; // forceCollide (O(n²)) only ≤1200
    fg.d3Force("cluster", on ? (forceCluster(CLUSTER_STRENGTH) as never) : null);
    fg.d3Force("collide", collideOn ? (forceCollide(COLLIDE_RADIUS) as never) : null);
    const charge = fg.d3Force("charge") as { strength?: (v: number) => unknown } | undefined;
    charge?.strength?.(on ? CHARGE_STRENGTH : DEFAULT_CHARGE);
    // Release auto-pinned positions so the changed force can actually re-lay
    // the graph out — without this, a cluster toggle does nothing visible
    // because pin-on-settle has fixed every node. User pins (drag / context
    // menu) stay put. Then reheat so the new force takes effect.
    for (const n of liveNodes(fg)) {
      if (!pinnedRef.current.has(n.uri)) {
        n.fx = undefined;
        n.fy = undefined;
      }
    }
    // Don't reheat on a reduced-motion preference — the freeze effect owns
    // pause/resume; reheating from alpha=1 is the cost Freeze exists to avoid.
    if (!prefersReducedMotion()) fg.d3ReheatSimulation();
    return () => {
      fgRef.current?.d3Force("cluster", null);
      fgRef.current?.d3Force("collide", null);
    };
  }, [clustered, tier.collide]);

  const paintNode = useCallback(
    (n: RenderNode, ctx: CanvasRenderingContext2D, scale: number) => {
      const x = n.x || 0;
      const y = n.y || 0;
      const isSelected = n.uri === selected;
      const isPinned = pinned.has(n.uri);
      const related = isRelated(n.uri);
      const deg = degree.get(n.uri) ?? 0;
      // Degree sizing (hubs read larger) clamped to a screen-pixel range, then
      // converted back to graph units, so the node is a constant-ish on-screen
      // size at any zoom — never ballooning on a small fit-to-view graph nor
      // shrinking to nothing on a big one.
      const baseSz = NODE_SIZE + Math.min(8, deg);
      const sz = Math.max(NODE_MIN_PX, Math.min(NODE_MAX_PX, baseSz * scale)) / scale;
      // Cluster membership re-tints the node ring (keeping the kind-based
      // fill + glyph so document/table/file stay distinguishable). Selection
      // always wins; ungrouped nodes keep their kind stroke.
      const groupStroke = clustered && n.group ? groupColor(n.group, colors.cat) : null;

      ctx.save();
      // Hover/selection neighbor-highlight: dim everything not adjacent to the
      // focused node so its neighborhood pops out of a dense graph.
      if (focusUri && !related) ctx.globalAlpha = 0.18;

      ctx.beginPath();
      ctx.rect(x - sz / 2, y - sz / 2, sz, sz);
      switch (n.kind) {
        case "document":
          ctx.fillStyle = colors.surfaceMuted;
          ctx.strokeStyle = isSelected ? colors.accent : (groupStroke ?? colors.foreground);
          ctx.lineWidth = isSelected ? 2.5 : 1.5;
          ctx.setLineDash([]);
          break;
        case "table":
          ctx.fillStyle = colors.surface;
          ctx.strokeStyle = isSelected ? colors.accent : (groupStroke ?? colors.accent);
          ctx.lineWidth = isSelected ? 2.5 : 1.5;
          ctx.setLineDash([]);
          break;
        case "file":
          ctx.fillStyle = "transparent";
          ctx.strokeStyle = isSelected ? colors.accent : (groupStroke ?? colors.foregroundMuted);
          ctx.lineWidth = isSelected ? 2.5 : 1;
          ctx.setLineDash([3, 2]);
          break;
      }
      ctx.fill();
      ctx.stroke();
      ctx.setLineDash([]);

      // Selected node: a glowing accent halo ring so the selection is
      // unmistakable (the inner accent border alone read too subtly).
      if (isSelected) {
        const pad = 5;
        ctx.save();
        ctx.shadowColor = colors.accent;
        ctx.shadowBlur = 12;
        ctx.strokeStyle = colors.accent;
        ctx.lineWidth = 2;
        ctx.strokeRect(x - sz / 2 - pad, y - sz / 2 - pad, sz + pad * 2, sz + pad * 2);
        ctx.restore();
      }

      if (isPinned) {
        ctx.fillStyle = colors.accent;
        ctx.fillRect(x + sz / 2 - 3, y - sz / 2, 3, 3);
      }

      if (scale > 0.6) {
        const glyph = n.kind === "document" ? "D" : n.kind === "table" ? "T" : "F";
        // Glyph scales with the (clamped) node size so it always fits the box.
        ctx.font = `bold ${sz * 0.5}px ui-monospace, SFMono-Regular, monospace`;
        ctx.textAlign = "center";
        ctx.textBaseline = "middle";
        ctx.fillStyle = colors.foregroundMuted;
        ctx.fillText(glyph, x, y);
      }

      // Label — readability rules synthesized from Obsidian (zoom restraint),
      // sigma/Cytoscape (declutter + rendered-size gate), Gephi (degree
      // priority), G6/yFiles (pill background):
      //  • constant ON-SCREEN size: divide the graph-space font by the zoom, so
      //    labels don't balloon when a small graph fits-to-view zoomed-in;
      //  • restraint: forced (selected/hovered/pinned/focus-neighbor) always;
      //    otherwise only when zoomed in past LABEL_ALL_ZOOM or for hubs
      //    (degree ≥ 4); in focus mode only the focused neighborhood labels;
      //  • collision culling: a non-forced label that would overlap one already
      //    drawn this frame is skipped (labelBoxes resets in onRenderFramePre);
      //  • translucent pill behind the text for legibility over edges/nodes.
      const forced = isSelected || n.uri === hovered || isPinned || (focusUri != null && related);
      const wantLabel = forced || (focusUri == null && (scale > LABEL_ALL_ZOOM || deg >= 4));
      if (wantLabel && n.name) {
        const fontSize = 12 / scale;
        const lp = 3 / scale;
        ctx.font = `${fontSize}px ui-monospace, SFMono-Regular, monospace`;
        // Title as-authored — never .toUpperCase() (corrupts acronyms/non-Latin).
        const label = n.name.length > 24 ? n.name.slice(0, 24) + "…" : n.name;
        const w = ctx.measureText(label).width;
        const ly = y + sz / 2 + lp;
        const box = { x: x - w / 2 - lp, y: ly, w: w + lp * 2, h: fontSize + lp * 2 };
        if (forced || !overlaps(box)) {
          labelBoxes.current.push(box);
          ctx.globalAlpha = 0.82;
          ctx.fillStyle = colors.background;
          ctx.fillRect(box.x, box.y, box.w, box.h);
          ctx.globalAlpha = 1;
          ctx.textAlign = "center";
          ctx.textBaseline = "top";
          ctx.fillStyle = isSelected ? colors.accent : colors.foreground;
          ctx.fillText(label, x, ly + lp);
        }
      }
      ctx.restore();
    },
    [colors, selected, pinned, clustered, hovered, focusUri, isRelated, degree, overlaps],
  );

  const paintLink = useCallback(
    (l: RenderEdge, ctx: CanvasRenderingContext2D) => {
      const src = typeof l.source === "object" ? l.source : null;
      const tgt = typeof l.target === "object" ? l.target : null;
      if (!src || !tgt) return;
      const dash = RELATION_DASH[l.relation];
      const isAccent = l.relation === "implements" || l.relation === "derived_from";
      // Edges touching the focused node (hovered, else selected) light up
      // (accent + thicker + glow); the rest dim so the focused node's
      // connections stand out of a dense graph.
      const incident = focusUri != null && (src.uri === focusUri || tgt.uri === focusUri);
      ctx.save();
      ctx.beginPath();
      ctx.moveTo(src.x || 0, src.y || 0);
      ctx.lineTo(tgt.x || 0, tgt.y || 0);
      if (incident) {
        ctx.strokeStyle = colors.accent;
        ctx.lineWidth = 2.5;
        ctx.shadowColor = colors.accent;
        ctx.shadowBlur = 6;
      } else {
        ctx.strokeStyle = isAccent ? colors.accent : colors.foregroundMuted;
        ctx.lineWidth = l.relation === "attached_to" ? 1 : 1.5;
        if (focusUri != null) ctx.globalAlpha = 0.15;
      }
      ctx.setLineDash(dash);
      ctx.stroke();
      ctx.restore();
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
      <div className="absolute top-3 left-3 z-10 flex gap-1">
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
        nodeCanvasObject={paintNode as never}
        linkCanvasObject={paintLink as never}
        linkDirectionalArrowLength={5}
        linkDirectionalArrowRelPos={1}
        linkDirectionalArrowColor={colors.foregroundMuted}
        warmupTicks={tier.warmup}
        cooldownTicks={frozen ? 0 : tier.cooldownTicks}
        // Steadier, quicker settle: more friction + faster cooling so the
        // layout reaches equilibrium in fewer ticks and overshoots less
        // (less jiggle on a small/medium graph).
        d3VelocityDecay={0.5}
        d3AlphaDecay={0.04}
        onRenderFramePre={() => {
          // Reset the per-frame label-collision accumulator before nodes paint.
          labelBoxes.current = [];
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
    <div className="absolute bottom-3 right-3 z-10">
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
              <span className="inline-flex h-3.5 w-3.5 items-center justify-center border border-foreground rounded-[2px] bg-surface-muted font-mono text-[7px] text-foreground-muted">D</span>
              Document
            </li>
            <li className="flex items-center gap-2">
              <span className="inline-flex h-3.5 w-3.5 items-center justify-center border border-accent rounded-[2px] bg-surface font-mono text-[7px] text-foreground-muted">T</span>
              Table
            </li>
            <li className="flex items-center gap-2">
              <span className="inline-flex h-3.5 w-3.5 items-center justify-center border border-dashed border-foreground-muted rounded-[2px] font-mono text-[7px] text-foreground-muted">F</span>
              File
            </li>
            <li className="flex items-center gap-2 pt-1">
              <svg width="22" height="6" aria-hidden><line x1="0" y1="3" x2="22" y2="3" stroke="currentColor" strokeWidth="1.5" /></svg>
              Structural
            </li>
            <li className="flex items-center gap-2">
              <svg width="22" height="6" aria-hidden><line x1="0" y1="3" x2="22" y2="3" stroke="currentColor" strokeWidth="1.5" strokeDasharray="3 3" /></svg>
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
