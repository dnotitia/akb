// frontend/src/components/graph/GraphSidebar.tsx
import { useEffect, useRef, useState } from "react";
import { Search as SearchIcon, X } from "lucide-react";
import { searchDocs } from "@/lib/api";
import { useGraphHistory } from "@/hooks/use-graph-history";
import {
  ALL_NODE_KINDS,
  ALL_RELATIONS,
  type GraphView,
  type NodeKind,
  type RelationKind,
} from "./graph-types";
import { cn } from "@/lib/utils";

interface Props {
  vault: string;
  view: GraphView;
  currentUrl: string; // e.g. "?entry=d-1&depth=2" — used when saving a view
  onChange: (next: GraphView) => void;
  onNavigate: (queryString: string) => void;
}

interface SearchHit {
  doc_id: string;
  title: string;
  type: NodeKind;
}

/** Build a query string from the current GraphView so saved views survive prop drift. */
function viewToQueryString(view: GraphView, currentUrl: string): string {
  // If entry is present, build a minimal query string from view state.
  if (view.entry) {
    const p = new URLSearchParams();
    p.set("entry", view.entry);
    p.set("depth", String(view.depth));
    return "?" + p.toString();
  }
  return currentUrl;
}

export function GraphSidebar({ vault, view, currentUrl, onChange, onNavigate }: Props) {
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

  // Focus the input whenever it appears (savingName transitions from null → "").
  useEffect(() => {
    if (savingName !== null) {
      saveNameRef.current?.focus();
    }
  }, [savingName]);

  // Debounced search → /search
  useEffect(() => {
    if (!query.trim()) {
      setHits([]);
      return;
    }
    const handle = setTimeout(async () => {
      try {
        // searchDocs(query, vault?, limit) — order: query first, vault second
        const resp = await searchDocs(query.trim(), vault, 8);
        const rows = (resp.results || []).slice(0, 8).map((r: any) => ({
          doc_id: r.doc_id || r.id,
          title: r.title || r.name || r.doc_id || "(untitled)",
          type: (r.resource_type || r.type || "document") as NodeKind,
        }));
        setHits(rows);
      } catch {
        setHits([]);
      }
    }, 300);
    return () => clearTimeout(handle);
  }, [query, vault]);

  function commitEntry(hit: SearchHit) {
    pushRecent({ doc_id: hit.doc_id, title: hit.title });
    onChange({ ...view, entry: hit.doc_id });
    setQuery("");
    setHits([]);
  }

  function toggleType(k: NodeKind) {
    const next = new Set(view.types);
    next.has(k) ? next.delete(k) : next.add(k);
    onChange({ ...view, types: next });
  }

  function toggleRelation(r: RelationKind) {
    const next = new Set(view.relations);
    next.has(r) ? next.delete(r) : next.add(r);
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
    const url = viewToQueryString(view, currentUrl);
    saveView(trimmed, url);
    setSavingName(null);
  }

  function cancelSave() {
    saveCommittedRef.current = true; // suppress onBlur
    setSavingName(null);
  }

  return (
    <aside
      className="flex flex-col h-full overflow-y-auto border-r border-border bg-surface"
      aria-label="Graph controls"
    >
      <Section label="ENTRY POINT">
        <div className="relative">
          <SearchIcon className="absolute left-2 top-1/2 -translate-y-1/2 h-3 w-3 text-foreground-muted pointer-events-none" />
          <input
            type="search"
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            placeholder="Search documents"
            aria-label="Search documents"
            className="w-full h-9 pl-6 pr-2 bg-background border border-border text-[11px] focus:outline-none focus:border-accent"
          />
        </div>
        {hits.length > 0 && (
          <ul className="mt-1 flex flex-col gap-px">
            {hits.map((h) => (
              <li key={h.doc_id}>
                <button
                  type="button"
                  onClick={() => commitEntry(h)}
                  className="w-full flex items-center justify-between gap-2 px-2 h-7 text-left text-[11px] hover:bg-surface-muted"
                >
                  <span className="truncate">{h.title}</span>
                  <span className="coord">{h.type}</span>
                </button>
              </li>
            ))}
          </ul>
        )}
        {view.entry && hits.length === 0 && (
          <div className="mt-1 flex items-center justify-between gap-2 px-2 h-7 text-[11px] bg-surface-muted">
            <span className="truncate">entry: {view.entry}</span>
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

      <Section label="DEPTH">
        <div className="flex items-center gap-3">
          {([1, 2, 3] as const).map((d) => (
            <label key={d} className="inline-flex items-center gap-1 text-[11px] cursor-pointer">
              <input
                type="radio"
                name="depth"
                checked={view.depth === d}
                disabled={!view.entry}
                onChange={() => onChange({ ...view, depth: d })}
                aria-label={`Depth ${d}`}
              />
              {d}
            </label>
          ))}
        </div>
      </Section>

      <Section label="TYPES">
        <div className="flex flex-wrap gap-1">
          {ALL_NODE_KINDS.map((k) => (
            <button
              key={k}
              type="button"
              onClick={() => toggleType(k)}
              aria-label={`Toggle ${k}`}
              aria-pressed={view.types.has(k)}
              className={cn(
                "px-1.5 py-0.5 border font-mono text-[10px] uppercase tracking-[0.12em]",
                view.types.has(k)
                  ? "border-foreground text-foreground"
                  : "border-border text-foreground-muted opacity-50",
              )}
            >
              {k}
            </button>
          ))}
        </div>
      </Section>

      <Section label="RELATIONS">
        <div className="grid grid-cols-2 gap-1">
          {ALL_RELATIONS.map((r) => (
            <button
              key={r}
              type="button"
              onClick={() => toggleRelation(r)}
              aria-label={`Toggle ${r}`}
              aria-pressed={view.relations.has(r)}
              className={cn(
                "px-1.5 py-0.5 border font-mono text-[10px] uppercase tracking-[0.12em] text-left",
                view.relations.has(r)
                  ? "border-foreground text-foreground"
                  : "border-border text-foreground-muted opacity-50",
              )}
            >
              {r}
            </button>
          ))}
        </div>
      </Section>

      <Section
        label="RECENT"
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
                  className="w-full text-left px-2 h-7 text-[11px] hover:bg-surface-muted truncate"
                >
                  {r.title}
                </button>
              </li>
            ))}
          </ul>
        )}
      </Section>

      <Section
        label="SAVED VIEWS"
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
            className="w-full h-7 px-2 bg-background border border-accent text-[11px] focus:outline-none mb-1"
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
                  className="flex-1 text-left px-2 h-7 text-[11px] hover:bg-surface-muted truncate"
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

function Section({
  label,
  rightAction,
  children,
}: {
  label: string;
  rightAction?: React.ReactNode;
  children: React.ReactNode;
}) {
  return (
    <section className="border-b border-border px-2 py-3">
      <div className="flex items-center justify-between mb-2">
        <span className="coord">§ {label}</span>
        {rightAction}
      </div>
      {children}
    </section>
  );
}
