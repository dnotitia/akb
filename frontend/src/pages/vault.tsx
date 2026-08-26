import { useEffect, useRef, useState, type ReactNode } from "react";
import { Link, useNavigate, useParams } from "react-router-dom";
import {
  AlertTriangle,
  BookText,
  Box,
  ChevronRight,
  FileClock,
  Files,
  FilePlus,
  FileText,
  FolderInput,
  FolderTree,
  GitCommit,
  Plug,
  ShieldCheck,
  Table as TableIcon,
  Upload,
  Users,
  type LucideIcon,
} from "lucide-react";
import { useQuery } from "@tanstack/react-query";
import {
  getDocument,
  getRecent,
  getSkillTemplate,
  getVaultActivity,
  getVaultInfo,
  importKnowledgeBundle,
  type KnowledgeImportResult,
} from "@/lib/api";
import { RelativeTime } from "@/components/ui/relative-time";
import { recentIcon, recentTone } from "@/lib/recent";
import { Alert } from "@/components/ui/alert";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Panel } from "@/components/ui/panel";
import { EmptyState } from "@/components/empty-state";
import {
  IndexingBadge,
  RoleBadge,
  VaultStateBadge,
} from "@/components/status-badge";
import { useVaultHealth } from "@/hooks/use-vault-health";
import { VAULT_SKILL_PATH } from "@/lib/skill";
import { TooltipText } from "@/components/ui/tooltip-text";
import { cn } from "@/lib/utils";
import { useVaultRefresh } from "@/contexts/vault-refresh-context";
import { useOpenDocumentCreateDialog } from "@/contexts/document-create-dialog-context";
import { WorkspacePageHeader } from "@/components/ui/workspace-page-header";
import { VaultContextBadge } from "@/components/ui/vault-context-badge";
import { TonalIcon, type TonalIconTone } from "@/components/ui/tonal-icon";
import { FileUploadDialog } from "@/components/file-upload-dialog";
import { TableCreateDialog } from "@/components/table-create-dialog";
import { parseFileUri } from "@/lib/uri";

interface TableMeta {
  name: string;
  row_count?: number;
  columns?: Array<{ name: string; type: string }>;
}

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
  // Pre-loaded table schema (name + row_count + columns) — the overview surfaces
  // it as a tables-at-a-glance band instead of dropping it on a single tile.
  tables?: TableMeta[];
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

/** First prose paragraph of the vault-skill doc, frontmatter + headings
 *  stripped, for the "About this vault" excerpt. */
function aboutExcerpt(md?: string): string {
  if (!md) return "";
  const body = md.replace(/^---\n[\s\S]*?\n---\n/, "");
  const out: string[] = [];
  for (const raw of body.split("\n")) {
    const t = raw.trim();
    if (!t || t.startsWith("#") || t.startsWith(">")) {
      if (out.length) break;
      continue;
    }
    out.push(t);
  }
  return out.join(" ");
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

function StatTileSkeleton() {
  return (
    <div
      className="flex min-h-12 items-center gap-2 bg-surface px-3 py-2"
      aria-hidden
    >
      <div className="h-4 w-4 shrink-0 rounded bg-surface-muted animate-pulse" />
      <div className="h-4 w-7 rounded bg-surface-muted animate-pulse" />
      <div className="h-3 w-14 rounded bg-surface-muted animate-pulse" />
    </div>
  );
}

export default function VaultPage() {
  const { name } = useParams<{ name: string }>();
  const navigate = useNavigate();
  const { refetchTree } = useVaultRefresh();
  const openCreateDocument = useOpenDocumentCreateDialog();
  const uploadButtonRef = useRef<HTMLButtonElement>(null);
  const tableButtonRef = useRef<HTMLButtonElement>(null);
  const [uploadOpen, setUploadOpen] = useState(false);
  const [tableCreateOpen, setTableCreateOpen] = useState(false);
  const [info, setInfo] = useState<VaultInfo | null>(null);
  const [infoError, setInfoError] = useState(false);
  const [recent, setRecent] = useState<RecentRow[]>([]);
  const [recentLoading, setRecentLoading] = useState(true);
  const [recentError, setRecentError] = useState(false);
  const [activity, setActivity] = useState<ActivityRow[]>([]);
  const [activityLoading, setActivityLoading] = useState(true);
  const [activityError, setActivityError] = useState(false);

  const vaultHealth = useVaultHealth(name);
  // Same shape as the global header: `pending` from the backend includes
  // retry-exhausted (abandoned) chunks, so subtract them to get the
  // "actively indexing" count and surface abandoned separately.
  const vUpsert = vaultHealth?.vector_store?.backfill?.upsert;
  const vaultAbandoned: number = vUpsert?.abandoned || 0;
  const vaultPending: number | null = vaultHealth
    ? Math.max(0, (vUpsert?.pending || 0) - vaultAbandoned) +
      (vaultHealth.metadata_backfill?.pending || 0)
    : null;

  const skillQuery = useQuery({
    queryKey: ["document", name, VAULT_SKILL_PATH],
    queryFn: () => getDocument(name!, VAULT_SKILL_PATH),
    retry: false,
    enabled: !!name && !!info && !info.is_external_git,
  });
  const skillExists = !skillQuery.isError && !!skillQuery.data;
  const about = skillExists ? aboutExcerpt(skillQuery.data!.content) : "";

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

  function loadInfo(vault: string, alive: () => boolean = () => true) {
    getVaultInfo(vault)
      .then((d) => alive() && setInfo(d))
      .catch(() => alive() && setInfoError(true));
  }

  async function loadRecent(vault: string, alive: () => boolean = () => true) {
    setRecentLoading(true);
    setRecentError(false);
    try {
      const d = await getRecent(vault, 12);
      if (!alive()) return;
      setRecent(d.changes || []);
    } catch {
      if (!alive()) return;
      setRecentError(true);
    } finally {
      if (alive()) setRecentLoading(false);
    }
  }

  async function loadActivity(
    vault: string,
    alive: () => boolean = () => true,
  ) {
    setActivityLoading(true);
    setActivityError(false);
    try {
      const result = await getVaultActivity(vault, { limit: 10 });
      if (!alive()) return;
      setActivity(result.activity || []);
    } catch {
      if (!alive()) return;
      setActivity([]);
      setActivityError(true);
    } finally {
      if (alive()) setActivityLoading(false);
    }
  }

  useEffect(() => {
    if (!name) return;
    let alive = true;
    const isAlive = () => alive;
    // Reset stale state from the previous param before the re-fetch resolves;
    // the `alive` guard keeps a fast vault switch from clobbering the newer one.
    setInfo(null);
    setInfoError(false);
    setRecent([]);
    setRecentError(false);
    setRecentLoading(true);
    setActivity([]);
    setActivityError(false);
    setActivityLoading(true);
    loadInfo(name, isAlive);
    loadRecent(name, isAlive);
    loadActivity(name, isAlive);
    return () => {
      alive = false;
    };
  }, [name]);

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
    canWrite && !info?.is_archived && !info?.is_external_git;

  // "Just getting started" = no real content yet. A freshly created vault is
  // auto-seeded with an overview/vault-skill.md scaffold, so that one doc
  // doesn't count as content; gate on the skill probe having settled so the
  // layout doesn't flip once it resolves. Show an onboarding hero instead of a
  // barren 1/0/0 stat wall + a lone scaffold commit.
  const scaffoldDocs = skillExists ? 1 : 0;
  const isEmpty =
    !!info &&
    !skillQuery.isLoading &&
    (info.document_count ?? 0) - scaffoldDocs <= 0 &&
    (info.table_count ?? 0) === 0 &&
    (info.file_count ?? 0) === 0;

  const inventory: Array<{
    label: string;
    value: number;
    Icon: LucideIcon;
    tone: TonalIconTone;
  }> = info
    ? [
        {
          label: "Documents",
          value: info.document_count ?? 0,
          Icon: FileText,
          tone: "knowledge",
        },
        {
          label: "Collections",
          value: info.collection_count ?? 0,
          Icon: FolderTree,
          tone: "collection",
        },
        {
          label: "Tables",
          value: info.table_count ?? 0,
          Icon: TableIcon,
          tone: "data",
        },
        {
          label: "Files",
          value: info.file_count ?? 0,
          Icon: Files,
          tone: "file",
        },
        {
          label: "Members",
          value: info.member_count ?? 0,
          Icon: Users,
          tone: "people",
        },
      ]
    : [];

  return (
    <div
      role="region"
      aria-label={`${name} Vault overview`}
      className="flex min-h-full w-full flex-col gap-4 bg-background p-3 sm:p-4 xl:p-5"
    >
      {infoError && (
        <Alert variant="destructive">
          Could not load this vault's details. Some information may be missing.
          <div className="mt-2">
            <Button
              variant="outline"
              size="sm"
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

      <WorkspacePageHeader
        icon={Box}
        iconTone="knowledge"
        title={name}
        context={
          info?.description ? (
            <span className="line-clamp-1 max-w-3xl">{info.description}</span>
          ) : undefined
        }
        meta={
          <>
            <VaultContextBadge name={name!} address copyable />
            {info?.role && <RoleBadge role={info.role} />}
            <VaultStateBadge
              archived={info?.is_archived}
              externalGit={info?.is_external_git}
              publicAccess={info?.public_access}
            />
            <IndexingBadge
              pending={vaultPending}
              abandoned={vaultAbandoned}
            />
          </>
        }
        actions={
          canCreateContent ? (
            <>
              <Button
                ref={uploadButtonRef}
                variant="outline"
                size="sm"
                onClick={() => setUploadOpen(true)}
              >
                <Upload className="h-4 w-4" aria-hidden />
                Upload file
              </Button>
              <Button
                ref={tableButtonRef}
                variant="outline"
                size="sm"
                onClick={() => setTableCreateOpen(true)}
              >
                <TableIcon className="h-4 w-4" aria-hidden />
                New table
              </Button>
              <Button
                variant="accent"
                size="sm"
                onClick={() => openCreateDocument()}
              >
                <FilePlus className="h-4 w-4" aria-hidden />
                New document
              </Button>
            </>
          ) : undefined
        }
        className="rounded-none border-0 border-b border-border-strong bg-transparent px-0 pb-3 pt-0 shadow-none"
      />

      {info?.is_archived && (
        <Alert variant="info">
          This vault is archived — content is read-only. Existing documents
          stay browsable; new writes are disabled.
        </Alert>
      )}

      {!infoError && (
        <Panel variant="workspace" className="min-w-0">
          <section aria-labelledby="vault-inventory-heading">
            <h2 id="vault-inventory-heading" className="sr-only">
              Vault inventory
            </h2>
            <div className="grid grid-cols-2 gap-px bg-border sm:grid-cols-3 xl:grid-cols-[8.75rem_repeat(5,minmax(0,1fr))]">
              <div className="col-span-2 flex min-h-14 items-center gap-2.5 bg-surface-2/70 px-3 text-xs font-semibold text-foreground sm:col-span-3 xl:col-span-1 xl:px-4">
                <TonalIcon tone="neutral" size="sm">
                  <FolderTree aria-hidden />
                </TonalIcon>
                <span>Inventory</span>
              </div>
              {info
                ? inventory.map(({ label, value, Icon, tone }) => (
                    <div
                      key={label}
                      className="flex min-h-14 min-w-0 items-center gap-2.5 bg-surface px-3 py-2"
                    >
                      <TonalIcon tone={tone} size="sm">
                        <Icon aria-hidden />
                      </TonalIcon>
                      <span className="min-w-0">
                        <span
                          className={cn(
                            "block text-sm font-semibold tabular-nums",
                            value === 0 ? "text-subtle" : "text-foreground",
                          )}
                        >
                          {fmt(value)}
                        </span>
                        <span className="block truncate text-xs text-foreground-muted">
                          {label}
                        </span>
                      </span>
                    </div>
                  ))
                : Array.from({ length: 5 }).map((_, i) => (
                    <StatTileSkeleton key={i} />
              ))}
            </div>
          </section>
        </Panel>
      )}

      <div className="grid min-w-0 items-start gap-4 xl:grid-cols-[minmax(0,1fr)_20rem] 2xl:grid-cols-[minmax(0,1fr)_22rem]">
        <div className="flex min-w-0 flex-col gap-4">
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
          ) : (
            <RecentActivityPanel
              name={name!}
              rows={recent}
              loading={recentLoading}
              error={recentError}
              onRetry={() => name && loadRecent(name)}
            />
          )}

          {info && !isEmpty && (
            <CommitHistoryPanel
              name={name!}
              rows={activity}
              loading={activityLoading}
              error={activityError}
              onRetry={() => name && loadActivity(name)}
            />
          )}
        </div>

        {info ? (
          <VaultOverviewAside
            name={name!}
            info={info}
            about={about}
            guideDefined={skillExists}
            guideCustomized={skillCustomized}
            guideLoading={skillQuery.isLoading}
          />
        ) : (
          <VaultContextSkeleton />
        )}
      </div>

      {name && (
        <>
          <FileUploadDialog
            open={uploadOpen}
            onOpenChange={setUploadOpen}
            vault={name}
            returnFocusRef={uploadButtonRef}
            onUploaded={(file) => {
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
            onOpenChange={setTableCreateOpen}
            vault={name}
            returnFocusRef={tableButtonRef}
            onCreated={(tableName) => {
              refetchTree();
              loadInfo(name);
              void loadRecent(name);
              navigate(`/vault/${name}/table/${encodeURIComponent(tableName)}`);
            }}
          />
        </>
      )}
    </div>
  );
}

function RecentActivityPanel({
  name,
  rows,
  loading,
  error,
  onRetry,
}: {
  name: string;
  rows: RecentRow[];
  loading: boolean;
  error: boolean;
  onRetry: () => void;
}) {
  return (
    <Panel
      variant="workspace"
      flush
      role="region"
      aria-labelledby="recent-heading"
      aria-busy={loading}
      className="min-w-0"
    >
      <div className="flex min-h-12 flex-wrap items-center gap-3 border-b border-border-strong bg-surface-2/55 px-4 py-2 sm:px-5">
        <TonalIcon tone="knowledge" size="sm">
          <FileClock aria-hidden />
        </TonalIcon>
        <div className="flex min-w-0 flex-1 flex-wrap items-center gap-2">
          <h2
            id="recent-heading"
            className="text-sm font-semibold text-foreground"
          >
            Recent activity
          </h2>
          {!loading && !error && (
            <Badge variant="default" className="tabular-nums">
              {rows.length} change{rows.length === 1 ? "" : "s"}
            </Badge>
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

      {loading ? (
        <ul className="divide-y divide-border" aria-hidden>
          {Array.from({ length: 4 }).map((_, index) => (
            <li
              key={index}
              className="flex items-center gap-3 px-4 py-3 sm:px-5"
            >
              <span className="h-7 w-7 rounded-[var(--radius-sm)] bg-surface-muted" />
              <span className="h-3 flex-1 rounded bg-surface-muted" />
              <span className="h-2.5 w-14 rounded bg-surface-muted" />
            </li>
          ))}
        </ul>
      ) : error ? (
        <div className="p-4 sm:p-5">
          <EmptyState
            icon={<AlertTriangle className="h-6 w-6" aria-hidden />}
            title="Couldn't load recent activity"
            description="Something went wrong fetching this vault's latest changes."
            action={
              <Button variant="outline" size="sm" onClick={onRetry}>
                Retry
              </Button>
            }
          />
        </div>
      ) : rows.length === 0 ? (
        <div className="p-4 sm:p-5">
          <EmptyState
            icon={<FileClock className="h-6 w-6" aria-hidden />}
            title="Nothing written yet"
            description="Document writes in this vault will appear here."
          />
        </div>
      ) : (
        <ol className="divide-y divide-border bg-surface">
          {rows.map((row, index) => {
            const Icon = recentIcon(row.type);
            const tone = recentTone(row.type);
            return (
              <li key={`${row.doc_id}:${row.commit ?? ""}:${index}`}>
                <Link
                  to={`/vault/${name}/doc/${encodeURIComponent(row.path || row.doc_id)}`}
                  className="group grid grid-cols-[28px_minmax(0,1fr)_auto] items-center gap-x-3 px-4 py-2.5 transition-token hover:bg-surface-hover focus:outline-none focus-visible:ring-2 focus-visible:ring-inset focus-visible:ring-ring sm:grid-cols-[28px_minmax(0,1fr)_auto_auto] sm:px-5"
                >
                  <span
                    className="inline-flex h-7 w-7 shrink-0 items-center justify-center rounded-[var(--radius-sm)]"
                    style={{
                      color: tone,
                      backgroundColor: `color-mix(in srgb, ${tone} 12%, transparent)`,
                    }}
                    aria-hidden
                  >
                    <Icon className="h-3.5 w-3.5" aria-hidden />
                  </span>
                  <span className="min-w-0 sm:flex sm:items-baseline sm:gap-2">
                    <span
                      title={row.title}
                      className="block truncate text-sm font-medium tracking-tight text-foreground transition-colors group-hover:text-link sm:max-w-[42%] sm:shrink-0"
                    >
                      {row.title}
                    </span>
                    <span title={row.path} className="coord block truncate">
                      {row.path}
                    </span>
                  </span>
                  {row.commit && (
                    <span
                      className="coord hidden font-mono tabular-nums sm:block"
                      title={`commit ${row.commit}`}
                    >
                      {row.commit.slice(0, 7)}
                    </span>
                  )}
                  <RelativeTime
                    iso={row.changed_at}
                    className="w-[60px] justify-end text-right"
                  />
                </Link>
              </li>
            );
          })}
        </ol>
      )}
    </Panel>
  );
}

function CommitHistoryPanel({
  name,
  rows,
  loading,
  error,
  onRetry,
}: {
  name: string;
  rows: ActivityRow[];
  loading: boolean;
  error: boolean;
  onRetry: () => void;
}) {
  const [expanded, setExpanded] = useState(false);
  const expandable = loading || error || rows.length > 0;

  return (
    <Panel
      variant="workspace"
      flush
      role="region"
      aria-labelledby="commit-history-heading"
      aria-busy={loading}
      className="w-full min-w-0"
    >
      <div className="flex min-h-12 flex-wrap items-center gap-2 border-b border-border-strong bg-surface-2/55 px-4 py-2 sm:px-5">
        <TonalIcon tone="neutral" size="sm">
          <GitCommit aria-hidden />
        </TonalIcon>
        <div className="flex min-w-0 flex-1 flex-wrap items-baseline gap-2">
          <h2
            id="commit-history-heading"
            className="text-sm font-semibold text-foreground"
          >
            Commit history
          </h2>
          <span className="text-xs tabular-nums text-foreground-muted">
            {loading
              ? "Loading commits…"
              : error
                ? "Unavailable"
                : rows.length > 0
                  ? `${fmt(rows.length)} recent`
                  : "No commits yet"}
          </span>
        </div>
        {expandable && (
          <button
            type="button"
            aria-expanded={expanded}
            aria-controls="commit-history-list"
            onClick={() => setExpanded((current) => !current)}
            className="inline-flex min-h-8 cursor-pointer items-center rounded-[var(--radius-sm)] px-2 text-xs font-medium text-foreground-muted transition-token hover:bg-surface-hover hover:text-foreground focus:outline-none focus-visible:ring-2 focus-visible:ring-ring"
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
          to={`/vault/${name}/activity`}
          className="inline-flex min-h-8 items-center rounded-[var(--radius-sm)] px-2 text-xs font-medium text-link transition-token hover:bg-surface-hover hover:text-link-hover focus:outline-none focus-visible:ring-2 focus-visible:ring-ring"
        >
          Full commit log
          <ChevronRight className="ml-1 h-3.5 w-3.5" aria-hidden />
        </Link>
      </div>

      {expanded && (
        <div id="commit-history-list">
          {loading ? (
            <ul className="divide-y divide-border" aria-hidden>
              {Array.from({ length: 5 }).map((_, index) => (
                <li
                  key={index}
                  className="flex items-center gap-3 px-4 py-3 sm:px-5"
                >
                  <span className="h-3 w-14 rounded bg-surface-muted" />
                  <span className="h-3 w-24 rounded bg-surface-muted" />
                  <span className="h-3 flex-1 rounded bg-surface-muted" />
                </li>
              ))}
            </ul>
          ) : error ? (
            <div className="p-4 sm:p-5">
              <EmptyState
                icon={<AlertTriangle className="h-6 w-6" aria-hidden />}
                title="Couldn't load commit history"
                description="The latest commits are temporarily unavailable."
                action={
                  <Button variant="outline" size="sm" onClick={onRetry}>
                    Retry
                  </Button>
                }
              />
            </div>
          ) : rows.length === 0 ? (
            <p className="px-4 py-6 text-sm text-foreground-muted sm:px-5">
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
                      className="group grid grid-cols-[62px_minmax(0,1fr)_auto] items-center gap-3 px-4 py-2.5 transition-token hover:bg-surface-hover focus:outline-none focus-visible:ring-2 focus-visible:ring-inset focus-visible:ring-ring sm:grid-cols-[62px_120px_minmax(0,1fr)_auto] sm:px-5"
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
                            className="truncate text-sm text-foreground transition-colors group-hover:text-link"
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

function VaultOverviewAside({
  name,
  info,
  about,
  guideDefined,
  guideCustomized,
  guideLoading,
}: {
  name: string;
  info: VaultInfo;
  about: string;
  guideDefined: boolean;
  guideCustomized?: boolean;
  guideLoading: boolean;
}) {
  const guideStatus = guideLoading
    ? "Checking…"
    : !guideDefined
      ? "Guide not configured"
      : guideCustomized === undefined
        ? "Guide ready"
        : guideCustomized
          ? "Guide customized"
          : "Starter template";
  const summary = info.is_external_git
    ? "This vault mirrors an external Git source, which remains the authoritative guide."
    : guideCustomized && about
      ? about
      : guideDefined
        ? "Add purpose, scope, and agent instructions when you are ready to customize this guide."
        : "Set up a vault guide so connected agents understand this knowledge space.";
  const owner = info.owner_display_name || info.owner || "Not available";
  const publicAccess =
    info.public_access === "writer"
      ? "Public write"
      : info.public_access === "reader"
        ? "Public read"
        : "Private";
  const tables = info.tables || [];
  const shownTables = tables.slice(0, TABLES_PREVIEW);
  const visibilityVariant =
    info.public_access === "writer"
      ? "warning"
      : info.public_access === "reader"
        ? "info-outline"
        : "default";

  return (
    <aside className="min-w-0" aria-label="Vault overview details">
      <Panel variant="workspace" flush className="min-w-0">
        <div className="flex min-h-12 items-center justify-between gap-3 border-b border-border-strong bg-surface-2/55 px-4 py-2.5">
          <div className="flex min-w-0 items-center gap-2.5">
            <TonalIcon tone="neutral" size="sm">
              <Box aria-hidden />
            </TonalIcon>
            <div className="min-w-0">
              <h2 className="text-sm font-semibold text-foreground">
                Vault context
              </h2>
              <p className="truncate text-xs text-foreground-muted">
                Guide and access
              </p>
            </div>
          </div>
          <Badge variant={visibilityVariant}>{publicAccess}</Badge>
        </div>

        <section
          aria-labelledby="vault-guide-heading"
          className="border-b border-border"
        >
          <div className="flex items-start gap-3 px-4 py-3.5">
            <TonalIcon tone="guide" size="sm">
              <BookText aria-hidden />
            </TonalIcon>
            <div className="min-w-0 flex-1">
              <div className="flex flex-wrap items-center justify-between gap-2">
                <h3
                  id="vault-guide-heading"
                  className="text-xs font-semibold text-foreground"
                >
                  Vault guide
                </h3>
                <Badge variant="info-outline">{guideStatus}</Badge>
              </div>
              <p className="mt-2 text-xs leading-relaxed text-foreground-muted">
                {summary}
              </p>
              {!info.is_external_git && (
                <Link
                  to={`/vault/${name}/settings#skill`}
                  aria-label={
                    guideDefined ? "Open vault guide" : "Set up vault guide"
                  }
                  className="mt-2 inline-flex min-h-7 items-center gap-1 rounded-[var(--radius-sm)] text-xs font-medium text-link hover:text-link-hover focus:outline-none focus-visible:ring-2 focus-visible:ring-ring"
                >
                  {guideDefined ? "Open guide" : "Set up guide"}
                  <ChevronRight className="h-3.5 w-3.5" aria-hidden />
                  <span className="sr-only">{guideStatus}</span>
                </Link>
              )}
            </div>
          </div>
        </section>

        <section
          aria-labelledby="access-overview-heading"
          className="border-b border-border"
        >
          <div className="flex items-center gap-3 px-4 pt-3.5">
            <TonalIcon
              tone={info.public_access === "none" ? "info" : "success"}
              size="sm"
            >
              <ShieldCheck aria-hidden />
            </TonalIcon>
            <h3
              id="access-overview-heading"
              className="min-w-0 flex-1 text-xs font-semibold text-foreground"
            >
              Access and ownership
            </h3>
            <Link
              to={`/vault/${name}/members`}
              className="inline-flex min-h-7 items-center rounded-[var(--radius-sm)] text-xs font-medium text-link hover:text-link-hover focus:outline-none focus-visible:ring-2 focus-visible:ring-ring"
            >
              Members
            </Link>
          </div>
          <dl className="mt-2 divide-y divide-border px-4 pb-1">
            <OverviewContextRow label="Owner" value={owner} />
            <OverviewContextRow label="Visibility" value={publicAccess} />
            <OverviewContextRow
              label="Members"
              value={fmt(info.member_count ?? 0)}
            />
          </dl>
        </section>

        {(info.table_count ?? 0) > 0 && (
          <section aria-labelledby="tables-overview-heading">
            <div className="flex min-h-11 items-center gap-3 border-b border-border px-4 py-2">
              <TonalIcon tone="data" size="sm">
                <TableIcon aria-hidden />
              </TonalIcon>
              <h3
                id="tables-overview-heading"
                className="min-w-0 flex-1 text-xs font-semibold text-foreground"
              >
                Tables
              </h3>
              <Badge variant="default" className="tabular-nums">
                {fmt(info.table_count ?? 0)}
              </Badge>
            </div>
            {shownTables.length > 0 ? (
              <ul className="divide-y divide-border">
                {shownTables.map((table) => (
                  <li key={table.name}>
                    <Link
                      to={`/vault/${name}/table/${encodeURIComponent(table.name)}`}
                      className="group flex min-h-11 items-center gap-2 px-4 py-2 transition-token hover:bg-surface-hover focus:outline-none focus-visible:ring-2 focus-visible:ring-inset focus-visible:ring-ring"
                    >
                      <TableIcon
                        className="h-3.5 w-3.5 shrink-0 text-[var(--color-cat-3)]"
                        aria-hidden
                      />
                      <span className="min-w-0 flex-1 truncate text-sm text-foreground group-hover:text-link">
                        {table.name}
                      </span>
                      <span className="coord shrink-0 tabular-nums">
                        {fmt(table.row_count ?? 0)} rows
                      </span>
                    </Link>
                  </li>
                ))}
              </ul>
            ) : (
              <p className="p-4 text-xs leading-relaxed text-foreground-muted">
                Browse this Vault's tables from Collections.
              </p>
            )}
            {(info.table_count ?? 0) > shownTables.length && (
              <p className="border-t border-border px-4 py-2.5 text-xs text-foreground-muted">
                +{fmt((info.table_count ?? 0) - shownTables.length)} more in
                Collections
              </p>
            )}
          </section>
        )}
      </Panel>
    </aside>
  );
}

const TABLES_PREVIEW = 3;

function OverviewContextRow({
  label,
  value,
}: {
  label: string;
  value: ReactNode;
}) {
  return (
    <div className="flex min-h-10 items-center justify-between gap-3 py-2 text-xs">
      <dt className="text-foreground-muted">{label}</dt>
      <dd className="min-w-0 truncate text-right text-foreground">{value}</dd>
    </div>
  );
}

function VaultContextSkeleton() {
  return (
    <aside
      className="min-w-0 overflow-hidden rounded-[var(--radius-md)] border border-border bg-surface shadow-xs"
      aria-hidden
    >
      <div className="h-12 border-b border-border-strong bg-surface-2/55" />
      <div className="space-y-3 p-4">
        <div className="h-4 w-24 animate-pulse rounded bg-surface-muted" />
        <div className="h-3 w-full animate-pulse rounded bg-surface-muted" />
        <div className="h-3 w-4/5 animate-pulse rounded bg-surface-muted" />
      </div>
    </aside>
  );
}

/** First-run guidance that occupies the same primary-content slot as Recent
 *  activity. Adding the first resource changes state, not the page grammar. */
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
      <div className="flex min-h-14 flex-wrap items-center gap-3 border-b border-border-strong bg-surface-2/55 px-4 py-2.5 sm:px-5">
        <TonalIcon tone="guide" size="sm">
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

      <div className="flex items-start gap-3 border-b border-border px-4 py-4 sm:px-5">
        <TonalIcon tone="knowledge">
          <BookText aria-hidden />
        </TonalIcon>
        <div className="min-w-0">
          <h3 className="font-display text-lg font-semibold tracking-tight text-foreground">
            This vault is just getting started
          </h3>
          <p className="mt-2 max-w-2xl text-sm leading-relaxed text-foreground-muted">
            {isMirror
              ? "This vault mirrors an external Git source. Content is managed by that source."
              : canWrite
                ? "Add the first document, file, or structured table to begin building this knowledge space."
                : "There is no content here yet. A writer or connected agent can add the first documents."}
          </p>
        </div>
      </div>

      {canWrite && (
        <div className="grid grid-cols-1 gap-px bg-border sm:grid-cols-3">
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
          <div className="flex min-h-10 flex-wrap items-center justify-between gap-2 border-y border-border-strong bg-surface-2 px-4 py-2 sm:px-5">
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
          <div className="grid grid-cols-1 gap-px bg-border sm:grid-cols-3">
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
    "group flex min-h-24 w-full flex-col items-start bg-surface px-4 py-3 text-left transition-token hover:bg-surface-hover focus:outline-none focus-visible:z-10 focus-visible:ring-2 focus-visible:ring-inset focus-visible:ring-ring disabled:pointer-events-none disabled:opacity-50";
  const content = (
    <>
      <TonalIcon tone={tone} size="sm">
        <Icon className={cn(busy && "animate-pulse")} aria-hidden />
      </TonalIcon>
      <span className="mt-2 min-w-0">
        <span className="block text-sm font-semibold text-foreground transition-colors group-hover:text-link">
          {title}
        </span>
        <span className="mt-1 block text-xs leading-relaxed text-foreground-muted">
          {body}
        </span>
      </span>
    </>
  );

  if (to) {
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
