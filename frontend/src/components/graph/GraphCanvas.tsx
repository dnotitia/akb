// frontend/src/components/graph/GraphCanvas.tsx
import { forwardRef, useCallback, useEffect, useImperativeHandle, useMemo, useRef, useState } from "react";
import ForceGraph2D, { type ForceGraphMethods } from "react-force-graph-2d";
import { Pause, Play, Maximize2, Minus, Plus } from "lucide-react";
import { useTheme } from "@/hooks/use-theme";
import {
  RELATION_DASH,
  readColors,
  type GraphEdge,
  type GraphNode,
  type GraphColors,
} from "./graph-types";
import { endpointUri } from "./use-graph-data";

export interface GraphCanvasHandle {
  centerOnNode: (uri: string) => void;
}

interface Props {
  nodes: GraphNode[];
  edges: GraphEdge[];
  selected?: string;
  pinned: Set<string>;
  hidden: Set<string>;
  degraded: boolean;
  onSelect: (uri: string | undefined) => void;
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

const NODE_SIZE = 16;
const LABEL_ZOOM_THRESHOLD = 0.3;

export const GraphCanvas = forwardRef<GraphCanvasHandle, Props>(function GraphCanvas(
  {
    nodes,
    edges,
    selected,
    pinned,
    hidden,
    degraded,
    onSelect,
    onContextMenu,
  },
  ref,
) {
  const fgRef = useRef<ForceGraphMethods | undefined>(undefined);
  const fittedRef = useRef(false);
  const { theme } = useTheme();
  const [colors, setColors] = useState<GraphColors>(() => readColors());
  const [frozen, setFrozen] = useState(false);

  useImperativeHandle(
    ref,
    (): GraphCanvasHandle => ({
      centerOnNode: (uri) => {
        const n = nodes.find((node) => node.uri === uri);
        if (!n || n.x == null || n.y == null) return;
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
    if (degraded || frozen) {
      fg.d3Force("center", null);
      fg.pauseAnimation();
    } else {
      fg.resumeAnimation();
    }
  }, [degraded, frozen]);

  // Auto fit-to-view once when nodes first populate
  useEffect(() => {
    if (fittedRef.current) return;
    if (nodes.length === 0) return;
    const t = setTimeout(() => {
      fgRef.current?.zoomToFit(400, 60);
      fittedRef.current = true;
    }, 500);
    return () => clearTimeout(t);
  }, [nodes.length]);

  const visibleNodes = useMemo(
    () => nodes.filter((n) => !hidden.has(n.uri)),
    [nodes, hidden],
  );
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

  const paintNode = useCallback(
    (n: RenderNode, ctx: CanvasRenderingContext2D, scale: number) => {
      const x = n.x || 0;
      const y = n.y || 0;
      const isSelected = n.uri === selected;
      const isPinned = pinned.has(n.uri);

      ctx.beginPath();
      ctx.rect(x - NODE_SIZE / 2, y - NODE_SIZE / 2, NODE_SIZE, NODE_SIZE);
      switch (n.kind) {
        case "document":
          ctx.fillStyle = colors.surfaceMuted;
          ctx.strokeStyle = isSelected ? colors.accent : colors.foreground;
          ctx.lineWidth = isSelected ? 2.5 : 1.5;
          ctx.setLineDash([]);
          break;
        case "table":
          ctx.fillStyle = colors.surface;
          ctx.strokeStyle = colors.accent;
          ctx.lineWidth = isSelected ? 2.5 : 1.5;
          ctx.setLineDash([]);
          break;
        case "file":
          ctx.fillStyle = "transparent";
          ctx.strokeStyle = isSelected ? colors.accent : colors.foregroundMuted;
          ctx.lineWidth = isSelected ? 2.5 : 1;
          ctx.setLineDash([3, 2]);
          break;
      }
      ctx.fill();
      ctx.stroke();
      ctx.setLineDash([]);

      if (isPinned) {
        ctx.fillStyle = colors.accent;
        ctx.fillRect(x + NODE_SIZE / 2 - 3, y - NODE_SIZE / 2, 3, 3);
      }

      if (scale > 0.6) {
        const glyph = n.kind === "document" ? "D" : n.kind === "table" ? "T" : "F";
        ctx.font = `bold 8px ui-monospace, SFMono-Regular, monospace`;
        ctx.textAlign = "center";
        ctx.textBaseline = "middle";
        ctx.fillStyle = colors.foregroundMuted;
        ctx.fillText(glyph, x, y);
      }

      if (scale > LABEL_ZOOM_THRESHOLD) {
        const label = (n.name || "").slice(0, 16).toUpperCase();
        ctx.font = `10px ui-monospace, SFMono-Regular, monospace`;
        ctx.textAlign = "center";
        ctx.textBaseline = "top";
        ctx.fillStyle = colors.foregroundMuted;
        ctx.fillText(label, x, y + NODE_SIZE / 2 + 4);
      }
    },
    [colors, selected, pinned],
  );

  const paintLink = useCallback(
    (l: RenderEdge, ctx: CanvasRenderingContext2D) => {
      const src = typeof l.source === "object" ? l.source : null;
      const tgt = typeof l.target === "object" ? l.target : null;
      if (!src || !tgt) return;
      const dash = RELATION_DASH[l.relation];
      const isAccent = l.relation === "implements" || l.relation === "derived_from";
      ctx.beginPath();
      ctx.moveTo(src.x || 0, src.y || 0);
      ctx.lineTo(tgt.x || 0, tgt.y || 0);
      ctx.strokeStyle = isAccent ? colors.accent : colors.foregroundMuted;
      ctx.lineWidth = l.relation === "attached_to" ? 1 : 1.5;
      ctx.setLineDash(dash);
      ctx.stroke();
      ctx.setLineDash([]);
    },
    [colors],
  );

  const handleNodeClick = useCallback(
    (n: RenderNode) => {
      onSelect(n.uri);
    },
    [onSelect],
  );

  const handleBackgroundClick = useCallback(() => {
    onSelect(undefined);
  }, [onSelect]);

  const handleNodeRightClick = useCallback(
    (n: RenderNode, ev: MouseEvent) => {
      // Native context menu remains available until the custom menu lands.
      // TODO(graph-context-menu): preventDefault here once the floating
      // menu (Pin/Unpin/Hide/Copy/Open-new-tab) is wired in the page shell.
      onContextMenu(n, ev.clientX, ev.clientY);
    },
    [onContextMenu],
  );

  const handleNodeDrag = useCallback((_n: RenderNode, _t: { x: number; y: number }) => {
    // TODO(task-6): shift+drag pinning.
    //
    // react-force-graph-2d's `onNodeDrag` does NOT pass a MouseEvent, so we
    // cannot read ev.shiftKey here. The page shell (Task 6) must:
    //   1. Track shift-key state via window.addEventListener('keydown'|'keyup').
    //   2. When a drag begins while shift is held, add the node's URI to the
    //      `pinned` set passed in via props. Pinning then takes effect through
    //      the existing `onNodeDragEnd` branch that preserves fx/fy for pinned
    //      nodes (line below).
    // This canvas component intentionally stays stateless about keyboard.
  }, []);

  return (
    <div
      className="absolute inset-0"
      role="img"
      aria-label={`Knowledge graph: ${nodes.length} nodes, ${edges.length} edges`}
    >
      <div className="absolute top-3 left-3 z-10 flex gap-1">
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
      <ForceGraph2D
        ref={fgRef as never}
        graphData={graphData as never}
        backgroundColor={colors.background}
        nodeId="uri"
        linkSource="source"
        linkTarget="target"
        nodeCanvasObject={paintNode as never}
        linkCanvasObject={paintLink as never}
        linkDirectionalArrowLength={5}
        linkDirectionalArrowRelPos={1}
        linkDirectionalArrowColor={colors.foregroundMuted}
        cooldownTicks={degraded ? 0 : 200}
        onNodeClick={handleNodeClick as never}
        onBackgroundClick={handleBackgroundClick}
        onNodeRightClick={handleNodeRightClick as never}
        onNodeDrag={handleNodeDrag as never}
        onNodeDragEnd={(n) => {
          if (!pinned.has((n as unknown as RenderNode).uri)) {
            (n as unknown as RenderNode).fx = undefined;
            (n as unknown as RenderNode).fy = undefined;
          }
        }}
      />
    </div>
  );
});

function CanvasButton({
  onClick,
  label,
  icon: Icon,
}: {
  onClick: () => void;
  label: string;
  icon: React.ComponentType<{ className?: string; "aria-hidden"?: boolean }>;
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      title={label}
      aria-label={label}
      className="inline-flex items-center justify-center h-8 w-8 bg-surface border border-border text-foreground-muted hover:text-foreground hover:bg-surface-muted focus:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2 focus-visible:ring-offset-background transition-colors cursor-pointer"
    >
      <Icon className="h-3 w-3" aria-hidden />
    </button>
  );
}
