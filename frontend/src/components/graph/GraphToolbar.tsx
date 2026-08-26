import * as DropdownMenu from "@radix-ui/react-dropdown-menu";
import {
  Bookmark,
  Check,
  CircleHelp,
  File,
  FileText,
  List,
  Network,
  RotateCcw,
  Search,
  SlidersHorizontal,
  Table2,
  Trash2,
  X,
} from "lucide-react";
import { useEffect, useMemo, useRef, useState } from "react";
import { Button } from "@/components/ui/button";
import { useDebounce } from "@/hooks/use-debounce";
import { useGraphHistory } from "@/hooks/use-graph-history";
import { searchDocs } from "@/lib/api";
import { cn } from "@/lib/utils";
import { viewToQuery } from "./graph-state";
import {
  ALL_NODE_KINDS,
  ALL_RELATIONS,
  RELATION_LABEL,
  type GraphNode,
  type GraphView,
  type NodeKind,
  type RelationKind,
} from "./graph-types";
import { KindSwatch, RelationSwatch } from "./graph-swatches";
import { docIdFromUri } from "./use-graph-data";

export type GraphDisplayMode = "graph" | "list";

interface SearchHit {
  docId: string;
  uri?: string;
  title: string;
  kind: NodeKind;
  source: "search" | "recent" | "hub";
}

interface ApiSearchResult {
  uri?: string;
  path?: string;
  source_type?: string;
  title?: string;
}

interface Props {
  vault: string;
  view: GraphView;
  onChange: (next: GraphView) => void;
  onNavigate: (queryString: string) => void;
  hubs: GraphNode[];
  nodeCount: number;
  edgeCount: number;
  totalNodes?: number;
  truncated?: boolean;
  focusTitle?: string;
  displayMode: GraphDisplayMode;
  onDisplayModeChange: (mode: GraphDisplayMode) => void;
  orphanCount: number;
  hideOrphans: boolean;
  onToggleOrphans: () => void;
  hiddenCount: number;
  onUnhideAll: () => void;
  onFit: () => void;
}

const HOPS_KEY = "akb:graph:hops";

function savedHops(): 1 | 2 | 3 {
  const value = typeof localStorage !== "undefined" ? localStorage.getItem(HOPS_KEY) : null;
  return value === "1" ? 1 : value === "3" ? 3 : 2;
}

function kindIcon(kind: NodeKind) {
  if (kind === "table") return Table2;
  if (kind === "file") return File;
  return FileText;
}

export function GraphToolbar({
  vault,
  view,
  onChange,
  onNavigate,
  hubs,
  nodeCount,
  edgeCount,
  totalNodes,
  truncated,
  focusTitle,
  displayMode,
  onDisplayModeChange,
  orphanCount,
  hideOrphans,
  onToggleOrphans,
  hiddenCount,
  onUnhideAll,
  onFit,
}: Props) {
  const history = useGraphHistory(vault);
  const [query, setQuery] = useState("");
  const [hits, setHits] = useState<SearchHit[]>([]);
  const [searching, setSearching] = useState(false);
  const [searchOpen, setSearchOpen] = useState(false);
  const [activeIndex, setActiveIndex] = useState(0);
  const searchRootRef = useRef<HTMLDivElement>(null);
  const debouncedQuery = useDebounce(query, 250);

  useEffect(() => {
    function closeOnOutside(event: PointerEvent) {
      if (!searchRootRef.current?.contains(event.target as Node)) setSearchOpen(false);
    }
    document.addEventListener("pointerdown", closeOnOutside);
    return () => document.removeEventListener("pointerdown", closeOnOutside);
  }, []);

  useEffect(() => {
    if (!debouncedQuery.trim()) {
      setHits([]);
      setSearching(false);
      return;
    }

    let cancelled = false;
    setSearching(true);
    searchDocs(debouncedQuery.trim(), vault, 8)
      .then((response) => {
        if (cancelled) return;
        const next = ((response.results || []) as ApiSearchResult[])
          .slice(0, 8)
          .map((result): SearchHit | null => {
            const docId = (result.uri ? docIdFromUri(result.uri) : null) ?? result.path ?? "";
            if (!docId) return null;
            const kind: NodeKind =
              result.source_type === "table"
                ? "table"
                : result.source_type === "file"
                  ? "file"
                  : "document";
            return {
              docId,
              uri: result.uri,
              title: result.title || result.path || "Untitled resource",
              kind,
              source: "search",
            };
          })
          .filter((item: SearchHit | null): item is SearchHit => item !== null);
        setHits(next);
        setActiveIndex(0);
      })
      .catch(() => {
        if (!cancelled) setHits([]);
      })
      .finally(() => {
        if (!cancelled) setSearching(false);
      });

    return () => {
      cancelled = true;
    };
  }, [debouncedQuery, vault]);

  const suggestions = useMemo<SearchHit[]>(() => {
    if (query.trim()) return hits;
    const seen = new Set<string>();
    const rows: SearchHit[] = [];
    for (const recent of history.recent) {
      if (seen.has(recent.doc_id)) continue;
      seen.add(recent.doc_id);
      rows.push({
        docId: recent.doc_id,
        uri: recent.uri,
        title: recent.title,
        kind: recent.kind || "document",
        source: "recent",
      });
    }
    for (const hub of hubs) {
      const docId = hub.doc_id || docIdFromUri(hub.uri);
      if (!docId || seen.has(docId)) continue;
      seen.add(docId);
      rows.push({ docId, uri: hub.uri, title: hub.name, kind: hub.kind, source: "hub" });
    }
    return rows.slice(0, 8);
  }, [query, hits, history.recent, hubs]);

  function focusOn(item: SearchHit) {
    history.pushRecent({ doc_id: item.docId, title: item.title, kind: item.kind, uri: item.uri });
    onChange({
      ...view,
      entry: item.kind === "document" ? item.docId : item.uri || item.docId,
      selected: undefined,
      hops: savedHops(),
    });
    setQuery("");
    setSearchOpen(false);
  }

  function setHops(hops: 1 | 2 | 3) {
    localStorage.setItem(HOPS_KEY, String(hops));
    onChange({ ...view, hops });
  }

  const filterCount =
    ALL_NODE_KINDS.length - view.types.size +
    ALL_RELATIONS.length - view.relations.size +
    (hideOrphans ? 1 : 0) +
    (hiddenCount > 0 ? 1 : 0);

  const statusText = truncated && totalNodes
    ? `Showing ${nodeCount} of ${totalNodes} resources`
    : `${nodeCount} resource${nodeCount === 1 ? "" : "s"}`;

  return (
    <header className="relative z-[var(--z-sticky)] shrink-0 border-b border-border bg-surface">
      <h1 className="sr-only">Knowledge graph</h1>
      <div className="flex min-h-14 flex-wrap items-center gap-2 px-3 py-2 lg:px-4">
        <div ref={searchRootRef} className="relative min-w-0 flex-1 basis-72">
          <label htmlFor="graph-search" className="sr-only">
            Find a resource to explore its relationships
          </label>
          <Search
            className="pointer-events-none absolute left-3 top-1/2 z-10 h-4 w-4 -translate-y-1/2 text-foreground-muted"
            aria-hidden
          />
          <input
            id="graph-search"
            type="search"
            role="combobox"
            aria-expanded={searchOpen}
            aria-controls="graph-search-results"
            aria-activedescendant={
              searchOpen && suggestions[activeIndex]
                ? `graph-search-option-${activeIndex}`
                : undefined
            }
            autoComplete="off"
            value={query}
            onFocus={() => setSearchOpen(true)}
            onChange={(event) => {
              setQuery(event.target.value);
              setSearchOpen(true);
              setActiveIndex(0);
            }}
            onKeyDown={(event) => {
              if (event.key === "ArrowDown") {
                event.preventDefault();
                setSearchOpen(true);
                setActiveIndex((index) => Math.min(index + 1, suggestions.length - 1));
              } else if (event.key === "ArrowUp") {
                event.preventDefault();
                setActiveIndex((index) => Math.max(index - 1, 0));
              } else if (event.key === "Enter" && searchOpen && suggestions[activeIndex]) {
                event.preventDefault();
                focusOn(suggestions[activeIndex]);
              } else if (event.key === "Escape") {
                event.preventDefault();
                setSearchOpen(false);
              }
            }}
            placeholder="Find a resource to explore…"
            className="h-10 w-full rounded-[var(--radius-md)] border border-border-strong bg-background pl-9 pr-9 text-sm text-foreground shadow-xs placeholder:text-foreground-muted focus:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2 focus-visible:ring-offset-surface"
          />
          {query && (
            <button
              type="button"
              onClick={() => {
                setQuery("");
                setHits([]);
              }}
              aria-label="Clear graph search"
              className="absolute right-2 top-1/2 z-10 inline-flex h-7 w-7 -translate-y-1/2 items-center justify-center rounded-[var(--radius-sm)] text-foreground-muted transition-token hover:bg-surface-hover hover:text-foreground focus:outline-none focus-visible:ring-2 focus-visible:ring-ring"
            >
              <X className="h-3.5 w-3.5" aria-hidden />
            </button>
          )}

          {searchOpen && (
            <div
              id="graph-search-results"
              role="listbox"
              className="absolute inset-x-0 top-[calc(100%+0.5rem)] z-[var(--z-popover)] overflow-hidden rounded-[var(--radius-lg)] border border-border bg-surface shadow-lg"
            >
              <div className="flex items-center justify-between border-b border-border px-3 py-2">
                <span className="text-xs font-medium text-foreground">
                  {query.trim() ? "Matches in this Vault" : "Start exploring"}
                </span>
                {!query.trim() && history.recent.length > 0 && (
                  <button
                    type="button"
                    onClick={history.clearRecent}
                    className="text-xs text-foreground-muted hover:text-link focus:outline-none focus-visible:ring-2 focus-visible:ring-ring"
                  >
                    Clear recent
                  </button>
                )}
              </div>
              <div aria-live="polite" className="sr-only">
                {searching
                  ? "Searching"
                  : `${suggestions.length} graph search suggestion${suggestions.length === 1 ? "" : "s"}`}
              </div>
              {searching ? (
                <div className="px-3 py-6 text-center text-sm text-foreground-muted" role="status">
                  Searching this Vault…
                </div>
              ) : suggestions.length > 0 ? (
                <ul className="max-h-80 overflow-y-auto py-1 rail-scroll">
                  {suggestions.map((item, index) => {
                    const Icon = kindIcon(item.kind);
                    return (
                      <li key={`${item.source}:${item.docId}`}>
                        <button
                          id={`graph-search-option-${index}`}
                          type="button"
                          role="option"
                          aria-selected={index === activeIndex}
                          onPointerMove={() => setActiveIndex(index)}
                          onClick={() => focusOn(item)}
                          className={cn(
                            "flex w-full items-center gap-3 px-3 py-2 text-left transition-token focus:outline-none focus-visible:ring-2 focus-visible:ring-inset focus-visible:ring-ring",
                            index === activeIndex ? "bg-surface-selected" : "hover:bg-surface-hover",
                          )}
                        >
                          <span className="flex h-8 w-8 shrink-0 items-center justify-center rounded-[var(--radius-md)] border border-border bg-surface-2 text-foreground-muted">
                            <Icon className="h-4 w-4" aria-hidden />
                          </span>
                          <span className="min-w-0 flex-1">
                            <span className="block truncate text-sm font-medium text-foreground">
                              {item.title}
                            </span>
                            <span className="block text-xs text-foreground-muted">
                              {item.source === "recent"
                                ? "Recently explored"
                                : item.source === "hub"
                                  ? "Connected starting point"
                                  : item.kind}
                            </span>
                          </span>
                          <span className="text-xs text-link">Focus</span>
                        </button>
                      </li>
                    );
                  })}
                </ul>
              ) : (
                <div className="px-4 py-8 text-center">
                  <p className="text-sm font-medium text-foreground">
                    {query.trim() ? "No matching resources" : "No connected starting points yet"}
                  </p>
                  <p className="mt-1 text-xs text-foreground-muted">
                    {query.trim()
                      ? "Try a title, path, or another term."
                      : "Create relationships between resources to make exploration useful."}
                  </p>
                </div>
              )}
            </div>
          )}
        </div>

        <div className="flex min-w-0 items-center gap-1 rounded-[var(--radius-md)] border border-border bg-background p-1">
          <button
            type="button"
            onClick={() => onDisplayModeChange("graph")}
            aria-pressed={displayMode === "graph"}
            className={cn(
              "inline-flex h-8 items-center gap-2 rounded-[var(--radius-sm)] px-3 text-xs font-medium transition-token focus:outline-none focus-visible:ring-2 focus-visible:ring-ring",
              displayMode === "graph"
                ? "bg-surface-selected text-surface-selected-foreground shadow-xs"
                : "text-foreground-muted hover:bg-surface-hover hover:text-foreground",
            )}
          >
            <Network className="h-3.5 w-3.5" aria-hidden />
            Graph
          </button>
          <button
            type="button"
            onClick={() => onDisplayModeChange("list")}
            aria-pressed={displayMode === "list"}
            className={cn(
              "inline-flex h-8 items-center gap-2 rounded-[var(--radius-sm)] px-3 text-xs font-medium transition-token focus:outline-none focus-visible:ring-2 focus-visible:ring-ring",
              displayMode === "list"
                ? "bg-surface-selected text-surface-selected-foreground shadow-xs"
                : "text-foreground-muted hover:bg-surface-hover hover:text-foreground",
            )}
          >
            <List className="h-3.5 w-3.5" aria-hidden />
            List
          </button>
        </div>

        <GraphFilterMenu
          view={view}
          onChange={onChange}
          filterCount={filterCount}
          orphanCount={orphanCount}
          hideOrphans={hideOrphans}
          onToggleOrphans={onToggleOrphans}
          hiddenCount={hiddenCount}
          onUnhideAll={onUnhideAll}
        />

        <GraphViewsMenu
          view={view}
          saved={history.saved}
          onSave={history.saveView}
          onDelete={history.deleteView}
          onNavigate={onNavigate}
        />

        <DropdownMenu.Root>
          <DropdownMenu.Trigger asChild>
            <Button variant="outline" size="icon" aria-label="Graph help">
              <CircleHelp className="h-4 w-4" aria-hidden />
            </Button>
          </DropdownMenu.Trigger>
          <DropdownMenu.Portal>
            <DropdownMenu.Content
              align="end"
              sideOffset={8}
              className="z-[var(--z-popover)] w-72 rounded-[var(--radius-lg)] border border-border bg-surface p-3 shadow-lg"
            >
              <p className="text-sm font-semibold text-foreground">Explore relationships</p>
              <ul className="mt-2 space-y-2 text-xs leading-relaxed text-foreground-muted">
                <li>Click a node to inspect it. Double-click to reveal direct neighbors.</li>
                <li>Drag the canvas to pan and scroll to zoom. Use List for a keyboard-first view.</li>
                <li>Search for a resource to switch from the whole Vault to a focused neighborhood.</li>
              </ul>
            </DropdownMenu.Content>
          </DropdownMenu.Portal>
        </DropdownMenu.Root>
      </div>

      <div className="flex min-h-10 flex-wrap items-center justify-between gap-x-4 gap-y-2 border-t border-border px-3 py-1.5 text-xs lg:px-4">
        <div className="flex min-w-0 flex-wrap items-center gap-2 text-foreground-muted">
          {view.entry ? (
            <>
              <button
                type="button"
                onClick={() => onChange({ ...view, entry: undefined, selected: undefined })}
                className="font-medium text-link hover:text-link-hover focus:outline-none focus-visible:ring-2 focus-visible:ring-ring"
              >
                Whole Vault
              </button>
              <span aria-hidden>/</span>
              <span className="max-w-56 truncate font-medium text-foreground" title={focusTitle || view.entry}>
                {focusTitle || view.entry}
              </span>
              <div className="ml-1 flex items-center rounded-[var(--radius-sm)] border border-border bg-background p-0.5">
                {([1, 2, 3] as const).map((hops) => (
                  <button
                    key={hops}
                    type="button"
                    onClick={() => setHops(hops)}
                    aria-pressed={view.hops === hops}
                    aria-label={`${hops} hop neighborhood`}
                    className={cn(
                      "h-6 min-w-7 rounded-[var(--radius-sm)] px-1.5 tabular-nums transition-token focus:outline-none focus-visible:ring-2 focus-visible:ring-ring",
                      view.hops === hops
                        ? "bg-surface-selected font-medium text-surface-selected-foreground"
                        : "hover:bg-surface-hover hover:text-foreground",
                    )}
                  >
                    {hops}
                  </button>
                ))}
              </div>
              <span>hops</span>
            </>
          ) : (
            <span className="font-medium text-foreground">Whole Vault</span>
          )}
        </div>
        <div className="flex items-center gap-2 text-foreground-muted">
          <span className="tabular-nums">{statusText}</span>
          <span aria-hidden>·</span>
          <span className="tabular-nums">{edgeCount} relationship{edgeCount === 1 ? "" : "s"}</span>
          {displayMode === "graph" && nodeCount > 0 && (
            <>
              <span aria-hidden>·</span>
              <button
                type="button"
                onClick={onFit}
                className="font-medium text-link hover:text-link-hover focus:outline-none focus-visible:ring-2 focus-visible:ring-ring"
              >
                Fit view
              </button>
            </>
          )}
        </div>
      </div>
    </header>
  );
}

function GraphFilterMenu({
  view,
  onChange,
  filterCount,
  orphanCount,
  hideOrphans,
  onToggleOrphans,
  hiddenCount,
  onUnhideAll,
}: Pick<
  Props,
  | "view"
  | "onChange"
  | "orphanCount"
  | "hideOrphans"
  | "onToggleOrphans"
  | "hiddenCount"
  | "onUnhideAll"
> & { filterCount: number }) {
  function toggleType(kind: NodeKind) {
    const types = new Set(view.types);
    if (types.has(kind)) types.delete(kind);
    else types.add(kind);
    onChange({ ...view, types });
  }

  function toggleRelation(relation: RelationKind) {
    const relations = new Set(view.relations);
    if (relations.has(relation)) relations.delete(relation);
    else relations.add(relation);
    onChange({ ...view, relations });
  }

  function reset() {
    onChange({
      ...view,
      types: new Set(ALL_NODE_KINDS),
      relations: new Set(ALL_RELATIONS),
    });
    if (hideOrphans) onToggleOrphans();
    if (hiddenCount) onUnhideAll();
  }

  return (
    <DropdownMenu.Root>
      <DropdownMenu.Trigger asChild>
        <Button variant="outline" size="md" aria-label={`Filters${filterCount ? `, ${filterCount} active` : ""}`}>
          <SlidersHorizontal className="h-4 w-4" aria-hidden />
          <span className="hidden sm:inline">Filters</span>
          {filterCount > 0 && (
            <span className="inline-flex min-w-5 items-center justify-center rounded-full bg-primary px-1.5 py-0.5 text-xs tabular-nums text-primary-foreground">
              {filterCount}
            </span>
          )}
        </Button>
      </DropdownMenu.Trigger>
      <DropdownMenu.Portal>
        <DropdownMenu.Content
          align="end"
          sideOffset={8}
          className="z-[var(--z-popover)] w-80 rounded-[var(--radius-lg)] border border-border bg-surface shadow-lg"
        >
          <div className="flex items-center justify-between border-b border-border px-3 py-2.5">
            <div>
              <p className="text-sm font-semibold text-foreground">Filter the scene</p>
              <p className="mt-0.5 text-xs text-foreground-muted">Narrow resources without leaving the graph.</p>
            </div>
            {filterCount > 0 && (
              <button
                type="button"
                onClick={reset}
                className="inline-flex h-8 items-center gap-1.5 rounded-[var(--radius-sm)] px-2 text-xs text-link hover:bg-surface-hover hover:text-link-hover focus:outline-none focus-visible:ring-2 focus-visible:ring-ring"
              >
                <RotateCcw className="h-3.5 w-3.5" aria-hidden />
                Reset
              </button>
            )}
          </div>

          <div className="p-2">
            <p className="px-2 pb-1 pt-1 text-xs font-medium text-foreground-muted">Resources</p>
            {ALL_NODE_KINDS.map((kind) => (
              <DropdownMenu.CheckboxItem
                key={kind}
                checked={view.types.has(kind)}
                onCheckedChange={() => toggleType(kind)}
                onSelect={(event) => event.preventDefault()}
                className="relative flex h-9 cursor-pointer select-none items-center gap-2 rounded-[var(--radius-sm)] pl-8 pr-2 text-sm text-foreground outline-none hover:bg-surface-hover focus:bg-surface-hover"
              >
                <DropdownMenu.ItemIndicator className="absolute left-2.5">
                  <Check className="h-3.5 w-3.5 text-primary" aria-hidden />
                </DropdownMenu.ItemIndicator>
                <KindSwatch kind={kind} />
                <span className="capitalize">{kind}</span>
              </DropdownMenu.CheckboxItem>
            ))}

            <DropdownMenu.Separator className="my-2 h-px bg-border" />
            <p className="px-2 pb-1 text-xs font-medium text-foreground-muted">Relationships</p>
            <div className="grid grid-cols-1 gap-px sm:grid-cols-2">
              {ALL_RELATIONS.map((relation) => (
                <DropdownMenu.CheckboxItem
                  key={relation}
                  checked={view.relations.has(relation)}
                  onCheckedChange={() => toggleRelation(relation)}
                  onSelect={(event) => event.preventDefault()}
                  className="relative flex min-h-9 cursor-pointer select-none items-center gap-2 rounded-[var(--radius-sm)] pl-8 pr-2 text-xs text-foreground outline-none hover:bg-surface-hover focus:bg-surface-hover"
                >
                  <DropdownMenu.ItemIndicator className="absolute left-2.5">
                    <Check className="h-3.5 w-3.5 text-primary" aria-hidden />
                  </DropdownMenu.ItemIndicator>
                  <RelationSwatch relation={relation} />
                  <span className="truncate">{RELATION_LABEL[relation]}</span>
                </DropdownMenu.CheckboxItem>
              ))}
            </div>

            {(orphanCount > 0 || hiddenCount > 0) && (
              <>
                <DropdownMenu.Separator className="my-2 h-px bg-border" />
                {orphanCount > 0 && (
                  <DropdownMenu.CheckboxItem
                    checked={hideOrphans}
                    onCheckedChange={onToggleOrphans}
                    onSelect={(event) => event.preventDefault()}
                    className="relative flex min-h-9 cursor-pointer select-none items-center rounded-[var(--radius-sm)] pl-8 pr-2 text-sm text-foreground outline-none hover:bg-surface-hover focus:bg-surface-hover"
                  >
                    <DropdownMenu.ItemIndicator className="absolute left-2.5">
                      <Check className="h-3.5 w-3.5 text-primary" aria-hidden />
                    </DropdownMenu.ItemIndicator>
                    Hide {orphanCount} unconnected resource{orphanCount === 1 ? "" : "s"}
                  </DropdownMenu.CheckboxItem>
                )}
                {hiddenCount > 0 && (
                  <DropdownMenu.Item
                    onSelect={onUnhideAll}
                    className="flex min-h-9 cursor-pointer select-none items-center gap-2 rounded-[var(--radius-sm)] px-2 text-sm text-link outline-none hover:bg-surface-hover focus:bg-surface-hover"
                  >
                    <RotateCcw className="h-3.5 w-3.5" aria-hidden />
                    Restore {hiddenCount} hidden resource{hiddenCount === 1 ? "" : "s"}
                  </DropdownMenu.Item>
                )}
              </>
            )}
          </div>
        </DropdownMenu.Content>
      </DropdownMenu.Portal>
    </DropdownMenu.Root>
  );
}

function GraphViewsMenu({
  view,
  saved,
  onSave,
  onDelete,
  onNavigate,
}: {
  view: GraphView;
  saved: Array<{ name: string; url: string }>;
  onSave: (name: string, url: string) => void;
  onDelete: (name: string) => void;
  onNavigate: (url: string) => void;
}) {
  const [name, setName] = useState("");

  function save() {
    const trimmed = name.trim();
    if (!trimmed) return;
    onSave(trimmed, `?${viewToQuery(view)}`);
    setName("");
  }

  return (
    <DropdownMenu.Root>
      <DropdownMenu.Trigger asChild>
        <Button variant="outline" size="icon" aria-label="Saved graph views">
          <Bookmark className="h-4 w-4" aria-hidden />
        </Button>
      </DropdownMenu.Trigger>
      <DropdownMenu.Portal>
        <DropdownMenu.Content
          align="end"
          sideOffset={8}
          className="z-[var(--z-popover)] w-72 rounded-[var(--radius-lg)] border border-border bg-surface p-2 shadow-lg"
        >
          <div className="px-2 pb-2 pt-1">
            <p className="text-sm font-semibold text-foreground">Saved views</p>
            <p className="mt-0.5 text-xs text-foreground-muted">Keep the current focus and filters.</p>
          </div>
          <div className="flex gap-1 border-y border-border px-2 py-2">
            <label htmlFor="graph-view-name" className="sr-only">View name</label>
            <input
              id="graph-view-name"
              value={name}
              onChange={(event) => setName(event.target.value)}
              onKeyDown={(event) => {
                event.stopPropagation();
                if (event.key === "Enter") save();
              }}
              placeholder="Name this view"
              className="h-8 min-w-0 flex-1 rounded-[var(--radius-sm)] border border-border bg-background px-2 text-xs text-foreground placeholder:text-foreground-muted focus:outline-none focus-visible:ring-2 focus-visible:ring-ring"
            />
            <Button type="button" size="sm" onClick={save} disabled={!name.trim()}>
              Save
            </Button>
          </div>
          {saved.length > 0 ? (
            <ul className="max-h-64 overflow-y-auto py-1 rail-scroll">
              {saved.map((item) => (
                <li key={item.name} className="group flex items-center gap-1">
                  <DropdownMenu.Item
                    onSelect={() => onNavigate(item.url)}
                    className="flex h-9 min-w-0 flex-1 cursor-pointer items-center gap-2 rounded-[var(--radius-sm)] px-2 text-sm text-foreground outline-none hover:bg-surface-hover focus:bg-surface-hover"
                  >
                    <Bookmark className="h-3.5 w-3.5 shrink-0 text-foreground-muted" aria-hidden />
                    <span className="truncate">{item.name}</span>
                  </DropdownMenu.Item>
                  <button
                    type="button"
                    onClick={() => onDelete(item.name)}
                    aria-label={`Delete saved view ${item.name}`}
                    className="inline-flex h-8 w-8 items-center justify-center rounded-[var(--radius-sm)] text-foreground-muted hover:bg-surface-hover hover:text-destructive focus:outline-none focus-visible:ring-2 focus-visible:ring-ring"
                  >
                    <Trash2 className="h-3.5 w-3.5" aria-hidden />
                  </button>
                </li>
              ))}
            </ul>
          ) : (
            <p className="px-2 py-4 text-center text-xs text-foreground-muted">No saved views yet.</p>
          )}
        </DropdownMenu.Content>
      </DropdownMenu.Portal>
    </DropdownMenu.Root>
  );
}
