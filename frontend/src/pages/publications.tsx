import {
  useCallback,
  useEffect,
  useMemo,
  useState,
  type ComponentType,
} from "react";
import { Link, useParams } from "react-router-dom";
import * as DropdownMenu from "@radix-ui/react-dropdown-menu";
import {
  AlertCircle,
  ArrowRight,
  CheckCircle2,
  Clock3,
  Copy,
  ExternalLink,
  FileText,
  Globe,
  KeyRound,
  Loader2,
  MoreHorizontal,
  Paperclip,
  Search,
  Share2,
  Table as TableIcon,
  Trash2,
} from "lucide-react";

import { ConfirmDialog } from "@/components/ui/confirm-dialog";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Panel } from "@/components/ui/panel";
import { RelativeTime } from "@/components/ui/relative-time";
import { Skeleton } from "@/components/ui/skeleton";
import { TooltipText } from "@/components/ui/tooltip-text";
import { TonalIcon, type TonalIconTone } from "@/components/ui/tonal-icon";
import { WorkspaceSectionHeader } from "@/components/ui/workspace-section-header";
import {
  deletePublication,
  getDocument,
  listPublications,
  type Publication,
} from "@/lib/api";
import { parseDocUri, parseFileUri } from "@/lib/uri";
import { formatDate } from "@/lib/utils";

const RESOURCE_ICON: Record<
  Publication["resource_type"],
  ComponentType<{ className?: string; "aria-hidden"?: boolean }>
> = {
  document: FileText,
  table_query: TableIcon,
  file: Paperclip,
};

const RESOURCE_LABEL: Record<Publication["resource_type"], string> = {
  document: "Document",
  table_query: "Table",
  file: "File",
};

const RESOURCE_TONE: Record<Publication["resource_type"], TonalIconTone> = {
  document: "knowledge",
  table_query: "data",
  file: "file",
};

type PublicationFilter = "all" | Publication["resource_type"];

const FILTER_THRESHOLD = 8;
const PUBLICATION_FILTERS: Array<{ value: PublicationFilter; label: string }> =
  [
    { value: "all", label: "All" },
    { value: "document", label: "Documents" },
    { value: "table_query", label: "Tables" },
    { value: "file", label: "Files" },
  ];

export default function PublicationsPage() {
  const { name } = useParams<{ name: string }>();
  const [items, setItems] = useState<Publication[] | null>(null);
  const [error, setError] = useState("");
  const [copiedId, setCopiedId] = useState<string | null>(null);
  const [revokingId, setRevokingId] = useState<string | null>(null);
  const [pendingRevoke, setPendingRevoke] = useState<Publication | null>(null);
  const [query, setQuery] = useState("");
  const [typeFilter, setTypeFilter] = useState<PublicationFilter>("all");

  const load = useCallback(async (vault: string) => {
    setError("");
    try {
      const result = await listPublications(vault);
      const publications: Publication[] = result.publications || [];
      const enriched = await Promise.all(
        publications.map(async (publication) => {
          if (
            publication.title ||
            publication.resource_type !== "document" ||
            !publication.resource_uri
          ) {
            return publication;
          }
          const docPath = parseDocUri(publication.resource_uri)?.id;
          if (!docPath) return publication;
          try {
            const document = await getDocument(vault, docPath);
            return {
              ...publication,
              title: document.title || publication.title,
            };
          } catch {
            return publication;
          }
        }),
      );
      setItems(enriched);
    } catch (caught: unknown) {
      setError(errorMessage(caught, "Failed to load publications"));
      setItems(null);
    }
  }, []);

  useEffect(() => {
    if (!name) return;
    setItems(null);
    setError("");
    setQuery("");
    setTypeFilter("all");
    void load(name);
  }, [load, name]);

  useEffect(() => {
    if (!name) return;
    const previous = document.title;
    document.title = `${name} · Publish · AKB`;
    return () => {
      document.title = previous;
    };
  }, [name]);

  const totalViews = useMemo(
    () =>
      (items || []).reduce(
        (total, publication) => total + (publication.view_count || 0),
        0,
      ),
    [items],
  );

  const filteredItems = useMemo(() => {
    if (!items) return [];
    const normalizedQuery = query.trim().toLocaleLowerCase();
    return items.filter((publication) => {
      const matchesType =
        typeFilter === "all" || publication.resource_type === typeFilter;
      const searchable = [
        publicationTitle(publication),
        publication.slug,
        RESOURCE_LABEL[publication.resource_type],
      ]
        .join(" ")
        .toLocaleLowerCase();
      return (
        matchesType &&
        (!normalizedQuery || searchable.includes(normalizedQuery))
      );
    });
  }, [items, query, typeFilter]);

  const publicationNumbers = useMemo(
    () =>
      new Map(
        (items || []).map((publication, index) => [
          publication.slug,
          index + 1,
        ]),
      ),
    [items],
  );

  async function confirmRevoke() {
    if (!name || !pendingRevoke) return;
    setRevokingId(pendingRevoke.slug);
    try {
      await deletePublication(name, pendingRevoke.slug);
      await load(name);
    } finally {
      setRevokingId(null);
    }
  }

  async function copyLink(publication: Publication) {
    try {
      if (!navigator.clipboard) return;
      await navigator.clipboard.writeText(publication.share_url);
      setCopiedId(publication.slug);
      setTimeout(() => setCopiedId(null), 1500);
    } catch {
      // The public page remains available through Open when clipboard access is blocked.
    }
  }

  if (!name) return null;

  const count = items?.length ?? 0;
  const filtersVisible = count > FILTER_THRESHOLD;
  const filtersActive = query.trim().length > 0 || typeFilter !== "all";

  return (
    <>
      <div data-testid="publication-workspace" className="w-full max-w-none">
        <h1 id="publications-heading" className="sr-only">
          Publish
        </h1>

        <WorkspaceSectionHeader
          id="published-links-heading"
          icon={Share2}
          title="Published links"
          description="Manage public, read-only links created from this Vault."
          tone="publish"
          testId="publication-ledger-header"
          right={
            <>
              {items !== null && (
                <Badge variant="default">
                  {count} link{count === 1 ? "" : "s"}
                </Badge>
              )}
              <span className="text-xs tabular-nums text-foreground-muted">
                {items === null
                  ? error
                    ? "Unavailable"
                    : "Loading links…"
                  : count > 0
                    ? `${totalViews.toLocaleString()} total views`
                    : "Vault content is private by default"}
              </span>
              {count > 0 && (
                <Button asChild variant="ghost" size="sm" className="shrink-0">
                  <Link to={`/vault/${name}`}>
                    Browse vault
                    <ArrowRight className="h-3.5 w-3.5" aria-hidden />
                  </Link>
                </Button>
              )}
            </>
          }
        />

        <Panel variant="workspace" className="w-full">
          <section aria-labelledby="published-links-heading">
            <PublicationPolicySummary />

            {items !== null && filtersVisible && (
              <PublicationControls
                query={query}
                typeFilter={typeFilter}
                visibleCount={filteredItems.length}
                totalCount={count}
                onQueryChange={setQuery}
                onTypeFilterChange={setTypeFilter}
              />
            )}

            {error ? (
              <PublicationError error={error} onRetry={() => void load(name)} />
            ) : items === null ? (
              <PublicationSkeleton />
            ) : items.length === 0 ? (
              <PublicationEmptyState vault={name} />
            ) : filteredItems.length === 0 ? (
              <PublicationNoResults
                onClear={() => {
                  setQuery("");
                  setTypeFilter("all");
                }}
              />
            ) : (
              <ol
                aria-label="Published links"
                className="divide-y divide-border bg-surface"
              >
                {filteredItems.map((publication) => (
                  <PublicationRow
                    key={publication.slug}
                    publication={publication}
                    number={publicationNumbers.get(publication.slug) ?? 0}
                    vault={name}
                    copied={copiedId === publication.slug}
                    revoking={revokingId === publication.slug}
                    onCopy={() => void copyLink(publication)}
                    onRevoke={() => setPendingRevoke(publication)}
                  />
                ))}
              </ol>
            )}
            {filtersVisible && filtersActive && filteredItems.length > 0 && (
              <p className="border-t border-border bg-surface-2/40 px-4 py-2 text-right text-xs tabular-nums text-foreground-muted sm:px-5">
                Showing {filteredItems.length} of {count} links
              </p>
            )}
          </section>
        </Panel>
      </div>

      <ConfirmDialog
        open={pendingRevoke !== null}
        onOpenChange={(open) => !open && setPendingRevoke(null)}
        title={
          pendingRevoke ? `Unpublish "${publicationTitle(pendingRevoke)}"?` : ""
        }
        description={
          pendingRevoke
            ? `The link /p/${pendingRevoke.slug} will stop working immediately.\nThis cannot be undone.`
            : ""
        }
        confirmLabel="Unpublish"
        variant="destructive"
        onConfirm={confirmRevoke}
      />
    </>
  );
}

function PublicationRow({
  publication,
  number,
  vault,
  copied,
  revoking,
  onCopy,
  onRevoke,
}: {
  publication: Publication;
  number: number;
  vault: string;
  copied: boolean;
  revoking: boolean;
  onCopy: () => void;
  onRevoke: () => void;
}) {
  const Icon = RESOURCE_ICON[publication.resource_type];
  const title = publicationTitle(publication);
  const views = publication.max_views
    ? `${publication.view_count ?? 0}/${publication.max_views} views`
    : `${publication.view_count ?? 0} views`;

  return (
    <li className="grid gap-2.5 px-4 py-2.5 transition-token hover:bg-surface-hover sm:grid-cols-[minmax(0,1fr)_auto] sm:items-center sm:px-5">
      <div className="flex min-w-0 items-start gap-3">
        <span
          data-testid="publication-index"
          aria-hidden
          className="w-5 shrink-0 pt-1.5 text-right text-xs tabular-nums text-subtle"
        >
          {number}
        </span>
        <TonalIcon tone={RESOURCE_TONE[publication.resource_type]}>
          <Icon className="h-4 w-4" aria-hidden />
        </TonalIcon>
        <div className="min-w-0 flex-1">
          <div className="flex min-w-0 flex-wrap items-center gap-2">
            <TooltipText asChild tip={title}>
              <Link
                to={resourceHref(vault, publication)}
                className="truncate rounded-[var(--radius-sm)] text-sm font-semibold tracking-tight text-foreground hover:text-link focus:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2 focus-visible:ring-offset-surface"
              >
                {title}
              </Link>
            </TooltipText>
            <Badge variant="success">
              <Globe className="h-3 w-3" aria-hidden />
              Live
            </Badge>
            <Badge variant="outline">
              {RESOURCE_LABEL[publication.resource_type]}
            </Badge>
            {publication.mode === "snapshot" && (
              <Badge variant="default">Snapshot</Badge>
            )}
          </div>

          <div className="mt-1 flex min-w-0 flex-wrap items-center gap-x-2 gap-y-1 text-xs text-foreground-muted">
            <span
              title={`/p/${publication.slug}`}
              className="truncate font-mono"
            >
              /p/{publication.slug}
            </span>
            <span aria-hidden>·</span>
            <span className="tabular-nums">{views}</span>
            <span aria-hidden>·</span>
            <span>
              Published <RelativeTime iso={publication.created_at} />
            </span>
            {publication.password_protected && (
              <>
                <span aria-hidden>·</span>
                <span className="inline-flex items-center gap-1">
                  <KeyRound className="h-3 w-3" aria-hidden />
                  Password
                </span>
              </>
            )}
            <span aria-hidden>·</span>
            <span className="inline-flex items-center gap-1">
              <Clock3 className="h-3 w-3" aria-hidden />
              {publication.expires_at
                ? `Expires ${formatDate(publication.expires_at)}`
                : "No expiry"}
            </span>
          </div>
        </div>
      </div>

      <div className="flex shrink-0 items-center justify-end gap-1">
        <button
          type="button"
          onClick={onCopy}
          aria-label={
            copied ? "Public link copied" : `Copy public link for ${title}`
          }
          className="inline-flex h-8 cursor-pointer items-center gap-1.5 rounded-[var(--radius-sm)] px-2.5 text-xs font-medium text-foreground-muted transition-token hover:bg-surface-hover hover:text-foreground focus:outline-none focus-visible:ring-2 focus-visible:ring-ring"
        >
          {copied ? (
            <CheckCircle2 className="h-3.5 w-3.5 text-success" aria-hidden />
          ) : (
            <Copy className="h-3.5 w-3.5" aria-hidden />
          )}
          {copied ? "Copied" : "Copy"}
        </button>
        <a
          href={publication.share_url}
          target="_blank"
          rel="noopener noreferrer"
          aria-label={`Open public page for ${title}`}
          className="inline-flex h-8 cursor-pointer items-center gap-1.5 rounded-[var(--radius-sm)] px-2.5 text-xs font-medium text-foreground-muted transition-token hover:bg-surface-hover hover:text-foreground focus:outline-none focus-visible:ring-2 focus-visible:ring-ring"
        >
          <ExternalLink className="h-3.5 w-3.5" aria-hidden />
          Open
        </a>
        <PublicationMenu
          title={title}
          disabled={revoking}
          onRevoke={onRevoke}
        />
      </div>
    </li>
  );
}

function PublicationPolicySummary() {
  return (
    <div
      data-testid="publication-policy-summary"
      className="flex flex-wrap items-center gap-x-2 gap-y-1 border-b border-border bg-surface px-4 py-2.5 text-xs sm:px-5"
    >
      <span className="inline-flex items-center gap-1.5 font-semibold text-foreground">
        <Globe className="h-3.5 w-3.5 text-success" aria-hidden />
        Public
      </span>
      <span className="text-subtle" aria-hidden>
        ·
      </span>
      <span className="font-medium text-foreground">Read-only</span>
      <span className="text-subtle" aria-hidden>
        ·
      </span>
      <span className="font-medium text-foreground">No sign-in required</span>
      <span className="basis-full text-foreground-muted lg:ml-2 lg:basis-auto">
        Password, expiry, and view limits remain configurable for each link.
      </span>
    </div>
  );
}

function PublicationMenu({
  title,
  disabled,
  onRevoke,
}: {
  title: string;
  disabled: boolean;
  onRevoke: () => void;
}) {
  return (
    <DropdownMenu.Root>
      <DropdownMenu.Trigger asChild>
        <Button
          type="button"
          variant="ghost"
          size="icon"
          disabled={disabled}
          aria-label={`More actions for ${title}`}
        >
          {disabled ? (
            <Loader2 className="h-4 w-4 animate-spin" aria-hidden />
          ) : (
            <MoreHorizontal className="h-4 w-4" aria-hidden />
          )}
        </Button>
      </DropdownMenu.Trigger>
      <DropdownMenu.Portal>
        <DropdownMenu.Content
          align="end"
          sideOffset={6}
          className="z-[var(--z-popover)] min-w-40 overflow-hidden rounded-[var(--radius-md)] border border-border bg-surface p-1 shadow-md"
        >
          <DropdownMenu.Item
            onSelect={onRevoke}
            className="flex cursor-pointer select-none items-center gap-2 rounded-[var(--radius-sm)] px-3 py-2 text-sm text-destructive outline-none data-[highlighted]:bg-surface-hover"
          >
            <Trash2 className="h-4 w-4" aria-hidden />
            Unpublish
          </DropdownMenu.Item>
        </DropdownMenu.Content>
      </DropdownMenu.Portal>
    </DropdownMenu.Root>
  );
}

function PublicationControls({
  query,
  typeFilter,
  visibleCount,
  totalCount,
  onQueryChange,
  onTypeFilterChange,
}: {
  query: string;
  typeFilter: PublicationFilter;
  visibleCount: number;
  totalCount: number;
  onQueryChange: (value: string) => void;
  onTypeFilterChange: (value: PublicationFilter) => void;
}) {
  return (
    <div
      data-testid="publication-controls"
      className="flex flex-col gap-2 border-b border-border bg-surface px-4 py-3 sm:flex-row sm:items-center sm:px-5"
    >
      <label className="relative min-w-0 flex-1 sm:max-w-sm">
        <span className="sr-only">Filter published links</span>
        <Search
          className="pointer-events-none absolute left-2.5 top-1/2 h-3.5 w-3.5 -translate-y-1/2 text-foreground-muted"
          aria-hidden
        />
        <Input
          type="search"
          value={query}
          onChange={(event) => onQueryChange(event.target.value)}
          placeholder="Filter published links…"
          className="h-8 rounded-[var(--radius-sm)] pl-8 text-xs"
        />
      </label>
      <div
        role="group"
        aria-label="Filter published links by type"
        className="flex flex-wrap items-center gap-1"
      >
        {PUBLICATION_FILTERS.map((filter) => (
          <button
            key={filter.value}
            type="button"
            aria-pressed={typeFilter === filter.value}
            onClick={() => onTypeFilterChange(filter.value)}
            className="inline-flex h-8 cursor-pointer items-center rounded-[var(--radius-sm)] px-2.5 text-xs font-medium text-foreground-muted transition-token hover:bg-surface-hover hover:text-foreground focus:outline-none focus-visible:ring-2 focus-visible:ring-ring aria-pressed:bg-surface-selected aria-pressed:text-surface-selected-foreground"
          >
            {filter.label}
          </button>
        ))}
      </div>
      <span className="shrink-0 text-xs tabular-nums text-foreground-muted sm:pl-1">
        {visibleCount}/{totalCount}
      </span>
    </div>
  );
}

function PublicationNoResults({ onClear }: { onClear: () => void }) {
  return (
    <div className="flex min-h-48 items-center justify-center bg-surface px-6 py-10 text-center">
      <div className="max-w-sm">
        <Search className="mx-auto h-5 w-5 text-foreground-muted" aria-hidden />
        <h2 className="mt-3 text-sm font-semibold text-foreground">
          No matching public links
        </h2>
        <p className="mt-1 text-xs leading-relaxed text-foreground-muted">
          Try another title or show all resource types.
        </p>
        <Button variant="outline" size="sm" className="mt-4" onClick={onClear}>
          Clear filters
        </Button>
      </div>
    </div>
  );
}

function PublicationEmptyState({ vault }: { vault: string }) {
  return (
    <div className="flex min-h-80 items-center justify-center bg-surface px-6 py-12">
      <div className="max-w-md text-center">
        <TonalIcon
          tone="publish"
          size="lg"
          className="mx-auto h-12 w-12 rounded-[var(--radius-lg)] shadow-xs"
        >
          <Share2 className="h-5 w-5" aria-hidden />
        </TonalIcon>
        <h2 className="mt-4 text-base font-semibold text-foreground">
          Nothing is public
        </h2>
        <p className="mt-1.5 text-sm leading-relaxed text-foreground-muted">
          Documents, tables, and files stay private until you create a public
          link from their page.
        </p>
        <Button asChild variant="outline" size="sm" className="mt-5">
          <Link to={`/vault/${vault}`}>
            Browse vault content
            <ArrowRight className="h-3.5 w-3.5" aria-hidden />
          </Link>
        </Button>
        <p className="mt-4 text-xs text-foreground-muted">
          Open a resource and choose Publish to create a read-only /p/ link.
        </p>
      </div>
    </div>
  );
}

function PublicationError({
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
          Couldn&apos;t load published links
        </h2>
        <p className="mt-1.5 text-sm text-foreground-muted">{error}</p>
        <Button variant="outline" size="sm" className="mt-5" onClick={onRetry}>
          Retry
        </Button>
      </div>
    </div>
  );
}

function PublicationSkeleton() {
  return (
    <div role="status" aria-live="polite" aria-label="Loading published links">
      {[0, 1, 2].map((index) => (
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
          <Skeleton className="h-8 w-28 rounded-[var(--radius-md)]" />
        </div>
      ))}
      <span className="sr-only">Loading published links…</span>
    </div>
  );
}

function resourceHref(vault: string, publication: Publication): string {
  if (!publication.resource_uri) return `/vault/${vault}`;
  if (publication.resource_type === "document") {
    const docPath = parseDocUri(publication.resource_uri)?.id;
    return docPath
      ? `/vault/${vault}/doc/${encodeURIComponent(docPath)}`
      : `/vault/${vault}`;
  }
  if (publication.resource_type === "file") {
    const fileId = parseFileUri(publication.resource_uri)?.id;
    return fileId ? `/vault/${vault}/file/${fileId}` : `/vault/${vault}`;
  }
  return `/vault/${vault}`;
}

function publicationTitle(publication: Publication): string {
  return publication.title?.trim() || publication.slug;
}

function errorMessage(error: unknown, fallback: string): string {
  return error instanceof Error && error.message ? error.message : fallback;
}
