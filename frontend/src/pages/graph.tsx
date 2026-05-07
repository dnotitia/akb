import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useNavigate, useParams, useSearchParams } from "react-router-dom";
import cytoscape, {
  type Core,
  type ElementDefinition,
  type Layouts,
  type LayoutOptions,
} from "cytoscape";
// @ts-expect-error — cytoscape-fcose lacks modern type declarations
import fcose from "cytoscape-fcose";
// @ts-expect-error — cytoscape-cola lacks modern type declarations
import cola from "cytoscape-cola";
import { ExternalLink, Focus, X } from "lucide-react";
import { getGraph } from "@/lib/api";
import { useTheme } from "@/hooks/use-theme";
import { Skeleton } from "@/components/ui/skeleton";
import { EmptyState } from "@/components/empty-state";

/* eslint-disable @typescript-eslint/no-explicit-any */

cytoscape.use(fcose);
cytoscape.use(cola);

type RelationKind =
  | "depends_on"
  | "related_to"
  | "implements"
  | "references"
  | "attached_to"
  | "derived_from";

type NodeType = "document" | "table" | "file";
type LayoutKind = "force" | "hierarchical" | "radial";

interface ApiNode {
  uri: string;
  name?: string;
  resource_type?: string;
}
interface ApiEdge {
  source: string;
  target: string;
  relation?: RelationKind | string;
}

function normalizeType(raw: string | undefined): NodeType {
  if (raw === "table") return "table";
  if (raw === "file") return "file";
  return "document";
}

const TYPE_LABEL: Record<NodeType, string> = {
  document: "document",
  table: "table",
  file: "file",
};

const RELATION_DASH: Record<RelationKind, number[]> = {
  depends_on: [],
  related_to: [2, 2],
  implements: [],
  references: [4, 4],
  attached_to: [],
  derived_from: [6, 2],
};

interface GraphColors {
  background: string;
  surface: string;
  foreground: string;
  mutedFg: string;
  accent: string;
  good: string;
  info: string;
  warning: string;
  border: string;
}

function readColors(): GraphColors {
  const root = getComputedStyle(document.documentElement);
  const g = (v: string, fb: string) =>
    root.getPropertyValue(v).trim() || fb;
  return {
    background: g("--color-background", "#faf9f5"),
    surface: g("--color-surface", "#ffffff"),
    foreground: g("--color-foreground", "#0a0908"),
    mutedFg: g("--color-foreground-muted", "#75716b"),
    accent: g("--color-accent", "#ff4d12"),
    good: g("--color-good", "#16a34a"),
    info: g("--color-info", "#3b82f6"),
    warning: g("--color-warning", "#ca8a04"),
    border: g("--color-border", "#0a0908"),
  };
}

function nodeFill(type: NodeType, c: GraphColors): string {
  if (type === "table") return c.info;
  if (type === "file") return c.good;
  return c.accent;
}

function relationColor(kind: string | undefined, c: GraphColors): string {
  switch (kind) {
    case "implements":
    case "attached_to":
      return c.good;
    case "depends_on":
    case "references":
      return c.info;
    case "derived_from":
      return c.warning;
    default:
      return c.mutedFg;
  }
}

function relationStyle(kind: string | undefined): string {
  const arr = RELATION_DASH[kind as RelationKind] ?? [];
  return arr.length === 0 ? "solid" : "dashed";
}

function relationDashPattern(kind: string | undefined): string {
  const arr = RELATION_DASH[kind as RelationKind] ?? [];
  return arr.join(" ");
}

function uriToRoute(uri: string): string | null {
  const m = uri.match(/^akb:\/\/([^/]+)\/(doc|table|file)\/(.+)$/);
  if (!m) return null;
  const [, vault, kind, rest] = m;
  return `/vault/${vault}/${kind}/${encodeURIComponent(rest)}`;
}

function layoutConfig(kind: LayoutKind, focusId?: string | null): LayoutOptions {
  if (kind === "hierarchical") {
    return {
      name: "breadthfirst",
      directed: true,
      padding: 30,
      spacingFactor: 1.1,
      animate: true,
      animationDuration: 700,
      animationEasing: "ease-in-out-cubic",
      fit: true,
      roots: focusId ? [focusId] : undefined,
    } as any;
  }
  if (kind === "radial") {
    return {
      name: "concentric",
      fit: true,
      padding: 40,
      minNodeSpacing: 22,
      animate: true,
      animationDuration: 700,
      animationEasing: "ease-in-out-cubic",
      concentric: (node: any) => {
        if (focusId && node.id() === focusId) return 100;
        return node.degree(false);
      },
      levelWidth: () => 1,
    } as any;
  }
  // force — one-shot fcose arranges the whole graph, then nodes stay
  // static. Continuous physics is cheap to kick off on demand (during
  // drag) but expensive to leave running on hundreds of nodes, so we
  // settle the initial frame here and let the drag handler spin up a
  // live cola only while the user is actively grabbing something.
  return {
    name: "fcose" as any,
    quality: "default",
    animate: true,
    animationDuration: 800,
    animationEasing: "ease-out",
    randomize: true,
    padding: 30,
    nodeRepulsion: 8000,
    idealEdgeLength: 70,
    fit: true,
  } as any;
}

// Live-drag layout — cola runs only while a node is being grabbed so
// neighbors shove apart in real time. Tight packing params keep the
// overall shape rounded rather than scattered.
function dragLayoutConfig(): LayoutOptions {
  return {
    name: "cola" as any,
    animate: true,
    refresh: 2,
    maxSimulationTime: 8000,
    ungrabifyWhileSimulating: false,
    fit: false,
    padding: 30,
    randomize: false,
    avoidOverlap: true,
    handleDisconnected: false,
    centerGraph: false,
    nodeSpacing: 4,
    edgeLength: 45,
    gravity: 0.3,
    infinite: true,
  } as any;
}

export default function GraphPage() {
  const { name } = useParams<{ name: string }>();
  const navigate = useNavigate();
  const [searchParams, setSearchParams] = useSearchParams();
  const focusUri = searchParams.get("focus") || undefined;
  const depth = Number(searchParams.get("depth") || "2");

  const { resolved } = useTheme();
  const containerRef = useRef<HTMLDivElement>(null);
  const cyRef = useRef<Core | null>(null);
  const layoutRef = useRef<Layouts | null>(null);

  const [graphData, setGraphData] = useState<{ nodes: ApiNode[]; edges: ApiEdge[] }>(
    { nodes: [], edges: [] },
  );
  const [loading, setLoading] = useState(true);
  const [layoutKind, setLayoutKind] = useState<LayoutKind>("force");
  const [typeFilter, setTypeFilter] = useState<Record<NodeType, boolean>>({
    document: true,
    table: true,
    file: true,
  });
  const [selectedUri, setSelectedUri] = useState<string | null>(null);

  const colors = useMemo<GraphColors>(() => readColors(), [resolved]);

  // Fetch graph data on name/focus/depth change
  useEffect(() => {
    if (!name) return;
    setLoading(true);
    const focusDocId = focusUri?.startsWith("akb://")
      ? focusUri.split("/").slice(4).join("/")
      : focusUri;
    getGraph(name, focusDocId, depth, focusUri ? 150 : 200)
      .then((d) => {
        const validUris = new Set(d.nodes.map((n: ApiNode) => n.uri));
        const edges = (d.edges || []).filter(
          (e: ApiEdge) => validUris.has(e.source) && validUris.has(e.target),
        );
        setGraphData({ nodes: d.nodes || [], edges });
        if (focusUri) setSelectedUri(focusUri);
      })
      .catch(console.error)
      .finally(() => setLoading(false));
  }, [name, focusUri, depth]);

  // Filtered elements for cytoscape — also mark top-degree "hub" nodes
  // so their labels stay visible at every zoom level.
  const elements = useMemo<ElementDefinition[]>(() => {
    const allowed = new Set(
      (Object.keys(typeFilter) as NodeType[]).filter((t) => typeFilter[t]),
    );
    const visibleUris = new Set(
      graphData.nodes
        .filter((n) => allowed.has(normalizeType(n.resource_type)))
        .map((n) => n.uri),
    );
    // Degree count per visible URI
    const degree = new Map<string, number>();
    for (const e of graphData.edges) {
      if (!visibleUris.has(e.source) || !visibleUris.has(e.target)) continue;
      degree.set(e.source, (degree.get(e.source) ?? 0) + 1);
      degree.set(e.target, (degree.get(e.target) ?? 0) + 1);
    }
    // Adaptive hub selection — statistical outlier test (degree above
    // mean+σ) intersected with a log-scale ceiling so the hub set scales
    // smoothly with graph size:
    //
    //   ~50 nodes  → up to  ~11 hubs
    //   ~500 nodes → up to  ~18 hubs
    //   ~2k nodes  → up to  ~22 hubs (clamped at 24)
    //
    // A floor of 5 keeps tiny graphs from ending up with zero anchors
    // when variance is low; min degree 2 still filters true isolates.
    const n = visibleUris.size;
    const degreeList = [...degree.values()];
    const mean =
      degreeList.length > 0
        ? degreeList.reduce((a, b) => a + b, 0) / degreeList.length
        : 0;
    const variance =
      degreeList.length > 0
        ? degreeList.reduce((a, b) => a + (b - mean) ** 2, 0) /
          degreeList.length
        : 0;
    const stdev = Math.sqrt(variance);
    const statThreshold = Math.max(mean + stdev, 2);
    const maxHubs = Math.min(
      24,
      Math.max(5, Math.ceil(Math.log2(Math.max(n, 2)) * 2)),
    );
    const hubs = new Set(
      [...degree.entries()]
        .filter(([, d]) => d >= statThreshold)
        .sort((a, b) => b[1] - a[1])
        .slice(0, maxHubs)
        .map(([uri]) => uri),
    );

    const nodes: ElementDefinition[] = graphData.nodes
      .filter((n) => visibleUris.has(n.uri))
      .map((n) => ({
        data: {
          id: n.uri,
          label: n.name || n.uri.split("/").pop() || "",
          kind: normalizeType(n.resource_type),
          isHub: hubs.has(n.uri),
        },
      }));
    const edges: ElementDefinition[] = graphData.edges
      .filter((e) => visibleUris.has(e.source) && visibleUris.has(e.target))
      .map((e, i) => ({
        data: {
          id: `e${i}`,
          source: e.source,
          target: e.target,
          relation: e.relation || "related_to",
        },
      }));
    return [...nodes, ...edges];
  }, [graphData, typeFilter]);

  // Initialize cytoscape once
  useEffect(() => {
    if (!containerRef.current) return;
    const cy = cytoscape({
      container: containerRef.current,
      elements: [],
      minZoom: 0.1,
      maxZoom: 4,
      style: [
        {
          selector: "node",
          style: {
            "background-color": (ele: any) =>
              nodeFill(ele.data("kind") as NodeType, colors),
            "width": 14,
            "height": 14,
            "border-width": 0,
            "border-color": colors.accent,
            "label": "",
            "color": colors.foreground,
            "font-size": 10,
            "font-family":
              '"JetBrains Mono Variable", "JetBrains Mono", ui-monospace, monospace',
            "text-valign": "bottom",
            "text-halign": "center",
            "text-margin-y": 4,
            "text-wrap": "ellipsis",
            "text-max-width": "160",
            "text-background-color": colors.background,
            "text-background-opacity": 0.85,
            "text-background-padding": 2 as any,
            "text-background-shape": "roundrectangle",
            "overlay-opacity": 0,
            // Smooth style transitions — width/height/border animate when
            // the node moves between default/hover/selected states.
            "transition-property":
              "width, height, border-width, opacity, overlay-opacity",
            "transition-duration": 200 as any,
            "transition-timing-function": "ease-out",
          },
        },
        // Hubs (top-degree nodes) — always labeled so the graph has
        // readable anchor points even at full-graph zoom.
        {
          selector: "node[?isHub]",
          style: {
            label: "data(label)",
            "font-weight": 600 as any,
            "width": 16,
            "height": 16,
          },
        },
        // Zoom-gated: when the .zoom-label class is toggled (by the
        // zoom handler), non-hub nodes show their labels too.
        {
          selector: "node.zoom-label",
          style: {
            label: "data(label)",
          },
        },
        {
          selector: "node.hover",
          style: {
            "border-width": 2.5,
            "width": 18,
            "height": 18,
            "label": "data(label)",
            // Subtle halo via cytoscape's overlay (behaves like a glow).
            "overlay-color": colors.accent,
            "overlay-opacity": 0.12,
            "overlay-padding": 10 as any,
          },
        },
        {
          selector: "node:selected",
          style: {
            "border-width": 3,
            "width": 20,
            "height": 20,
            "label": "data(label)",
            "overlay-color": colors.accent,
            "overlay-opacity": 0.18,
            "overlay-padding": 14 as any,
            "z-index": 999,
          },
        },
        {
          selector: "node.focus",
          style: {
            "background-color": colors.accent,
            "border-width": 3,
            "width": 22,
            "height": 22,
            "label": "data(label)",
            "overlay-color": colors.accent,
            "overlay-opacity": 0.22,
            "overlay-padding": 18 as any,
            "z-index": 1000,
          },
        },
        {
          selector: "node.neighbor",
          style: {
            "border-width": 1.5,
            "label": "data(label)",
          },
        },
        {
          selector: "node.faded",
          style: { opacity: 0.15 },
        },
        {
          selector: "edge",
          style: {
            "width": 0.9,
            "line-color": (ele: any) =>
              relationColor(ele.data("relation"), colors),
            "line-style": (ele: any) => relationStyle(ele.data("relation")) as any,
            "line-dash-pattern": ((ele: any) => {
              const p = relationDashPattern(ele.data("relation"));
              return p ? p.split(" ").map(Number) : [];
            }) as any,
            // Curved edges feel organic and prevent overlap at high degree
            // nodes. 0.2 keeps them gentle.
            "curve-style": "unbundled-bezier",
            "control-point-distances": [12] as any,
            "control-point-weights": [0.5] as any,
            "target-arrow-shape": "triangle-backcurve",
            "target-arrow-color": (ele: any) =>
              relationColor(ele.data("relation"), colors),
            "arrow-scale": 0.9,
            "opacity": 0.55,
            "transition-property": "line-color, width, opacity",
            "transition-duration": 200 as any,
            "transition-timing-function": "ease-out",
          },
        },
        {
          selector: "edge.highlighted",
          style: {
            width: 1.8,
            opacity: 1,
            "line-color": colors.accent,
            "target-arrow-color": colors.accent,
          },
        },
        {
          selector: "edge.faded",
          style: { opacity: 0.08 },
        },
      ],
      layout: { name: "preset" } as any,
    });
    cyRef.current = cy;

    // Zoom-gated labels: once the camera is zoomed in enough, reveal
    // every node's label. Threshold chosen so the overview stays
    // readable (hubs only) but hub-adjacent detail appears quickly as
    // the user leans in. Hub labels are driven by the `isHub` data
    // flag in the stylesheet and are NOT affected by this toggle.
    const ZOOM_LABEL_THRESHOLD = 1.3;
    const syncZoomLabels = () => {
      const showAll = cy.zoom() >= ZOOM_LABEL_THRESHOLD;
      const nonHubs = cy.nodes('[!isHub]');
      if (showAll) nonHubs.addClass("zoom-label");
      else nonHubs.removeClass("zoom-label");
    };
    cy.on("zoom", syncZoomLabels);
    // Initial pass after first layout settles.
    setTimeout(syncZoomLabels, 50);

    // Hover: halo + highlight connected + dim the rest.
    cy.on("mouseover", "node", (e) => {
      const n = e.target;
      const neighborhood = n.closedNeighborhood();
      n.addClass("hover");
      neighborhood.nodes().not(n).addClass("neighbor");
      neighborhood.edges().addClass("highlighted");
      cy.elements().not(neighborhood).addClass("faded");
    });
    cy.on("mouseout", "node", () => {
      cy.nodes().removeClass("hover neighbor faded");
      cy.edges().removeClass("highlighted faded");
    });

    // Single click — select node + animate camera toward it
    cy.on("tap", "node", (e) => {
      const n = e.target;
      setSelectedUri(n.id());
      // Smooth pan/zoom toward the clicked node (seahorse-graph style).
      cy.animate(
        {
          center: { eles: n },
          zoom: Math.max(cy.zoom(), 1.2),
        },
        { duration: 420, easing: "ease-in-out-cubic" },
      );
    });
    cy.on("tap", (e) => {
      if (e.target === cy) setSelectedUri(null);
    });

    // Double click — navigate to document
    cy.on("dbltap", "node", (e) => {
      const route = uriToRoute(e.target.id());
      if (route) navigate(route);
    });

    // Live reorganize on drag: spin up a cola simulation only while
    // the user is holding a node. This keeps the page idle-quiet when
    // nothing's happening, but gives the neighborhood a real-time
    // elastic response while dragging — without burning CPU forever.
    let dragLayout: Layouts | null = null;
    let settleTimer: number | undefined;
    cy.on("grab", "node", () => {
      if (settleTimer) {
        window.clearTimeout(settleTimer);
        settleTimer = undefined;
      }
      if (dragLayout) {
        try {
          dragLayout.stop();
        } catch {
          /* noop */
        }
      }
      dragLayout = cy.layout(dragLayoutConfig());
      dragLayout.run();
    });
    cy.on("free", "node", (e) => {
      // Lock the released node at its dropped position so the ongoing
      // cola simulation can't spring it back to force-equilibrium.
      // Other nodes stay free, so neighbors still settle nicely around
      // whichever nodes the user has pinned. Layout changes unlock
      // everything (see the sync effect).
      e.target.lock();
      // Let cola settle the rest for a beat after release, then halt.
      settleTimer = window.setTimeout(() => {
        if (dragLayout) {
          try {
            dragLayout.stop();
          } catch {
            /* noop */
          }
          dragLayout = null;
        }
      }, 600);
    });

    // Keep cytoscape's internal canvas in sync with container resizes —
    // mount-time layout can still be 0-height when the effect fires and
    // the tree sidebar toggle later changes grid column widths too.
    const ro = new ResizeObserver(() => {
      cy.resize();
      cy.fit(undefined, 40);
    });
    ro.observe(containerRef.current);

    return () => {
      ro.disconnect();
      if (settleTimer) window.clearTimeout(settleTimer);
      if (dragLayout) {
        try {
          dragLayout.stop();
        } catch {
          /* noop */
        }
      }
      if (layoutRef.current) {
        try {
          layoutRef.current.stop();
        } catch {
          /* noop */
        }
        layoutRef.current = null;
      }
      cy.destroy();
      cyRef.current = null;
    };
    // Re-init only on theme change so the color palette is fresh.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [resolved]);

  // Sync elements + layout. All three layouts are one-shot; Force just
  // uses fcose. A separate cola simulation kicks in only while the
  // user is actually dragging a node (see the grab/free handlers in
  // the init effect).
  useEffect(() => {
    const cy = cyRef.current;
    if (!cy) return;
    if (layoutRef.current) {
      try {
        layoutRef.current.stop();
      } catch {
        /* noop */
      }
      layoutRef.current = null;
    }
    cy.batch(() => {
      cy.elements().remove();
      cy.add(elements);
    });
    if (elements.length === 0) return;
    // Clear any pinned positions from previous drags so the fresh
    // layout is free to arrange every node from scratch.
    cy.nodes().unlock();
    const l = cy.layout(layoutConfig(layoutKind, focusUri));
    l.run();
    layoutRef.current = l;
  }, [elements, layoutKind, focusUri]);

  // Apply focus styling when focusUri or graph changes
  useEffect(() => {
    const cy = cyRef.current;
    if (!cy || !focusUri) return;
    cy.nodes().removeClass("focus");
    const focusNode = cy.getElementById(focusUri);
    if (focusNode && focusNode.length) focusNode.addClass("focus");
  }, [focusUri, elements]);

  // Apply selection styling
  useEffect(() => {
    const cy = cyRef.current;
    if (!cy) return;
    cy.nodes().unselect();
    if (selectedUri) {
      const n = cy.getElementById(selectedUri);
      if (n && n.length) n.select();
    }
  }, [selectedUri, elements]);

  const typeCounts = useMemo(() => {
    const c: Record<NodeType, number> = { document: 0, table: 0, file: 0 };
    for (const n of graphData.nodes) {
      c[normalizeType(n.resource_type)]++;
    }
    return c;
  }, [graphData.nodes]);

  const selectedNode = useMemo(() => {
    if (!selectedUri) return null;
    return graphData.nodes.find((n) => n.uri === selectedUri) || null;
  }, [selectedUri, graphData.nodes]);

  const selectedDegree = useMemo(() => {
    if (!selectedUri) return { inc: 0, out: 0 };
    let inc = 0;
    let out = 0;
    for (const e of graphData.edges) {
      if (e.source === selectedUri) out++;
      else if (e.target === selectedUri) inc++;
    }
    return { inc, out };
  }, [selectedUri, graphData.edges]);

  const selectedRoute = selectedNode ? uriToRoute(selectedNode.uri) : null;
  const selectedLabel =
    selectedNode?.name || selectedNode?.uri?.split("/").pop();

  function toggleType(t: NodeType) {
    setTypeFilter((s) => ({ ...s, [t]: !s[t] }));
  }

  function clearFocus() {
    const next = new URLSearchParams(searchParams);
    next.delete("focus");
    next.delete("depth");
    setSearchParams(next, { replace: true });
  }

  function focusOnSelected() {
    if (!selectedUri) return;
    const next = new URLSearchParams(searchParams);
    next.set("focus", selectedUri);
    next.set("depth", String(depth));
    setSearchParams(next, { replace: true });
  }

  const fitToView = useCallback(() => {
    const cy = cyRef.current;
    if (!cy) return;
    cy.animate(
      { fit: { eles: cy.elements(), padding: 40 } },
      { duration: 450, easing: "ease-in-out-cubic" },
    );
  }, []);

  const visibleCount = useMemo(() => {
    const allowed = new Set(
      (Object.keys(typeFilter) as NodeType[]).filter((t) => typeFilter[t]),
    );
    const ns = graphData.nodes.filter((n) =>
      allowed.has(normalizeType(n.resource_type)),
    );
    const nsSet = new Set(ns.map((n) => n.uri));
    const es = graphData.edges.filter(
      (e) => nsSet.has(e.source) && nsSet.has(e.target),
    );
    return { nodes: ns.length, edges: es.length };
  }, [graphData, typeFilter]);

  return (
    <div className="fade-up flex h-full bg-background">
      {/* Canvas */}
      <div className="relative flex-1 min-w-0 min-h-0 overflow-hidden">
        {/* Top-left — layout switcher + fit */}
        <div className="absolute top-3 left-3 z-10 flex items-center gap-px border border-border bg-surface/95 backdrop-blur">
          {(
            [
              ["force", "Force"],
              ["hierarchical", "Tree"],
              ["radial", "Radial"],
            ] as [LayoutKind, string][]
          ).map(([kind, label]) => (
            <button
              key={kind}
              onClick={() => setLayoutKind(kind)}
              className={
                "font-mono text-[10px] uppercase tracking-wider min-h-9 px-3 py-2 transition-colors cursor-pointer " +
                (layoutKind === kind
                  ? "bg-foreground text-background"
                  : "text-foreground-muted hover:text-foreground hover:bg-surface-muted")
              }
            >
              {label}
            </button>
          ))}
          <div className="w-px h-5 bg-border mx-0.5 self-center" />
          <button
            onClick={fitToView}
            title="Fit to view"
            className="font-mono text-[10px] uppercase tracking-wider min-h-9 px-3 py-2 text-foreground-muted hover:text-foreground hover:bg-surface-muted transition-colors cursor-pointer"
          >
            Fit
          </button>
        </div>

        {/* Top-right — node/edge counter */}
        <div className="absolute top-3 right-3 z-10 font-mono text-[10px] uppercase tracking-wider text-foreground-muted bg-surface/95 backdrop-blur px-2 py-1.5 border border-border tabular-nums">
          <span className="text-foreground font-medium">
            {visibleCount.nodes}
          </span>
          {visibleCount.nodes !== graphData.nodes.length && (
            <> / {graphData.nodes.length}</>
          )}{" "}
          nodes ·{" "}
          <span className="text-foreground font-medium">
            {visibleCount.edges}
          </span>{" "}
          edges
        </div>

        {/* Focus pill — left, below layout switcher */}
        {!loading && focusUri && (
          <div className="absolute top-14 left-3 z-10 border border-border bg-surface/95 backdrop-blur px-3 py-1.5 flex items-center gap-2 font-mono text-[11px]">
            <Focus className="h-3 w-3 text-accent" aria-hidden />
            <span className="text-foreground-muted">FOCUS</span>
            <span className="text-accent truncate max-w-[260px]">
              {focusUri.split("/").slice(4).join("/") || focusUri}
            </span>
            <span className="text-foreground-muted">· depth {depth}</span>
            <button
              onClick={clearFocus}
              aria-label="Clear focus"
              className="text-foreground-muted hover:text-destructive transition-colors cursor-pointer ml-1 focus:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2 focus-visible:ring-offset-surface"
            >
              <X className="h-3 w-3" aria-hidden />
            </button>
          </div>
        )}

        {/* Canvas container — always mounted so cytoscape can initialize.
            Cytoscape overrides `position: absolute` with its own container
            class, so use explicit h-full/w-full to fill the parent. */}
        <div
          ref={containerRef}
          className="h-full w-full"
          aria-label={`Knowledge graph. ${graphData.nodes.length} node${graphData.nodes.length === 1 ? "" : "s"}, ${graphData.edges.length} edge${graphData.edges.length === 1 ? "" : "s"}.`}
          aria-describedby="graph-help"
          role="application"
          tabIndex={0}
        />
        <p id="graph-help" className="sr-only">
          Pointer device required for full interaction. Drag to pan, scroll to zoom,
          click a node to focus its relations. Use the right rail to filter by type
          and inspect the selected node.
        </p>

        {/* Loading / empty overlays */}
        {loading && (
          <div className="absolute inset-0 p-6 bg-background">
            <Skeleton className="h-full" />
          </div>
        )}
        {!loading && graphData.nodes.length === 0 && (
          <div className="absolute inset-0 p-6 flex items-center justify-center bg-background">
            <EmptyState
              title="No relations on record"
              description="Tag a document's frontmatter with depends_on / related_to / implements, or call akb_link from your agent to wire two resources together. The graph rebuilds automatically."
            />
          </div>
        )}
      </div>

      {/* Right rail — legend + selected */}
      <aside
        className="w-[280px] shrink-0 border-l border-border bg-surface overflow-y-auto p-4 text-sm"
        aria-label="Graph legend and selection"
      >
        <div className="coord-ink mb-2">LEGEND · KIND</div>
        <ul className="text-xs text-foreground-muted mb-5 space-y-1">
          {(["document", "table", "file"] as const).map((t) => (
            <li key={t} className="flex items-center gap-2">
              <span
                className="inline-block h-2 w-2"
                style={{ backgroundColor: nodeFill(t, colors) }}
                aria-hidden
              />
              <label className="flex-1 inline-flex items-center gap-1.5 cursor-pointer">
                <input
                  type="checkbox"
                  checked={typeFilter[t]}
                  onChange={() => toggleType(t)}
                  className="h-3 w-3 accent-accent cursor-pointer"
                />
                <span className="font-mono">{TYPE_LABEL[t]}</span>
              </label>
              <span className="font-mono tabular-nums">{typeCounts[t]}</span>
            </li>
          ))}
        </ul>

        <div className="coord-ink mb-2">LEGEND · RELATION</div>
        <ul className="text-xs text-foreground-muted mb-6 space-y-1 font-mono">
          {(
            [
              ["implements", "implements"],
              ["depends_on", "depends on"],
              ["references", "references"],
              ["related_to", "related to"],
              ["attached_to", "attached to"],
              ["derived_from", "derived from"],
            ] as [RelationKind, string][]
          ).map(([rel, label]) => (
            <li key={rel} className="flex items-center gap-2">
              <DashSwatch kind={rel} colors={colors} />
              <span>{label}</span>
            </li>
          ))}
        </ul>

        {selectedNode ? (
          <section>
            <div className="coord-ink mb-2">SELECTED</div>
            <div className="font-serif text-[18px] leading-tight text-foreground mb-1">
              {selectedLabel}
            </div>
            <div className="coord mb-3 truncate">{selectedNode.uri}</div>
            <ul className="font-mono text-[11px] text-foreground-muted mb-3 space-y-0.5">
              <li>
                degree ·{" "}
                <span className="text-foreground tabular-nums">
                  {selectedDegree.inc + selectedDegree.out}
                </span>{" "}
                (↑{selectedDegree.out} ↓{selectedDegree.inc})
              </li>
              <li>
                kind ·{" "}
                <span className="text-foreground">
                  {normalizeType(selectedNode.resource_type)}
                </span>
              </li>
            </ul>
            <div className="flex flex-col gap-1.5 text-xs">
              {selectedRoute && (
                <button
                  onClick={() => navigate(selectedRoute)}
                  className="inline-flex items-center gap-1.5 text-accent hover:underline cursor-pointer focus:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2 focus-visible:ring-offset-surface"
                >
                  <ExternalLink className="h-3 w-3" aria-hidden />
                  open document
                </button>
              )}
              <button
                onClick={focusOnSelected}
                className="inline-flex items-center gap-1.5 text-foreground-muted hover:text-accent transition-colors cursor-pointer focus:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2 focus-visible:ring-offset-surface"
              >
                <Focus className="h-3 w-3" aria-hidden />
                focus + expand neighbors
              </button>
            </div>
          </section>
        ) : (
          <div className="coord">Click a node. Double-click to open.</div>
        )}
      </aside>
    </div>
  );
}

function DashSwatch({
  kind,
  colors,
}: {
  kind: RelationKind;
  colors: GraphColors;
}) {
  const color = relationColor(kind, colors);
  const dash = RELATION_DASH[kind];
  const dashArray = dash.length > 0 ? dash.join(" ") : "none";
  return (
    <svg width="20" height="6" aria-hidden className="shrink-0">
      <line
        x1={0}
        y1={3}
        x2={20}
        y2={3}
        stroke={color}
        strokeWidth={1.5}
        strokeDasharray={dashArray}
      />
    </svg>
  );
}
