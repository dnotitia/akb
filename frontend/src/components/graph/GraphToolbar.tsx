import * as DropdownMenu from "@radix-ui/react-dropdown-menu";
import {
  Bookmark,
  Check,
  CircleHelp,
  ChevronRight,
  File,
  FileText,
  List,
  Maximize2,
  Network,
  MoreHorizontal,
  RotateCcw,
  Search,
  SlidersHorizontal,
  Table2,
  Trash2,
  X,
} from "lucide-react";
import { useEffect, useId, useLayoutEffect, useMemo, useRef, useState } from "react";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Dialog, DialogContent, DialogDescription, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { useAccessVerification, useCurrentUser } from "@/contexts/current-user-context";
import { useDebounce } from "@/hooks/use-debounce";
import { useGraphHistory } from "@/hooks/use-graph-history";
import { searchDocs } from "@/lib/api";
import { cn } from "@/lib/utils";
import { docUri, fileUri, parseUri, tableUri } from "@/lib/uri";
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
import { GraphNodeMarker, KindSwatch, RelationSwatch } from "./graph-swatches";
import { docIdFromUri } from "./use-graph-data";

export type GraphDisplayMode = "graph" | "list";
type UpdateView = (update: (current: GraphView) => GraphView) => void;

interface SearchHit {
  docId: string;
  uri: string;
  title: string;
  kind: NodeKind;
  source: "search" | "loaded" | "recent";
  context?: string;
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
  nodes?: GraphNode[];
  /** Counts before display filters, limited to resources loaded in this graph. */
  resourceCounts?: Record<NodeKind, number>;
  onSelect?: (uri: string) => void;
  onRearrange?: () => void;
}

function kindIcon(kind: NodeKind) {
  if (kind === "table") return Table2;
  if (kind === "file") return File;
  return FileText;
}

const RESOURCE_LABELS: Record<NodeKind, string> = {
  document: "Documents",
  table: "Tables",
  file: "Files",
};

export function GraphToolbar(props: Props) {
  const user = useCurrentUser();
  const { revision, checking } = useAccessVerification();
  const history = useGraphHistory(props.vault);
  // A scope transition discards transient query/results before the next paint.
  return <GraphControls key={[user?.user_id, props.vault, revision, checking].join(":")} {...props} history={history} />;
}

function GraphControls({
  vault, view, onChange, onNavigate, hubs, nodes, resourceCounts, onSelect, onRearrange,
  focusTitle, displayMode, onDisplayModeChange, orphanCount, hideOrphans,
  onToggleOrphans, hiddenCount, onUnhideAll, onFit, history,
}: Props & { history: ReturnType<typeof useGraphHistory> }) {
  // URL navigation can commit after another control fires. Compose those
  // actions against the latest dispatch; external navigation replaces it.
  const pendingView = useRef(view);
  useLayoutEffect(() => { pendingView.current = view; }, [view]);
  const updateView: UpdateView = (update) => {
    const next = update(pendingView.current);
    pendingView.current = next;
    onChange(next);
  };
  const [query, setQuery] = useState("");
  const [searchOpen, setSearchOpen] = useState(false);
  const [activeUri, setActiveUri] = useState<string>();
  const [retry, setRetry] = useState(0);
  const [lookup, setLookup] = useState<{ query: string; status: "loading" | "ready" | "error"; hits: SearchHit[] }>({
    query: "", status: "ready", hits: [],
  });
  const searchRootRef = useRef<HTMLDivElement>(null);
  const listRef = useRef<HTMLDivElement>(null);
  const inputRef = useRef<HTMLInputElement>(null);
  const searchId = useId();
  const resourceCountId = useId();
  const listId = searchId + "-results";
  const debouncedQuery = useDebounce(query.trim(), 250);
  const term = query.trim();

  useEffect(() => {
    function closeOnOutside(event: PointerEvent) {
      if (!searchRootRef.current?.contains(event.target as Node)) setSearchOpen(false);
    }
    document.addEventListener("pointerdown", closeOnOutside);
    return () => document.removeEventListener("pointerdown", closeOnOutside);
  }, []);

  useEffect(() => {
    if (!term || term !== debouncedQuery) return;
    const controller = new AbortController();
    let cancelled = false;
    setLookup({ query: term, status: "loading", hits: [] });
    searchDocs(term, vault, 8, {}, { signal: controller.signal })
      .then((response) => {
        if (cancelled) return;
        const hits = ((response.results || []) as ApiSearchResult[]).flatMap((result): SearchHit[] => {
          const parsed = parseUri(result.uri);
          if (result.uri && (!parsed || parsed.kind === "coll" || parsed.kind === "vault")) return [];
          const kind: NodeKind = parsed?.kind === "table" || parsed?.kind === "file"
            ? parsed.kind : result.source_type === "table" || result.source_type === "file"
              ? result.source_type : "document";
          const path = result.path || "";
          const slash = path.lastIndexOf("/");
          const name = path.slice(slash + 1);
          const collection = slash >= 0 ? path.slice(0, slash) : undefined;
          const uri = result.uri || (path ? kind === "document" ? docUri(vault, path)
            : kind === "table" ? tableUri(vault, name, collection) : fileUri(vault, name, collection) : "");
          const docId = uri ? docIdFromUri(uri) : null;
          if (!docId) return [];
          return [{ docId, uri, title: result.title || path || "Untitled resource", kind,
            source: "search", context: parsed?.collection || collection }];
        }).slice(0, 8);
        setLookup({ query: term, status: response.degraded ? "error" : "ready", hits });
      })
      .catch(() => {
        if (!cancelled) setLookup({ query: term, status: "error", hits: [] });
      });
    return () => { cancelled = true; controller.abort(); };
  }, [term, debouncedQuery, vault, retry]);

  const loaded = nodes ?? hubs;
  const groups = useMemo(() => {
    const seen = new Set<string>();
    const local = loaded.filter(node => !term || node.name.toLocaleLowerCase().includes(term.toLocaleLowerCase()))
      .slice(0, 20).map((node): SearchHit => {
        seen.add(node.uri);
        return { docId: node.doc_id || docIdFromUri(node.uri) || node.uri, uri: node.uri,
          title: node.name, kind: node.kind, source: "loaded", context: parseUri(node.uri)?.collection || undefined };
      });
    const remote = term && lookup.query === term
      ? lookup.hits.filter(hit => !seen.has(hit.uri)) : [];
    // Use current loaded titles for recents: stored metadata is not access proof.
    const recent = !term ? history.recent.flatMap((entry): SearchHit[] => {
      const node = loaded.find(node => entry.uri ? node.uri === entry.uri : docIdFromUri(node.uri) === entry.doc_id);
      if (!node || seen.has(node.uri)) return [];
      seen.add(node.uri);
      return [{ docId: entry.doc_id, uri: node.uri, title: node.name, kind: node.kind, source: "recent" }];
    }) : [];
    return [
      { name: "Loaded resources", hits: local },
      { name: "Recently explored", hits: recent },
      { name: "Vault search", hits: remote },
    ].filter(group => group.hits.length > 0);
  }, [loaded, term, lookup, history.recent]);
  const suggestions = groups.flatMap(group => group.hits);
  const activeIndex = Math.max(0, suggestions.findIndex(hit => hit.uri === activeUri));
  const active = suggestions[activeIndex];
  const status = lookup.query === term && term === debouncedQuery ? lookup.status : "loading";
  const activeId = active ? searchId + "-option-" + encodeURIComponent(active.uri) : undefined;

  useEffect(() => {
    if (!searchOpen || !activeId) return;
    const option = document.getElementById(activeId);
    if (option && listRef.current?.contains(option)) option.scrollIntoView({ block: "nearest" });
  }, [activeId, searchOpen]);

  function choose(item: SearchHit) {
    history.pushRecent({ doc_id: item.docId, title: item.title, kind: item.kind, uri: item.uri });
    if (loaded.some(node => node.uri === item.uri) && onSelect) {
      onSelect(item.uri);
    } else {
      updateView(current => ({ ...current, entry: item.uri, selected: undefined, hops: 1 }));
    }
    setQuery("");
    inputRef.current?.focus();
    setSearchOpen(false);
  }

  const filterCount = ALL_NODE_KINDS.length - view.types.size +
    ALL_RELATIONS.length - view.relations.size + (hideOrphans ? 1 : 0) + (hiddenCount > 0 ? 1 : 0);

  function toggleType(kind: NodeKind) {
    updateView(current => {
      const types = new Set(current.types);
      if (types.has(kind)) types.delete(kind);
      else types.add(kind);
      return { ...current, types };
    });
  }

  return (
    <header className="@container/graph-toolbar relative z-[var(--z-sticky)] flex w-full min-w-0 flex-col gap-2">
      <h1 className="sr-only">Knowledge graph</h1>
      <div className="pointer-events-auto grid min-w-0 grid-cols-[minmax(0,1fr)_auto] items-center gap-2 rounded-[var(--radius-lg)] border border-border bg-surface p-2 shadow-md @min-[40rem]/graph-toolbar:grid-cols-[auto_minmax(0,1fr)_auto] @min-[40rem]/graph-toolbar:gap-x-3">
        <div role="group" aria-label="Display mode" className="col-span-2 flex items-center gap-1 @min-[40rem]/graph-toolbar:col-span-1">
          {(["graph", "list"] as const).map(mode => {
            const Icon = mode === "graph" ? Network : List;
            return <Button key={mode} type="button" variant="ghost" onClick={() => onDisplayModeChange(mode)} aria-pressed={displayMode === mode}
              className={cn("relative h-11 min-w-20 flex-1 gap-2 rounded-[var(--radius-sm)] px-3 text-sm focus-visible:ring-inset after:absolute after:inset-x-3 after:bottom-0 after:h-0.5 @min-[40rem]/graph-toolbar:h-9 @min-[40rem]/graph-toolbar:flex-none",
                displayMode === mode ? "font-semibold text-link after:bg-link hover:bg-surface-hover" : "text-foreground-muted hover:bg-surface-hover hover:text-foreground")}>
              <Icon className="h-4 w-4" aria-hidden />{mode === "graph" ? "Graph" : "List"}
            </Button>;
          })}
        </div>
        <div
          ref={searchRootRef}
          className="relative w-full min-w-0 justify-self-end @min-[40rem]/graph-toolbar:max-w-md"
          onBlur={(event) => {
            if (!event.currentTarget.contains(event.relatedTarget)) setSearchOpen(false);
          }}
        >
          <label htmlFor={searchId} className="sr-only">Find a resource</label>
          <Search className="pointer-events-none absolute left-3 top-1/2 z-10 h-4 w-4 -translate-y-1/2 text-foreground-muted" aria-hidden />
          <Input
            ref={inputRef}
            id={searchId}
            type="search"
            role="combobox"
            aria-autocomplete="list"
            aria-expanded={searchOpen}
            aria-controls={listId}
            aria-activedescendant={searchOpen ? activeId : undefined}
            autoComplete="off"
            value={query}
            onFocus={() => setSearchOpen(true)}
            onChange={(event) => { setQuery(event.target.value); setSearchOpen(true); setActiveUri(undefined); }}
            onKeyDown={(event) => {
              if (event.key === "ArrowDown" || event.key === "ArrowUp") {
                event.preventDefault();
                event.stopPropagation();
                const next = !searchOpen ? event.key === "ArrowDown" ? 0 : suggestions.length - 1
                  : Math.max(0, Math.min(suggestions.length - 1, activeIndex + (event.key === "ArrowDown" ? 1 : -1)));
                setSearchOpen(true);
                setActiveUri(suggestions[next]?.uri);
              } else if (event.key === "Enter" && searchOpen && active) {
                event.preventDefault();
                event.stopPropagation();
                choose(active);
              } else if (event.key === "Escape" && searchOpen) {
                event.preventDefault();
                event.stopPropagation();
                setSearchOpen(false);
              }
            }}
            placeholder="Find a resource…"
            className="h-11 rounded-[var(--radius-md)] bg-background pl-9 pr-11 focus:bg-surface @min-[40rem]/graph-toolbar:h-9"
          />
          {query && <button type="button" aria-label="Clear graph search"
            onClick={() => { setQuery(""); setActiveUri(undefined); inputRef.current?.focus(); }}
            className="absolute right-0 top-1/2 inline-flex h-11 w-11 -translate-y-1/2 items-center justify-center rounded-[var(--radius-sm)] text-foreground-muted hover:bg-surface-hover focus:outline-none focus-visible:ring-2 focus-visible:ring-ring @min-[40rem]/graph-toolbar:h-9 @min-[40rem]/graph-toolbar:w-9">
            <X className="h-3.5 w-3.5" aria-hidden />
          </button>}
          {searchOpen && (
            <div className="absolute left-0 top-[calc(100%+0.5rem)] z-[var(--z-popover)] w-full max-w-[calc(100vw-2rem)] overflow-hidden rounded-[var(--radius-md)] border border-border bg-surface shadow-md">
              <div className="flex items-center justify-between border-b border-border px-3 py-2 text-xs text-foreground-muted">
                <span>Select a resource to inspect</span>
                {!term && history.recent.length > 0 && <button type="button" onClick={history.clearRecent} className="text-link hover:text-link-hover focus:outline-none focus-visible:ring-2 focus-visible:ring-ring">Clear recent</button>}
              </div>
              <div ref={listRef} id={listId} role="listbox" aria-label="Resource suggestions"
                className="max-h-[min(20rem,50dvh)] overflow-y-auto py-1 rail-scroll">
                {groups.map(group => <div key={group.name} role="group" aria-label={group.name}>
                  <p className="px-3 py-1.5 text-xs font-medium text-foreground-muted" aria-hidden>{group.name}</p>
                  {group.hits.map(item => {
                    const Icon = kindIcon(item.kind);
                    return <button
                      key={item.uri}
                      id={searchId + "-option-" + encodeURIComponent(item.uri)}
                      type="button"
                      role="option"
                      tabIndex={-1}
                      aria-selected={item.uri === active?.uri}
                      onPointerMove={() => setActiveUri(item.uri)}
                      onMouseDown={event => event.preventDefault()}
                      onClick={() => choose(item)}
                      className={cn("flex min-h-11 w-full items-center gap-2 px-3 py-2 text-left transition-token focus:outline-none focus-visible:ring-2 focus-visible:ring-inset focus-visible:ring-ring",
                        item.uri === active?.uri ? "bg-surface-selected" : "hover:bg-surface-hover")}
                    >
                      <Icon className="h-4 w-4 shrink-0 text-foreground-muted" aria-hidden />
                      <span className="min-w-0 flex-1">
                        <span className="block truncate text-sm font-medium text-foreground">{item.title}</span>
                        <span className="block truncate text-xs text-foreground-muted">{item.kind}{item.context ? " · " + item.context : ""}</span>
                      </span>
                      <span className="text-xs text-link">{loaded.some(node => node.uri === item.uri) ? "Select" : "Focus"}</span>
                    </button>;
                  })}
                </div>)}
              </div>
              {term && status === "loading" && <p role="status" className="border-t border-border px-3 py-3 text-xs text-foreground-muted">Searching this Vault…</p>}
              {term && status === "error" && <div role="alert" className="border-t border-border p-3 text-xs text-foreground-muted">
                <p>Vault search is unavailable. Try again.</p>
                <Button variant="outline" size="sm" className="mt-2" onClick={() => setRetry(value => value + 1)}>Retry Vault search</Button>
              </div>}
              {suggestions.length === 0 && (!term || status === "ready") && <p className="px-3 py-4 text-xs text-foreground-muted">
                {term ? "No matching resources. Try another title or term." : "Type to search this Vault."}
              </p>}
              <p aria-live="polite" className="sr-only">{suggestions.length} resource suggestions</p>
            </div>
          )}
        </div>
        <div className="flex shrink-0 items-center gap-2">
          <GraphFilterMenu view={view} onUpdate={updateView} filterCount={filterCount}
            orphanCount={orphanCount} hideOrphans={hideOrphans} onToggleOrphans={onToggleOrphans}
            hiddenCount={hiddenCount} onUnhideAll={onUnhideAll} />
          <GraphMoreMenu view={view} saved={history.saved} onSave={history.saveView}
            onDelete={history.deleteView} onNavigate={onNavigate} onFit={onFit} onRearrange={onRearrange} />
        </div>
      </div>
      <div className="flex min-w-0 flex-wrap items-center gap-2">
        <div role="group" aria-label="Resource types" className="pointer-events-auto flex min-w-0 flex-wrap items-center rounded-[var(--radius-md)] border border-border bg-surface px-1 py-0.5 shadow-xs">
          <span className="mx-2 hidden text-xs text-foreground-muted @min-[40rem]/graph-toolbar:inline">Show</span>
          {ALL_NODE_KINDS.map(kind => {
            const selected = view.types.has(kind);
            const label = RESOURCE_LABELS[kind];
            const count = resourceCounts?.[kind];
            const countId = `${resourceCountId}-${kind}`;
            return <Button key={kind} type="button" variant="ghost" aria-label={`Show ${label.toLowerCase()}`}
              aria-pressed={selected} aria-describedby={count === undefined ? undefined : countId}
              title={count === undefined ? undefined : `${count} ${label.toLowerCase()} loaded in this graph`}
              onClick={() => toggleType(kind)}
              className={cn("h-11 gap-1.5 rounded-[var(--radius-sm)] px-1.5 text-xs hover:bg-surface-hover @min-[40rem]/graph-toolbar:h-7 @min-[40rem]/graph-toolbar:px-2",
                selected ? "text-foreground" : "text-foreground-muted")}>
              <GraphNodeMarker kind={kind} />
              <span className={selected ? undefined : "line-through"}>{label}</span>
              {count !== undefined && <>
                <span aria-hidden className="min-w-3 text-right tabular-nums text-foreground-muted">{count}</span>
                <span id={countId} className="sr-only">{`${count} ${label.toLowerCase()} loaded in this graph`}</span>
              </>}
              <Check aria-hidden className={cn("h-3 w-3 text-link", !selected && "invisible")} />
            </Button>;
          })}
        </div>
        {view.entry && (
          <div className="pointer-events-auto flex min-w-0 max-w-full flex-wrap items-center gap-2 rounded-[var(--radius-md)] border border-border bg-surface px-2 py-0.5 text-xs shadow-xs">
            <button type="button" onClick={() => updateView(current => ({ ...current, entry: undefined, selected: undefined, hops: 1 }))}
              className="min-h-11 font-medium text-link hover:text-link-hover focus:outline-none focus-visible:ring-2 focus-visible:ring-ring @min-[40rem]/graph-toolbar:min-h-9">Whole Vault</button>
            <ChevronRight className="h-3 w-3 shrink-0 text-foreground-muted" aria-hidden />
            <span className="max-w-44 truncate font-medium text-foreground" title={focusTitle || view.entry}>{focusTitle || view.entry}</span>
            <div role="group" aria-label="Neighborhood depth" className="flex items-center rounded-[var(--radius-sm)] bg-surface-2">
              {([1, 2, 3] as const).map(hops => <button key={hops} type="button"
                onClick={() => updateView(current => ({ ...current, hops }))} aria-pressed={view.hops === hops} aria-label={hops + " hop neighborhood"}
                className={cn("h-11 min-w-11 rounded-[var(--radius-sm)] px-1.5 tabular-nums focus:outline-none focus-visible:ring-2 focus-visible:ring-ring @min-[40rem]/graph-toolbar:h-9 @min-[40rem]/graph-toolbar:min-w-9",
                  view.hops === hops ? "bg-surface-selected font-medium text-surface-selected-foreground" : "text-foreground-muted hover:bg-surface-hover")}>{hops}</button>)}
            </div>
            <span className="text-foreground-muted">hops</span>
          </div>
        )}
      </div>
    </header>
  );
}

function GraphFilterMenu({
  view,
  onUpdate,
  filterCount,
  orphanCount,
  hideOrphans,
  onToggleOrphans,
  hiddenCount,
  onUnhideAll,
}: Pick<
  Props,
  | "view"
  | "orphanCount"
  | "hideOrphans"
  | "onToggleOrphans"
  | "hiddenCount"
  | "onUnhideAll"
> & { filterCount: number; onUpdate: UpdateView }) {
  function toggleType(kind: NodeKind) {
    onUpdate(current => {
      const types = new Set(current.types);
      if (types.has(kind)) types.delete(kind);
      else types.add(kind);
      return { ...current, types };
    });
  }

  function toggleRelation(relation: RelationKind) {
    onUpdate(current => {
      const relations = new Set(current.relations);
      if (relations.has(relation)) relations.delete(relation);
      else relations.add(relation);
      return { ...current, relations };
    });
  }

  function reset() {
    onUpdate(current => ({
      ...current,
      types: new Set(ALL_NODE_KINDS),
      relations: new Set(ALL_RELATIONS),
    }));
    if (hideOrphans) onToggleOrphans();
    if (hiddenCount) onUnhideAll();
  }

  return (
    <DropdownMenu.Root modal={false}>
      <DropdownMenu.Trigger asChild>
        <Button variant="outline" size="md" aria-label={`Filters${filterCount ? `, ${filterCount} active` : ""}`} title="Graph filters"
          className={cn("relative h-11 w-11 gap-2 rounded-[var(--radius-md)] px-2 shadow-none data-[state=open]:bg-surface-selected data-[state=open]:text-surface-selected-foreground @min-[24rem]/graph-toolbar:w-auto @min-[24rem]/graph-toolbar:px-3 @min-[40rem]/graph-toolbar:h-9",
            filterCount > 0 && "bg-surface-selected text-surface-selected-foreground")}>
          <SlidersHorizontal className="h-4 w-4" aria-hidden />
          <span className="hidden @min-[24rem]/graph-toolbar:inline">Filters</span>
          {filterCount > 0 && <span aria-hidden className="absolute -right-1 -top-1 inline-flex h-4 min-w-4 items-center justify-center rounded-full border border-border bg-surface-selected px-0.5 text-xs tabular-nums text-surface-selected-foreground">
            {filterCount}
          </span>}
        </Button>
      </DropdownMenu.Trigger>
      <DropdownMenu.Portal>
        <DropdownMenu.Content
          align="end"
          sideOffset={8}
          collisionPadding={12}
          className="z-[var(--z-popover)] max-h-[var(--radix-dropdown-menu-content-available-height)] w-80 max-w-[calc(100vw-1.5rem)] overflow-y-auto rounded-[var(--radius-md)] border border-border bg-surface shadow-md rail-scroll"
        >
          <div className="flex items-center justify-between border-b border-border px-3 py-2.5">
            <div>
              <p className="text-sm font-semibold text-foreground">Graph filters</p>
              <p className="mt-0.5 text-xs text-foreground-muted">Filters apply to loaded resources.</p>
            </div>
            <button
              type="button"
              onClick={reset}
              disabled={filterCount === 0}
              className="inline-flex h-11 items-center gap-1.5 rounded-[var(--radius-sm)] px-2 text-xs text-link hover:bg-surface-hover hover:text-link-hover focus:outline-none focus-visible:ring-2 focus-visible:ring-ring disabled:pointer-events-none disabled:opacity-50 sm:h-9"
            >
              <RotateCcw className="h-3.5 w-3.5" aria-hidden />
              Reset
            </button>
          </div>

          <div className="p-2">
            <DropdownMenu.Label className="px-2 pb-1 pt-1 text-xs font-medium text-foreground-muted">Resources</DropdownMenu.Label>
            {ALL_NODE_KINDS.map((kind) => (
              <DropdownMenu.CheckboxItem
                key={kind}
                checked={view.types.has(kind)}
                onCheckedChange={() => toggleType(kind)}
                onSelect={(event) => event.preventDefault()}
                className="relative flex h-11 cursor-pointer select-none items-center gap-2 rounded-[var(--radius-sm)] pl-8 pr-2 text-sm text-foreground outline-none hover:bg-surface-hover focus:bg-surface-hover sm:h-9"
              >
                <DropdownMenu.ItemIndicator className="absolute left-2.5">
                  <Check className="h-3.5 w-3.5 text-primary" aria-hidden />
                </DropdownMenu.ItemIndicator>
                <KindSwatch kind={kind} />
                <span className="capitalize">{kind}</span>
              </DropdownMenu.CheckboxItem>
            ))}

            <DropdownMenu.Separator className="my-2 h-px bg-border" />
            <DropdownMenu.Label className="px-2 pb-1 text-xs font-medium text-foreground-muted">Relationships</DropdownMenu.Label>
            <div className="grid grid-cols-1 gap-px sm:grid-cols-2">
              {ALL_RELATIONS.map((relation) => (
                <DropdownMenu.CheckboxItem
                  key={relation}
                  checked={view.relations.has(relation)}
                  onCheckedChange={() => toggleRelation(relation)}
                  onSelect={(event) => event.preventDefault()}
                  className="relative flex min-h-11 cursor-pointer select-none items-center gap-2 rounded-[var(--radius-sm)] pl-8 pr-2 text-xs text-foreground outline-none hover:bg-surface-hover focus:bg-surface-hover sm:min-h-9"
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
                    className="relative flex min-h-11 cursor-pointer select-none items-center rounded-[var(--radius-sm)] pl-8 pr-2 text-sm text-foreground outline-none hover:bg-surface-hover focus:bg-surface-hover sm:min-h-9"
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
                    className="flex min-h-11 cursor-pointer select-none items-center gap-2 rounded-[var(--radius-sm)] px-2 text-sm text-link outline-none hover:bg-surface-hover focus:bg-surface-hover sm:min-h-9"
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

function GraphMoreMenu({
  view, saved, onSave, onDelete, onNavigate, onFit, onRearrange,
}: {
  view: GraphView;
  saved: Array<{ name: string; url: string }>;
  onSave: (name: string, url: string) => void;
  onDelete: (name: string) => void;
  onNavigate: (url: string) => void;
  onFit: () => void;
  onRearrange?: () => void;
}) {
  const [panel, setPanel] = useState<"saved" | "help" | null>(null);
  const [name, setName] = useState("");
  const triggerRef = useRef<HTMLButtonElement>(null);
  const nameId = useId();
  const menuItem = "flex min-h-11 cursor-pointer select-none items-center gap-2 rounded-[var(--radius-sm)] px-2 text-sm text-foreground outline-none focus:bg-surface-hover sm:min-h-9";
  const menuLabel = "px-2 py-1.5 text-xs font-medium text-foreground-muted";

  function save() {
    const trimmed = name.trim();
    if (!trimmed) return;
    onSave(trimmed, "?" + viewToQuery(view));
    setName("");
  }

  return (
    <>
      <DropdownMenu.Root modal={false}>
        <DropdownMenu.Trigger asChild>
          <Button ref={triggerRef} variant="ghost" size="icon" aria-label="More graph actions" title="More graph actions"
            className="h-11 w-11 shrink-0 rounded-[var(--radius-sm)] data-[state=open]:bg-surface-selected data-[state=open]:text-surface-selected-foreground @min-[40rem]/graph-toolbar:h-9 @min-[40rem]/graph-toolbar:w-9">
            <MoreHorizontal className="h-4 w-4" aria-hidden />
          </Button>
        </DropdownMenu.Trigger>
        <DropdownMenu.Portal>
          <DropdownMenu.Content
            align="end"
            sideOffset={8}
            collisionPadding={12}
            onCloseAutoFocus={event => { if (panel) event.preventDefault(); }}
            className="z-[var(--z-popover)] max-h-[var(--radix-dropdown-menu-content-available-height)] w-56 max-w-[calc(100vw-1.5rem)] overflow-y-auto rounded-[var(--radius-md)] border border-border bg-surface p-1.5 shadow-md rail-scroll"
          >
            <DropdownMenu.Group>
              <DropdownMenu.Label className={menuLabel}>View</DropdownMenu.Label>
              <DropdownMenu.Item onSelect={onFit} className={menuItem}><Maximize2 className="h-4 w-4 text-foreground-muted" aria-hidden />Fit view</DropdownMenu.Item>
              {onRearrange && <DropdownMenu.Item onSelect={onRearrange} className={menuItem}><RotateCcw className="h-4 w-4 text-foreground-muted" aria-hidden />Rearrange</DropdownMenu.Item>}
            </DropdownMenu.Group>
            <DropdownMenu.Separator className="my-1 h-px bg-border" />
            <DropdownMenu.Group>
              <DropdownMenu.Label className={menuLabel}>Saved views</DropdownMenu.Label>
              <DropdownMenu.Item onSelect={() => setPanel("saved")} className={menuItem}><Bookmark className="h-4 w-4 text-foreground-muted" aria-hidden />Manage saved views</DropdownMenu.Item>
            </DropdownMenu.Group>
            <DropdownMenu.Separator className="my-1 h-px bg-border" />
            <DropdownMenu.Group>
              <DropdownMenu.Label className={menuLabel}>Help</DropdownMenu.Label>
              <DropdownMenu.Item onSelect={() => setPanel("help")} className={menuItem}><CircleHelp className="h-4 w-4 text-foreground-muted" aria-hidden />Graph help</DropdownMenu.Item>
            </DropdownMenu.Group>
          </DropdownMenu.Content>
        </DropdownMenu.Portal>
      </DropdownMenu.Root>
      <Dialog open={panel !== null} onOpenChange={open => { if (!open) setPanel(null); }}>
        <DialogContent className="max-w-sm" onCloseAutoFocus={event => {
          event.preventDefault();
          triggerRef.current?.focus();
        }}>
          <DialogHeader>
            <DialogTitle>{panel === "saved" ? "Saved graph views" : "Explore relationships"}</DialogTitle>
            <DialogDescription>{panel === "saved"
              ? "Save focus, resource types, and relationship filters in this browser. Positions and hidden resources are not saved."
              : "Read connections, explore a neighborhood, and open the original resource."}</DialogDescription>
          </DialogHeader>
          {panel === "saved" ? <>
            <form onSubmit={event => { event.preventDefault(); save(); }} className="flex gap-2">
              <label htmlFor={nameId} className="sr-only">View name</label>
              <Input id={nameId} value={name} onChange={event => setName(event.target.value)} placeholder="Name this view" className="min-w-0 flex-1" />
              <Button type="submit" disabled={!name.trim()}>Save</Button>
            </form>
            {saved.length > 0 ? <ul className="max-h-64 overflow-y-auto rail-scroll">
              {saved.map(item => <li key={item.name} className="flex items-center gap-1">
                <button type="button" onClick={() => { onNavigate(item.url); setPanel(null); }}
                  className="flex min-h-10 min-w-0 flex-1 items-center gap-2 rounded-[var(--radius-sm)] px-2 text-left text-sm text-link hover:bg-surface-hover focus:outline-none focus-visible:ring-2 focus-visible:ring-ring">
                  <Bookmark className="h-3.5 w-3.5 shrink-0 text-foreground-muted" aria-hidden /><span className="truncate">{item.name}</span>
                </button>
                <Button type="button" variant="ghost" size="icon" onClick={() => onDelete(item.name)} aria-label={"Delete saved view " + item.name}>
                  <Trash2 className="h-3.5 w-3.5" aria-hidden />
                </Button>
              </li>)}
            </ul> : <p className="text-sm text-foreground-muted">No saved views yet.</p>}
          </> : <ul className="space-y-3 text-sm leading-relaxed text-foreground-muted">
            <li>Select a resource to inspect its connections. Use Show neighborhood to focus around it.</li>
            <li>Loaded resources select within this scene. Vault search can open a different neighborhood.</li>
            <li>Drag to rotate in 3D, right-drag to pan, and scroll to zoom. On touch screens, use two fingers to pan or pinch to zoom.</li>
            <li>Camera buttons provide the same controls without dragging. Focus the canvas and use arrow keys to rotate, Shift + arrows to pan, + / − to zoom, or Home to fit. List provides a keyboard-accessible view.</li>
            <li>Pin a position from the resource actions. Rearrange calculates a fresh layout.</li>
          </ul>}
        </DialogContent>
      </Dialog>
    </>
  );
}
