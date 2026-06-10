import { useEffect, useMemo, useState } from "react";
import { Link, useParams } from "react-router-dom";
import { ArrowLeft, GitCommit, Search as SearchIcon, X } from "lucide-react";
import { getVaultActivity, type ActivityEntry } from "@/lib/api";
import { Alert } from "@/components/ui/alert";
import { Input } from "@/components/ui/input";
import { EmptyState } from "@/components/empty-state";
import { useDebounce } from "@/hooks/use-debounce";
import { timeAgo } from "@/lib/utils";

const PAGE_SIZE = 50;

export default function VaultActivityPage() {
  const { name } = useParams<{ name: string }>();
  const [author, setAuthor] = useState("");
  const debounced = useDebounce(author.trim(), 250);
  const [entries, setEntries] = useState<ActivityEntry[] | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");

  useEffect(() => {
    if (!name) return;
    // Reset stale state from previous param before re-fetch resolves.
    setEntries(null);
    setLoading(true);
    setError("");
    getVaultActivity(name, { author: debounced || undefined, limit: PAGE_SIZE })
      .then((r) => setEntries(r.activity || []))
      .catch((e) => {
        setError(e?.message || "Failed to load activity");
        setEntries([]);
      })
      .finally(() => setLoading(false));
  }, [name, debounced]);

  // Distinct author chips from current result set so the user can quick-filter
  // without remembering names. Top 6 by frequency.
  const authorChips = useMemo(() => {
    if (!entries) return [];
    const counts = new Map<string, number>();
    for (const e of entries) {
      const a = e.author_name || e.agent || e.author;
      if (a) counts.set(a, (counts.get(a) || 0) + 1);
    }
    return Array.from(counts.entries())
      .sort((a, b) => b[1] - a[1])
      .slice(0, 6);
  }, [entries]);

  if (!name) return null;

  return (
    <div className="fade-up">
      <div className="flex items-baseline justify-between mb-6 flex-wrap gap-y-2">
        <Link
          to={`/vault/${name}`}
          className="inline-flex items-center gap-1.5 coord hover:text-link transition-colors rounded-[var(--radius-sm)] focus:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2 focus-visible:ring-offset-background"
        >
          <ArrowLeft className="h-3 w-3" aria-hidden />
          Back to {name}
        </Link>
      </div>

      <div className="coord mb-3">Vault · {name} · Activity</div>
      <h1 className="font-display text-3xl tracking-tight text-foreground mb-2">
        Activity
      </h1>
      <p className="text-sm leading-relaxed text-foreground-muted mb-10 max-w-prose">
        Every commit landed in this vault. Filter by who made it — agents and humans
        share the same log.
      </p>

      {/* Filter row */}
      <div className="flex items-center gap-3 flex-wrap pb-4 border-b border-border mb-0">
        <div className="relative flex-1 min-w-[240px] max-w-md">
          <SearchIcon
            className="absolute left-3 top-1/2 -translate-y-1/2 h-4 w-4 text-foreground-muted pointer-events-none"
            aria-hidden
          />
          <Input
            value={author}
            onChange={(e) => setAuthor(e.target.value)}
            placeholder="Filter by author / agent name"
            aria-label="Filter activity by author"
            className="pl-9 pr-9"
            type="search"
          />
          {author && (
            <button
              onClick={() => setAuthor("")}
              aria-label="Clear filter"
              className="absolute right-2 top-1/2 -translate-y-1/2 text-foreground-muted hover:text-foreground cursor-pointer focus:outline-none focus-visible:ring-2 focus-visible:ring-ring"
            >
              <X className="h-4 w-4" aria-hidden />
            </button>
          )}
        </div>
        {authorChips.length > 0 && !author && (
          <div className="flex items-center gap-1.5 flex-wrap">
            <span className="coord">Quick</span>
            {authorChips.map(([a, n]) => (
              <button
                key={a}
                type="button"
                onClick={() => setAuthor(a)}
                className="inline-flex items-baseline gap-1 px-2 py-1 rounded-[var(--radius-md)] border border-border text-xs hover:border-border-strong hover:text-link transition-colors cursor-pointer focus:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2 focus-visible:ring-offset-background"
              >
                <span>{a}</span>
                <span className="coord tabular-nums">{n}</span>
              </button>
            ))}
          </div>
        )}
      </div>

      {/* List */}
      {error ? (
        <Alert variant="destructive" title="Failed to load" className="mt-4">{error}</Alert>
      ) : entries === null || (loading && entries.length === 0) ? (
        <div className="coord px-3 py-8" role="status" aria-live="polite">Loading…</div>
      ) : entries.length === 0 ? (
        <EmptyState
          title={author ? `No commits by "${author}"` : "No activity yet"}
          description={
            author
              ? "Try a different name, or clear the filter."
              : "Once an agent writes here, every change shows up in this log."
          }
        />
      ) : (
        <ol className="rounded-[var(--radius-lg)] overflow-hidden border border-border bg-surface shadow-sm divide-y divide-border mt-4">
          {entries.map((e, i) => {
            const filesCount = e.files?.length || 0;
            const primary = e.files?.[0];
            const link = primary
              ? `/vault/${name}/doc/${encodeURIComponent(primary.path)}` +
                (e.hash ? `?commit=${encodeURIComponent(e.hash)}` : "")
              : `/vault/${name}`;
            return (
              <li key={(e.hash || "") + i}>
                <Link
                  to={link}
                  className="group grid grid-cols-[70px_140px_1fr_auto] items-baseline gap-3 px-3 py-2 hover:bg-surface-hover transition-colors focus:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2 focus-visible:ring-offset-background"
                >
                  <span className="font-mono text-[11px] text-foreground-muted tabular-nums">
                    {(e.hash || "").slice(0, 7)}
                  </span>
                  <span title={e.author_name || e.agent || e.author || "unknown"} className="text-xs text-foreground truncate">
                    <GitCommit
                      className="inline-block h-3 w-3 mr-1 text-info -translate-y-px"
                      aria-hidden
                    />
                    {e.author_name || e.agent || e.author || "unknown"}
                  </span>
                  <div className="min-w-0">
                    <div title={e.subject || primary?.path || "(no subject)"} className="text-sm tracking-tight truncate text-foreground group-hover:text-link">
                      {e.subject || primary?.path || "(no subject)"}
                    </div>
                    {primary && (
                      <div className="coord truncate">
                        {primary.path}
                        {filesCount > 1 && (
                          <span className="text-foreground-muted">
                            {" "}
                            · +{filesCount - 1} more file{filesCount - 1 === 1 ? "" : "s"}
                          </span>
                        )}
                      </div>
                    )}
                  </div>
                  <span
                    className="coord tabular-nums w-[64px] text-right shrink-0"
                    title={e.timestamp}
                  >
                    {timeAgo(e.timestamp)}
                  </span>
                </Link>
              </li>
            );
          })}
        </ol>
      )}

      {entries && entries.length === PAGE_SIZE && (
        <p className="coord mt-4">
          Showing last {PAGE_SIZE} entries · use filter to narrow
        </p>
      )}
    </div>
  );
}
