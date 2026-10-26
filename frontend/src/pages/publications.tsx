import {
  useCallback,
  useEffect,
  useMemo,
  useRef,
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
import { Alert } from "@/components/ui/alert";
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
  getVaultInfo,
  listPublications,
  type Publication,
} from "@/lib/api";
import { parseDocUri, parseFileUri } from "@/lib/uri";
import { formatDate } from "@/lib/utils";
import { publicationAvailability } from "@/lib/publication-state";
import {
  useAccessVerification,
  useCurrentUser,
} from "@/contexts/current-user-context";

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
  const user = useCurrentUser();
  const { checking, revision } = useAccessVerification();
  return (
    <PublicationsWorkspace
      key={JSON.stringify([user?.user_id, revision, name, checking])}
      name={name}
      checking={checking}
    />
  );
}

interface VaultPublicationInfo {
  role?: string;
  role_source?: string;
  is_archived?: boolean;
  is_external_git?: boolean;
  managed_by?: string | null;
}

function PublicationsWorkspace({
  name,
  checking,
}: {
  name?: string;
  checking: boolean;
}) {
  const active = useRef(true);
  const loadGeneration = useRef(0);
  const permissionGeneration = useRef(0);
  const [info, setInfo] = useState<VaultPublicationInfo | null>(null);
  const [permissionError, setPermissionError] = useState("");
  const [permissionsLoading, setPermissionsLoading] = useState(false);
  const [items, setItems] = useState<Publication[] | null>(null);
  const [error, setError] = useState("");
  const [refreshing, setRefreshing] = useState(false);
  const [copiedId, setCopiedId] = useState<string | null>(null);
  const [revokingId, setRevokingId] = useState<string | null>(null);
  const [pendingRevoke, setPendingRevoke] = useState<Publication | null>(null);
  const [query, setQuery] = useState("");
  const [typeFilter, setTypeFilter] = useState<PublicationFilter>("all");
  const [visibleLimit, setVisibleLimit] = useState(30);

  useEffect(() => {
    active.current = true;
    return () => {
      active.current = false;
      loadGeneration.current += 1;
      permissionGeneration.current += 1;
    };
  }, []);

  const load = useCallback(async (vault: string) => {
    if (checking || !active.current) return;
    const generation = ++loadGeneration.current;
    setRefreshing(true);
    setError("");
    try {
      const result = await listPublications(vault);
      if (!active.current || generation !== loadGeneration.current) return;
      const publications: Publication[] = result.publications || [];
      setItems(publications);
      setRefreshing(false);
      void Promise.all(
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
            if (active.current && generation === loadGeneration.current) {
              setItems(current => current?.map(item => item.slug === publication.slug ? { ...item, title: document.title || item.title } : item) ?? null);
            }
            return publication;
          } catch {
            return publication;
          }
        }),
      );

    } catch (caught: unknown) {
      if (!active.current || generation !== loadGeneration.current) return;
      setError(errorMessage(caught, "Failed to load public links"));
      if ([401, 403].includes((caught as { status?: number })?.status ?? 0)) setItems(null);
    } finally {
      if (active.current && generation === loadGeneration.current) setRefreshing(false);
    }
  }, [checking]);

  const loadPermissions = useCallback(async (vault: string) => {
    if (checking || !active.current) return;
    const generation = ++permissionGeneration.current;
    setInfo(null);
    setPermissionsLoading(true);
    try {
      const nextInfo = await getVaultInfo(vault);
      if (!active.current || generation !== permissionGeneration.current) return;
      if (!nextInfo) throw new Error("Vault permissions are unavailable.");
      setInfo(nextInfo);
      setPermissionError("");
    } catch (caught: unknown) {
      if (!active.current || generation !== permissionGeneration.current) return;
      setPermissionError(errorMessage(caught, "Failed to verify Vault permissions"));
    } finally {
      if (active.current && generation === permissionGeneration.current) {
        setPermissionsLoading(false);
      }
    }
  }, [checking]);

  useEffect(() => {
    if (!name) return;
    setItems(null);
    setError("");
    setQuery("");
    setTypeFilter("all");
    void load(name);
    void loadPermissions(name);
  }, [load, loadPermissions, name]);

  useEffect(() => {
    if (!name) return;
    const previous = document.title;
    document.title = `${name} · Public links · AKB`;
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

  const unpublishDisabledReason = !info
    ? "Unpublish is unavailable until Vault permissions can be verified."
    : info.is_archived
      ? `Vault '${name}' is archived (read-only)`
      : info.is_external_git
        ? `Vault '${name}' is a read-only external git mirror`
        : info.managed_by && info.role_source !== "system_admin"
          ? `Vault '${name}' is write-managed by ${info.managed_by}; writes require a granted service token with the required write action`
          : !["writer", "admin", "owner"].includes(info.role || "")
            ? "Unpublish requires a Writer, Admin, or Owner role."
            : undefined;

  async function confirmRevoke() {
    if (!name || !pendingRevoke || !active.current) return;
    if (unpublishDisabledReason) throw new Error(unpublishDisabledReason);
    setRevokingId(pendingRevoke.slug);
    try {
      await deletePublication(name, pendingRevoke.slug);
      await load(name);
    } finally {
      if (active.current) setRevokingId(null);
    }
  }

  async function copyLink(publication: Publication) {
    try {
      if (!navigator.clipboard) return;
      await navigator.clipboard.writeText(publication.share_url);
      if (!active.current) return;
      setCopiedId(publication.slug);
      setTimeout(() => {
        if (active.current) setCopiedId(null);
      }, 1500);
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
      <div data-testid="publication-workspace" className="workspace-density-compact @container/publications mx-auto w-full max-w-7xl">
        <h1 id="publications-heading" className="sr-only">
          Public links
        </h1>

        <WorkspaceSectionHeader
          id="published-links-heading"
          icon={Share2}
          title="Public links"
          tone="publish"
          testId="publication-ledger-header" compact
          right={
            <>
              {items !== null && (
                <Badge variant="default">
                  {count} link{count === 1 ? "" : "s"}
                </Badge>
              )}
              {(items === null || count > 0) && (
                <span className="text-xs tabular-nums text-foreground-muted">
                  {items === null
                    ? error
                      ? "Unavailable"
                      : "Loading links…"
                    : `${totalViews.toLocaleString()} total views`}
                </span>
              )}
              {count > 0 && (
                <Button asChild variant="ghost" size="sm" className="shrink-0">
                  <Link to={`/vault/${name}`} className="workspace-control">
                    Browse vault
                    <ArrowRight className="h-3.5 w-3.5" aria-hidden />
                  </Link>
                </Button>
              )}
            </>
          }
        />

        <Panel variant="workspace" className="w-full">
          <section aria-labelledby="published-links-heading" aria-busy={refreshing}>
            <PublicationPolicySummary />
            {items !== null && unpublishDisabledReason && (
              <p id="unpublish-restriction" className="border-b border-border px-4 py-2.5 text-xs text-foreground-muted sm:px-5">
                {unpublishDisabledReason}
              </p>
            )}
            {items !== null && permissionError && (
              <Alert variant="destructive" title="Couldn't verify Vault permissions" className="mx-4 my-3 sm:mx-5">
                {permissionError}
                <div className="mt-2">
                  <Button
                    variant="outline"
                    size="sm"
                    loading={permissionsLoading}
                    onClick={() => void loadPermissions(name)}
                  >
                    Retry permissions
                  </Button>
                </div>
              </Alert>
            )}

            {items !== null && filtersVisible && (
              <PublicationControls
                query={query}
                typeFilter={typeFilter}
                visibleCount={filteredItems.length}
                totalCount={count}
                onQueryChange={value => { setQuery(value); setVisibleLimit(30); }}
                onTypeFilterChange={value => { setTypeFilter(value); setVisibleLimit(30); }}
              />
            )}

            {refreshing && items !== null && <p role="status" className="px-4 py-3 text-sm text-foreground-muted">Updating public links…</p>}
            {error && items !== null && <Alert variant="destructive" className="m-4">Could not update public links. Previous links are shown. {error}<Button variant="outline" size="sm" loading={refreshing} onClick={() => void load(name)}>Retry</Button></Alert>}
            {error && items === null ? (
              <PublicationError error={error} loading={refreshing} onRetry={() => void load(name)} />
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
                aria-label="Public links"
                className="divide-y divide-border bg-surface"
              >
                {filteredItems.slice(0, visibleLimit).map((publication) => (
                  <PublicationRow
                    key={publication.slug}
                    publication={publication}
                    number={publicationNumbers.get(publication.slug) ?? 0}
                    vault={name}
                    copied={copiedId === publication.slug}
                    revoking={revokingId === publication.slug}
                    unpublishDisabledReason={unpublishDisabledReason}
                    onCopy={() => void copyLink(publication)}
                    onRevoke={() => setPendingRevoke(publication)}
                  />
                ))}
              </ol>
            )}
            {filteredItems.length > visibleLimit && <div className="border-t border-border p-3">
              <Button variant="outline" className="w-full" onClick={() => setVisibleLimit(limit => limit + 30)}>
                Show more links ({filteredItems.length - visibleLimit} remaining)
              </Button>
            </div>}
            {(count > 30 || filtersActive) && filteredItems.length > 0 && (
              <p className="border-t border-border bg-surface px-4 py-2 text-right text-xs tabular-nums text-foreground-muted sm:px-5">
                Showing {Math.min(visibleLimit, filteredItems.length)} of {filteredItems.length} {filtersActive ? "matching" : "loaded"} link{filteredItems.length === 1 ? "" : "s"}
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
  unpublishDisabledReason,
  onCopy,
  onRevoke,
}: {
  publication: Publication;
  number: number;
  vault: string;
  copied: boolean;
  revoking: boolean;
  unpublishDisabledReason?: string;
  onCopy: () => void;
  onRevoke: () => void;
}) {
  const Icon = RESOURCE_ICON[publication.resource_type];
  const title = publicationTitle(publication);
  const [expiryTick, setExpiryTick] = useState(0);
  useEffect(() => {
    if (!publication.expires_at) return;
    const expiry = Date.parse(publication.expires_at);
    const delay = expiry - Date.now();
    if (!Number.isFinite(expiry) || delay <= 0) return;
    const timer = setTimeout(() => setExpiryTick((tick) => tick + 1), Math.min(delay, 2_147_483_647));
    return () => clearTimeout(timer);
  }, [publication.expires_at, expiryTick]);
  const availability = publicationAvailability(publication);
  const availabilityLabel = {
    active: "Active",
    expired: "Expired",
    "view-limit-reached": "View limit reached",
    unknown: "Availability unknown",
  }[availability];
  const AvailabilityIcon = availability === "active"
    ? Globe
    : availability === "expired" ? Clock3 : AlertCircle;
  const views = publication.max_views
    ? `${publication.view_count ?? 0}/${publication.max_views} views`
    : `${publication.view_count ?? 0} views`;

  return (
    <li className="grid gap-2 px-4 py-3 transition-token hover:bg-surface-hover @lg/publications:px-5 @lg/publications:py-2.5 @min-[42rem]/publications:grid-cols-[minmax(0,1fr)_auto] @min-[42rem]/publications:items-center">
      <div className="flex min-w-0 items-start gap-2.5">
        <span
          data-testid="publication-index"
          aria-hidden
          className="w-5 shrink-0 pt-1.5 text-right text-xs tabular-nums text-subtle"
        >
          {number}
        </span>
        <TonalIcon tone={RESOURCE_TONE[publication.resource_type]} size="sm">
          <Icon className="h-4 w-4" aria-hidden />
        </TonalIcon>
        <div className="min-w-0 flex-1">
          <div className="flex min-w-0 flex-wrap items-center gap-2">
            <TooltipText asChild tip={title}>
              <Link
                to={resourceHref(vault, publication)}
                className="line-clamp-2 break-words rounded-[var(--radius-sm)] text-ui font-medium text-link hover:text-link-hover focus:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2 focus-visible:ring-offset-surface"
              >
                {title}
              </Link>
            </TooltipText>
            <Badge
              variant={availability === "active" ? "success" : availability === "unknown" ? "default" : "warning"}
            >
              <AvailabilityIcon className="h-3 w-3" aria-hidden />
              {availabilityLabel}
            </Badge>
          </div>

          <div className="mt-0.5 flex min-w-0 flex-wrap items-center gap-x-2 gap-y-1 text-xs leading-4 text-foreground-muted">
            <span>{RESOURCE_LABEL[publication.resource_type]}</span><span aria-hidden>·</span>
            <span>
              Delivery: {publication.mode === "snapshot" ? "Snapshot" : publication.mode === "live" ? "Live" : "Unknown"}
            </span>
            <span aria-hidden>·</span>
            <details className="min-w-0"><summary className="cursor-pointer rounded-[var(--radius-sm)] text-link focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring">Link address</summary>
              <span className="block break-all py-1 font-mono">/p/{publication.slug}</span>
            </details>
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
              {publication.expires_at && Number.isFinite(Date.parse(publication.expires_at))
                ? `Expires ${formatDate(publication.expires_at)}`
                : publication.expires_at === null ? "No expiry" : "Expiry unavailable"}
            </span>
          </div>
        </div>
      </div>

      <div className="ml-7 flex shrink-0 flex-wrap items-center justify-start gap-1 @min-[42rem]/publications:ml-0 @min-[42rem]/publications:justify-end">
        <button
          type="button"
          onClick={onCopy}
          aria-label={
            copied ? "Public link copied" : `Copy public link for ${title}`
          }
          className="workspace-control inline-flex h-8 cursor-pointer items-center gap-1.5 rounded-[var(--radius-sm)] px-2.5 text-xs font-medium text-foreground-muted transition-token hover:bg-surface-hover hover:text-foreground focus:outline-none focus-visible:ring-2 focus-visible:ring-ring"
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
          className="workspace-control inline-flex h-8 cursor-pointer items-center gap-1.5 rounded-[var(--radius-sm)] px-2.5 text-xs font-medium text-foreground-muted transition-token hover:bg-surface-hover hover:text-foreground focus:outline-none focus-visible:ring-2 focus-visible:ring-ring"
        >
          <ExternalLink className="h-3.5 w-3.5" aria-hidden />
          Open
        </a>
        <PublicationMenu
          title={title}
          disabled={revoking}
          disabledReason={unpublishDisabledReason}
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
      className="flex flex-wrap items-center gap-x-2 gap-y-1 border-b border-border bg-surface px-4 py-2 text-xs @lg/publications:px-5"
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
    </div>
  );
}

function PublicationMenu({
  title,
  disabled,
  disabledReason,
  onRevoke,
}: {
  title: string;
  disabled: boolean;
  disabledReason?: string;
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
          className="workspace-icon-control"
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
            disabled={Boolean(disabledReason)}
            title={disabledReason}
            aria-describedby={disabledReason ? "unpublish-restriction" : undefined}
            className="flex cursor-pointer select-none items-center gap-2 rounded-[var(--radius-sm)] px-3 py-2 text-sm text-destructive outline-none data-[highlighted]:bg-surface-hover data-[disabled]:cursor-not-allowed data-[disabled]:opacity-50"
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
      className="flex flex-wrap items-center gap-2 border-b border-border bg-surface px-4 py-3 @lg/publications:px-5 @lg/publications:py-2.5"
    >
      <label className="relative min-w-0 flex-1 basis-64">
        <span className="sr-only">Filter public links</span>
        <Search
          className="pointer-events-none absolute left-2.5 top-1/2 h-3.5 w-3.5 -translate-y-1/2 text-foreground-muted"
          aria-hidden
        />
        <Input
          type="search"
          value={query}
          onChange={(event) => onQueryChange(event.target.value)}
          placeholder="Filter public links…"
          className="h-8 rounded-[var(--radius-sm)] pl-8 text-xs"
        />
      </label>
      <div
        role="group"
        aria-label="Filter public links by type"
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
      <span className="ml-auto shrink-0 text-xs tabular-nums text-foreground-muted">
        {visibleCount}/{totalCount}
      </span>
    </div>
  );
}

function PublicationNoResults({ onClear }: { onClear: () => void }) {
  return (
    <div className="flex flex-wrap items-center justify-between gap-3 bg-surface px-4 py-4 @lg/publications:px-5">
      <div className="min-w-0">
        <h3 className="text-sm font-semibold text-foreground">
          No matching public links
        </h3>
        <p className="mt-1 text-xs leading-relaxed text-foreground-muted">
          Try another title or show all resource types.
        </p>
      </div>
      <Button variant="outline" size="sm" onClick={onClear}>
        Clear filters
      </Button>
    </div>
  );
}

function PublicationEmptyState({ vault }: { vault: string }) {
  return (
    <div data-testid="publication-empty-state" className="flex flex-wrap items-center justify-between gap-3 bg-surface px-4 py-4 @lg/publications:px-5">
      <div className="min-w-0 max-w-xl">
        <h3 className="text-sm font-semibold text-foreground">
          No public links yet
        </h3>
        <p className="mt-1 text-ui leading-relaxed text-foreground-muted">
          Open a document, table, or file and choose Publish to create a public link.
        </p>
      </div>
      <Button asChild variant="outline" size="sm" className="shrink-0">
          <Link to={`/vault/${vault}`} className="workspace-control">
          Browse vault content
          <ArrowRight className="h-3.5 w-3.5" aria-hidden />
        </Link>
      </Button>
    </div>
  );
}

function PublicationError({
  loading,
  error,
  onRetry,
}: {
  error: string;
  onRetry: () => void;
  loading: boolean;
}) {
  return (
    <div className="bg-surface p-4 sm:p-5">
      <Alert variant="destructive" title="Couldn't load public links">
        {error}
        <div className="mt-3">
          <Button variant="outline" size="sm" loading={loading} onClick={onRetry}>
            Retry
          </Button>
        </div>
      </Alert>
    </div>
  );
}

function PublicationSkeleton() {
  return (
    <div role="status" aria-live="polite" aria-label="Loading public links">
      {[0, 1, 2].map((index) => (
        <div
          key={index}
          className="flex items-center gap-3 border-b border-border px-4 py-3 @lg/publications:px-5 @lg/publications:py-2.5"
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
      <span className="sr-only">Loading public links…</span>
    </div>
  );
}

function resourceHref(vault: string, publication: Publication): string {
  if (publication.resource_type === "table_query") {
    // Table publications created by the bounded UI use one safe source table.
    // Older/raw SQL publications keep the Vault fallback if a single source
    // cannot be identified without pretending the query itself is a resource.
    const tableName = publication.query_sql?.match(/\bFROM\s+([a-z][a-z0-9_]*)\b/i)?.[1];
    return tableName
      ? `/vault/${vault}/table/${encodeURIComponent(tableName)}`
      : `/vault/${vault}`;
  }
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
