// frontend/src/components/graph/GraphSidebar.tsx
import { useEffect, useRef, useState } from "react";
import { PanelLeftClose, Search as SearchIcon, X } from "lucide-react";
import { searchDocs } from "@/lib/api";
import { useGraphHistory } from "@/hooks/use-graph-history";
import { useDebounce } from "@/hooks/use-debounce";
import {
  ALL_NODE_KINDS,
  ALL_RELATIONS,
  type GraphView,
  type NodeKind,
  type RelationKind,
} from "./graph-types";
import { viewToQuery } from "./graph-state";
import { Section } from "./Section";
import { cn } from "@/lib/utils";

interface Props {
  vault: string;
  view: GraphView;
  onChange: (next: GraphView) => void;
  onNavigate: (queryString: string) => void;
  /** When provided, renders a collapse control in the sidebar header. */
  onCollapse?: () => void;
}

interface SearchHit {
  doc_id: string;
  title: string;
  type: NodeKind;
}

export function GraphSidebar({ vault, view, onChange, onNavigate, onCollapse }: Props) {
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
    onChange({ ...view, entry: hit.doc_id });
    setQuery("");
    setHits([]);
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
        <div className="flex items-center justify-between h-9 px-2 border-b border-border shrink-0">
          <span className="coord text-foreground-muted">Graph</span>
          <button
            type="button"
            onClick={onCollapse}
            aria-label="Hide sidebar"
            title="Hide sidebar"
            className="inline-flex h-6 w-6 items-center justify-center text-foreground-muted hover:text-foreground hover:bg-surface-hover cursor-pointer transition-colors focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
          >
            <PanelLeftClose className="h-3.5 w-3.5" />
          </button>
        </div>
      )}

      <Section label="Entry point" className="px-2">
        <div className="relative">
          <SearchIcon className="absolute left-2 top-1/2 -translate-y-1/2 h-3 w-3 text-foreground-muted pointer-events-none" />
          <input
            type="search"
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            placeholder="Search documents"
            aria-label="Search documents"
            className="w-full h-9 pl-6 pr-2 rounded-[var(--radius-md)] bg-background border border-border text-[11px] focus:outline-none focus:border-primary"
          />
        </div>
        {hits.length > 0 && (
          <ul className="mt-1 flex flex-col gap-px">
            {hits.map((h) => (
              <li key={h.doc_id}>
                <button
                  type="button"
                  onClick={() => commitEntry(h)}
                  className="w-full flex items-center justify-between gap-2 px-2 h-7 text-left text-[11px] hover:bg-surface-hover"
                >
                  <span title={h.title} className="truncate">{h.title}</span>
                  <span className="coord">{h.type}</span>
                </button>
              </li>
            ))}
          </ul>
        )}
        {view.entry && hits.length === 0 && (
          <div className="mt-1 flex items-center justify-between gap-2 px-2 h-7 text-[11px] bg-surface-muted">
            <span title={`entry: ${view.entry}`} className="truncate">entry: {view.entry}</span>
            <button
              type="button"
              onClick={() => onChange({ ...view, entry: undefined })}
              aria-label="Clear entry"
              className="text-foreground-muted hover:text-foreground"
            >
              <X className="h-3 w-3" />
            </button>
          </div>
        )}
      </Section>

      <Section label="Hops" className="px-2">
        <div
          className={cn(
            "flex items-center gap-3 text-[11px]",
            !view.entry && "opacity-50",
          )}
          aria-disabled={!view.entry}
        >
          {([1, 2, 3] as const).map((d) => (
            <label
              key={d}
              className={cn(
                "inline-flex items-center gap-1",
                view.entry ? "cursor-pointer" : "cursor-not-allowed",
              )}
            >
              <input
                type="radio"
                name="hops"
                checked={view.hops === d}
                disabled={!view.entry}
                onChange={() => onChange({ ...view, hops: d })}
                aria-label={`${d} hop${d === 1 ? "" : "s"}`}
              />
              {d}
            </label>
          ))}
        </div>
        {!view.entry && (
          <p className="coord text-foreground-muted mt-2">
            Set an entry point to enable
          </p>
        )}
      </Section>

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
                "inline-flex items-center h-7 px-2.5 rounded-[var(--radius-sm)] border text-[10px] font-semibold",
                view.types.has(k)
                  ? "border-primary bg-surface-selected text-surface-selected-foreground"
                  : "border-border text-foreground-muted hover:bg-surface-hover",
              )}
            >
              {k}
            </button>
          ))}
        </div>
      </Section>

      <Section label="Relations" className="px-2">
        <div className="grid grid-cols-2 gap-1">
          {ALL_RELATIONS.map((r) => (
            <button
              key={r}
              type="button"
              onClick={() => toggleRelation(r)}
              aria-label={`Toggle ${r}`}
              aria-pressed={view.relations.has(r)}
              className={cn(
                "inline-flex items-center h-7 px-2.5 rounded-[var(--radius-sm)] border text-[10px] font-semibold text-left",
                view.relations.has(r)
                  ? "border-primary bg-surface-selected text-surface-selected-foreground"
                  : "border-border text-foreground-muted hover:bg-surface-hover",
              )}
            >
              {r}
            </button>
          ))}
        </div>
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
                  onClick={() => onChange({ ...view, entry: r.doc_id })}
                  title={r.title}
                  className="w-full text-left px-2 h-7 text-[11px] hover:bg-surface-hover active:bg-surface-active truncate"
                >
                  {r.title}
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
                  title={s.name}
                  className="flex-1 text-left px-2 h-7 text-[11px] hover:bg-surface-hover active:opacity-60 transition-opacity duration-150 truncate"
                >
                  {s.name}
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
