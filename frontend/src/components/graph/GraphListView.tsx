import { ArrowDownLeft, ArrowUpRight, ChevronRight, Network } from "lucide-react";
import { useEffect, useMemo, useState } from "react";
import { Button } from "@/components/ui/button";
import { cn } from "@/lib/utils";
import { parseUri } from "@/lib/uri";
import type { GraphEdge, GraphNode } from "./graph-types";
import { KindSwatch } from "./graph-swatches";
import { degreeMap, endpointUri } from "./use-graph-data";

interface Props {
  nodes: GraphNode[];
  edges: GraphEdge[];
  selected?: string;
  onSelect: (uri: string) => void;
}

const PAGE_SIZE = 50;

export function GraphListView({ nodes, edges, selected, onSelect }: Props) {
  const [visibleCount, setVisibleCount] = useState(PAGE_SIZE);

  useEffect(() => {
    setVisibleCount(PAGE_SIZE);
  }, [nodes, edges]);

  const rows = useMemo(() => {
    const degree = degreeMap(edges);
    const directions = new Map<string, { incoming: number; outgoing: number }>();
    for (const edge of edges) {
      const source = endpointUri(edge.source);
      const target = endpointUri(edge.target);
      const from = directions.get(source) ?? { incoming: 0, outgoing: 0 };
      from.outgoing += 1;
      directions.set(source, from);
      const to = directions.get(target) ?? { incoming: 0, outgoing: 0 };
      to.incoming += 1;
      directions.set(target, to);
    }
    return [...nodes]
      .map((node) => ({
        node,
        location: parseUri(node.uri)?.collection || "Vault root",
        connections: degree.get(node.uri) ?? 0,
        incoming: directions.get(node.uri)?.incoming ?? 0,
        outgoing: directions.get(node.uri)?.outgoing ?? 0,
      }))
      .sort((a, b) => b.connections - a.connections || a.node.name.localeCompare(b.node.name));
  }, [nodes, edges]);

  return (
    <section className="absolute inset-0 overflow-y-auto bg-background rail-scroll" aria-labelledby="graph-list-title">
      <div className="mx-auto w-full max-w-6xl px-3 py-4 lg:px-5 lg:py-5">
        <div className="overflow-hidden rounded-[var(--radius-lg)] border border-border bg-surface shadow-xs">
          <div className="flex flex-wrap items-center justify-between gap-3 border-b border-border bg-surface-2 px-4 py-3">
            <div>
              <h2 id="graph-list-title" className="text-sm font-semibold text-foreground">
                Relationship index
              </h2>
              <p className="mt-0.5 text-xs text-foreground-muted">
                A keyboard-friendly view of every resource in this scene.
              </p>
            </div>
            <span className="text-xs tabular-nums text-foreground-muted">
              {rows.length} resource{rows.length === 1 ? "" : "s"}
            </span>
          </div>

          <div
            className="hidden grid-cols-[minmax(0,1fr)_8rem_9rem_2rem] gap-3 border-b border-border px-4 py-2 text-xs font-medium text-foreground-muted md:grid"
            aria-hidden
          >
            <span>Resource</span>
            <span>Type</span>
            <span>Connections</span>
            <span />
          </div>

          <ul className="divide-y divide-border">
            {rows.slice(0, visibleCount).map(({ node, location, connections, incoming, outgoing }, index) => (
              <li key={node.uri}>
                <button
                  type="button"
                  onClick={() => onSelect(node.uri)}
                  aria-current={selected === node.uri ? "true" : undefined}
                  className={cn(
                    "grid w-full grid-cols-[2rem_minmax(0,1fr)_auto] items-center gap-3 px-3 py-3 text-left transition-token focus:outline-none focus-visible:ring-2 focus-visible:ring-inset focus-visible:ring-ring md:grid-cols-[2rem_minmax(0,1fr)_8rem_9rem_2rem] md:px-4",
                    selected === node.uri
                      ? "bg-surface-selected text-surface-selected-foreground"
                      : "hover:bg-surface-hover",
                  )}
                >
                  <span className="text-xs tabular-nums text-foreground-muted">{index + 1}</span>
                  <span className="min-w-0">
                    <span className="block truncate text-sm font-medium text-foreground">{node.name}</span>
                    <span className="mt-0.5 block truncate text-xs text-foreground-muted" title={location}>
                      {node.group || location}
                    </span>
                    <span className="mt-1 flex items-center gap-3 text-xs text-foreground-muted md:hidden">
                      <span className="inline-flex items-center gap-1">
                        <ArrowUpRight className="h-3 w-3" aria-hidden /> {outgoing} out
                      </span>
                      <span className="inline-flex items-center gap-1">
                        <ArrowDownLeft className="h-3 w-3" aria-hidden /> {incoming} in
                      </span>
                    </span>
                  </span>
                  <span className="hidden items-center gap-2 text-xs capitalize text-foreground-muted md:flex">
                    <KindSwatch kind={node.kind} />
                    {node.kind}
                  </span>
                  <span className="hidden items-center gap-3 text-xs tabular-nums text-foreground-muted md:flex">
                    <span className="inline-flex items-center gap-1" title={`${outgoing} outgoing relationships`}>
                      <ArrowUpRight className="h-3 w-3" aria-hidden /> {outgoing}
                    </span>
                    <span className="inline-flex items-center gap-1" title={`${incoming} incoming relationships`}>
                      <ArrowDownLeft className="h-3 w-3" aria-hidden /> {incoming}
                    </span>
                    <span className="sr-only">{connections} total connections</span>
                  </span>
                  <ChevronRight className="h-4 w-4 text-foreground-muted" aria-hidden />
                </button>
              </li>
            ))}
          </ul>

          {rows.length > visibleCount && (
            <div className="border-t border-border px-4 py-3 text-center">
              <Button
                type="button"
                variant="outline"
                size="sm"
                onClick={() => setVisibleCount((count) => count + PAGE_SIZE)}
              >
                Show {Math.min(PAGE_SIZE, rows.length - visibleCount)} more
              </Button>
            </div>
          )}
        </div>

        <div className="mt-3 flex items-start gap-2 px-1 text-xs leading-relaxed text-foreground-muted">
          <Network className="mt-0.5 h-3.5 w-3.5 shrink-0" aria-hidden />
          <p>Select a row to inspect its properties and move between connected resources.</p>
        </div>
      </div>
    </section>
  );
}
