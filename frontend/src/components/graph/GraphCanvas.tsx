import { Pin } from "lucide-react";
import { forwardRef, useEffect, useImperativeHandle, useRef, useState } from "react";
import { Button } from "@/components/ui/button";
import { useTheme } from "@/hooks/use-theme";
import { cn } from "@/lib/utils";
import { categoryOf, displayName, edgeId, shortName, type GraphEdge, type GraphNode } from "./graph-types";
import { ringRadius, seedSpatialPositions, type GraphDimension, type SpatialPoint } from "./graph-spatial-layout";
import { SpatialGraphRenderer } from "./graph-spatial-renderer";
import type { ProjectedLabel } from "./graph-spatial-view";

export interface GraphCanvasHandle {
  centerOnNode(uri: string): void;
  fit(): void;
  zoomIn(): void;
  zoomOut(): void;
  rearrange(): void;
  pan(x: number, y: number): void;
  orbit(x: number, y: number): void;
}
interface Props {
  nodes: GraphNode[]; edges: GraphEdge[]; selected?: string; inspectedEdge?: GraphEdge; pinned: Set<string>;
  onSelect(uri: string | undefined): void;
  onExpand(node: GraphNode): void;
  onContextMenu(node: GraphNode, x: number, y: number): void;
  onSelectEdge?(edge: GraphEdge): void;
  onUseList?(): void;
  /** Keep focusable labels clear of the floating toolbar without shrinking the scene. */
  topInset?: number;
  /** A flat map (default) or the 3D perspective volume. */
  dimension?: GraphDimension;
  /** Hops from the focused resource; present only for a focused neighborhood. */
  hops?: Map<string, number>;
  /** Resources joined to the focus by a body link only; drawn faded. */
  weak?: Set<string>;
  /** Around a focus, the ``edgeId``s of the walk's backbone: drawn at full weight, other strong edges faded. */
  tree?: Set<string>;
  /** Whole-Vault colouring: collection (node.group) -> colour token. */
  groupTokens?: Map<string, string>;
}

function ringsFor(nodes: GraphNode[], hops: Map<string, number>, weak: Set<string> | undefined): Record<string, number> {
  const deepest = Math.max(0, ...nodes.filter(n => !weak?.has(n.uri)).map(n => hops.get(n.uri) ?? 0));
  return Object.fromEntries(nodes.map(n => [n.uri, ringRadius(hops.get(n.uri), !!weak?.has(n.uri), deepest)]));
}

export const GraphCanvas = forwardRef<GraphCanvasHandle, Props>(function GraphCanvas(props, ref) {
  const host = useRef<HTMLDivElement>(null), renderer = useRef<SpatialGraphRenderer | null>(null);
  const latest = useRef(props); latest.current = props;
  const positions = useRef(new Map<string, SpatialPoint>());
  const worker = useRef<Worker | null>(null);
  const timeout = useRef<ReturnType<typeof setTimeout> | undefined>(undefined);
  const cameraRevision = useRef(0), firstLayout = useRef(true);
  // Resources the reader dragged keep their place until Rearrange.
  const moved = useRef(new Set<string>());
  const dimension = props.dimension ?? 2;
  // A category prefix only helps when the map holds more than one.
  const showCategory = new Set(props.nodes.map(categoryOf)).size > 1;
  const [labels, setLabels] = useState<ProjectedLabel[]>([]);
  const [busy, setBusy] = useState(false), [layoutError, setLayoutError] = useState("");
  const [renderError, setRenderError] = useState("");
  const { theme } = useTheme();

  function stopWorker() { worker.current?.terminate(); worker.current = null; clearTimeout(timeout.current); }
  function arrange(fixed: string[] = [], refit = true) {
    if (!renderer.current || !latest.current.nodes.length) return;
    stopWorker(); setBusy(true); setLayoutError("");
    const startingCamera = cameraRevision.current;
    let current: Worker;
    const fail = () => { stopWorker(); setBusy(false); setLayoutError("Layout could not be calculated. Your current graph is still available."); };
    try { current = new Worker(new URL("./graph-spatial.worker.ts", import.meta.url), { type: "module" }); }
    catch { fail(); return; }
    worker.current = current;
    current.onmessage = (event: MessageEvent<{ positions?: Record<string, SpatialPoint>; error?: string }>) => {
      if (worker.current !== current || !renderer.current) return;
      if (!event.data.positions || event.data.error) { fail(); return; }
      for (const [id, point] of Object.entries(event.data.positions)) {
        if (!latest.current.pinned.has(id) && !moved.current.has(id) && [point.x, point.y, point.z].every(Number.isFinite)) positions.current.set(id, { ...point });
      }
      renderer.current.update(latest.current.nodes, latest.current.edges, positions.current);
      if (startingCamera === cameraRevision.current) {
        if (latest.current.selected) renderer.current.focus(latest.current.selected);
        else if (refit) renderer.current.fit();
      }
      stopWorker(); setBusy(false);
    };
    current.onerror = () => { if (worker.current === current) fail(); };
    timeout.current = setTimeout(() => { if (worker.current === current) fail(); }, 30000);
    current.postMessage({ nodes: latest.current.nodes.map(n => ({ id: n.uri, position: positions.current.get(n.uri)! })),
      // With rings, only the walk's backbone pulls: cross links between rings would drag a whole ring to one side.
      edges: latest.current.edges.filter(e => !latest.current.hops || !latest.current.tree || latest.current.tree.has(edgeId(e)))
        .map(e => ({ source: e.source, target: e.target })),
      pinned: [...new Set([...fixed, ...latest.current.pinned, ...moved.current])], dimensions: latest.current.dimension ?? 2,
      ...(latest.current.hops ? { rings: ringsFor(latest.current.nodes, latest.current.hops, latest.current.weak) } : {}) });
  }

  useImperativeHandle(ref, () => ({
    fit: () => { cameraRevision.current++; renderer.current?.fit(); },
    rearrange: () => { moved.current.clear(); arrange(); }, centerOnNode: uri => renderer.current?.focus(uri),
    zoomIn: () => renderer.current?.zoom(1 / 1.25), zoomOut: () => renderer.current?.zoom(1.25),
    pan: (x, y) => renderer.current?.pan(x, y), orbit: (x, y) => renderer.current?.orbit(x, y),
  }));

  useEffect(() => {
    if (!host.current) return;
    let engine: SpatialGraphRenderer;
    try {
      engine = new SpatialGraphRenderer(host.current, {
        onSelect: uri => latest.current.onSelect(uri), onEdge: edge => latest.current.onSelectEdge?.(edge),
        onContext: (node, x, y) => latest.current.onContextMenu(node, x, y),
        onLabels: setLabels, onCameraChange: () => { cameraRevision.current++; },
        onMove: uri => { moved.current.add(uri); stopWorker(); setBusy(false); },
        onContextLost: () => { setRenderError("The graphics context was lost. Use List, or reload this page to restore it."); setLabels([]); },
      }, latest.current.dimension ?? 2);
    } catch {
      setRenderError("The graph requires WebGL 2, which is unavailable in this browser. You can still explore every loaded resource in List.");
      return;
    }
    renderer.current = engine;
    engine.setTopInset(latest.current.topInset ?? 0);
    const observer = new ResizeObserver(() => engine.resize()); observer.observe(host.current);
    return () => { observer.disconnect(); stopWorker(); engine.dispose(); renderer.current = null; firstLayout.current = true; };
  }, []);

  useEffect(() => {
    const engine = renderer.current; if (!engine) return;
    const existing = [...positions.current.keys()];
    const added = props.nodes.some(node => !positions.current.has(node.uri));
    stopWorker(); setBusy(false);
    positions.current = seedSpatialPositions(props.nodes.map(n => n.uri), positions.current, props.edges, dimension);
    engine.update(props.nodes, props.edges, positions.current);
    if (firstLayout.current && props.nodes.length) { firstLayout.current = false; engine.fit(); arrange(); }
    else if (added) arrange(existing, false);
    // Selection changes appearance, never the layout or graph coordinates.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [props.nodes, props.edges]);
  useEffect(() => {
    renderer.current?.selection(props.selected, props.inspectedEdge, props.pinned);
    if (props.selected) renderer.current?.focus(props.selected);
  }, [props.selected, props.inspectedEdge, props.pinned, props.nodes, props.edges]);
  useEffect(() => { renderer.current?.theme(); }, [theme]);
  useEffect(() => { renderer.current?.setTopInset(props.topInset ?? 0); }, [props.topInset]);
  useEffect(() => { renderer.current?.setHops(props.hops, props.weak, props.tree, props.groupTokens); }, [props.hops, props.weak, props.tree, props.groupTokens]);
  // Switching between the flat map and the volume lays the map out afresh.
  const shownDimension = useRef(dimension);
  useEffect(() => {
    const engine = renderer.current;
    if (!engine || shownDimension.current === dimension) return;
    shownDimension.current = dimension;
    stopWorker(); moved.current.clear();
    engine.setDimension(dimension);
    positions.current = seedSpatialPositions(latest.current.nodes.map(n => n.uri), new Map(), latest.current.edges, dimension);
    engine.update(latest.current.nodes, latest.current.edges, positions.current);
    cameraRevision.current++; engine.fit(); arrange();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [dimension]);

  function keyboard(event: React.KeyboardEvent) {
    if (event.target !== event.currentTarget) return;
    const axis: Record<string, [number, number]> = { ArrowLeft: [-0.18, 0], ArrowRight: [0.18, 0], ArrowUp: [0, -0.18], ArrowDown: [0, 0.18] };
    if (axis[event.key]) {
      event.preventDefault();
      if (event.shiftKey || dimension === 2) renderer.current?.pan(-axis[event.key][0] * 400, -axis[event.key][1] * 400);
      else renderer.current?.orbit(...axis[event.key]);
    } else if (event.key === "+" || event.key === "=") { event.preventDefault(); renderer.current?.zoom(0.8); }
    else if (event.key === "-") { event.preventDefault(); renderer.current?.zoom(1.25); }
    else if (event.key === "Home") { event.preventDefault(); cameraRevision.current++; renderer.current?.fit(); }
    else if (event.key === "Escape") latest.current.onSelect(undefined);
  }

  return <>
    <div ref={host} data-testid="graph-canvas" style={{ position: "absolute", inset: 0 }} tabIndex={0} onKeyDown={keyboard}
      className="touch-none bg-surface outline-none focus-visible:ring-2 focus-visible:ring-inset focus-visible:ring-ring" role="img"
      aria-label={dimension === 2
        ? `Knowledge graph map: ${props.nodes.length} resources, ${props.edges.length} relationships. Arrow keys pan, plus and minus zoom, Home resets. Use List to browse all resources.`
        : `3D knowledge graph: ${props.nodes.length} resources, ${props.edges.length} relationships. Arrow keys rotate, Shift and arrows pan, plus and minus zoom, Home resets. Use List to browse all resources.`} />
    {!renderError && <div className="pointer-events-none absolute inset-0 overflow-hidden" aria-label="Visible graph labels">
      {labels.filter(label => label.y >= (props.topInset ?? 0)).map(label => {
        const node = props.nodes.find(n => n.uri === label.uri); if (!node) return null;
        return <button key={node.uri} type="button" data-graph-node={node.uri} aria-label={`Inspect ${displayName(node)}`} aria-description={node.kind} title={`${displayName(node)} · ${node.name} · ${node.kind}`}
          onClick={() => props.onSelect(node.uri)} onDoubleClick={() => props.onExpand(node)}
          style={{ left: label.x, top: label.y, transform: `translate(${(label.ax ?? -0.5) * 100}%, ${(label.ay ?? 0) * 100}%)` }}
          className={cn("pointer-events-auto absolute flex h-6 items-center gap-1 rounded-[var(--radius-xs)] px-1 text-xs text-foreground-muted [text-shadow:0_0_4px_var(--color-surface),0_0_2px_var(--color-surface)] hover:text-link focus-visible:bg-surface focus-visible:outline-2 focus-visible:outline-ring", props.selected === node.uri && "font-semibold text-link") }>
          <span className="whitespace-nowrap leading-tight">
            {showCategory && categoryOf(node) && <span className="mr-1 text-[10px] uppercase tracking-wide text-foreground-muted/80">{categoryOf(node)}</span>}
            {shortName(node)}
          </span>{props.pinned.has(node.uri) && <Pin className="h-3 w-3 shrink-0" aria-label="Position pinned" />}
        </button>;
      })}
    </div>}
    {busy && <div role="status" className="pointer-events-none absolute bottom-16 left-4 rounded-[var(--radius-sm)] border border-border bg-surface px-3 py-2 text-xs text-foreground-muted shadow-sm">Arranging the map…</div>}
    {layoutError && <div role="alert" className="absolute bottom-16 left-4 max-w-sm rounded-[var(--radius-sm)] border border-border bg-surface p-3 text-sm shadow-sm"><p>{layoutError}</p><Button variant="outline" size="sm" className="mt-2" onClick={() => arrange()}>Retry layout</Button></div>}
    {renderError && <div role="alert" className="absolute left-1/2 top-1/2 w-80 max-w-[90%] -translate-x-1/2 -translate-y-1/2 rounded-[var(--radius-md)] border border-border bg-surface p-5 text-sm shadow-sm"><p>{renderError}</p><Button variant="outline" size="sm" className="mt-3" onClick={props.onUseList}>Use List</Button></div>}
  </>;
});
