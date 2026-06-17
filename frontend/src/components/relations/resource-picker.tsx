import { useEffect, useRef, useState } from "react";
import { Loader2, Search as SearchIcon, X } from "lucide-react";
import { searchDocs } from "@/lib/api";
import { parseUri } from "@/lib/uri";
import { TooltipText } from "@/components/ui/tooltip-text";

export interface PickedResource {
  uri: string;
  title: string;
  path: string;
}

interface ResourcePickerProps {
  /** Search is scoped to this vault — links must stay intra-vault. */
  vault: string;
  /** The source doc's own URI, filtered out so you can't self-link. */
  excludeUri: string;
  value: PickedResource | null;
  onChange: (value: PickedResource | null) => void;
}

/**
 * A lightweight search-as-you-type picker for choosing a target document within
 * one vault. There is no shared combobox primitive yet, so this wraps an input +
 * a results popdown over `searchDocs`, debounced. Selecting collapses to a chip.
 */
export function ResourcePicker({ vault, excludeUri, value, onChange }: ResourcePickerProps) {
  const [query, setQuery] = useState("");
  const [results, setResults] = useState<PickedResource[]>([]);
  const [loading, setLoading] = useState(false);
  const [searchError, setSearchError] = useState(false);
  const [open, setOpen] = useState(false);
  const boxRef = useRef<HTMLDivElement>(null);

  // Debounced search. A blank query clears results rather than listing the corpus.
  // `cancelled` lives at effect scope (not inside setTimeout) so the cleanup
  // actually flips it — an in-flight request that resolves after the user types
  // again, clears the box, or unmounts must not clobber newer state.
  useEffect(() => {
    const q = query.trim();
    if (!q) {
      setResults([]);
      setLoading(false);
      setSearchError(false);
      return;
    }
    let cancelled = false;
    setLoading(true);
    const t = setTimeout(() => {
      searchDocs(q, vault, 8)
        .then((r) => {
          if (cancelled) return;
          setSearchError(false);
          const rows: PickedResource[] = (r.results || [])
            .map((d: { uri?: string; title?: string; path?: string }) => ({
              uri: d.uri ?? "",
              title: d.title || d.path || d.uri || "",
              path: d.path || parseUri(d.uri)?.id || "",
            }))
            .filter((d) => d.uri && d.uri !== excludeUri);
          setResults(rows);
        })
        .catch((e) => {
          if (cancelled) return;
          // Surface as a distinct state — a backend/network failure must not
          // masquerade as a genuine "no matching documents" empty result.
          console.error("ResourcePicker: target search failed", e);
          setSearchError(true);
          setResults([]);
        })
        .finally(() => {
          if (!cancelled) setLoading(false);
        });
    }, 250);
    return () => {
      cancelled = true;
      clearTimeout(t);
    };
  }, [query, vault, excludeUri]);

  // Dismiss the popdown on an outside click.
  useEffect(() => {
    if (!open) return;
    const onDown = (e: MouseEvent) => {
      if (boxRef.current && !boxRef.current.contains(e.target as Node)) setOpen(false);
    };
    document.addEventListener("mousedown", onDown);
    return () => document.removeEventListener("mousedown", onDown);
  }, [open]);

  if (value) {
    return (
      <div className="flex items-center justify-between gap-2 rounded-[var(--radius-md)] border border-border bg-surface px-3 py-2">
        <TooltipText className="truncate text-sm text-foreground" tip={value.path}>
          {value.title}
        </TooltipText>
        <button
          type="button"
          onClick={() => onChange(null)}
          aria-label="Clear selected target"
          className="shrink-0 rounded-[var(--radius-sm)] text-foreground-muted transition-colors hover:text-foreground focus:outline-none focus-visible:ring-2 focus-visible:ring-ring"
        >
          <X className="h-4 w-4" aria-hidden />
        </button>
      </div>
    );
  }

  return (
    <div ref={boxRef} className="relative">
      <div className="relative">
        <SearchIcon
          className="pointer-events-none absolute left-3 top-1/2 h-4 w-4 -translate-y-1/2 text-foreground-muted"
          aria-hidden
        />
        {loading && (
          <Loader2 className="absolute right-3 top-1/2 h-4 w-4 -translate-y-1/2 animate-spin text-foreground-muted" aria-hidden />
        )}
        <input
          type="search"
          value={query}
          onChange={(e) => {
            setQuery(e.target.value);
            setOpen(true);
          }}
          onFocus={() => setOpen(true)}
          placeholder="Search a document to link…"
          aria-label="Search a target document"
          className="h-10 w-full rounded-[var(--radius-md)] border border-border bg-surface pl-9 pr-9 text-sm text-foreground placeholder:text-foreground-muted transition-colors focus:border-primary focus:outline-none focus-visible:ring-2 focus-visible:ring-ring"
        />
      </div>
      {open && query.trim() && (
        <div className="absolute z-[var(--z-popover)] mt-1 max-h-60 w-full overflow-y-auto rounded-[var(--radius-md)] border border-border bg-surface p-1 shadow-md">
          {results.length === 0 ? (
            <div className="px-3 py-2 text-xs text-foreground-muted">
              {loading
                ? "Searching…"
                : searchError
                  ? "Search is unavailable — try again."
                  : "No matching documents"}
            </div>
          ) : (
            results.map((r) => (
              <button
                key={r.uri}
                type="button"
                onClick={() => {
                  onChange(r);
                  setOpen(false);
                  setQuery("");
                }}
                className="flex w-full flex-col items-start gap-0.5 rounded-[var(--radius-sm)] px-3 py-2 text-left outline-none transition-colors hover:bg-surface-hover focus-visible:bg-surface-hover"
              >
                <TooltipText className="w-full truncate text-sm text-foreground" tip={r.title}>
                  {r.title}
                </TooltipText>
                <TooltipText className="w-full truncate text-xs text-foreground-muted" tip={r.path}>
                  {r.path}
                </TooltipText>
              </button>
            ))
          )}
        </div>
      )}
    </div>
  );
}
