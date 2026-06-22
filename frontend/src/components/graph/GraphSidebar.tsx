// frontend/src/components/graph/GraphSidebar.tsx
import { useEffect, useRef, useState } from "react";
import { PanelLeftClose, Search as SearchIcon, X } from "lucide-react";
import { searchDocs } from "@/lib/api";
import { useGraphHistory } from "@/hooks/use-graph-history";
import { useDebounce } from "@/hooks/use-debounce";
import {
  ALL_NODE_KINDS,
  ALL_RELATIONS,
  RELATION_CLASS,
  RELATION_DASH,
  RELATION_LABEL,
  type GraphNode,
  type GraphView,
  type NodeKind,
  type RelationKind,
} from "./graph-types";
import { viewToQuery } from "./graph-state";
import { Section } from "./Section";
import { TooltipText } from "@/components/ui/tooltip-text";
import { cn } from "@/lib/utils";

interface Props {
  vault: string;
  view: GraphView;
  onChange: (next: GraphView) => void;
  onNavigate: (queryString: string) => void;
  /** Highest-degree nodes in the current graph — a visible way INTO a large
   *  graph (click to select + center). */
  hubs: GraphNode[];
  /** How many degree-0 nodes the current graph has (for the orphans toggle). */
  orphanCount: number;
  hideOrphans: boolean;
  onToggleOrphans: () => void;
  /** Select + center a node (used by the Hubs list). */
  onSelectNode: (uri: string) => void;
  /** When provided, renders a collapse control in the sidebar header. */
  onCollapse?: () => void;
}

interface SearchHit {
  doc_id: string;
  title: string;
  type: NodeKind;
}

/** Last-used traversal depth, persisted so a focus picks up the user's
 *  preferred radius instead of always resetting to 2. */
const HOPS_KEY = "akb:graph:hops";
function savedHops(): 1 | 2 | 3 {
  const v = typeof localStorage !== "undefined" ? localStorage.getItem(HOPS_KEY) : null;
  return v === "1" ? 1 : v === "3" ? 3 : 2;
}

/** A tiny line in the relation's own encoding (structural = solid/darker/
 *  thicker, associative = dashed/muted/thinner) so the sidebar doubles as the
 *  legend — the user can connect "this dashed line in the canvas = related_to". */
function RelationSwatch({ relation }: { relation: RelationKind }) {
  const structural = RELATION_CLASS[relation] === "structural";
  const dash = RELATION_DASH[relation].join(" ") || undefined;
  return (
    <svg
      width="20"
      height="6"
      aria-hidden
      className={structural ? "text-foreground" : "text-foreground-muted"}
    >
      <line
        x1="0"
        y1="3"
        x2="20"
        y2="3"
        stroke="currentColor"
        strokeWidth={structural ? 1.6 : 1.1}
        strokeDasharray={dash}
      />
    </svg>
  );
}

/** The kind's canvas silhouette (document = circle, table = rounded square,
 *  file = dashed-ring circle) as a small DOM swatch. */
function KindSwatch({ kind }: { kind: NodeKind }) {
  const base = "inline-block h-3 w-3 shrink-0";
  if (kind === "table") return <span aria-hidden className={cn(base, "border border-foreground rounded-[3px] bg-surface")} />;
  if (kind === "file") return <span aria-hidden className={cn(base, "border border-dashed border-foreground-muted rounded-full")} />;
  return <span aria-hidden className={cn(base, "border border-foreground rounded-full bg-surface-muted")} />;
}

export function GraphSidebar({
  vault,
  view,
  onChange,
  onNavigate,
  hubs,
  orphanCount,
  hideOrphans,
  onToggleOrphans,
  onSelectNode,
  onCollapse,
}: Props) {
  const { recent, pushRecent, clearRecent, saved, saveView, deleteView } =
    useGraphHistory(vault);
  const [query, setQuery] = useState("");
  const [hits, setHits] = useState<SearchHit[]>([]);
  // null = not saving; string = name input in progress
  const [savingName, setSavingName] = useState<string | null>(null);
  const saveNameRef = useRef<HTMLInputElement>(null);
  // Tracks whether commitSave was already invoked via Enter/Escape to suppress the
  // subsequent onBlur double-commit.
  const saveCommittedRef = useRef(false);

  const debouncedQuery = useDebounce(query, 300);

  // Focus the input whenever it appears (savingName transitions from null → "").
  useEffect(() => {
    if (savingName !== null) {
      saveNameRef.current?.focus();
    }
  }, [savingName]);

  useEffect(() => {
    if (!debouncedQuery.trim()) {
      setHits([]);
      return;
    }
    let cancelled = false;
    searchDocs(debouncedQuery.trim(), vault, 8)
      .then((resp) => {
        if (cancelled) return;
        const rows = (resp.results || []).slice(0, 8).map((r: any) => ({
          doc_id: r.doc_id || r.id,
          title: r.title || r.name || r.doc_id || "(untitled)",
          type: (r.resource_type || r.type || "document") as NodeKind,
        }));
        setHits(rows);
      })
      .catch(() => { if (!cancelled) setHits([]); });
    return () => { cancelled = true; };
  }, [debouncedQuery, vault]);

  function commitEntry(hit: SearchHit) {
    pushRecent({ doc_id: hit.doc_id, title: hit.title });
    onChange({ ...view, entry: hit.doc_id, hops: savedHops() });
    setQuery("");
    setHits([]);
  }

  function setHops(d: 1 | 2 | 3) {
    if (typeof localStorage !== "undefined") localStorage.setItem(HOPS_KEY, String(d));
    onChange({ ...view, hops: d });
  }

  function toggleType(k: NodeKind) {
    const next = new Set(view.types);
    if (next.has(k)) next.delete(k);
    else next.add(k);
    onChange({ ...view, types: next });
  }

  function toggleRelation(r: RelationKind) {
    const next = new Set(view.relations);
    if (next.has(r)) next.delete(r);
    else next.add(r);
    onChange({ ...view, relations: next });
  }

  function beginSave() {
    saveCommittedRef.current = false;
    setSavingName("");
  }

  function commitSave(name: string) {
    // Guard against double-commit (Enter fires keyDown then onBlur).
    if (saveCommittedRef.current) return;
    saveCommittedRef.current = true;

    const trimmed = name.trim();
    if (!trimmed) {
      setSavingName(null);
      return;
    }
    saveView(trimmed, "?" + viewToQuery(view));
    setSavingName(null);
  }

  function cancelSave() {
    saveCommittedRef.current = true; // suppress onBlur
    setSavingName(null);
  }

  return (
    <aside
      className="flex flex-col overflow-y-auto m-2 rounded-[var(--radius-lg)] border border-border bg-surface shadow-md rail-scroll"
      aria-label="Graph controls"
    >
      {onCollapse && (
        <div className="flex items-center justify-between h-9 px-3 border-b border-border shrink-0">
          <span className="coord-ink">Graph</span>
          <button
            type="button"
            onClick={onCollapse}
            aria-label="Collapse graph controls"
            title="Collapse graph controls"
            className="text-foreground-muted hover:text-link transition-colors cursor-pointer focus:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2 focus-visible:ring-offset-surface"
          >
            <PanelLeftClose className="h-4 w-4" aria-hidden />
          </button>
        </div>
      )}

      <Section label="Focus" className="px-2">
        <div className="relative">
          <SearchIcon className="absolute left-2 top-1/2 -translate-y-1/2 h-3 w-3 text-foreground-muted pointer-events-none" />
          <input
            type="search"
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            placeholder="Search to focus on a document"
            aria-label="Search to focus on a document"
            className="w-full h-9 pl-6 pr-2 rounded-[var(--radius-md)] bg-background border border-border text-[11px] focus:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2 focus-visible:ring-offset-background"
          />
        </div>
        {!view.entry && hits.length === 0 && (
          <p className="coord text-foreground-muted mt-1.5 leading-relaxed">
            Showing the whole graph. Search to zoom into one document&rsquo;s neighborhood.
          </p>
        )}
        {hits.length > 0 && (
          <ul className="mt-1 flex flex-col gap-px">
            {hits.map((h) => (
              <li key={h.doc_id}>
                <button
                  type="button"
                  onClick={() => commitEntry(h)}
                  className="w-full flex items-center justify-between gap-2 px-2 h-7 text-left text-[11px] hover:bg-surface-hover"
                >
                  <TooltipText className="truncate">{h.title}</TooltipText>
                  <span className="coord">{h.type}</span>
                </button>
              </li>
            ))}
          </ul>
        )}
        {view.entry && (
          <div className="mt-1 flex items-center justify-between gap-2 px-2 h-7 text-[11px] bg-surface-muted rounded-[var(--radius-sm)]">
            <TooltipText className="truncate" tip={`focused on ${view.entry}`}>
              Focused on <span className="coord">{view.entry}</span>
            </TooltipText>
            <button
              type="button"
              onClick={() => onChange({ ...view, entry: undefined })}
              aria-label="Show whole graph"
              title="Show whole graph"
              className="shrink-0 text-foreground-muted hover:text-foreground"
            >
              <X className="h-3 w-3" />
            </button>
          </div>
        )}
      </Section>

      {/* Hops shown in BOTH modes (it sets the depth a future focus will use),
          with a hint when there's no focus yet so the rail isn't half-disabled. */}
      <Section label="Depth" className="px-2">
        <div className="flex items-center gap-3 text-[11px]">
          {([1, 2, 3] as const).map((d) => (
            <label key={d} className="inline-flex items-center gap-1 cursor-pointer">
              <input
                type="radio"
                name="hops"
                checked={view.hops === d}
                onChange={() => setHops(d)}
                aria-label={`${d} hop${d === 1 ? "" : "s"}`}
              />
              {d}
            </label>
          ))}
          <span className="coord text-foreground-muted">hops</span>
        </div>
        {!view.entry && (
          <p className="coord text-foreground-muted mt-1 leading-relaxed">
            Focus on a document to traverse this many hops out.
          </p>
        )}
      </Section>

      {orphanCount > 0 && (
        <Section label="Display" className="px-2">
          <label className="inline-flex items-center gap-2 text-[11px] cursor-pointer">
            <input type="checkbox" checked={hideOrphans} onChange={onToggleOrphans} />
            Hide orphans
            <span className="coord text-foreground-muted">
              ({orphanCount} unconnected)
            </span>
          </label>
        </Section>
      )}

      {hubs.length > 0 && (
        <Section label="Hubs" className="px-2">
          <ul className="flex flex-col gap-px">
            {hubs.map((h) => (
              <li key={h.uri}>
                <button
                  type="button"
                  onClick={() => onSelectNode(h.uri)}
                  className="w-full flex items-center gap-2 px-2 h-7 text-left text-[11px] hover:bg-surface-hover rounded-[var(--radius-sm)]"
                >
                  <KindSwatch kind={h.kind} />
                  <TooltipText className="truncate">{h.name}</TooltipText>
                </button>
              </li>
            ))}
          </ul>
        </Section>
      )}

      <Section label="Types" className="px-2">
        <div className="flex flex-wrap gap-1">
          {ALL_NODE_KINDS.map((k) => (
            <button
              key={k}
              type="button"
              onClick={() => toggleType(k)}
              aria-label={`Toggle ${k}`}
              aria-pressed={view.types.has(k)}
              className={cn(
                "inline-flex items-center gap-1.5 h-7 px-2.5 rounded-[var(--radius-sm)] border text-[10px] font-semibold",
                view.types.has(k)
                  ? "border-primary bg-surface-selected text-surface-selected-foreground"
                  : "border-border text-foreground-muted hover:bg-surface-hover",
              )}
            >
              <KindSwatch kind={k} />
              {k}
            </button>
          ))}
        </div>
      </Section>

      <Section label="Relations" className="px-2">
        <ul className="flex flex-col gap-px">
          {ALL_RELATIONS.map((r) => {
            const on = view.relations.has(r);
            return (
              <li key={r}>
                <button
                  type="button"
                  onClick={() => toggleRelation(r)}
                  aria-label={`Toggle ${RELATION_LABEL[r]}`}
                  aria-pressed={on}
                  className={cn(
                    "w-full flex items-center gap-2 px-2 h-7 rounded-[var(--radius-sm)] text-[11px] text-left text-foreground hover:bg-surface-hover transition-opacity",
                    on ? "" : "opacity-40",
                  )}
                >
                  <RelationSwatch relation={r} />
                  <span className="truncate">{RELATION_LABEL[r]}</span>
                </button>
              </li>
            );
          })}
        </ul>
      </Section>

      <Section
        label="Recent"
        className="px-2"
        rightAction={
          recent.length > 0 ? (
            <button type="button" onClick={clearRecent} className="coord hover:text-foreground">
              clear
            </button>
          ) : null
        }
      >
        {recent.length === 0 ? (
          <p className="coord text-foreground-muted">none</p>
        ) : (
          <ul className="flex flex-col gap-px">
            {recent.map((r) => (
              <li key={r.doc_id}>
                <button
                  type="button"
                  onClick={() => onChange({ ...view, entry: r.doc_id, hops: savedHops() })}
                  className="w-full text-left px-2 h-7 text-[11px] hover:bg-surface-hover active:bg-surface-active truncate"
                >
                  <TooltipText className="truncate">{r.title}</TooltipText>
                </button>
              </li>
            ))}
          </ul>
        )}
      </Section>

      <Section
        label="Saved views"
        className="px-2"
        rightAction={
          <button
            type="button"
            onClick={beginSave}
            aria-label="Save view"
            className="coord hover:text-foreground"
          >
            + save
          </button>
        }
      >
        {savingName !== null && (
          <input
            ref={saveNameRef}
            value={savingName}
            onChange={(e) => setSavingName(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === "Enter") {
                commitSave(savingName);
              } else if (e.key === "Escape") {
                cancelSave();
              }
            }}
            onBlur={() => {
              if (!saveCommittedRef.current) {
                commitSave(savingName ?? "");
              }
            }}
            placeholder="Name this view"
            aria-label="View name"
            className="w-full h-7 px-2 rounded-[var(--radius-sm)] bg-background border border-input text-[11px] focus:outline-none mb-1"
          />
        )}
        {saved.length === 0 && savingName === null ? (
          <p className="coord text-foreground-muted">none</p>
        ) : (
          <ul className="flex flex-col gap-px">
            {saved.map((s) => (
              <li key={s.name} className="flex items-center gap-1">
                <button
                  type="button"
                  onClick={() => onNavigate(s.url)}
                  className="flex-1 text-left px-2 h-7 text-[11px] hover:bg-surface-hover active:opacity-60 transition-opacity duration-150 truncate"
                >
                  <TooltipText className="truncate">{s.name}</TooltipText>
                </button>
                <button
                  type="button"
                  onClick={() => deleteView(s.name)}
                  aria-label={`Delete ${s.name}`}
                  className="text-foreground-muted hover:text-destructive px-1"
                >
                  <X className="h-3 w-3" />
                </button>
              </li>
            ))}
          </ul>
        )}
      </Section>
    </aside>
  );
}
