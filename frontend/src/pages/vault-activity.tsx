import { useEffect, useState } from "react";
import { Link, useParams } from "react-router-dom";
import {
  AlertCircle,
  ArrowRight,
  Bot,
  GitBranch,
  GitCommit,
  Search,
  UserRound,
  X,
} from "lucide-react";

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Panel } from "@/components/ui/panel";
import { RelativeTime } from "@/components/ui/relative-time";
import { Skeleton } from "@/components/ui/skeleton";
import { TooltipText } from "@/components/ui/tooltip-text";
import { TonalIcon, type TonalIconTone } from "@/components/ui/tonal-icon";
import { useDebounce } from "@/hooks/use-debounce";
import { getVaultActivity, type ActivityEntry } from "@/lib/api";

const PAGE_SIZE = 50;

export default function VaultActivityPage() {
  const { name } = useParams<{ name: string }>();
  const [author, setAuthor] = useState("");
  const debouncedAuthor = useDebounce(author.trim(), 250);
  const [entries, setEntries] = useState<ActivityEntry[] | null>(null);
  const [quickAuthors, setQuickAuthors] = useState<Array<[string, number]>>([]);
  const [total, setTotal] = useState(0);
  const [error, setError] = useState("");
  const [retryKey, setRetryKey] = useState(0);

  useEffect(() => {
    if (!name) return;
    let active = true;
    setEntries(null);
    setError("");

    getVaultActivity(name, {
      author: debouncedAuthor || undefined,
      limit: PAGE_SIZE,
    })
      .then((result) => {
        if (!active) return;
        const activity = result.activity || [];
        setEntries(activity);
        setTotal(result.total ?? activity.length);
        if (!debouncedAuthor) setQuickAuthors(topActivityAuthors(activity));
      })
      .catch((caught: unknown) => {
        if (!active) return;
        setError(errorMessage(caught, "Failed to load activity"));
        setEntries(null);
      });

    return () => {
      active = false;
    };
  }, [debouncedAuthor, name, retryKey]);

  useEffect(() => {
    if (!name) return;
    const previous = document.title;
    document.title = `${name} · Activity · AKB`;
    return () => {
      document.title = previous;
    };
  }, [name]);

  if (!name) return null;

  const count = entries?.length ?? 0;
  return (
    <div className="w-full max-w-none">
      <h1 id="activity-heading" className="sr-only">
        Activity
      </h1>

      <Panel variant="workspace" className="w-full">
        <section aria-labelledby="activity-heading">
          <div
            data-testid="activity-ledger-header"
            className="flex min-h-16 flex-wrap items-center gap-3 border-b border-border-strong bg-surface-2/55 px-4 py-3 sm:px-5"
          >
            <TonalIcon tone="neutral">
              <GitCommit aria-hidden />
            </TonalIcon>
            <div className="min-w-0 flex-1">
              <div className="flex flex-wrap items-center gap-x-2 gap-y-1">
                <span className="text-sm font-semibold text-foreground">
                  Vault activity
                </span>
                {entries !== null && (
                  <Badge variant="default">
                    {total} change{total === 1 ? "" : "s"}
                  </Badge>
                )}
                <span className="text-xs text-foreground-muted">
                  {entries === null
                    ? error
                      ? "Unavailable"
                      : "Loading changes…"
                    : debouncedAuthor
                      ? `Changes by ${debouncedAuthor}`
                      : `Latest commits in ${name}`}
                </span>
              </div>
            </div>
            {count > 0 && (
              <Button asChild variant="ghost" size="sm" className="shrink-0">
                <Link to={`/vault/${name}`}>
                  Browse vault
                  <ArrowRight className="h-3.5 w-3.5" aria-hidden />
                </Link>
              </Button>
            )}
          </div>

          <ActivitySummary />

          {entries !== null && (
            <ActivityControls
              author={author}
              knownAuthors={quickAuthors}
              visibleCount={count}
              totalCount={total}
              onAuthorChange={setAuthor}
            />
          )}

          {error ? (
            <ActivityError
              error={error}
              onRetry={() => setRetryKey((current) => current + 1)}
            />
          ) : entries === null ? (
            <ActivitySkeleton />
          ) : entries.length === 0 ? (
            <ActivityEmptyState
              filtered={Boolean(debouncedAuthor)}
              onClear={() => setAuthor("")}
            />
          ) : (
            <ol
              aria-label="Vault activity"
              className="divide-y divide-border bg-surface"
            >
              {entries.map((entry, index) => (
                <ActivityRow
                  key={`${entry.hash || "activity"}-${index}`}
                  entry={entry}
                  number={index + 1}
                  vault={name}
                />
              ))}
            </ol>
          )}

          {entries && entries.length === PAGE_SIZE && (
            <p className="border-t border-border bg-surface-2/40 px-4 py-2 text-right text-xs text-foreground-muted sm:px-5">
              Showing the latest {PAGE_SIZE} changes · filter by author to
              narrow the log
            </p>
          )}
        </section>
      </Panel>
    </div>
  );
}

function ActivitySummary() {
  return (
    <div
      data-testid="activity-summary"
      className="flex flex-wrap items-center gap-x-2 gap-y-1 border-b border-border bg-surface px-4 py-2.5 text-xs sm:px-5"
    >
      <span className="inline-flex items-center gap-1.5 font-semibold text-foreground">
        <GitBranch
          className="h-3.5 w-3.5 text-[var(--color-cat-2)]"
          aria-hidden
        />
        Git-backed
      </span>
      <span className="text-subtle" aria-hidden>
        ·
      </span>
      <span className="inline-flex items-center gap-1 font-medium text-foreground">
        <UserRound className="h-3.5 w-3.5" aria-hidden />
        People
      </span>
      <span className="text-subtle" aria-hidden>
        +
      </span>
      <span className="inline-flex items-center gap-1 font-medium text-foreground">
        <Bot className="h-3.5 w-3.5" aria-hidden />
        Agents
      </span>
      <span className="text-subtle" aria-hidden>
        ·
      </span>
      <span className="font-medium text-foreground">Newest first</span>
      <span className="basis-full text-foreground-muted lg:ml-2 lg:basis-auto">
        Every commit in this vault is recorded here. Filter by who made
        it—people and agents share the same log.
      </span>
    </div>
  );
}

function ActivityControls({
  author,
  knownAuthors,
  visibleCount,
  totalCount,
  onAuthorChange,
}: {
  author: string;
  knownAuthors: Array<[string, number]>;
  visibleCount: number;
  totalCount: number;
  onAuthorChange: (value: string) => void;
}) {
  return (
    <div
      data-testid="activity-controls"
      className="flex flex-col gap-2 border-b border-border bg-surface px-4 py-3 sm:flex-row sm:items-center sm:px-5"
    >
      <div className="relative min-w-0 flex-1 sm:max-w-sm">
        <label htmlFor="activity-author-filter" className="sr-only">
          Filter activity by author
        </label>
        <Search
          className="pointer-events-none absolute left-2.5 top-1/2 h-3.5 w-3.5 -translate-y-1/2 text-foreground-muted"
          aria-hidden
        />
        <Input
          id="activity-author-filter"
          type="search"
          value={author}
          onChange={(event) => onAuthorChange(event.target.value)}
          placeholder="Filter by person or agent…"
          className="h-8 rounded-[var(--radius-sm)] pl-8 pr-8 text-xs"
        />
        {author && (
          <button
            type="button"
            aria-label="Clear author filter"
            onClick={() => onAuthorChange("")}
            className="absolute right-0 top-0 inline-flex h-8 w-8 cursor-pointer items-center justify-center rounded-[var(--radius-sm)] text-foreground-muted transition-token hover:text-foreground focus:outline-none focus-visible:ring-2 focus-visible:ring-ring"
          >
            <X className="h-3.5 w-3.5" aria-hidden />
          </button>
        )}
      </div>

      {knownAuthors.length > 0 && (
        <div
          aria-label="Frequent authors"
          className="flex flex-wrap items-center gap-1"
        >
          <span className="mr-1 text-xs font-medium text-foreground-muted">
            Quick
          </span>
          {knownAuthors.map(([actor, count]) => (
            <button
              key={actor}
              type="button"
              aria-label={`Filter by ${actor} (${count} ${count === 1 ? "change" : "changes"})`}
              aria-pressed={author.trim() === actor}
              onClick={() => onAuthorChange(actor)}
              className="inline-flex h-8 cursor-pointer items-center gap-1.5 rounded-[var(--radius-sm)] px-2.5 text-xs font-medium text-foreground-muted transition-token hover:bg-surface-hover hover:text-foreground focus:outline-none focus-visible:ring-2 focus-visible:ring-ring aria-pressed:bg-surface-selected aria-pressed:text-surface-selected-foreground"
            >
              {actor}
              <span className="tabular-nums text-subtle">{count}</span>
            </button>
          ))}
        </div>
      )}

      <span className="shrink-0 text-xs tabular-nums text-foreground-muted sm:pl-1">
        {visibleCount}/{totalCount}
      </span>
    </div>
  );
}

function ActivityRow({
  entry,
  number,
  vault,
}: {
  entry: ActivityEntry;
  number: number;
  vault: string;
}) {
  const primary = entry.files?.[0];
  const filesCount = entry.files?.length || 0;
  const title = entry.subject || primary?.path || "Untitled change";
  const href = primary
    ? `/vault/${vault}/doc/${encodeURIComponent(primary.path)}` +
      (entry.hash ? `?commit=${encodeURIComponent(entry.hash)}` : "")
    : `/vault/${vault}`;
  const change = changeLabel(primary?.change);
  const tone = activityTone(primary?.change);

  return (
    <li className="grid gap-2.5 px-4 py-2.5 transition-token hover:bg-surface-hover sm:grid-cols-[minmax(0,1fr)_auto] sm:items-center sm:px-5">
      <div className="flex min-w-0 items-start gap-3">
        <span
          aria-hidden
          className="w-5 shrink-0 pt-1.5 text-right text-xs tabular-nums text-subtle"
        >
          {number}
        </span>
        <TonalIcon tone={tone}>
          <GitCommit className="h-4 w-4" aria-hidden />
        </TonalIcon>
        <div className="min-w-0 flex-1">
          <div className="flex min-w-0 flex-wrap items-center gap-2">
            <TooltipText asChild tip={title}>
              <Link
                to={href}
                className="truncate rounded-[var(--radius-sm)] text-sm font-semibold tracking-tight text-foreground hover:text-link focus:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2 focus-visible:ring-offset-surface"
              >
                {title}
              </Link>
            </TooltipText>
            {change && <Badge variant="outline">{change}</Badge>}
            {filesCount > 1 && (
              <Badge variant="default">+{filesCount - 1} files</Badge>
            )}
          </div>
          <div className="mt-1 flex min-w-0 flex-wrap items-center gap-x-2 gap-y-1 text-xs text-foreground-muted">
            <span className="font-medium text-foreground">
              {activityActor(entry)}
            </span>
            {primary && (
              <>
                <span aria-hidden>·</span>
                <span title={primary.path} className="max-w-full truncate">
                  {primary.path}
                </span>
              </>
            )}
          </div>
        </div>
      </div>
      <div className="flex shrink-0 items-center justify-end gap-2 pl-11 sm:pl-0">
        {entry.hash && (
          <span className="font-mono text-[11px] tabular-nums text-foreground-muted">
            {entry.hash.slice(0, 7)}
          </span>
        )}
        <span className="text-xs text-foreground-muted">
          <RelativeTime iso={activityTimestamp(entry)} />
        </span>
      </div>
    </li>
  );
}

function ActivityEmptyState({
  filtered,
  onClear,
}: {
  filtered: boolean;
  onClear: () => void;
}) {
  return (
    <div className="flex min-h-80 items-center justify-center bg-surface px-6 py-12">
      <div className="max-w-md text-center">
        <TonalIcon
          tone="neutral"
          size="lg"
          className="mx-auto h-12 w-12 rounded-[var(--radius-lg)] shadow-xs"
        >
          {filtered ? (
            <Search className="h-5 w-5" aria-hidden />
          ) : (
            <GitCommit className="h-5 w-5" aria-hidden />
          )}
        </TonalIcon>
        <h2 className="mt-4 text-base font-semibold text-foreground">
          {filtered ? "No matching activity" : "No activity yet"}
        </h2>
        <p className="mt-1.5 text-sm leading-relaxed text-foreground-muted">
          {filtered
            ? "Try another person or agent name, or clear the current filter."
            : "Changes appear here after a person or connected agent writes to this vault."}
        </p>
        {filtered && (
          <Button
            variant="outline"
            size="sm"
            className="mt-5"
            onClick={onClear}
          >
            Clear filter
          </Button>
        )}
      </div>
    </div>
  );
}

function ActivityError({
  error,
  onRetry,
}: {
  error: string;
  onRetry: () => void;
}) {
  return (
    <div className="flex min-h-72 items-center justify-center bg-surface px-6 py-12">
      <div className="max-w-md text-center">
        <span className="mx-auto flex h-11 w-11 items-center justify-center rounded-[var(--radius-lg)] bg-destructive-soft text-destructive-soft-foreground">
          <AlertCircle className="h-5 w-5" aria-hidden />
        </span>
        <h2 className="mt-4 text-base font-semibold text-foreground">
          Failed to load
        </h2>
        <p className="mt-1.5 text-sm text-foreground-muted">{error}</p>
        <Button variant="outline" size="sm" className="mt-5" onClick={onRetry}>
          Retry
        </Button>
      </div>
    </div>
  );
}

function ActivitySkeleton() {
  return (
    <div role="status" aria-live="polite" aria-label="Loading activity">
      {[0, 1, 2, 3].map((index) => (
        <div
          key={index}
          className="flex items-center gap-3 border-b border-border px-4 py-2.5 sm:px-5"
          aria-hidden
        >
          <Skeleton className="h-3 w-5 shrink-0 rounded-[var(--radius-sm)]" />
          <Skeleton className="h-8 w-8 shrink-0 rounded-[var(--radius-sm)]" />
          <div className="min-w-0 flex-1 space-y-2">
            <Skeleton className="h-3.5 w-2/5 rounded-[var(--radius-sm)]" />
            <Skeleton className="h-3 w-3/5 rounded-[var(--radius-sm)]" />
          </div>
          <Skeleton className="h-3 w-24 rounded-[var(--radius-sm)]" />
        </div>
      ))}
      <span className="sr-only">Loading activity…</span>
    </div>
  );
}

function activityActor(entry: ActivityEntry): string {
  return entry.author_name || entry.agent || entry.author || "Unknown actor";
}

function topActivityAuthors(entries: ActivityEntry[]): Array<[string, number]> {
  const counts = new Map<string, number>();
  for (const entry of entries) {
    const actor = activityActor(entry);
    counts.set(actor, (counts.get(actor) || 0) + 1);
  }
  return Array.from(counts.entries())
    .sort((left, right) => right[1] - left[1])
    .slice(0, 5);
}

function activityTimestamp(entry: ActivityEntry): string | undefined {
  return entry.timestamp || entry.date;
}

function changeLabel(change?: string): string | null {
  if (!change) return null;
  const normalized = change.trim().toLocaleLowerCase();
  if (normalized.startsWith("a")) return "Added";
  if (normalized.startsWith("m")) return "Modified";
  if (normalized.startsWith("d")) return "Deleted";
  if (normalized.startsWith("r")) return "Renamed";
  return change;
}

function activityTone(change?: string): TonalIconTone {
  const normalized = change?.trim().toLocaleLowerCase() || "";
  if (normalized.startsWith("a")) return "success";
  if (normalized.startsWith("m")) return "warning";
  if (normalized.startsWith("d")) return "danger";
  if (normalized.startsWith("r")) return "info";
  return "neutral";
}

function errorMessage(error: unknown, fallback: string): string {
  return error instanceof Error && error.message ? error.message : fallback;
}
