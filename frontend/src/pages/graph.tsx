import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useNavigate, useParams, useSearchParams } from "react-router-dom";
import { PanelLeftOpen, X } from "lucide-react";
import { Skeleton } from "@/components/ui/skeleton";
import { EmptyState } from "@/components/empty-state";
import { Alert } from "@/components/ui/alert";
import { Button } from "@/components/ui/button";
import { TooltipText } from "@/components/ui/tooltip-text";
import { GraphCanvas, type GraphCanvasHandle } from "@/components/graph/GraphCanvas";
import { GraphSidebar } from "@/components/graph/GraphSidebar";
import { GraphDetailPanel } from "@/components/graph/GraphDetailPanel";
import {
  useFullGraph,
  useNeighborhood,
  applyFilters,
  mergeGraph,
  fetchNeighbors,
  docIdFromUri,
} from "@/components/graph/use-graph-data";
import { GraphContextMenu, type GraphMenuState } from "@/components/graph/GraphContextMenu";
import { viewToQuery, queryToView } from "@/components/graph/graph-state";
import { kindToSegment, type GraphEdge, type GraphNode, type GraphView, type RelatedRef } from "@/components/graph/graph-types";
import { groupOf } from "@/components/graph/cluster";

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
  // One-time orientation hint (persisted dismissed).
  const [hintOpen, setHintOpen] = useState(
    () => localStorage.getItem("akb:graph:hint-dismissed") !== "1",
  );
  const dismissHint = useCallback(() => {
    setHintOpen(false);
    localStorage.setItem("akb:graph:hint-dismissed", "1");
  }, []);
  // Floating context-menu state (right-click on a node).
  const [menu, setMenu] = useState<GraphMenuState | null>(null);

  // Single click selects (highlight-only — never relayouts). Double-click
  // (expand) and navigation are handled separately: the canvas calls onExpand
  // on double-click; navigation lives in the context menu / detail panel.
  function handleSelect(uri: string | undefined) {
    const sel = uri ? (docIdFromUri(uri) ?? uri) : undefined;
    if (sel === view.selected) return;
    setView({ ...view, selected: sel });
  }

  function openNode(node: GraphNode, newTab = false) {
    const id = node.doc_id || docIdFromUri(node.uri);
    if (!id) return;
    const url = `/vault/${vault}/${kindToSegment(node.kind)}/${encodeURIComponent(id)}`;
    if (newTab) window.open(url, "_blank", "noopener");
    else navigate(url);
  }

  // Expand a node's immediate neighborhood via the backend graph BFS (one round
  // trip) and merge it into the session overlay, so new neighbors appear
  // without changing the URL/base view.
  async function expandNode(node: GraphNode) {
    const id = node.doc_id || docIdFromUri(node.uri);
    if (!id) return;
    try {
      const payload = await fetchNeighbors(vault!, id, 1);
      setOverlay((prev) => mergeGraph(prev, payload));
    } catch {
      /* node simply doesn't expand — leave the graph unchanged */
    }
  }

  const pinNode = (uri: string) =>
    setPinned((prev) => (prev.has(uri) ? prev : new Set(prev).add(uri)));
  const togglePin = (uri: string) =>
    setPinned((prev) => {
      const next = new Set(prev);
      if (next.has(uri)) next.delete(uri);
      else next.add(uri);
      return next;
    });
  const hideNode = (uri: string) => setHidden((prev) => new Set(prev).add(uri));

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

  // Friendly title for the focus-mode banner.
  const entryTitle =
    merged.nodes.find((n) => docIdFromUri(n.uri) === view.entry || n.doc_id === view.entry)?.name ??
    view.entry;

  // Degree-ranked top 50 for the sr-only list — 600 raw buttons is a
  // screen-reader wall; surface the structurally important nodes first, with
  // the sidebar search as the path to the long tail.
  const topNodes = useMemo(() => {
    const degree = new Map<string, number>();
    for (const e of merged.edges) {
      degree.set(e.source, (degree.get(e.source) ?? 0) + 1);
      degree.set(e.target, (degree.get(e.target) ?? 0) + 1);
    }
    return [...merged.nodes]
      .sort((a, b) => (degree.get(b.uri) ?? 0) - (degree.get(a.uri) ?? 0))
      .slice(0, 50);
  }, [merged]);

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
        // Collapsed: a thin strip with just the expand toggle at the top —
        // the same pattern as the workspace tree column's collapsed strip, so
        // collapsing the graph controls feels identical to collapsing the tree.
        <nav
          aria-label="Graph controls (collapsed)"
          className="h-full w-10 flex flex-col items-center py-2"
        >
          <button
            type="button"
            onClick={() => setSidebarOpen(true)}
            aria-label="Show graph controls"
            title="Show graph controls"
            className="flex h-9 w-9 items-center justify-center rounded-[var(--radius-md)] text-foreground-muted hover:text-foreground hover:bg-surface-hover transition-token focus:outline-none focus-visible:ring-2 focus-visible:ring-ring cursor-pointer"
          >
            <PanelLeftOpen className="h-4 w-4" aria-hidden />
          </button>
        </nav>
      )}

      <div className="relative bg-background overflow-hidden">
        {/* Focus mode: a clear "you're zoomed into one node — get out" banner.
            The whole-graph view (no entry) shows the orientation hint instead. */}
        {view.entry && (
          <div className="absolute top-3 left-1/2 -translate-x-1/2 z-10 w-max max-w-[90%]">
            <Alert variant="info">
              <div className="flex items-center gap-3">
                <TooltipText className="truncate max-w-[40ch]" tip={entryTitle}>Focused on {entryTitle}</TooltipText>
                <Button
                  variant="outline"
                  size="sm"
                  onClick={() => setView({ ...view, entry: undefined })}
                >
                  Show whole graph
                </Button>
              </div>
            </Alert>
          </div>
        )}

        {/* Non-blocking orientation hint (whole-graph view only), dismissible
            + persisted. Replaces the old blocking "pick an entry point" gate. */}
        {!view.entry && hintOpen && !loading && !error && merged.nodes.length > 0 && (
          <div className="absolute bottom-3 left-3 z-10 w-max max-w-[90%]">
            <Alert variant="info">
              <div className="flex items-center gap-3">
                <span>
                  {merged.nodes.length} nodes · {merged.edges.length} links — drag to pan ·
                  scroll to zoom · click a node to focus
                </span>
                <button
                  type="button"
                  onClick={dismissHint}
                  aria-label="Dismiss hint"
                  className="shrink-0 text-foreground-muted hover:text-foreground transition-colors cursor-pointer rounded-[var(--radius-sm)] focus:outline-none focus-visible:ring-2 focus-visible:ring-ring"
                >
                  <X className="h-3 w-3" aria-hidden />
                </button>
              </div>
            </Alert>
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
          <>
            <GraphCanvas
              // Remount on a STRUCTURAL change (entry/hops/filters), not on
              // selection, so the new graph auto-fits instead of rendering
              // off-screen. structureKey excludes `selected` by design.
              key={structureKey}
              ref={canvasRef}
              nodes={merged.nodes}
              edges={merged.edges}
              selected={selectedNode?.uri}
              pinned={pinned}
              hidden={hidden}
              onSelect={handleSelect}
              onExpand={expandNode}
              onPinNode={pinNode}
              onContextMenu={(node, x, y) => setMenu({ node, x, y })}
            />
            {/* Text alternative + keyboard path: the canvas is opaque to AT, so
                expose every node as a focusable button that selects it (opening
                the detail panel — the same handler the canvas click uses). */}
            <div className="sr-only">
              <h2>Graph nodes ({merged.nodes.length})</h2>
              <ul>
                {topNodes.map((n) => (
                  <li key={n.uri}>
                    <button type="button" onClick={() => handleSelect(n.uri)}>
                      {n.name} — {n.kind}
                      {n.group ? ` — cluster ${n.group}` : ""}
                    </button>
                  </li>
                ))}
                {merged.nodes.length > topNodes.length && (
                  <li>
                    {merged.nodes.length - topNodes.length} more — use the sidebar search to
                    reach them.
                  </li>
                )}
              </ul>
            </div>
          </>
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

      {menu && (
        <GraphContextMenu
          state={menu}
          pinned={pinned.has(menu.node.uri)}
          onClose={() => setMenu(null)}
          onOpen={(newTab) => openNode(menu.node, newTab)}
          onExpand={() => expandNode(menu.node)}
          onTogglePin={() => togglePin(menu.node.uri)}
          onHide={() => hideNode(menu.node.uri)}
          onFocus={() =>
            setView({
              ...view,
              entry: docIdFromUri(menu.node.uri) ?? menu.node.uri,
              selected: undefined,
            })
          }
          onCopyUri={() => navigator.clipboard?.writeText(menu.node.uri)}
        />
      )}
    </div>
  );
}
