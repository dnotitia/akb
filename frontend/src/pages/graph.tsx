import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useNavigate, useParams, useSearchParams } from "react-router-dom";
import { PanelLeftOpen } from "lucide-react";
import { Skeleton } from "@/components/ui/skeleton";
import { EmptyState } from "@/components/empty-state";
import { Button } from "@/components/ui/button";
import { GraphCanvas, type GraphCanvasHandle } from "@/components/graph/GraphCanvas";
import { GraphSidebar } from "@/components/graph/GraphSidebar";
import { GraphDetailPanel } from "@/components/graph/GraphDetailPanel";
import {
  useFullGraph,
  useNeighborhood,
  applyFilters,
  mergeGraph,
  isDegraded,
  docIdFromUri,
} from "@/components/graph/use-graph-data";
import { viewToQuery, queryToView } from "@/components/graph/graph-state";
import { kindToSegment, type GraphEdge, type GraphNode, type GraphView, type RelatedRef } from "@/components/graph/graph-types";
import { groupOf } from "@/components/graph/cluster";

const DOUBLECLICK_MS = 250;

export default function GraphPage() {
  const { name: vault } = useParams<{ name: string }>();
  const [search, setSearch] = useSearchParams();
  const navigate = useNavigate();

  const view: GraphView = useMemo(() => queryToView(search), [search]);

  const setView = useCallback(
    (next: GraphView) => {
      setSearch(new URLSearchParams(viewToQuery(next)), { replace: true });
    },
    [setSearch],
  );

  // Hybrid fetch
  const fullQuery = useFullGraph(vault!, !view.entry);
  const neighborQuery = useNeighborhood(vault!, view.entry, view.hops);
  const base = view.entry ? neighborQuery.data : fullQuery.data;
  const loading = view.entry ? neighborQuery.isLoading : fullQuery.isLoading;
  const error = view.entry ? neighborQuery.error : fullQuery.error;
  const rawNodeCount = base?.nodes.length || 0;
  const degraded = isDegraded(rawNodeCount);

  // Click-expand merges into a session-scoped overlay so URL state stays clean.
  const [overlay, setOverlay] = useState<{ nodes: GraphNode[]; edges: GraphEdge[] }>({
    nodes: [],
    edges: [],
  });
  // Reset overlay when the base shape (mode/entry/depth) changes:
  useEffect(() => {
    setOverlay({ nodes: [], edges: [] });
  }, [view.entry, view.hops, vault]);

  // Selection is highlight-only and MUST NOT relayout the graph. `view` is a
  // fresh object on every selection (URL search change → queryToView), so
  // depending on `view` here rebuilt `merged` — and thus graphData identity —
  // on every click, which made force-graph reheat the simulation (alpha=1)
  // and the clusters drift farther apart each click. Recompute only when the
  // graph STRUCTURE changes (data, overlay, filters, entry/hops); a stable
  // structureKey across selection keeps graphData identity stable → no reheat.
  const structureKey = useMemo(
    () =>
      JSON.stringify({
        entry: view.entry ?? null,
        hops: view.hops,
        types: [...view.types].sort(),
        relations: [...view.relations].sort(),
      }),
    [view],
  );
  const merged = useMemo(
    () => applyFilters(mergeGraph(base || { nodes: [], edges: [] }, overlay), view),
    // `selected` is intentionally excluded; structureKey captures every view
    // field that affects graph structure/filtering.
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [base, overlay, structureKey],
  );

  const canvasRef = useRef<GraphCanvasHandle>(null);

  // UI-only state (not URL)
  const [pinned, setPinned] = useState<Set<string>>(new Set());
  const [hidden, setHidden] = useState<Set<string>>(new Set());
  const [sidebarOpen, setSidebarOpen] = useState(true);
  const lastClickRef = useRef<{ uri: string; at: number } | null>(null);

  // Double-click emulation: two clicks on the same URI within DOUBLECLICK_MS
  // navigate to the doc; otherwise the first click is treated as select.
  function handleSelect(uri: string | undefined) {
    if (uri && lastClickRef.current && lastClickRef.current.uri === uri && Date.now() - lastClickRef.current.at < DOUBLECLICK_MS) {
      const node = merged.nodes.find((n) => n.uri === uri);
      if (node) handleDoubleClick(node);
      lastClickRef.current = null;
      return;
    }
    lastClickRef.current = uri ? { uri, at: Date.now() } : null;
    const sel = uri ? (docIdFromUri(uri) ?? uri) : undefined;
    if (sel === view.selected) return;
    setView({ ...view, selected: sel });
  }

  function handleDoubleClick(node: GraphNode) {
    const id = node.doc_id || docIdFromUri(node.uri);
    if (!id) return;
    const segment = kindToSegment(node.kind);
    navigate(`/vault/${vault}/${segment}/${encodeURIComponent(id)}`);
  }

  function handleContextMenu(_n: GraphNode, _x: number, _y: number) {
    // Context menu wiring deferred — fall back to plain select for now.
    // TODO(graph-context-menu): floating menu with Pin/Unpin/Hide/Copy/Open-new-tab.
  }

  // Resolve the selected node + its lookup id once per (graph, selection)
  // change rather than on every render — the find scans up to ~200 nodes and
  // each comparison may parse a URI.
  const { selectedNode, selectedDocId } = useMemo(() => {
    const node = view.selected
      ? merged.nodes.find(
          (n) =>
            n.uri === view.selected ||
            n.doc_id === view.selected ||
            docIdFromUri(n.uri) === view.selected,
        )
      : undefined;
    const docId = node ? node.doc_id || docIdFromUri(node.uri) : null;
    return { selectedNode: node, selectedDocId: docId };
  }, [merged, view.selected]);
  const detailOpen = !!selectedNode && !!selectedDocId;

  // Clicking a relation in the detail panel selects that node in the graph.
  // If it isn't currently rendered (filtered out, or outside the loaded
  // neighbourhood), add it — plus the edge connecting it to the current node —
  // to the session overlay first, so it appears and can be highlighted.
  function handleSelectRelated(rel: RelatedRef) {
    const present = merged.nodes.some((n) => n.uri === rel.uri);
    if (!present) {
      const node: GraphNode = { uri: rel.uri, name: rel.name, kind: rel.kind, group: groupOf(rel.uri) };
      const sourceUri = selectedNode?.uri;
      const edge: GraphEdge | null = sourceUri
        ? rel.direction === "outgoing"
          ? { source: sourceUri, target: rel.uri, relation: rel.relation }
          : { source: rel.uri, target: sourceUri, relation: rel.relation }
        : null;
      setOverlay((prev) => ({
        nodes: prev.nodes.some((n) => n.uri === rel.uri) ? prev.nodes : [...prev.nodes, node],
        edges:
          edge &&
          !prev.edges.some(
            (e) => e.source === edge.source && e.target === edge.target && e.relation === edge.relation,
          )
            ? [...prev.edges, edge]
            : prev.edges,
      }));
    }
    setView({ ...view, selected: docIdFromUri(rel.uri) ?? rel.uri });
  }

  const gridCols = `${sidebarOpen ? "240px" : "40px"} 1fr ${detailOpen ? "320px" : "0px"}`;

  return (
    <div
      className="grid grid-cols-[var(--gcols)] h-full min-h-0"
      style={{ ["--gcols" as any]: gridCols }}
    >
      {sidebarOpen ? (
        <GraphSidebar
          vault={vault!}
          view={view}
          onChange={setView}
          onNavigate={(qs) => {
            navigate({ search: qs.startsWith("?") ? qs : `?${qs}` }, { replace: true });
          }}
          onCollapse={() => setSidebarOpen(false)}
        />
      ) : (
        // Collapsed rail: the sole "expand" affordance. The matching
        // "collapse" control lives in the sidebar header (GraphSidebar
        // onCollapse) so there is exactly one toggle, always on the left.
        <div className="flex flex-col items-center border-r border-border bg-surface">
          <button
            type="button"
            onClick={() => setSidebarOpen(true)}
            aria-label="Show sidebar"
            title="Show sidebar"
            className="h-9 w-9 mt-2 inline-flex items-center justify-center text-foreground-muted hover:text-foreground hover:bg-surface-muted cursor-pointer transition-colors focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent"
          >
            <PanelLeftOpen className="h-4 w-4" />
          </button>
        </div>
      )}

      <div className="relative bg-background overflow-hidden">
        {degraded && (
          <div className="absolute top-3 left-1/2 -translate-x-1/2 z-10 bg-warning/10 border border-warning text-warning px-3 py-1 font-mono text-[10px] uppercase">
            {rawNodeCount} nodes — pick an entry point to explore
          </div>
        )}

        {loading ? (
          <div className="p-8"><Skeleton className="h-64 w-full" /></div>
        ) : error ? (
          <EmptyState
            title="Failed to load graph"
            description={String(error)}
            action={
              <Button
                variant="outline"
                size="sm"
                onClick={() => (view.entry ? neighborQuery.refetch() : fullQuery.refetch())}
              >
                Retry
              </Button>
            }
          />
        ) : merged.nodes.length === 0 ? (
          <EmptyState title="Empty graph" description="No relations match the current filters." />
        ) : (
          <GraphCanvas
            ref={canvasRef}
            nodes={merged.nodes}
            edges={merged.edges}
            selected={selectedNode?.uri}
            pinned={pinned}
            hidden={hidden}
            degraded={degraded}
            onSelect={handleSelect}
            onContextMenu={handleContextMenu}
          />
        )}
      </div>

      {detailOpen && selectedNode && selectedDocId ? (
        <GraphDetailPanel
          vault={vault!}
          docId={selectedDocId}
          kind={selectedNode.kind}
          uri={selectedNode.uri}
          onSelectRelated={handleSelectRelated}
          onFitToNode={(fitUri) => canvasRef.current?.centerOnNode(fitUri)}
          onClose={() => setView({ ...view, selected: undefined })}
          onTogglePin={() => {
            setPinned((prev) => {
              const next = new Set(prev);
              if (next.has(selectedNode.uri)) next.delete(selectedNode.uri);
              else next.add(selectedNode.uri);
              return next;
            });
          }}
          pinned={pinned.has(selectedNode.uri)}
        />
      ) : null}
    </div>
  );
}
