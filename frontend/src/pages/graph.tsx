import {
  ChevronDown,
  ChevronUp,
  Maximize2,
  Minus,
  Network,
  Plus,
} from "lucide-react";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useNavigate, useParams, useSearchParams } from "react-router-dom";
import { EmptyState } from "@/components/empty-state";
import { GraphCanvas, type GraphCanvasHandle } from "@/components/graph/GraphCanvas";
import { GraphContextMenu, type GraphMenuState } from "@/components/graph/GraphContextMenu";
import { GraphDetailPanel } from "@/components/graph/GraphDetailPanel";
import { GraphListView } from "@/components/graph/GraphListView";
import { GraphToolbar, type GraphDisplayMode } from "@/components/graph/GraphToolbar";
import { KindSwatch, RelationSwatch } from "@/components/graph/graph-swatches";
import { queryToView, viewToQuery } from "@/components/graph/graph-state";
import {
  ALL_NODE_KINDS,
  ALL_RELATIONS,
  kindToSegment,
  type GraphEdge,
  type GraphNode,
  type GraphView,
  type RelatedRef,
} from "@/components/graph/graph-types";
import {
  applyFilters,
  degreeMap,
  docIdFromUri,
  endpointUri,
  fetchNeighbors,
  mergeGraph,
  useFullGraph,
  useNeighborhood,
} from "@/components/graph/use-graph-data";
import { groupOf } from "@/components/graph/cluster";
import { Button } from "@/components/ui/button";
import { Skeleton } from "@/components/ui/skeleton";

function initialDisplayMode(): GraphDisplayMode {
  if (typeof window === "undefined") return "graph";
  // The relationship canvas is the page's primary job on desktop. List is an
  // accessible alternate view, not a sticky replacement that can make the
  // graph appear to have vanished on a later visit.
  if (window.matchMedia?.("(max-width: 767px)").matches) return "list";
  return "graph";
}

export default function GraphPage() {
  const { name: vault } = useParams<{ name: string }>();
  const [search, setSearch] = useSearchParams();
  const navigate = useNavigate();
  const view = useMemo(() => queryToView(search), [search]);

  const setView = useCallback(
    (next: GraphView) => {
      setSearch(new URLSearchParams(viewToQuery(next)), { replace: true });
    },
    [setSearch],
  );

  const fullQuery = useFullGraph(vault!, !view.entry);
  const neighborhoodQuery = useNeighborhood(vault!, view.entry, view.hops);
  const base = view.entry ? neighborhoodQuery.data : fullQuery.data;
  const loading = view.entry ? neighborhoodQuery.isLoading : fullQuery.isLoading;
  const error = view.entry ? neighborhoodQuery.error : fullQuery.error;

  const [overlay, setOverlay] = useState<{ nodes: GraphNode[]; edges: GraphEdge[] }>({
    nodes: [],
    edges: [],
  });
  useEffect(() => {
    setOverlay({ nodes: [], edges: [] });
  }, [view.entry, view.hops, vault]);

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
    // Selection does not change graph structure and must not restart the force layout.
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [base, overlay, structureKey],
  );

  const canvasRef = useRef<GraphCanvasHandle>(null);
  const [displayMode, setDisplayModeState] = useState<GraphDisplayMode>(initialDisplayMode);
  const [pinned, setPinned] = useState<Set<string>>(new Set());
  const [hidden, setHidden] = useState<Set<string>>(new Set());
  const [hideOrphans, setHideOrphans] = useState(false);
  const [menu, setMenu] = useState<GraphMenuState | null>(null);

  function setDisplayMode(mode: GraphDisplayMode) {
    setDisplayModeState(mode);
  }

  function handleSelect(uri: string | undefined) {
    const selected = uri ? (docIdFromUri(uri) ?? uri) : undefined;
    if (selected === view.selected) return;
    setView({ ...view, selected });
  }

  function openNode(node: GraphNode, newTab = false) {
    const id = node.doc_id || docIdFromUri(node.uri);
    if (!id) return;
    const url = `/vault/${vault}/${kindToSegment(node.kind)}/${encodeURIComponent(id)}`;
    if (newTab) window.open(url, "_blank", "noopener");
    else navigate(url);
  }

  async function expandNode(node: GraphNode) {
    const id = node.doc_id || docIdFromUri(node.uri);
    if (!id) return;

    let payload;
    try {
      payload = await fetchNeighbors(vault!, id, 1);
    } catch (expandError) {
      console.error("graph: failed to expand node", id, expandError);
      return;
    }
    if (payload.nodes.length === 0) return;

    const present = new Set(merged.nodes.map((item) => item.uri));
    const hasPosition = node.x != null && node.y != null;
    let newIndex = 0;
    for (const next of payload.nodes) {
      if (present.has(next.uri)) continue;
      if (hasPosition) {
        const angle = newIndex * 2.39996;
        const radius = 12 + newIndex * 6;
        next.x = node.x! + Math.cos(angle) * radius;
        next.y = node.y! + Math.sin(angle) * radius;
      }
      newIndex += 1;
    }
    setOverlay((current) => mergeGraph(current, payload));
  }

  const connected = useMemo(() => {
    const uris = new Set<string>();
    for (const edge of merged.edges) {
      uris.add(endpointUri(edge.source));
      uris.add(endpointUri(edge.target));
    }
    return uris;
  }, [merged]);

  const orphanCount = useMemo(
    () => merged.nodes.reduce((count, node) => count + (connected.has(node.uri) ? 0 : 1), 0),
    [merged.nodes, connected],
  );

  const displayed = useMemo(() => {
    if (!hideOrphans || orphanCount === 0) return merged;
    return {
      nodes: merged.nodes.filter((node) => connected.has(node.uri)),
      edges: merged.edges,
      meta: merged.meta,
    };
  }, [merged, hideOrphans, orphanCount, connected]);

  const { selectedNode, selectedDocId } = useMemo(() => {
    const node = view.selected
      ? displayed.nodes.find(
          (item) =>
            item.uri === view.selected ||
            item.doc_id === view.selected ||
            docIdFromUri(item.uri) === view.selected,
        )
      : undefined;
    return {
      selectedNode: node,
      selectedDocId: node ? node.doc_id || docIdFromUri(node.uri) : null,
    };
  }, [displayed.nodes, view.selected]);

  const detailOpen = !!selectedNode && !!selectedDocId;

  const focusTitle = useMemo(() => {
    if (!view.entry) return undefined;
    return (
      merged.nodes.find(
        (node) =>
          node.uri === view.entry ||
          node.doc_id === view.entry ||
          docIdFromUri(node.uri) === view.entry,
      )?.name || view.entry
    );
  }, [merged.nodes, view.entry]);

  const hubs = useMemo(() => {
    const degree = degreeMap(displayed.edges);
    return [...displayed.nodes]
      .filter((node) => (degree.get(node.uri) ?? 0) > 0)
      .sort((a, b) => (degree.get(b.uri) ?? 0) - (degree.get(a.uri) ?? 0))
      .slice(0, 8);
  }, [displayed]);

  const selectionAnnouncement = useMemo(() => {
    if (!selectedNode) return "";
    let connections = 0;
    for (const edge of displayed.edges) {
      if (
        endpointUri(edge.source) === selectedNode.uri ||
        endpointUri(edge.target) === selectedNode.uri
      ) {
        connections += 1;
      }
    }
    return `Selected ${selectedNode.name}, ${selectedNode.kind}, ${connections} connection${connections === 1 ? "" : "s"}`;
  }, [selectedNode, displayed.edges]);

  function handleSelectRelated(relation: RelatedRef) {
    if (!merged.nodes.some((node) => node.uri === relation.uri)) {
      const node: GraphNode = {
        uri: relation.uri,
        name: relation.name,
        kind: relation.kind,
        group: groupOf(relation.uri),
      };
      const sourceUri = selectedNode?.uri;
      const edge: GraphEdge | null = sourceUri
        ? relation.direction === "outgoing"
          ? { source: sourceUri, target: relation.uri, relation: relation.relation }
          : { source: relation.uri, target: sourceUri, relation: relation.relation }
        : null;
      setOverlay((current) => ({
        nodes: current.nodes.some((item) => item.uri === relation.uri)
          ? current.nodes
          : [...current.nodes, node],
        edges:
          edge &&
          !current.edges.some(
            (item) =>
              endpointUri(item.source) === edge.source &&
              endpointUri(item.target) === edge.target &&
              item.relation === edge.relation,
          )
            ? [...current.edges, edge]
            : current.edges,
      }));
    }
    setView({ ...view, selected: docIdFromUri(relation.uri) ?? relation.uri });
  }

  function resetFilters() {
    setHideOrphans(false);
    setHidden(new Set());
    setView({
      ...view,
      types: new Set(ALL_NODE_KINDS),
      relations: new Set(ALL_RELATIONS),
    });
  }

  const sourceHasNodes = (base?.nodes.length || 0) > 0;

  return (
    <div className="flex h-full min-h-0 flex-col overflow-hidden bg-background">
      <GraphToolbar
        vault={vault!}
        view={view}
        onChange={setView}
        onNavigate={(queryString) => {
          navigate(
            { search: queryString.startsWith("?") ? queryString : `?${queryString}` },
            { replace: true },
          );
        }}
        hubs={hubs}
        nodeCount={displayed.nodes.length}
        edgeCount={displayed.edges.length}
        totalNodes={base?.meta?.nodesTotal}
        truncated={base?.meta?.truncated}
        focusTitle={focusTitle}
        displayMode={displayMode}
        onDisplayModeChange={setDisplayMode}
        orphanCount={orphanCount}
        hideOrphans={hideOrphans}
        onToggleOrphans={() => setHideOrphans((value) => !value)}
        hiddenCount={hidden.size}
        onUnhideAll={() => setHidden(new Set())}
        onFit={() => canvasRef.current?.fit()}
      />

      <main className="relative min-h-0 flex-1 overflow-hidden" aria-label="Graph exploration workspace">
        {loading ? (
          <GraphLoadingState />
        ) : error ? (
          <EmptyState
            title="The graph could not be loaded"
            description="The Vault is still available. Retry the relationship view without changing your data."
            action={
              <Button
                variant="outline"
                size="sm"
                onClick={() => (view.entry ? neighborhoodQuery.refetch() : fullQuery.refetch())}
              >
                Retry
              </Button>
            }
          />
        ) : displayed.nodes.length === 0 ? (
          sourceHasNodes ? (
            <EmptyState
              title="No resources match these filters"
              description="Reset the active filters to restore the graph scene."
              action={<Button variant="outline" size="sm" onClick={resetFilters}>Reset filters</Button>}
            />
          ) : (
            <EmptyState
              title="There is nothing to map yet"
              description="The graph appears as documents, tables, and files begin linking to one another."
              action={<Button variant="outline" size="sm" onClick={() => navigate(`/vault/${vault}`)}>Back to Overview</Button>}
            />
          )
        ) : displayMode === "list" ? (
          <GraphListView
            nodes={displayed.nodes}
            edges={displayed.edges}
            selected={selectedNode?.uri}
            onSelect={handleSelect}
          />
        ) : (
          <>
            <GraphCanvas
              key={`${structureKey}:${hideOrphans ? "orphans-hidden" : "orphans-shown"}`}
              ref={canvasRef}
              nodes={displayed.nodes}
              edges={displayed.edges}
              selected={selectedNode?.uri}
              pinned={pinned}
              hidden={hidden}
              onSelect={handleSelect}
              onExpand={expandNode}
              onPinNode={(uri) =>
                setPinned((current) => (current.has(uri) ? current : new Set(current).add(uri)))
              }
              onContextMenu={(node, x, y) => setMenu({ node, x, y })}
            />
            <CanvasControls
              onZoomOut={() => canvasRef.current?.zoomOut()}
              onZoomIn={() => canvasRef.current?.zoomIn()}
              onFit={() => canvasRef.current?.fit()}
            />
            <GraphLegend />
            {displayed.edges.length === 0 && <NoRelationshipsNotice />}
          </>
        )}

        {detailOpen && selectedNode && selectedDocId && (
          <>
            <button
              type="button"
              aria-label="Close resource inspector"
              onClick={() => setView({ ...view, selected: undefined })}
              className="absolute inset-0 z-[calc(var(--z-overlay)-1)] hidden bg-background/80 max-sm:block"
            />
            <GraphDetailPanel
              vault={vault!}
              docId={selectedDocId}
              name={selectedNode.name}
              kind={selectedNode.kind}
              uri={selectedNode.uri}
              onSelectRelated={handleSelectRelated}
              onFitToNode={(uri) => canvasRef.current?.centerOnNode(uri)}
              onFocus={
                view.entry !== (selectedNode.kind === "document" ? selectedDocId : selectedNode.uri)
                  ? () =>
                      setView({
                        ...view,
                        entry: selectedNode.kind === "document" ? selectedDocId : selectedNode.uri,
                        selected: undefined,
                      })
                  : undefined
              }
              onClose={() => setView({ ...view, selected: undefined })}
              onTogglePin={() =>
                setPinned((current) => {
                  const next = new Set(current);
                  if (next.has(selectedNode.uri)) next.delete(selectedNode.uri);
                  else next.add(selectedNode.uri);
                  return next;
                })
              }
              pinned={pinned.has(selectedNode.uri)}
            />
          </>
        )}

        <div aria-live="polite" className="sr-only">{selectionAnnouncement}</div>
      </main>

      {menu && (
        <GraphContextMenu
          state={menu}
          pinned={pinned.has(menu.node.uri)}
          onClose={() => setMenu(null)}
          onOpen={(newTab) => openNode(menu.node, newTab)}
          onExpand={() => expandNode(menu.node)}
          onTogglePin={() =>
            setPinned((current) => {
              const next = new Set(current);
              if (next.has(menu.node.uri)) next.delete(menu.node.uri);
              else next.add(menu.node.uri);
              return next;
            })
          }
          onHide={() => setHidden((current) => new Set(current).add(menu.node.uri))}
          onFocus={() =>
            setView({
              ...view,
              entry:
                menu.node.kind === "document"
                  ? docIdFromUri(menu.node.uri) ?? menu.node.uri
                  : menu.node.uri,
              selected: undefined,
            })
          }
          onCopyUri={() => navigator.clipboard?.writeText(menu.node.uri)}
        />
      )}
    </div>
  );
}

function GraphLoadingState() {
  return (
    <div className="absolute inset-0 p-3 lg:p-4" role="status" aria-label="Loading knowledge graph">
      <div className="relative h-full overflow-hidden rounded-[var(--radius-lg)] border border-border bg-surface">
        <Skeleton className="absolute left-1/2 top-1/2 h-20 w-20 -translate-x-1/2 -translate-y-1/2 rounded-full" />
        <Skeleton className="absolute left-[26%] top-[30%] h-12 w-12 rounded-full" />
        <Skeleton className="absolute bottom-[24%] right-[28%] h-14 w-14 rounded-full" />
      </div>
    </div>
  );
}

function NoRelationshipsNotice() {
  return (
    <div className="pointer-events-none absolute left-3 top-3 z-[var(--z-raised)] flex max-w-sm items-start gap-2 rounded-[var(--radius-md)] border border-border bg-surface/95 px-3 py-2 shadow-sm">
      <Network className="mt-0.5 h-4 w-4 shrink-0 text-primary" aria-hidden />
      <div>
        <p className="text-xs font-semibold text-foreground">No relationships yet</p>
        <p className="mt-0.5 text-xs leading-relaxed text-foreground-muted">
          Resources remain visible and selectable while links are added.
        </p>
      </div>
    </div>
  );
}

function CanvasControls({ onZoomOut, onZoomIn, onFit }: { onZoomOut: () => void; onZoomIn: () => void; onFit: () => void }) {
  const actions = [
    { label: "Zoom out", icon: Minus, action: onZoomOut },
    { label: "Zoom in", icon: Plus, action: onZoomIn },
    { label: "Fit graph", icon: Maximize2, action: onFit },
  ];
  return (
    <div className="absolute bottom-3 right-3 z-[var(--z-raised)] flex overflow-hidden rounded-[var(--radius-md)] border border-border bg-surface shadow-sm divide-x divide-border">
      {actions.map(({ label, icon: Icon, action }) => (
        <button
          key={label}
          type="button"
          onClick={action}
          aria-label={label}
          title={label}
          className="inline-flex h-9 w-9 items-center justify-center text-foreground-muted transition-token hover:bg-surface-hover hover:text-foreground focus:outline-none focus-visible:ring-2 focus-visible:ring-inset focus-visible:ring-ring"
        >
          <Icon className="h-4 w-4" aria-hidden />
        </button>
      ))}
    </div>
  );
}

function GraphLegend() {
  const [open, setOpen] = useState(false);
  return (
    <div className="absolute bottom-3 left-3 z-[var(--z-raised)]">
      {open ? (
        <div className="w-60 rounded-[var(--radius-md)] border border-border bg-surface p-3 shadow-md">
          <div className="flex items-center justify-between">
            <p className="text-xs font-semibold text-foreground">Visual key</p>
            <button
              type="button"
              onClick={() => setOpen(false)}
              aria-label="Hide visual key"
              className="inline-flex h-7 w-7 items-center justify-center rounded-[var(--radius-sm)] text-foreground-muted hover:bg-surface-hover hover:text-foreground focus:outline-none focus-visible:ring-2 focus-visible:ring-ring"
            >
              <ChevronDown className="h-3.5 w-3.5" aria-hidden />
            </button>
          </div>
          <div className="mt-2 grid grid-cols-2 gap-x-3 gap-y-2 text-xs text-foreground-muted">
            <span className="flex items-center gap-2"><KindSwatch kind="document" />Document</span>
            <span className="flex items-center gap-2"><KindSwatch kind="table" />Table</span>
            <span className="flex items-center gap-2"><KindSwatch kind="file" />File</span>
            <span className="flex items-center gap-2"><RelationSwatch relation="depends_on" />Structural</span>
            <span className="col-span-2 flex items-center gap-2"><RelationSwatch relation="references" />Reference or related link</span>
          </div>
        </div>
      ) : (
        <button
          type="button"
          onClick={() => setOpen(true)}
          className="inline-flex h-9 items-center gap-2 rounded-[var(--radius-md)] border border-border bg-surface px-3 text-xs font-medium text-foreground-muted shadow-sm transition-token hover:bg-surface-hover hover:text-foreground focus:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2 focus-visible:ring-offset-background"
        >
          <ChevronUp className="h-3.5 w-3.5" aria-hidden />
          Visual key
        </button>
      )}
    </div>
  );
}
