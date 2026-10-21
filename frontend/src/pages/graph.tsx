import { ArrowDown, ArrowLeft, ArrowRight, ArrowUp, Info, Maximize2, Minus, Move, Plus, RotateCcw, RotateCw, X } from "lucide-react";
import { lazy, Suspense, useCallback, useLayoutEffect, useMemo, useRef, useState, type CSSProperties } from "react";
import { useLocation, useNavigate, useParams, useSearchParams } from "react-router-dom";
import { useAccessVerification, useCurrentUser } from "@/contexts/current-user-context";
import { EmptyState } from "@/components/empty-state";
import type { GraphCanvasHandle } from "@/components/graph/GraphCanvas";
import { GraphContextMenu, type GraphMenuState } from "@/components/graph/GraphContextMenu";
import { GraphDetailPanel } from "@/components/graph/GraphDetailPanel";
import { GraphListView } from "@/components/graph/GraphListView";
import { GraphToolbar, type GraphDisplayMode } from "@/components/graph/GraphToolbar";
import { queryToView, viewToQuery } from "@/components/graph/graph-state";
import { graphCoverage } from "@/components/graph/graph-scene";
import { ALL_NODE_KINDS, ALL_RELATIONS, RELATION_LABEL, displayName, edgeStrength, kindToSegment, type GraphEdge, type GraphNode, type GraphView, type RelatedRef } from "@/components/graph/graph-types";
import { changedWithin, degreeMap, docIdFromUri, fetchNeighbors, hopDistances, mergeGraph, strongNeighborhood, useFullGraph, useNeighborhood, visibleGraph, type GraphPayload, type StrongScene } from "@/components/graph/use-graph-data";
import type { GraphDimension } from "@/components/graph/graph-spatial-layout";
import { Button } from "@/components/ui/button";
import { LoadingState } from "@/components/ui/loading-state";
import { Skeleton } from "@/components/ui/skeleton";
import { documentPreviewState } from "@/lib/document-preview-navigation";
import { cn } from "@/lib/utils";

const EMPTY: GraphPayload = { nodes: [], edges: [] };
const DIMENSION_KEY = "akb.graph.dimension";

/** The reader's last choice of flat map or 3D; a flat map unless they chose 3D. */
function storedDimension(): GraphDimension {
  try { return window.localStorage.getItem(DIMENSION_KEY) === "3" ? 3 : 2; } catch { return 2; }
}
function storeDimension(value: GraphDimension) {
  try { window.localStorage.setItem(DIMENSION_KEY, String(value)); } catch { /* the choice still applies to this visit */ }
}
const SCENE_LIMIT = 2000;
// Keep WebGL/Three.js out of the initial bundle and non-graph workspaces.
const GraphCanvas = lazy(() => import("@/components/graph/GraphCanvas").then(module => ({ default: module.GraphCanvas })));

export default function GraphPage() {
  const { name: vault = "" } = useParams<{ name: string }>();
  const user = useCurrentUser();
  const { revision } = useAccessVerification();
  return <GraphPresentation key={JSON.stringify([user?.user_id, revision, vault])} vault={vault} />;
}

function GraphPresentation({ vault }: { vault: string }) {
  const [params] = useSearchParams();
  const view = queryToView(params);
  const [mode, setMode] = useState<GraphDisplayMode>(() => window.matchMedia?.("(max-width: 767px)").matches ? "list" : "graph");
  // Traversals still isolate pending expansions, but do not reset the chosen
  // presentation (including the accessible fallback when WebGL is unavailable).
  const [dimension, setDimensionState] = useState<GraphDimension>(storedDimension);
  const setDimension = useCallback((value: GraphDimension) => { storeDimension(value); setDimensionState(value); }, []);
  return <GraphWorkspace key={JSON.stringify([view.entry, view.hops])} vault={vault} mode={mode} setMode={setMode} dimension={dimension} setDimension={setDimension} />;
}

function GraphWorkspace({ vault, mode, setMode, dimension, setDimension }: {
  vault: string; mode: GraphDisplayMode; setMode: (mode: GraphDisplayMode) => void;
  dimension: GraphDimension; setDimension: (value: GraphDimension) => void;
}) {
  const [search, setSearch] = useSearchParams();
  const navigate = useNavigate(), location = useLocation();
  const view = useMemo(() => queryToView(search), [search]);
  const setView = useCallback((next: GraphView) => {
    const structural = next.entry !== view.entry || next.hops !== view.hops || next.types !== view.types || next.relations !== view.relations;
    setSearch(new URLSearchParams(viewToQuery(next)), { replace: !structural });
  }, [setSearch, view]);
  const full = useFullGraph(vault, !view.entry), neighborhood = useNeighborhood(vault, view.entry, view.hops, view.links);
  const query = view.entry ? neighborhood : full;
  const base = query.data;
  const [overlay, setOverlay] = useState<GraphPayload>(EMPTY);
  const [pinned, setPinned] = useState(new Set<string>());
  const [hidden, setHidden] = useState(new Set<string>());
  const [hideOrphans, setHideOrphans] = useState(false);
  const [menu, setMenu] = useState<GraphMenuState | null>(null);
  const [edgeSelection, setEdge] = useState<GraphEdge | null>(null);
  const [expanding, setExpanding] = useState<string>();
  const [expansionError, setExpansionError] = useState<{ uri: string; message: string }>();
  const expandingRef = useRef(false);
  const canvas = useRef<GraphCanvasHandle>(null);
  const tools = useRef<HTMLDivElement>(null);
  const [toolsHeight, setToolsHeight] = useState(0);
  useLayoutEffect(() => {
    const element = tools.current;
    if (!element) return;
    const measure = () => setToolsHeight(Math.ceil(element.getBoundingClientRect().height));
    measure();
    const observer = new ResizeObserver(measure);
    observer.observe(element);
    return () => observer.disconnect();
  }, []);
  // The canvas remains full-size. Only overlays and the scrollable List need
  // clearance; measure wrapping/focus controls rather than guessing a height.
  const toolsBottom = toolsHeight + 24;
  const graphRevealed = useRef(mode === "graph");
  const focusRelationshipPanel = useCallback((node: HTMLElement | null) => { node?.focus({ preventScroll: true }); }, []);
  const merged = useMemo(() => mergeGraph(base ?? EMPTY, overlay), [base, overlay]);
  const resourceCounts = useMemo(() => {
    if (!base) return undefined;
    const counts = { document: 0, table: 0, file: 0 };
    for (const node of merged.nodes) counts[node.kind]++;
    return counts;
  }, [base, merged.nodes]);
  const filterKey = JSON.stringify([[...view.types].sort(), [...view.relations].sort()]);
  const entryNode = merged.nodes.find(n => n.uri === view.entry || docIdFromUri(n.uri) === view.entry);
  // Strong membership is decided on the whole loaded map, before display filters, so hiding a resource
  // cannot cut the walk; what the reader loaded or followed (the overlay) always stays.
  // Weak neighbours of a focus are folded into a count unless there are few of them or the reader opens them.
  const [weakChoice, setWeakChoice] = useState<boolean | undefined>(undefined);
  const scene = useMemo(() => {
    const empty: StrongScene = { weak: new Set(), weakDepth: new Map(), strong: false, reach: new Map(), tree: new Set(), perHop: [] };
    const revealed = overlay.nodes.map(n => n.uri);
    const recent = changedWithin(merged, view.since, [...revealed, ...(entryNode ? [entryNode.uri] : []), ...(view.selected ? [view.selected] : [])]);
    const strong = view.entry && entryNode && view.links === "strong"
      ? strongNeighborhood(recent, entryNode.uri, view.hops, revealed)
      : { ...recent, ...empty };
    const visible = visibleGraph(strong, view, hidden, hideOrphans);
    if (!view.entry && view.links === "strong" && visible.edges.some(e => edgeStrength(e) === "strong")) {
      // The whole Vault draws its strong structure; body links would bury it.
      return { ...visible, ...empty, strong: true, edges: visible.edges.filter(e => edgeStrength(e) === "strong"), overview: true };
    }
    return { ...visible, weak: strong.weak, weakDepth: strong.weakDepth, strong: strong.strong, reach: strong.reach, tree: strong.tree, perHop: strong.perHop, overview: false };
  },
    // Selection is deliberately excluded: it cannot invalidate renderer data.
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [merged, overlay, filterKey, hidden, hideOrphans, view.entry, entryNode, view.links, view.hops, view.since]);
  const weakShown = weakChoice ?? scene.weak.size <= 5;
  const displayed = useMemo(() => {
    if (weakShown || !scene.strong || !view.entry) return scene;
    // Folded: weak neighbours and every weak line go; what the reader loaded or followed stays.
    const revealed = new Set(overlay.nodes.map(n => n.uri));
    const fold = new Set([...scene.weak].filter(uri => !revealed.has(uri)));
    return { ...scene, nodes: scene.nodes.filter(n => !fold.has(n.uri)),
      edges: scene.edges.filter(e => !fold.has(e.source) && !fold.has(e.target) && (edgeStrength(e) === "strong" || revealed.has(e.source) || revealed.has(e.target))) };
  }, [scene, weakShown, overlay, view.entry]);
  const selected = view.selected ? displayed.nodes.find(n => n.uri === view.selected || n.doc_id === view.selected || docIdFromUri(n.uri) === view.selected) : undefined;
  const edge = displayed.edges.find(item => edgeSelection && item.source === edgeSelection.source && item.target === edgeSelection.target && item.relation === edgeSelection.relation && item.kind === edgeSelection.kind);
  const selectedId = selected && (selected.doc_id || docIdFromUri(selected.uri));
  const coverage = graphCoverage(base?.meta);
  const orphanCount = merged.nodes.filter(n => n.degree === 0).length;
  const degree = useMemo(() => degreeMap(displayed.edges), [displayed.edges]);
  const hubs = useMemo(() => [...displayed.nodes].sort((a, b) => (degree.get(b.uri) ?? 0) - (degree.get(a.uri) ?? 0)).slice(0, 8), [displayed.nodes, degree]);
  const focusTitle = entryNode && displayName(entryNode);
  // Colour by distance only around a focused resource. In strong mode the distance is the strong walk's
  // (weak neighbours sit at 1, faded); otherwise any loaded connection counts.
  const hops = useMemo(() => {
    if (!view.entry || !entryNode) return undefined;
    if (!scene.strong) return hopDistances(displayed.edges, entryNode.uri);
    const map = new Map(scene.reach);
    for (const [uri, depth] of scene.weakDepth) if (!map.has(uri)) map.set(uri, depth);
    return map;
  }, [view.entry, entryNode, displayed.edges, scene]);
  // The whole Vault colours resources by collection: the four largest get their own colour, the rest share grey.
  const collections = useMemo(() => {
    if (view.entry) return undefined;
    const counts = new Map<string, number>();
    for (const n of displayed.nodes) { const key = n.group ?? ""; counts.set(key, (counts.get(key) ?? 0) + 1); }
    const ranked = [...counts.entries()].filter(([key]) => key).sort((a, b) => b[1] - a[1] || a[0].localeCompare(b[0]));
    // One collection says nothing by colour; the kind colours stay.
    if (ranked.length < 2) return undefined;
    return ranked.map(([key, count], index) => ({ key, count, token: index < 4 ? `cat-${index + 1}` : "cat-6" }));
  }, [view.entry, displayed.nodes]);

  function select(uri: string | undefined) {
    const node = merged.nodes.find(item => item.uri === uri);
    if (uri) {
      setHidden(current => { if (!current.has(uri)) return current; const next = new Set(current); next.delete(uri); return next; });
      if (hideOrphans && !displayed.nodes.some(item => item.uri === uri)) setHideOrphans(false);
    }
    setEdge(null); setView({ ...view, selected: uri, types: node && !view.types.has(node.kind) ? new Set(view.types).add(node.kind) : view.types });
  }
  function open(node: GraphNode, newTab = false, preview = false) {
    const id = node.doc_id || docIdFromUri(node.uri);
    if (!id) return;
    const url = `/vault/${encodeURIComponent(vault)}/${kindToSegment(node.kind)}/${encodeURIComponent(id)}`;
    if (newTab) window.open(url, "_blank", "noopener");
    else navigate(url, preview && node.kind === "document" ? { state: documentPreviewState(location, "graph-preview-trigger", "graph-workspace") } : undefined);
  }
  function focus(node: GraphNode) { setView({ ...view, entry: node.uri, selected: undefined }); }
  function togglePin(uri: string) { setPinned(current => { const next = new Set(current); if (next.has(uri)) next.delete(uri); else next.add(uri); return next; }); }
  function hide(uri: string) { setHidden(current => new Set(current).add(uri)); select(undefined); }
  async function expand(node: GraphNode) {
    if (expandingRef.current) return;
    expandingRef.current = true; setExpanding(node.uri); setExpansionError(undefined);
    try {
      const payload = await fetchNeighbors(vault, node.uri, 1, 100);
      const next = mergeGraph(merged, payload);
      if (next.nodes.length > SCENE_LIMIT) {
        setExpansionError({ uri: node.uri, message: "This map has reached 2,000 resources. Explore connections from this resource to start a smaller map." });
      } else setOverlay(current => mergeGraph(current, payload));
    } catch (error) {
      setExpansionError({ uri: node.uri, message: error instanceof Error ? error.message : "Connections could not be loaded. Try again." });
    } finally { expandingRef.current = false; setExpanding(undefined); }
  }
  function related(item: RelatedRef) {
    const node: GraphNode = { uri: item.uri, name: item.name, kind: item.kind };
    const relation: GraphEdge[] = !selected ? [] : [{
      source: item.direction === "outgoing" ? selected.uri : item.uri,
      target: item.direction === "outgoing" ? item.uri : selected.uri,
      relation: item.relation, kind: item.source,
    }];
    if (merged.nodes.length >= SCENE_LIMIT && !merged.nodes.some(n => n.uri === item.uri)) {
      if (selected) setExpansionError({ uri: selected.uri, message: "This map has reached 2,000 resources. Explore connections to start a smaller map." });
      return;
    }
    const missing = relation.filter(edge => !merged.edges.some(e => e.source === edge.source && e.target === edge.target && e.relation === edge.relation && e.kind === edge.kind));
    setOverlay(current => mergeGraph(current, { nodes: [node], edges: missing }));
    setHidden(current => { const next = new Set(current); next.delete(item.uri); return next; });
    setHideOrphans(false);
    setEdge(null);
    setView({ ...view, selected: item.uri, types: new Set(view.types).add(item.kind), relations: new Set(view.relations).add(item.relation) });
  }
  function reset() {
    setHidden(new Set()); setHideOrphans(false);
    setView({ ...view, types: new Set(ALL_NODE_KINDS), relations: new Set(ALL_RELATIONS) });
  }

  return <div id="graph-workspace" tabIndex={-1}
    style={{ "--graph-tools-bottom": `${toolsBottom}px` } as CSSProperties}
    className="relative isolate flex h-full min-h-0 flex-col overflow-hidden bg-surface focus:outline-none">
    <div ref={tools} className="pointer-events-none absolute inset-x-3 top-3 z-[var(--z-popover)] mx-auto max-w-4xl">
      <GraphToolbar vault={vault} view={view} onChange={setView}
        onNavigate={query => navigate({ search: query })}
        nodes={displayed.nodes} resourceCounts={resourceCounts} hubs={hubs} onSelect={select} onReveal={uri => {
          setHidden(current => { if (!current.has(uri)) return current; const next = new Set(current); next.delete(uri); return next; });
          if (!displayed.nodes.some(node => node.uri === uri)) setHideOrphans(false);
        }}
        nodeCount={displayed.nodes.length} edgeCount={displayed.edges.length}
        strongWalk={view.entry && scene.strong ? { perHop: scene.perHop, weak: scene.weak.size, weakShown, onToggleWeak: () => setWeakChoice(!weakShown) } : undefined}
        collections={collections}
        focusTitle={focusTitle} displayMode={mode} onDisplayModeChange={next => {
          setMode(next);
          if (next === "graph" && !graphRevealed.current) {
            graphRevealed.current = true;
            canvas.current?.fit();
            if (selected) canvas.current?.centerOnNode(selected.uri);
          }
        }}
        orphanCount={orphanCount} hideOrphans={hideOrphans} onToggleOrphans={() => setHideOrphans(v => !v)}
        hiddenCount={hidden.size} onUnhideAll={() => setHidden(new Set())}
        onFit={() => canvas.current?.fit()} onRearrange={() => canvas.current?.rearrange()} />
    </div>
    <section aria-label="Graph exploration workspace" aria-busy={query.isFetching} className="@container/graph relative min-h-0 flex-1 overflow-hidden">
      <div className={cn("absolute inset-0", mode === "list" && "invisible pointer-events-none")} aria-hidden={mode === "list" || undefined}>
        <Suspense fallback={<div role="status" className="p-4 text-sm text-foreground-muted">Loading graph viewer…</div>}>
        <GraphCanvas ref={canvas} nodes={displayed.nodes} edges={displayed.edges} selected={selected?.uri} inspectedEdge={edge ?? undefined} pinned={pinned} topInset={toolsBottom}
          dimension={dimension} hops={hops} weak={scene.weak} tree={scene.strong && view.entry ? scene.tree : undefined}
          groupTokens={collections ? new Map(collections.map(c => [c.key, c.token])) : undefined}
          onUseList={() => setMode("list")}
          onSelect={select} onExpand={expand} onContextMenu={(node, x, y) => setMenu({ node, x, y })}
          onSelectEdge={value => { setView({ ...view, selected: undefined }); setEdge(value); }} />
        </Suspense>
      </div>
      {mode === "list" && displayed.nodes.length > 0 && <GraphListView nodes={displayed.nodes} edges={displayed.edges} selected={selected?.uri} onSelect={select} />}
      {query.isLoading ? <LoadingState label="Loading knowledge graph" className="absolute inset-0 flex items-center justify-center bg-surface">
        <div className="grid w-72 grid-cols-3 gap-8"><Skeleton className="h-12" /><Skeleton className="h-12" /><Skeleton className="h-12" /></div>
      </LoadingState> : query.error && !base ? <EmptyState className="mx-3 mt-[var(--graph-tools-bottom)]" title="The graph could not be loaded" description="Retry without changing your Vault data." action={<Button variant="outline" onClick={() => query.refetch()}>Retry</Button>} />
        : displayed.nodes.length === 0 ? <EmptyState className="mx-3 mt-[var(--graph-tools-bottom)]" title={merged.nodes.length ? "No resources match these filters" : "There is nothing to map yet"}
          description={merged.nodes.length ? "Reset filters to restore the loaded map." : "Add documents, tables or files to this Vault to begin exploring."}
          action={<Button variant="outline" onClick={merged.nodes.length ? reset : () => navigate(`/vault/${encodeURIComponent(vault)}`)}>{merged.nodes.length ? "Reset filters" : "Back to Overview"}</Button>} /> : null}
      {query.error && base && <div role="alert" className="absolute left-3 right-3 top-[var(--graph-tools-bottom)] rounded-[var(--radius-sm)] border border-border bg-surface p-3 text-sm">Refresh failed. Your loaded map is unchanged. <Button variant="outline" size="sm" onClick={() => query.refetch()}>Retry</Button></div>}
      {view.entry && view.links === "strong" && base && !scene.strong && mode === "graph" && <p className="pointer-events-none absolute bottom-14 left-4 right-4 max-w-md text-xs text-foreground-muted">
        No explicit relations around this resource, so every loaded connection is shown.</p>}
      {!query.isLoading && !query.error && displayed.nodes.length > 0 && displayed.edges.length === 0 && mode === "graph" && !selected && <p className="pointer-events-none absolute left-4 right-4 top-[var(--graph-tools-bottom)] max-w-sm text-xs text-foreground-muted">No relationships in this map. Select a resource to explore its connections.</p>}

      {selected && selectedId && <GraphDetailPanel key={selected.uri} vault={vault} docId={selectedId} name={selected.name} kind={selected.kind} uri={selected.uri}
        onSelectRelated={related} onFitToNode={uri => canvas.current?.centerOnNode(uri)} onClose={() => select(undefined)}
        onFocus={view.entry !== selected.uri ? () => focus(selected) : undefined} onOpen={() => open(selected)}
        onPreview={selected.kind === "document" ? () => open(selected, false, true) : undefined}
        onExpand={() => expand(selected)} expanding={!!expanding} expansionError={expansionError?.uri === selected.uri ? expansionError.message : undefined}
        onHide={() => hide(selected.uri)} pinned={pinned.has(selected.uri)} onTogglePin={() => togglePin(selected.uri)} />}
      {edge && <aside aria-label="Relationship details" tabIndex={-1} ref={focusRelationshipPanel} onKeyDown={event => { if (event.key === "Escape") { event.stopPropagation(); setEdge(null); document.getElementById("graph-workspace")?.focus({ preventScroll: true }); } }} className="absolute right-3 top-[var(--graph-tools-bottom)] z-[var(--z-overlay)] max-h-[calc(100%-var(--graph-tools-bottom)-1rem)] w-80 max-w-[calc(100%-1.5rem)] overflow-y-auto rounded-[var(--radius-md)] border border-border bg-surface p-4 shadow-md focus:outline-none">
        <div className="flex items-center justify-between"><h2 className="text-sm font-semibold">Relationship</h2><Button variant="ghost" size="icon" aria-label="Close relationship details" onClick={() => setEdge(null)}><X className="h-4 w-4" /></Button></div>
        <p className="mt-2 break-words text-sm">{displayed.nodes.find(n => n.uri === edge.source)?.name} <span className="text-link">→ {RELATION_LABEL[edge.relation]} →</span> {displayed.nodes.find(n => n.uri === edge.target)?.name}</p>
        <p className="mt-3 text-xs text-foreground-muted">{edgeStrength(edge) === "strong" ? "Strong" : "Weak"} · {edge.kind === "implicit" ? "Body link" : edge.kind === "explicit" ? "Explicit relationship" : "Relationship source not provided"}</p>
      </aside>}

      {mode === "graph" && <CanvasControls canvas={canvas} dimension={dimension} onDimension={setDimension} />}
      <div className="pointer-events-none absolute bottom-3 left-3 right-16 flex flex-wrap items-end gap-2 text-xs text-foreground-muted @min-[48rem]/graph:right-40">
        <details className="pointer-events-auto relative rounded-[var(--radius-sm)] border border-border bg-surface shadow-xs">
          <summary className="flex min-h-9 cursor-pointer list-none items-center gap-2 px-3 focus-visible:outline-2 focus-visible:outline-ring">
            <Info className="h-3.5 w-3.5" aria-hidden /><span className="tabular-nums">{displayed.nodes.length} resources · {displayed.edges.length} relationships</span>
            <span className="hidden @min-[40rem]/graph:inline">· {coverage.label}</span>
          </summary>
          <div className="absolute bottom-full left-0 mb-2 w-72 max-w-[80vw] rounded-[var(--radius-md)] border border-border bg-surface p-3 leading-relaxed shadow-md">{coverage.detail}</div>
        </details>
        {mode === "graph" && <p className="hidden h-9 items-center px-2 @min-[64rem]/graph:flex">Larger dots have more connections in this map</p>}
      </div>
      <div className="sr-only" aria-live="polite">{selected ? `Selected ${selected.name}, ${selected.kind}, ${degree.get(selected.uri) ?? 0} relationships in this map` : ""}</div>
    </section>
    {menu && <GraphContextMenu state={menu} pinned={pinned.has(menu.node.uri)} onClose={() => setMenu(null)} onOpen={newTab => open(menu.node, newTab)} onExpand={() => expand(menu.node)} onTogglePin={() => togglePin(menu.node.uri)} onHide={() => hide(menu.node.uri)} onFocus={() => focus(menu.node)} onCopyUri={() => navigator.clipboard?.writeText(menu.node.uri)} />}
  </div>;
}

function CanvasControls({ canvas, dimension, onDimension }: {
  canvas: React.RefObject<GraphCanvasHandle | null>; dimension: GraphDimension; onDimension: (value: GraphDimension) => void;
}) {
  return <div className="absolute bottom-3 right-3 z-[var(--z-raised)] flex flex-col items-end gap-2">
    <div role="group" aria-label="Map view" className="flex overflow-hidden rounded-[var(--radius-sm)] border border-border bg-surface text-xs shadow-xs">
      {([[2, "2D"], [3, "3D"]] as const).map(([value, label]) => <button key={value} type="button" aria-pressed={dimension === value}
        title={value === 2 ? "Flat map: drag the background to pan, drag a resource to move it" : "3D: drag to rotate, right-drag to pan, drag a resource to move it"}
        onClick={() => onDimension(value)}
        className={cn("h-9 min-w-11 px-2 font-medium tabular-nums focus:outline-none focus-visible:ring-2 focus-visible:ring-inset focus-visible:ring-ring",
          dimension === value ? "bg-surface-selected text-surface-selected-foreground" : "text-foreground-muted hover:bg-surface-hover")}>{label}</button>)}
    </div>
    {dimension === 3 && <div role="group" aria-label="Rotate 3D view" className="grid grid-cols-2 overflow-hidden rounded-[var(--radius-sm)] border border-border bg-surface shadow-xs @min-[40rem]/graph:flex">
      {[{ label: "Rotate left", Icon: RotateCcw, x: -0.2, y: 0 }, { label: "Rotate right", Icon: RotateCw, x: 0.2, y: 0 }, { label: "Rotate up", Icon: ArrowUp, x: 0, y: -0.2 }, { label: "Rotate down", Icon: ArrowDown, x: 0, y: 0.2 }].map(({ label, Icon, x, y }) => <Button key={label} size="icon" variant="ghost" className="h-10 w-10 rounded-none" aria-label={label} title={label} onClick={() => canvas.current?.orbit(x, y)}><Icon className="h-4 w-4" aria-hidden /></Button>)}
    </div>}
    <div className="flex items-end gap-2">
    <details className="relative">
      <summary aria-label="Pan map" className="flex h-9 w-9 cursor-pointer list-none items-center justify-center rounded-[var(--radius-sm)] border border-border bg-surface text-foreground-muted shadow-xs focus-visible:outline-2 focus-visible:outline-ring"><Move className="h-4 w-4" /></summary>
      <div className="absolute bottom-full right-0 mb-2 grid w-28 grid-cols-3 gap-1 rounded-[var(--radius-sm)] border border-border bg-surface p-1 shadow-md">
        {[{ label: "Pan left", Icon: ArrowLeft, x: 120, y: 0 }, { label: "Pan up", Icon: ArrowUp, x: 0, y: 120 }, { label: "Pan right", Icon: ArrowRight, x: -120, y: 0 }, { label: "Pan down", Icon: ArrowDown, x: 0, y: -120 }].map(({ label, Icon, x, y }) => <Button key={label} size="icon" variant="ghost" aria-label={label} onClick={() => canvas.current?.pan(x, y)}><Icon className="h-4 w-4" /></Button>)}
      </div>
    </details>
    <div className="flex flex-col overflow-hidden rounded-[var(--radius-sm)] border border-border bg-surface shadow-sm @min-[40rem]/graph:flex-row">
      {[{ label: "Zoom out", Icon: Minus, action: () => canvas.current?.zoomOut() }, { label: "Zoom in", Icon: Plus, action: () => canvas.current?.zoomIn() }, { label: "Fit graph", Icon: Maximize2, action: () => canvas.current?.fit() }].map(({ label, Icon, action }) => <Button key={label} variant="ghost" size="icon" aria-label={label} title={label} onClick={action} className="h-11 w-11 rounded-none @min-[48rem]/graph:h-9 @min-[48rem]/graph:w-9"><Icon className="h-4 w-4" aria-hidden /></Button>)}
    </div>
    </div>
  </div>;
}
