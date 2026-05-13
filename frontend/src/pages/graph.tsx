import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useNavigate, useParams, useSearchParams } from "react-router-dom";
import { PanelLeftClose, PanelLeftOpen } from "lucide-react";
import { Skeleton } from "@/components/ui/skeleton";
import { EmptyState } from "@/components/empty-state";
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
import { type GraphEdge, type GraphNode, type GraphView } from "@/components/graph/graph-types";

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
  const neighborQuery = useNeighborhood(vault!, view.entry, view.depth);
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
  }, [view.entry, view.depth, vault]);

  const merged = useMemo(() => {
    const m = mergeGraph(base || { nodes: [], edges: [] }, overlay);
    return applyFilters(m, view);
  }, [base, overlay, view]);

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
    setView({ ...view, selected: uri });
  }

  function handleDoubleClick(node: GraphNode) {
    const id = node.doc_id || docIdFromUri(node.uri);
    if (!id) return;
    const segment = node.kind === "table" ? "table" : node.kind === "file" ? "file" : "doc";
    navigate(`/vault/${vault}/${segment}/${encodeURIComponent(id)}`);
  }

  function handleContextMenu(_n: GraphNode, _x: number, _y: number) {
    // Context menu wiring deferred — fall back to plain select for now.
    // TODO(graph-context-menu): floating menu with Pin/Unpin/Hide/Copy/Open-new-tab.
  }

  const selectedNode = view.selected
    ? merged.nodes.find((n) => n.uri === view.selected)
    : undefined;
  const selectedDocId = selectedNode ? (selectedNode.doc_id || docIdFromUri(selectedNode.uri)) : null;
  const detailOpen = !!selectedNode && !!selectedDocId;

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
          currentUrl={"?" + viewToQuery(view)}
          onChange={setView}
          onNavigate={(qs) => {
            navigate({ search: qs.startsWith("?") ? qs : `?${qs}` }, { replace: true });
          }}
        />
      ) : (
        <button
          type="button"
          onClick={() => setSidebarOpen(true)}
          aria-label="Open sidebar"
          className="h-9 w-9 m-2 inline-flex items-center justify-center border border-border bg-surface text-foreground-muted hover:text-foreground"
        >
          <PanelLeftOpen className="h-4 w-4" />
        </button>
      )}

      <div className="relative bg-background overflow-hidden">
        <button
          type="button"
          onClick={() => setSidebarOpen((v) => !v)}
          aria-label={sidebarOpen ? "Hide sidebar" : "Show sidebar"}
          className="absolute top-3 right-3 z-10 h-6 w-6 inline-flex items-center justify-center bg-surface border border-border text-foreground-muted hover:text-foreground"
        >
          {sidebarOpen ? <PanelLeftClose className="h-3 w-3" /> : <PanelLeftOpen className="h-3 w-3" />}
        </button>

        {degraded && (
          <div className="absolute top-3 left-1/2 -translate-x-1/2 z-10 bg-warning/10 border border-warning text-warning px-3 py-1 font-mono text-[10px] uppercase">
            {rawNodeCount} nodes — pick an entry point to explore
          </div>
        )}

        {loading ? (
          <div className="p-8"><Skeleton className="h-64 w-full" /></div>
        ) : error ? (
          <EmptyState title="Failed to load graph" description={String(error)} />
        ) : merged.nodes.length === 0 ? (
          <EmptyState title="Empty graph" description="No relations match the current filters." />
        ) : (
          <GraphCanvas
            ref={canvasRef}
            nodes={merged.nodes}
            edges={merged.edges}
            selected={view.selected}
            pinned={pinned}
            hidden={hidden}
            degraded={degraded}
            onSelect={handleSelect}
            onDoubleClick={handleDoubleClick}
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
          onSelectUri={(uri) => setView({ ...view, selected: uri })}
          onFitToNode={(uri) => canvasRef.current?.centerOnNode(uri)}
          onClose={() => setView({ ...view, selected: undefined })}
          onTogglePin={() => {
            setPinned((prev) => {
              const next = new Set(prev);
              next.has(selectedNode.uri) ? next.delete(selectedNode.uri) : next.add(selectedNode.uri);
              return next;
            });
          }}
          pinned={pinned.has(selectedNode.uri)}
        />
      ) : null}
    </div>
  );
}
