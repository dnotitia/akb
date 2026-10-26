import { Activity, useEffect, useRef, useState } from "react";
import { Link, useLocation, useNavigate, useParams } from "react-router-dom";
import { SettingsLink } from "@/contexts/settings-dialog-context";
import {
  AlertTriangle,
  BookText,
  Box,
  ChevronRight,
  ChevronDown,
  X,
  FileClock,
  Files,
  FilePlus,
  FileText,
  FolderInput,
  FolderTree,
  GitCommit,
  Globe,
  HelpCircle,
  LockKeyhole,
  Plug,
  Table as TableIcon,
  Upload,
  type LucideIcon,
} from "lucide-react";
import { useQuery } from "@tanstack/react-query";
import { useAccessVerification, useCurrentUser } from "@/contexts/current-user-context";
import { useResourceNavigation } from "@/contexts/resource-navigation-context";
import {
  getDocument,
  getRecent,
  getSkillTemplate,
  getVaultActivity,
  getVaultInfo,
  importKnowledgeBundle,
  type KnowledgeImportResult,
} from "@/lib/api";
import { LoadingState, InlineLoadingState } from "@/components/ui/loading-state";
import { Skeleton } from "@/components/ui/skeleton";
import { RelativeTime } from "@/components/ui/relative-time";
import { recentIcon, recentTone } from "@/lib/recent";
import { Alert } from "@/components/ui/alert";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Panel } from "@/components/ui/panel";
import { Popover, PopoverTrigger, PopoverContent, PopoverClose } from "@/components/ui/popover";
import { EmptyState } from "@/components/empty-state";
import { RoleBadge, VaultStateBadge } from "@/components/status-badge";
import { VaultProcessingDetails } from "@/components/vault-processing-details";
import { VAULT_SKILL_PATH, isReservedCollection } from "@/lib/skill";
import { TooltipText } from "@/components/ui/tooltip-text";
import { cn } from "@/lib/utils";
import { useVaultRefresh } from "@/contexts/vault-refresh-context";
import { useOpenDocumentCreateDialog } from "@/contexts/document-create-dialog-context";
import { useCollectionProof } from "@/hooks/use-vault-tree";
import { WorkspacePageHeader } from "@/components/ui/workspace-page-header";
import { VaultAddressAction } from "@/components/vault-address-action";
import { VaultCreateMenu } from "@/components/vault-create-menu";
import { VaultOverviewNavigation } from "@/components/vault-overview-navigation";
import { VaultCollectionsWorkspace } from "@/components/vault-collections-workspace";
import { documentPreviewState } from "@/lib/document-preview-navigation";
import { TonalIcon, type TonalIconTone } from "@/components/ui/tonal-icon";
import { FileUploadDialog } from "@/components/file-upload-dialog";
import { TableCreateDialog } from "@/components/table-create-dialog";
import { CreateCollectionDialog } from "@/components/create-collection-dialog";
import { parseFileUri } from "@/lib/uri";

interface VaultInfo {
  name: string;
  description?: string;
  role?: "owner" | "admin" | "writer" | "reader";
  is_archived?: boolean;
  is_external_git?: boolean;
  public_access?: "none" | "reader" | "writer";
  member_count?: number;
  owner?: string;
  owner_display_name?: string;
  created_at?: string;
  last_activity?: string;
  // Authoritative, depth-safe totals from GET /vaults/:name/info — the headline
  // counts read straight from these (no client-side browse re-derivation).
  collection_count?: number;
  document_count?: number;
  table_count?: number;
  file_count?: number;
  edge_count?: number;
}

interface RecentRow {
  doc_id: string;
  vault: string;
  path: string;
  title: string;
  type?: string;
  commit?: string;
  changed_at?: string;
}

interface ActivityRow {
  hash?: string;
  agent?: string;
  author?: string;
  /** Resolved human author name (the raw agent/author is the actor's UUID). */
  author_name?: string;
  subject?: string;
  summary?: string;
  date?: string;
  timestamp?: string;
  files?: Array<{ path: string; change?: string }>;
}

const fmt = (n: number) => n.toLocaleString();

// Older servers may omit optional totals. Absence must not become an empty
// Vault, an empty table, or an assertion about its access policy.
function knownCount(value?: number): value is number {
  return typeof value === "number" && Number.isFinite(value) && value >= 0;
}

function OverviewCount({ value }: { value?: number }) {
  return knownCount(value) ? (
    <span className="tabular-nums">{fmt(value)}</span>
  ) : (
    <span><span aria-hidden>—</span><span className="sr-only">Not available</span></span>
  );
}

/** Content inventory is passive text, separate from page navigation. */
function VaultContentSummary({ info }: { info: VaultInfo | null }) {
  const contents = [
    { label: "Documents", value: info?.document_count, Icon: FileText },
    { label: "Collections", value: info?.collection_count, Icon: FolderTree },
    { label: "Tables", value: info?.table_count, Icon: TableIcon },
    { label: "Files", value: info?.file_count, Icon: Files },
  ];
  return (
    <section aria-label="Contents" aria-busy={!info} className="min-w-0 max-w-full">
      <dl className="flex flex-wrap gap-2">
        {contents.map(({ label, value, Icon }) => (
          <div
            key={label}
            title={info && !knownCount(value) ? `${label} count is unavailable on this server` : undefined}
            className="inline-flex min-h-8 items-center gap-2 text-xs"
          >
            <dt className="inline-flex items-center gap-1.5 whitespace-nowrap text-foreground-muted">
              <Icon className="h-3.5 w-3.5 shrink-0" aria-hidden />
              {label}
            </dt>
            <dd className="font-semibold text-foreground">
              {info ? <OverviewCount value={value} /> : (
                <span className="block h-3 w-5 animate-pulse rounded bg-surface-muted" aria-hidden />
              )}
            </dd>
          </div>
        ))}
      </dl>
      {!info ? (
        <span className="sr-only">Loading content counts</span>
      ) : contents.some(({ value }) => !knownCount(value)) && (
        <p className="mt-1.5 text-xs text-foreground-muted">Some counts aren't available on this server.</p>
      )}
    </section>
  );
}

/** A git per-file change → a single-letter mark with a paired color (color is
 *  never the only signal: the letter carries the meaning, the title the word). */
function changeMark(change?: string) {
  if (!change) return null;
  const map: Record<string, { letter: string; cls: string }> = {
    added: { letter: "A", cls: "text-success" },
    modified: { letter: "M", cls: "text-warning" },
    deleted: { letter: "D", cls: "text-destructive" },
    renamed: { letter: "R", cls: "text-link" },
  };
  const m = map[change.toLowerCase()] ?? {
    letter: change.slice(0, 1).toUpperCase(),
    cls: "text-foreground-muted",
  };
  return (
    <span
      title={change}
      aria-label={change}
      className={`inline-flex h-4 w-4 items-center justify-center rounded-[var(--radius-sm)] text-[10px] font-semibold ${m.cls}`}
    >
      {m.letter}
    </span>
  );
}

export default function VaultPage() {
  const user = useCurrentUser();
  const { checking, revision } = useAccessVerification();
  const { name } = useParams<{ name: string }>();
  return <VaultWorkspace key={JSON.stringify([user?.user_id, name])} checking={checking} scope={JSON.stringify([user?.user_id, revision])} />;
}

function VaultWorkspace({ checking, scope }: { checking: boolean; scope: string }) {
  const location = useLocation();
  const requestedCollection = new URLSearchParams(location.search).get("collection") || "";
  const creationCollection = isReservedCollection(requestedCollection) ? "" : requestedCollection;
  const { name } = useParams<{ name: string }>();
  const navigate = useNavigate();
  const collectionProof = useCollectionProof(name || "", requestedCollection, !checking);
  const { refetchTree } = useVaultRefresh();
  const openCreateDocument = useOpenDocumentCreateDialog();
  const createButtonRef = useRef<HTMLButtonElement>(null);
  const [uploadOpen, setUploadOpen] = useState(false);
  const [tableCreateOpen, setTableCreateOpen] = useState(false);
  const [collectionCreateOpen, setCollectionCreateOpen] = useState(false);
  const [info, setInfo] = useState<VaultInfo | null>(null);
  const [infoError, setInfoError] = useState(false);
  const [loadedScope, setLoadedScope] = useState<string | null>(null);
  const latestScope = useRef(scope);
  const latestChecking = useRef(checking);
  latestScope.current = scope;
  latestChecking.current = checking;
  const [recent, setRecent] = useState<RecentRow[]>([]);
  const [recentLoading, setRecentLoading] = useState(true);
  const [recentLoaded, setRecentLoaded] = useState(false);
  const recentRequest = useRef(0);
  const [recentError, setRecentError] = useState(false);
  const [activity, setActivity] = useState<ActivityRow[]>([]);
  const [activityLoading, setActivityLoading] = useState(true);
  const [activityLoaded, setActivityLoaded] = useState(false);
  const activityRequest = useRef(0);
  const [activityError, setActivityError] = useState(false);
  const mounted = useRef(true);
  useEffect(() => { mounted.current = true; return () => { mounted.current = false; }; }, []);

  const skillQuery = useQuery({
    queryKey: ["document", name, VAULT_SKILL_PATH, scope],
    queryFn: () => getDocument(name!, VAULT_SKILL_PATH),
    retry: false,
    enabled: !checking && !!name && !!info && !info.is_external_git,
  });
  const skillMissing = skillQuery.isError &&
    typeof skillQuery.error === "object" && skillQuery.error !== null &&
    "status" in skillQuery.error && skillQuery.error.status === 404;
  const skillError = skillQuery.isError && !skillMissing
    ? skillQuery.error.message || "Couldn't load the vault guide."
    : undefined;
  const skillExists = !skillMissing && !!skillQuery.data;

  // Every normal vault carries the guide, so "defined?" says nothing — the chip
  // reports whether anyone has written it yet. The seed body IS the template
  // with {vault} substituted, so compare against the template endpoint (one
  // fetch for the whole session: the seed is a build-time constant).
  const templateQuery = useQuery({
    queryKey: ["skill-template"],
    queryFn: getSkillTemplate,
    staleTime: Infinity,
    enabled: skillExists,
  });
  // Trim both sides: the stored body comes back parsed out of the .md file and
  // python-frontmatter strips surrounding whitespace on load, so an untouched
  // guide is the template minus its trailing newline. `undefined` until the
  // template resolves — the chip renders no state rather than a wrong one.
  const skillCustomized =
    skillExists && templateQuery.data != null
      ? (skillQuery.data!.content || "").trim() ===
          templateQuery.data.replaceAll("{vault}", name!).trim() ||
        (skillQuery.data!.created_at != null &&
          skillQuery.data!.updated_at != null &&
          Date.parse(skillQuery.data!.created_at) ===
            Date.parse(skillQuery.data!.updated_at))
        ? false
        : true
      : undefined;

  function loadInfo(vault: string, alive: () => boolean = () => mounted.current) {
    const requestScope = scope;
    const current = () => alive() && requestScope === latestScope.current && !latestChecking.current;
    getVaultInfo(vault)
      .then((d) => { if (current()) { setInfo(d); setLoadedScope(requestScope); } })
      .catch(() => { if (current()) { setInfoError(true); setLoadedScope(requestScope); } });
  }

  async function loadRecent(vault: string, alive: () => boolean = () => mounted.current) {
    const id = ++recentRequest.current;
    const requestScope = scope;
    const current = () => id === recentRequest.current && alive() && requestScope === latestScope.current && !latestChecking.current;
    setRecentLoading(true);
    setRecentError(false);
    try {
      const d = await getRecent(vault, 12);
      if (!current()) return;
      setRecent(d.changes || []);
      setRecentLoaded(true);
    } catch (caught) {
      if (!current()) return;
      if (typeof caught === "object" && caught !== null && "status" in caught && (caught.status === 401 || caught.status === 403)) { setRecent([]); setRecentLoaded(false); }
      setRecentError(true);
    } finally {
      if (current()) setRecentLoading(false);
    }
  }

  async function loadActivity(
    vault: string,
    alive: () => boolean = () => mounted.current,
  ) {
    const id = ++activityRequest.current;
    const requestScope = scope;
    const current = () => id === activityRequest.current && alive() && requestScope === latestScope.current && !latestChecking.current;
    setActivityLoading(true);
    setActivityError(false);
    try {
      const result = await getVaultActivity(vault, { limit: 10 });
      if (!current()) return;
      setActivity(result.activity || []);
      setActivityLoaded(true);
    } catch (caught) {
      if (!current()) return;
      if (typeof caught === "object" && caught !== null && "status" in caught && (caught.status === 401 || caught.status === 403)) { setActivity([]); setActivityLoaded(false); }
      setActivityError(true);
    } finally {
      if (current()) setActivityLoading(false);
    }
  }

  useEffect(() => {
    if (!name || checking) return;
    let alive = true;
    const isAlive = () => alive;
    // Reset stale state from the previous param before the re-fetch resolves;
    // the `alive` guard keeps a fast vault switch from clobbering the newer one.
    setInfo(null);
    setInfoError(false);
    setRecent([]);
    setRecentLoaded(false);
    setRecentError(false);
    setRecentLoading(true);
    setActivity([]);
    setActivityLoaded(false);
    setActivityError(false);
    setActivityLoading(true);
    loadInfo(name, isAlive);
    loadRecent(name, isAlive);
    loadActivity(name, isAlive);
    return () => {
      alive = false;
    };
    // Account/vault keys own the draft dialogs. Revisions only reset reads.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [name, checking, scope]);

  // Name the browser tab/history entry for this vault (helps tab switching and
  // screen-reader route-change orientation); restore the app default on leave.
  useEffect(() => {
    if (!name) return;
    const prev = document.title;
    document.title = `${name} · AKB`;
    return () => {
      document.title = prev;
    };
  }, [name]);

  const canWrite =
    info?.role === "writer" || info?.role === "admin" || info?.role === "owner";
  const canCreateContent =
    canWrite && !info?.is_archived && !info?.is_external_git
    && (!requestedCollection || collectionProof.verified);

  // "Just getting started" = no real content yet. A freshly created vault is
  // auto-seeded with an overview/vault-skill.md scaffold, so that one doc
  // doesn't count as content; gate on the skill probe having settled so the
  // layout doesn't flip once it resolves. Show an onboarding hero instead of a
  // barren 1/0/0 stat wall + a lone scaffold commit.
  const scaffoldDocs = skillExists ? 1 : 0;
  const isEmpty =
    !!info &&
    !skillQuery.isLoading &&
    !skillError &&
    knownCount(info.document_count) &&
    info.document_count - scaffoldDocs <= 0 &&
    info.table_count === 0 &&
    info.file_count === 0;

  return (
    <>
    {(checking || loadedScope !== scope) && <OverviewLoadingState checking={checking} />}
    <Activity mode={!checking && loadedScope === scope ? "visible" : "hidden"}>
    <div
      role="region"
      aria-label={`${name} Vault overview`}
      className="@container/vault-overview mx-auto flex min-h-full w-full max-w-7xl flex-col gap-5 bg-background"
    >
      {infoError && (
        <Alert variant="destructive">
          Could not load this vault's details. Some information may be missing.
          <div className="mt-2">
            <Button
              variant="outline"
              size="sm"
              className="h-11 sm:[@media(pointer:fine)]:h-8"
              onClick={() => {
                setInfoError(false);
                setInfo(null);
                if (name) loadInfo(name);
              }}
            >
              Try again
            </Button>
          </div>
        </Alert>
      )}

      <section aria-label="Vault summary" className="flex min-w-0 flex-col gap-2 border-b border-border pb-3">
        <WorkspacePageHeader
          icon={Box}
          iconTone="knowledge"
          title={name}
          context={
            info?.description ? (
              <span className="block break-words [overflow-wrap:anywhere]">{info.description}</span>
            ) : undefined
          }
          meta={
            <>
              <VaultAddressAction name={name!} />
              {info?.role && <RoleBadge role={info.role} />}
              <VaultStateBadge
                archived={info?.is_archived}
                externalGit={info?.is_external_git}
              />
              <VaultProcessingDetails vaultName={name!} />
            </>
          }
          actions={canCreateContent ? <div role="group" aria-label="Create content">
            <VaultCreateMenu triggerRef={createButtonRef} onDocument={() => openCreateDocument({ collection: creationCollection })}
              onTable={() => setTableCreateOpen(true)} onUpload={() => setUploadOpen(true)} onCollection={() => setCollectionCreateOpen(true)} />
          </div> : undefined}
          className="min-h-0 flex-nowrap items-start rounded-none border-0 bg-transparent p-0 shadow-none [&>div:first-child]:flex-1 [&>div:first-child]:max-w-full [&>div:first-child]:gap-2.5 [&>div:first-child>span]:h-7 [&>div:first-child>span]:w-7 [&_h1]:text-sm [&_h1]:whitespace-normal [&_h1]:break-words [&_h1]:[overflow-wrap:anywhere]"
        />
        {info && <VaultOverviewMetadata name={name!} info={info} guideDefined={skillExists}
          guideCustomized={skillCustomized} guideLoading={skillQuery.isLoading}
          guideError={skillError} guideRetrying={skillQuery.isFetching}
          onGuideRetry={() => { void skillQuery.refetch(); }} />}
        <div data-slot="vault-summary-links" className="flex min-w-0 flex-wrap items-center justify-between gap-x-5 gap-y-1">
          {!infoError && <VaultContentSummary info={info} />}
          {name && <VaultOverviewNavigation vault={name} memberCount={info?.member_count} />}
        </div>
      </section>

      {info?.is_archived && (
        <Alert variant="info">
          This vault is archived — content is read-only. Existing documents
          stay browsable; new writes are disabled.
        </Alert>
      )}

      <div className={cn("grid min-w-0 items-start gap-6", !isEmpty && "@[56rem]/vault-overview:grid-cols-[minmax(0,1fr)_16rem]")}>
        <div className="flex min-w-0 flex-col gap-5">
          {info && isEmpty ? (
            <VaultEmptyOnboarding
              name={name!}
              canWrite={canCreateContent}
              skillCustomized={skillCustomized}
              isMirror={!!info.is_external_git}
              onCreateDocument={() => openCreateDocument()}
              onUploadFile={() => setUploadOpen(true)}
              onCreateTable={() => setTableCreateOpen(true)}
              onImported={() => {
                refetchTree();
                loadInfo(name!);
                void loadRecent(name!);
                void loadActivity(name!);
              }}
            />
          ) : null}
          <VaultCollectionsWorkspace key={scope} vault={name!} enabled={!checking && loadedScope === scope} />
        </div>

        {info ? (
          !isEmpty && <aside className="flex min-w-0 flex-col gap-4" aria-label="Vault overview details">
            <RecentActivityPanel name={name!} rows={recent} loading={recentLoading} loaded={recentLoaded} error={recentError} onRetry={() => name && loadRecent(name)} />
            <CommitHistoryPanel name={name!} rows={activity} loading={activityLoading} loaded={activityLoaded} error={activityError} onRetry={() => name && loadActivity(name)} />
          </aside>
        ) : infoError ? (
          <aside aria-label="Vault overview details" className="flex min-w-0 flex-col gap-4">
            <RecentActivityPanel name={name!} rows={recent} loading={recentLoading} loaded={recentLoaded} error={recentError} onRetry={() => name && loadRecent(name)} />
            <CommitHistoryPanel name={name!} rows={activity} loading={activityLoading} loaded={activityLoaded} error={activityError} onRetry={() => name && loadActivity(name)} />
            <Panel variant="workspace" className="p-4 text-ui text-foreground-muted">
              Vault details are unavailable.
            </Panel>
          </aside>
        ) : (
          <VaultContextSkeleton />
        )}
      </div>

    </div>
    </Activity>

      {name && (
        <>
          <CreateCollectionDialog
            vault={name}
            open={collectionCreateOpen}
            visible={!checking && loadedScope === scope && canCreateContent}
            onOpenChange={setCollectionCreateOpen}
            initialPath={creationCollection || undefined}
            returnFocusRef={createButtonRef}
            onCreated={() => {
              if (!mounted.current || scope !== latestScope.current || latestChecking.current) return;
              refetchTree();
              loadInfo(name);
            }}
          />
          <FileUploadDialog
            open={uploadOpen}
            visible={!checking && loadedScope === scope && canCreateContent}
            onOpenChange={setUploadOpen}
            vault={name}
            initialCollection={creationCollection}
            returnFocusRef={createButtonRef}
            onUploaded={(file) => {
              if (!mounted.current || scope !== latestScope.current || latestChecking.current) return;
              refetchTree();
              loadInfo(name);
              void loadRecent(name);
              const parsed = parseFileUri(file.uri);
              if (parsed)
                navigate(
                  `/vault/${name}/file/${encodeURIComponent(parsed.id)}`,
                );
            }}
          />
          <TableCreateDialog
            open={tableCreateOpen}
            visible={!checking && loadedScope === scope && canCreateContent}
            onOpenChange={setTableCreateOpen}
            vault={name}
            initialCollection={creationCollection}
            returnFocusRef={createButtonRef}
            onCreated={(tableName) => {
              if (!mounted.current || scope !== latestScope.current || latestChecking.current) return;
              refetchTree();
              loadInfo(name);
              void loadRecent(name);
              navigate(`/vault/${name}/table/${encodeURIComponent(tableName)}`);
            }}
          />
        </>
      )}
    </>
  );
}

function RecentActivityPanel({
  name,
  rows,
  loading,
  loaded,
  error,
  onRetry,
}: {
  name: string;
  rows: RecentRow[];
  loading: boolean;
  loaded: boolean;
  error: boolean;
  onRetry: () => void;
}) {
  const [all, setAll] = useState(false);
  return (
    <Panel
      variant="workspace"
      flush
      role="region"
      aria-labelledby="recent-heading"
      aria-busy={loading}
      className="@container/recent min-w-0 rounded-[var(--radius-sm)]"
    >
      <div className="flex min-h-10 flex-wrap items-center gap-2 border-b border-border bg-background px-3 py-1.5">
        <FileClock className="h-4 w-4 shrink-0 text-foreground-muted" aria-hidden />
        <div className="flex min-w-0 flex-1 flex-wrap items-center gap-2">
          <h2
            id="recent-heading"
            className="text-sm font-semibold text-foreground"
          >
            Recent activity
          </h2>
          {!loading && !error && (
            <span className="text-xs tabular-nums text-foreground-muted">
              {rows.length} change{rows.length === 1 ? "" : "s"}
            </span>
          )}
        </div>
      </div>

      <span className="sr-only" role="status" aria-live="polite">
        {loading
          ? "Loading recent activity"
          : error
            ? "Could not load recent activity"
            : `${rows.length} recent change${rows.length === 1 ? "" : "s"}`}
      </span>

      {loaded && loading && <InlineLoadingState label="Updating recent activity; previous changes shown" className="px-4 py-2" />}
      {loaded && error && <Alert variant="destructive" className="m-3">Recent activity could not be updated; previous changes shown.<Button variant="outline" size="sm" className="ml-2 max-sm:min-h-11 pointer-coarse:min-h-11" loading={loading} onClick={onRetry}>Retry</Button></Alert>}
      {loading && !loaded ? (
        <ul className="divide-y divide-border" aria-hidden>
          {Array.from({ length: 4 }).map((_, index) => (
            <li
              key={index}
              className="flex items-center gap-2.5 px-4 py-2.5"
            >
              <span className="h-7 w-7 rounded-[var(--radius-sm)] bg-surface-muted" />
              <span className="h-3 flex-1 rounded bg-surface-muted" />
              <span className="h-2.5 w-14 rounded bg-surface-muted" />
            </li>
          ))}
        </ul>
      ) : error && !loaded ? (
        <div className="p-4">
          <EmptyState
            className="px-5 py-10 [&>p]:text-ui [&>p:first-of-type]:text-sm"
            icon={<AlertTriangle className="h-6 w-6" aria-hidden />}
            title="Couldn't load recent activity"
            description="Something went wrong fetching this vault's latest changes."
            action={
              <Button variant="outline" size="sm" className="h-11 sm:[@media(pointer:fine)]:h-8" onClick={onRetry}>
                Retry
              </Button>
            }
          />
        </div>
      ) : rows.length === 0 ? (
        <div className="p-4">
          <EmptyState
            className="px-5 py-10 [&>p]:text-ui [&>p:first-of-type]:text-sm"
            icon={<FileClock className="h-6 w-6" aria-hidden />}
            title="Nothing written yet"
            description="Document writes in this vault will appear here."
          />
        </div>
      ) : (
        <ol className="divide-y divide-border bg-surface">
          {(all ? rows : rows.slice(0, 4)).map((row, index) => {
            const Icon = recentIcon(row.type);
            const tone = recentTone(row.type);
            const collection = row.path?.split("/").slice(0, -1).join(" / ") || "Vault root";
            return (
              <li key={`${row.doc_id}:${row.commit ?? ""}:${index}`}>
                <Link
                  to={`/vault/${encodeURIComponent(name)}/doc/${encodeURIComponent(row.path || row.doc_id)}`}
                  className="group grid min-h-12 grid-cols-[1rem_minmax(0,1fr)] items-start gap-x-2 px-3 py-2 transition-token hover:bg-surface-hover focus:outline-none focus-visible:ring-2 focus-visible:ring-inset focus-visible:ring-ring"
                >
                  <span
                    className="inline-flex items-center justify-center"
                    style={{ color: tone }}
                    aria-hidden
                  >
                    <Icon className="h-4 w-4" aria-hidden />
                  </span>
                  <span className="min-w-0">
                    <span
                      className="line-clamp-2 break-words text-ui font-medium text-foreground transition-colors group-hover:text-link [overflow-wrap:anywhere]"
                      title={row.title}
                    >
                      {row.title}
                    </span>
                    <span className="block truncate text-xs leading-4 text-foreground-muted" title={collection}>
                      {collection}
                    </span>
                    <span className="mt-0.5 block">
                    <span className="sr-only">Updated </span>
                    <RelativeTime iso={row.changed_at} fallback="—" className="whitespace-nowrap text-xs" />
                    </span>
                  </span>
                </Link>
              </li>
            );
          })}
        </ol>
      )}
      {rows.length > 4 && <Button variant="ghost" size="sm" className="h-9 w-full rounded-t-none text-xs text-link max-sm:h-11 pointer-coarse:h-11" onClick={() => setAll(value => !value)}>{all ? "Show fewer updates" : `Show ${rows.length - 4} more updates`}</Button>}
    </Panel>
  );
}

function CommitHistoryPanel({
  name,
  rows,
  loading,
  loaded,
  error,
  onRetry,
}: {
  name: string;
  rows: ActivityRow[];
  loading: boolean;
  loaded: boolean;
  error: boolean;
  onRetry: () => void;
}) {
  const [expanded, setExpanded] = useState(false);
  const { requestNavigation } = useResourceNavigation();
  const activityHref = `/vault/${encodeURIComponent(name)}/activity`;
  const expandable = loading || error || rows.length > 0;

  return (
    <Panel
      variant="workspace"
      flush
      role="region"
      aria-labelledby="commit-history-heading"
      aria-busy={loading}
      className="w-full min-w-0 rounded-none border-0 border-t bg-transparent"
    >
      <div className="flex min-h-11 flex-wrap items-center gap-2 py-1.5">
        <div className="flex flex-1 items-center gap-2">
          <GitCommit className="h-4 w-4 shrink-0 text-foreground-muted" aria-hidden />
          <h2
            id="commit-history-heading"
            className="whitespace-nowrap text-sm font-semibold text-foreground"
          >
            Commit history
          </h2>
          <span className="whitespace-nowrap text-xs tabular-nums text-foreground-muted">
            {loading
              ? "Loading commits…"
              : error
                ? "Unavailable"
                : rows.length > 0
                  ? `${fmt(rows.length)} recent`
                  : "No commits yet"}
          </span>
        </div>
        <div className="flex flex-wrap items-center gap-2">
          {expandable && (
            <button
              type="button"
              aria-expanded={expanded}
              aria-controls="commit-history-list"
              onClick={() => setExpanded((current) => !current)}
              className="inline-flex min-h-11 cursor-pointer items-center sm:[@media(pointer:fine)]:min-h-8 rounded-[var(--radius-sm)] px-2 text-xs font-medium text-foreground-muted transition-token hover:bg-surface-hover hover:text-foreground focus:outline-none focus-visible:ring-2 focus-visible:ring-ring"
            >
              {expanded ? "Hide commits" : "Show commits"}
              <ChevronRight
                className={cn(
                  "ml-1 h-3.5 w-3.5 transition-transform",
                  expanded && "rotate-90",
                )}
                aria-hidden
              />
            </button>
          )}
          <Link
            to={activityHref}
            state={{ vaultPageNavigation: activityHref }}
            onClick={event => {
              if (event.defaultPrevented || event.button !== 0 || event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return;
              if (!requestNavigation(activityHref, { state: { vaultPageNavigation: activityHref } })) event.preventDefault();
            }}
            className="inline-flex min-h-11 items-center sm:[@media(pointer:fine)]:min-h-8 rounded-[var(--radius-sm)] px-2 text-xs font-medium text-link transition-token hover:bg-surface-hover hover:text-link-hover focus:outline-none focus-visible:ring-2 focus-visible:ring-ring"
          >
            Full commit log
            <ChevronRight className="ml-1 h-3.5 w-3.5" aria-hidden />
          </Link>
        </div>
      </div>

      {loaded && loading && <InlineLoadingState label="Updating commit history; previous commits shown" className="py-2" />}
      {loaded && error && <Alert variant="destructive" className="my-2">Commit history could not be updated; previous commits shown.<Button variant="outline" size="sm" className="ml-2 max-sm:min-h-11 pointer-coarse:min-h-11" loading={loading} onClick={onRetry}>Retry</Button></Alert>}
      {expanded && (
        <div id="commit-history-list">
          {loading && !loaded ? (
            <ul className="divide-y divide-border" aria-hidden>
              {Array.from({ length: 5 }).map((_, index) => (
                <li
                  key={index}
                  className="flex items-center gap-2.5 px-4 py-2.5"
                >
                  <span className="h-3 w-14 rounded bg-surface-muted" />
                  <span className="h-3 w-24 rounded bg-surface-muted" />
                  <span className="h-3 flex-1 rounded bg-surface-muted" />
                </li>
              ))}
            </ul>
          ) : error && !loaded ? (
            <div className="p-4">
              <EmptyState
                className="px-5 py-10 [&>p]:text-ui [&>p:first-of-type]:text-sm"
                icon={<AlertTriangle className="h-6 w-6" aria-hidden />}
                title="Couldn't load commit history"
                description="The latest commits are temporarily unavailable."
                action={
                  <Button variant="outline" size="sm" className="h-11 sm:[@media(pointer:fine)]:h-8" onClick={onRetry}>
                    Retry
                  </Button>
                }
              />
            </div>
          ) : rows.length === 0 ? (
            <p className="px-4 py-5 text-ui text-foreground-muted">
              No commits have landed in this vault yet.
            </p>
          ) : (
            <ol className="divide-y divide-border">
              {rows.map((row, index) => {
                const primaryPath = row.files?.[0]?.path;
                const filesCount = row.files?.length || 0;
                const author =
                  row.author_name || row.agent || row.author || "unknown";
                const link = primaryPath
                  ? `/vault/${name}/doc/${encodeURIComponent(primaryPath)}` +
                    (row.hash ? `?commit=${encodeURIComponent(row.hash)}` : "")
                  : `/vault/${name}`;
                return (
                  <li key={`${row.hash ?? "commit"}:${index}`}>
                    <Link
                      to={link}
                      className="group grid min-h-11 grid-cols-[62px_minmax(0,1fr)_auto] items-center gap-2.5 px-4 py-2 transition-token hover:bg-surface-hover focus:outline-none focus-visible:ring-2 focus-visible:ring-inset focus-visible:ring-ring sm:grid-cols-[62px_120px_minmax(0,1fr)_auto]"
                    >
                      <span className="font-mono text-xs tabular-nums text-foreground-muted">
                        {(row.hash || "").slice(0, 7)}
                      </span>
                      <TooltipText
                        tip={author}
                        className="hidden truncate text-xs text-foreground sm:block"
                      >
                        {author}
                      </TooltipText>
                      <span className="min-w-0">
                        <span className="flex min-w-0 items-center gap-2">
                          {changeMark(row.files?.[0]?.change)}
                          <TooltipText
                            tip={
                              row.subject ||
                              primaryPath ||
                              row.summary ||
                              "Commit"
                            }
                            className="truncate text-ui text-foreground transition-colors group-hover:text-link"
                          >
                            {row.subject ||
                              primaryPath ||
                              row.summary ||
                              "Commit"}
                          </TooltipText>
                        </span>
                        {primaryPath && (
                          <span className="coord block truncate pl-6">
                            {primaryPath}
                            {filesCount > 1 && ` · +${filesCount - 1} files`}
                          </span>
                        )}
                      </span>
                      <RelativeTime
                        iso={row.timestamp || row.date}
                        className="w-[60px] justify-end text-right"
                      />
                    </Link>
                  </li>
                );
              })}
            </ol>
          )}
        </div>
      )}
    </Panel>
  );
}

/** Stable Vault facts belong beside identity; recent changes stay complementary. */
function VaultOverviewMetadata({ name, info, guideDefined, guideCustomized, guideLoading, guideError, guideRetrying, onGuideRetry }: {
  name: string;
  info: VaultInfo;
  guideDefined: boolean;
  guideCustomized?: boolean;
  guideLoading: boolean;
  guideError?: string;
  guideRetrying: boolean;
  onGuideRetry: () => void;
}) {
  const location = useLocation();
  const guideStatus = guideCustomized === undefined ? "Guide ready" : guideCustomized ? "Guide customized" : "Starter template";
  const canSetUp = !info.is_archived && !info.is_external_git && ["owner", "admin", "writer"].includes(info.role || "");
  const publicAccess = info.public_access === "writer" ? "Public write" : info.public_access === "reader" ? "Public read" : info.public_access === "none" ? "Private" : "Access unavailable";
  const policy = info.public_access === "writer"
    ? info.is_archived || info.is_external_git ? "Any signed-in person can read this vault. Content is read-only." : "Signed-in users can read, and write when vault policies allow."
    : info.public_access === "reader" ? "Any signed-in person can read this vault."
    : info.public_access === "none" ? "Only people with access can open this vault." : "Visibility information is unavailable.";
  const VisibilityIcon = info.public_access === "none" ? LockKeyhole : info.public_access === "reader" || info.public_access === "writer" ? Globe : HelpCircle;
  const metaLink = "inline-flex min-h-9 min-w-0 items-center gap-1.5 rounded-[var(--radius-sm)] text-ui text-link hover:text-link-hover focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring max-sm:min-h-11 pointer-coarse:min-h-11";
  return <div data-slot="vault-overview-metadata" className="flex min-w-0 flex-wrap items-center gap-x-4 gap-y-0 text-ui text-foreground-muted">
    <section aria-label="Access and ownership" className="flex min-w-0 flex-wrap items-center gap-x-4">
      <Popover>
        <PopoverTrigger asChild>
          <button type="button" aria-label={`Vault access: ${publicAccess}`} className="inline-flex min-h-9 shrink-0 items-center gap-1.5 rounded-[var(--radius-sm)] text-ui text-foreground-muted hover:text-link focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring max-sm:min-h-11 pointer-coarse:min-h-11">
            <VisibilityIcon className="h-3.5 w-3.5" aria-hidden />{publicAccess}<ChevronDown className="h-3 w-3" aria-hidden />
          </button>
        </PopoverTrigger>
        <PopoverContent align="start" aria-label="Vault access" className="p-3 text-ui">
          <div className="flex items-center justify-between gap-3"><h2 className="text-sm font-semibold">Vault access</h2><PopoverClose asChild><Button variant="ghost" size="icon" className="h-9 w-9 max-sm:h-11 max-sm:w-11 pointer-coarse:h-11 pointer-coarse:w-11" aria-label="Close vault access"><X className="h-4 w-4" aria-hidden /></Button></PopoverClose></div>
          <p className="mt-1 leading-5">{policy}</p>
          <p className="mt-2 text-xs leading-5 text-foreground-muted">Public links are managed separately and may allow access without signing in.</p>
        </PopoverContent>
      </Popover>
      <p className="min-w-0 break-words py-1.5 [overflow-wrap:anywhere]"><span>Owner</span><span className="ml-1.5 text-foreground">{info.owner_display_name || info.owner || "Not available"}</span></p>
    </section>
    {!info.is_external_git && <section aria-label="Vault guide" className="min-w-0">
      {guideLoading ? <InlineLoadingState label="Checking vault guide" className="py-1 text-xs" />
        : guideError ? <button type="button" className={metaLink} onClick={onGuideRetry} disabled={guideRetrying} aria-busy={guideRetrying} aria-label="Retry vault guide"><BookText className="h-3.5 w-3.5" aria-hidden />{guideRetrying ? "Checking guide…" : "Guide unavailable · Retry"}</button>
        : guideDefined ? <><span id="vault-guide-status" className="sr-only">{guideStatus}</span><Link id="vault-overview-guide" to={`/vault/${encodeURIComponent(name)}/doc/${encodeURIComponent(VAULT_SKILL_PATH)}`} state={documentPreviewState(location, "vault-overview-guide")} aria-label="Open vault guide" aria-describedby="vault-guide-status" className={metaLink}><BookText className="h-3.5 w-3.5" aria-hidden />Vault guide<ChevronRight className="h-3 w-3" aria-hidden /></Link></>
        : canSetUp ? <Link id="vault-overview-guide" to={`/vault/${encodeURIComponent(name)}/settings#skill`} aria-label="Set up vault guide" className={metaLink}><BookText className="h-3.5 w-3.5" aria-hidden />Set up guide<ChevronRight className="h-3 w-3" aria-hidden /></Link>
        : <span className="inline-flex min-h-9 items-center text-xs max-sm:min-h-11 pointer-coarse:min-h-11">Guide not configured</span>}
    </section>}
  </div>;
}

function VaultContextSkeleton() {
  return (
    <aside className="flex min-w-0 flex-col gap-4" aria-label="Loading vault details" aria-busy="true">
      <Panel variant="workspace" flush className="rounded-[var(--radius-sm)]" aria-hidden>
        <div className="flex h-10 items-center gap-2 border-b border-border bg-background px-3">
          <Skeleton className="h-4 w-4" /><Skeleton className="h-4 w-28" />
        </div>
        <div className="divide-y divide-border">
          {Array.from({ length: 4 }, (_, index) => <div key={index} className="space-y-2 p-3">
            <Skeleton className="h-3 w-full" /><Skeleton className="h-3 w-3/4" /><Skeleton className="h-3 w-12" />
          </div>)}
        </div>
      </Panel>
      <Skeleton className="h-16 w-full" />
    </aside>
  );
}

/** First-run guidance precedes the primary collection directory. Adding the
 *  first resource changes state, not the page grammar. */
function VaultEmptyOnboarding({
  name,
  canWrite,
  skillCustomized,
  isMirror,
  onCreateDocument,
  onUploadFile,
  onCreateTable,
  onImported,
}: {
  name: string;
  canWrite: boolean;
  skillCustomized?: boolean;
  isMirror: boolean;
  onCreateDocument: () => void;
  onUploadFile: () => void;
  onCreateTable: () => void;
  onImported: () => void;
}) {
  const importInputRef = useRef<HTMLInputElement | null>(null);
  const [importing, setImporting] = useState(false);
  const [importResult, setImportResult] =
    useState<KnowledgeImportResult | null>(null);
  const [importError, setImportError] = useState("");

  async function importBundle(file: File) {
    setImporting(true);
    setImportResult(null);
    setImportError("");
    try {
      const result = await importKnowledgeBundle(name, file);
      setImportResult(result);
      if (result.created > 0) onImported();
    } catch (error) {
      setImportError(
        error instanceof Error
          ? error.message
          : "The knowledge bundle could not be imported.",
      );
    } finally {
      setImporting(false);
    }
  }

  return (
    <section
      aria-labelledby="vault-getting-started-heading"
      className="overflow-hidden rounded-[var(--radius-md)] border border-border bg-surface shadow-xs"
    >
      <div className="flex min-h-12 flex-wrap items-center gap-2.5 border-b border-border-strong bg-surface-2/55 px-4 py-2">
        <TonalIcon tone="guide" size="sm" className="h-6 w-6">
          <BookText aria-hidden />
        </TonalIcon>
        <div className="flex min-w-0 flex-1 flex-wrap items-center gap-2">
          <h2
            id="vault-getting-started-heading"
            className="text-sm font-semibold text-foreground"
          >
            Getting started
          </h2>
          <Badge variant="default">Ready for content</Badge>
        </div>
      </div>

      <div className="flex items-start gap-2.5 border-b border-border px-4 py-3">
        <TonalIcon tone="knowledge" size="sm">
          <BookText aria-hidden />
        </TonalIcon>
        <div className="min-w-0">
          <h3 className="font-display text-base font-semibold tracking-tight text-foreground">
            This vault is just getting started
          </h3>
          <p className="mt-1.5 max-w-2xl text-ui leading-5 text-foreground-muted">
            {isMirror
              ? "This vault mirrors an external Git source. Content is managed by that source."
              : canWrite
                ? "Add the first document, file, or structured table to begin building this knowledge space."
                : "There is no content here yet. A writer or connected agent can add the first documents."}
          </p>
        </div>
      </div>

      {canWrite && (
        <div className="grid grid-cols-1 gap-px bg-border @[40rem]/vault-overview:grid-cols-3">
          <OnboardStep
            icon={FilePlus}
            tone="knowledge"
            onClick={onCreateDocument}
            title="Write a document"
            body="Capture knowledge in the full editor with tags and collections."
          />
          <OnboardStep
            icon={Upload}
            tone="file"
            onClick={onUploadFile}
            title="Upload a file"
            body="Keep source files and attachments beside the rest of the Vault."
          />
          <OnboardStep
            icon={TableIcon}
            tone="data"
            onClick={onCreateTable}
            title="Create a table"
            body="Define a queryable schema for structured operational knowledge."
          />
        </div>
      )}

      {canWrite && !isMirror && (
        <section aria-labelledby="vault-setup-heading">
          <div className="flex min-h-10 flex-wrap items-center justify-between gap-2 border-y border-border-strong bg-surface-2 px-4 py-1.5">
            <h3
              id="vault-setup-heading"
              className="text-xs font-semibold text-foreground"
            >
              Set up this Vault
            </h3>
            <span className="text-xs text-foreground-muted">
              Optional next steps
            </span>
          </div>
          <div className="grid grid-cols-1 gap-px bg-border @[40rem]/vault-overview:grid-cols-3">
            <OnboardStep
              icon={FolderInput}
              tone="file"
              onClick={() => importInputRef.current?.click()}
              busy={importing}
              title={
                importing ? "Importing knowledge…" : "Import knowledge bundle"
              }
              body="Bring an existing AKB knowledge bundle into this Vault."
            />
            <OnboardStep
              icon={BookText}
              tone="guide"
              to={`/vault/${name}/settings#skill`}
              title={
                skillCustomized ? "Edit Vault guide" : "Describe this Vault"
              }
              body="Define its purpose, scope, and instructions for connected agents."
            />
            <OnboardStep
              icon={Plug}
              tone="people"
              to="/settings?tab=tokens"
              title="Connect an agent"
              body="Create a token and let an MCP client contribute knowledge here."
            />
          </div>
        </section>
      )}

      {!isMirror && (
        <input
          ref={importInputRef}
          type="file"
          accept=".zip,application/zip"
          className="sr-only"
          aria-label="Choose knowledge bundle"
          onChange={(event) => {
            const file = event.target.files?.[0];
            event.target.value = "";
            if (file) void importBundle(file);
          }}
        />
      )}
      {importError && (
        <div className="border-t border-border p-4">
          <Alert variant="destructive" title="Import failed">
            {importError}
          </Alert>
        </div>
      )}
      {importResult && (
        <div className="border-t border-border p-4">
          <Alert
            variant={importResult.failed > 0 ? "warning" : "success"}
            title={
              importResult.created > 0
                ? "Knowledge imported"
                : "Nothing new to import"
            }
          >
            {importResult.created} document
            {importResult.created === 1 ? "" : "s"} created
            {importResult.skipped > 0
              ? ` · ${importResult.skipped} already existed`
              : ""}
            {importResult.failed > 0 ? ` · ${importResult.failed} failed` : ""}.
          </Alert>
        </div>
      )}
    </section>
  );
}

function OnboardStep({
  icon: Icon,
  tone = "neutral",
  to,
  onClick,
  busy = false,
  title,
  body,
}: {
  icon: LucideIcon;
  tone?: TonalIconTone;
  to?: string;
  onClick?: () => void;
  busy?: boolean;
  title: string;
  body: string;
}) {
  const className =
    "group flex min-h-20 w-full flex-col items-start bg-surface px-4 py-2.5 text-left transition-token hover:bg-surface-hover focus:outline-none focus-visible:z-10 focus-visible:ring-2 focus-visible:ring-inset focus-visible:ring-ring disabled:pointer-events-none disabled:opacity-50";
  const content = (
    <>
      <TonalIcon tone={tone} size="sm" className="h-6 w-6">
        <Icon className={cn(busy && "animate-pulse")} aria-hidden />
      </TonalIcon>
      <span className="mt-1.5 min-w-0">
        <span className="block text-ui font-semibold text-foreground transition-colors group-hover:text-link">
          {title}
        </span>
        <span className="mt-1 block text-xs leading-4 text-foreground-muted">
          {body}
        </span>
      </span>
    </>
  );

  if (to) {
    if (to.startsWith("/settings")) return <SettingsLink to={to} className={className}>{content}</SettingsLink>;
    return (
      <Link to={to} className={className}>
        {content}
      </Link>
    );
  }
  return (
    <button
      type="button"
      onClick={onClick}
      disabled={busy}
      aria-busy={busy || undefined}
      className={className}
    >
      {content}
    </button>
  );
}

function OverviewLoadingState({ checking }: { checking: boolean }) {
  return <LoadingState label={checking ? "Verifying vault access…" : "Loading vault overview…"} className="@container/vault-overview-loading mx-auto w-full max-w-7xl space-y-5">
    <section aria-hidden className="space-y-2 border-b border-border pb-3">
      <div className="flex min-h-14 items-center gap-3"><Skeleton className="h-7 w-7" /><div className="flex-1 space-y-2"><Skeleton className="h-4 w-48" /><Skeleton className="h-3 w-3/5" /></div></div>
      <div className="flex min-h-9 flex-wrap items-center gap-4"><Skeleton className="h-3 w-16" /><Skeleton className="h-3 w-28" /><Skeleton className="h-3 w-20" /></div>
      <div className="flex min-h-8 flex-wrap items-center gap-4">{[0, 1, 2, 3].map(index => <Skeleton key={index} className="h-3 w-20" />)}</div>
    </section>
    <div className="grid items-start gap-6 @[56rem]/vault-overview-loading:grid-cols-[minmax(0,1fr)_16rem]">
      <div className="@container/overview-main min-w-0" aria-hidden>
        <div className="mb-4 flex min-h-12 items-start justify-between gap-3 border-b border-border pb-3"><Skeleton className="h-5 w-28" /><Skeleton className="h-9 w-2/5" /></div>
        <div className="grid grid-cols-1 gap-3 @[28rem]/overview-main:grid-cols-2 @[44rem]/overview-main:grid-cols-3">{[0, 1, 2, 3].map(index => <Skeleton key={index} className="h-24 w-full" />)}</div>
      </div>
      <VaultContextSkeleton />
    </div>
  </LoadingState>;
}
