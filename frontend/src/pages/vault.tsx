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
  Globe,
  HelpCircle,
  LockKeyhole,
  Plug,
  Shield,
  Table as TableIcon,
  Upload,
  UsersRound,
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
import { RoleBadge, VaultStateBadge } from "@/components/status-badge";
import { VaultIndexingStatus } from "@/components/pending-indexing-badge";
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

/** Passive totals share the creation row, but are not unimplemented buttons. */
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
            className="inline-flex min-h-8 items-center gap-2 rounded-[var(--radius-md)] border border-border bg-surface px-2.5 py-1 text-xs"
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
    knownCount(info.document_count) &&
    info.document_count - scaffoldDocs <= 0 &&
    info.table_count === 0 &&
    info.file_count === 0;

  return (
    <div
      role="region"
      aria-label={`${name} Vault overview`}
      className="@container/vault-overview flex min-h-full w-full flex-col gap-6 bg-background p-3 sm:p-4 lg:p-6"
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

      <section aria-label="Vault summary" className="flex min-w-0 flex-col gap-3 border-b border-border pb-4">
        <WorkspacePageHeader
          icon={Box}
          iconTone="knowledge"
          title={name}
          context={
            info?.description ? (
              <span className="block max-w-prose break-words [overflow-wrap:anywhere]">{info.description}</span>
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
              <VaultIndexingStatus vaultName={name!} />
            </>
          }
          className="min-h-0 rounded-none border-0 bg-transparent p-0 shadow-none [&>div:first-child]:max-w-full [&_h1]:whitespace-normal [&_h1]:break-words [&_h1]:[overflow-wrap:anywhere]"
        />
        <div className="flex min-w-0 flex-wrap items-start justify-between gap-x-6 gap-y-3">
          {!infoError && <VaultContentSummary info={info} />}
          {canCreateContent && (
            <div role="group" aria-label="Create content" className="flex max-w-full flex-wrap items-center gap-2">
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
            </div>
          )}
        </div>
      </section>

      {info?.is_archived && (
        <Alert variant="info">
          This vault is archived — content is read-only. Existing documents
          stay browsable; new writes are disabled.
        </Alert>
      )}

      <div className="grid min-w-0 items-start gap-6 @[60rem]/vault-overview:grid-cols-[minmax(0,1fr)_20rem]">
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
        ) : infoError ? (
          <aside aria-label="Vault overview details">
            <Panel variant="workspace" className="p-4 text-sm text-foreground-muted">
              Vault details are unavailable.
            </Panel>
          </aside>
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
      className="@container/recent min-w-0 rounded-[var(--radius-sm)]"
    >
      <div className="flex min-h-11 flex-wrap items-center gap-2 border-b border-border bg-background px-4 py-2">
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
            const collection = row.path?.split("/").slice(0, -1).join(" / ") || "Vault root";
            return (
              <li key={`${row.doc_id}:${row.commit ?? ""}:${index}`}>
                <Link
                  to={`/vault/${name}/doc/${encodeURIComponent(row.path || row.doc_id)}`}
                  className="group grid min-h-14 grid-cols-[1rem_minmax(0,1fr)_auto] items-center gap-x-3 px-4 py-2.5 transition-token hover:bg-surface-hover focus:outline-none focus-visible:ring-2 focus-visible:ring-inset focus-visible:ring-ring"
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
                      className="block break-words text-sm font-medium text-foreground transition-colors group-hover:text-link [overflow-wrap:anywhere]"
                    >
                      {row.title}
                    </span>
                    <span className="block break-words text-xs leading-relaxed text-foreground-muted [overflow-wrap:anywhere]">
                      {collection}
                    </span>
                  </span>
                  <span className="text-right">
                    <span className="sr-only">Updated </span>
                    <RelativeTime iso={row.changed_at} fallback="—" className="whitespace-nowrap text-xs" />
                  </span>
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
      className="w-full min-w-0 rounded-none border-0 border-t bg-transparent"
    >
      <div className="flex min-h-11 flex-wrap items-center gap-2 py-2">
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
              className="inline-flex min-h-9 cursor-pointer items-center rounded-[var(--radius-sm)] px-2 text-xs font-medium text-foreground-muted transition-token hover:bg-surface-hover hover:text-foreground focus:outline-none focus-visible:ring-2 focus-visible:ring-ring"
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
            className="inline-flex min-h-9 items-center rounded-[var(--radius-sm)] px-2 text-xs font-medium text-link transition-token hover:bg-surface-hover hover:text-link-hover focus:outline-none focus-visible:ring-2 focus-visible:ring-ring"
          >
            Full commit log
            <ChevronRight className="ml-1 h-3.5 w-3.5" aria-hidden />
          </Link>
        </div>
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
  const publicAccess =
    info.public_access === "writer"
      ? "Public write"
      : info.public_access === "reader"
        ? "Public read"
        : info.public_access === "none"
          ? "Private"
          : "Not available";
  const visibilityDescription =
    info.public_access === "writer"
      ? info.is_archived || info.is_external_git
        ? "Any signed-in person can read this vault. Content is read-only."
        : "Signed-in users can read, and write when vault policies allow."
      : info.public_access === "reader"
        ? "Any signed-in person can read this vault."
        : info.public_access === "none"
          ? "Only people with access can open this vault."
          : "Visibility information is unavailable.";
  const VisibilityIcon = info.public_access === "none"
    ? LockKeyhole
    : info.public_access === "reader" || info.public_access === "writer"
      ? Globe
      : HelpCircle;

  return (
    <aside className="flex min-w-0 flex-col gap-3" aria-label="Vault overview details">
      <Panel variant="workspace" flush className="min-w-0 rounded-[var(--radius-sm)]" role="region" aria-labelledby="vault-guide-heading">
        <OverviewContextHeading id="vault-guide-heading" icon={BookText} tone="guide">
          Vault guide
        </OverviewContextHeading>
        <div className="p-4">
          <p className="line-clamp-3 max-w-prose break-words text-sm leading-relaxed text-foreground [overflow-wrap:anywhere]">
            {summary}
          </p>
          <div className="mt-3 flex max-w-md flex-wrap items-center justify-between gap-2">
            <Badge id="vault-guide-status" variant="outline" className="rounded-[var(--radius-sm)] text-xs">
              {info.is_external_git ? "Git managed" : guideStatus}
            </Badge>
            {!info.is_external_git && (
              <Button asChild variant="outline" size="sm" className="h-9 rounded-[var(--radius-sm)] text-link">
                <Link
                  to={`/vault/${name}/settings#skill`}
                  aria-label={guideDefined ? "Open vault guide" : "Set up vault guide"}
                  aria-describedby="vault-guide-status"
                >
                  {guideDefined ? "Open guide" : "Set up guide"}
                  <ChevronRight className="h-3.5 w-3.5" aria-hidden />
                </Link>
              </Button>
            )}
          </div>
        </div>
      </Panel>

      <Panel variant="workspace" flush className="min-w-0 rounded-[var(--radius-sm)]" role="region" aria-labelledby="access-overview-heading">
        <OverviewContextHeading id="access-overview-heading" icon={Shield} tone="people">
          Access and ownership
        </OverviewContextHeading>
        <div className="p-4">
          <dl className="max-w-md divide-y divide-border">
            <OverviewContextRow label="Owner" value={info.owner_display_name || info.owner || "Not available"} />
            <OverviewContextRow label="Visibility" value={
              <Badge variant="outline" className="rounded-[var(--radius-sm)] text-xs text-foreground">
                <VisibilityIcon className="h-3.5 w-3.5" aria-hidden />
                {publicAccess}
              </Badge>
            } />
          </dl>
          <p className="mt-1 max-w-md text-xs leading-relaxed text-foreground-muted">{visibilityDescription}</p>
          <Link
            to={`/vault/${name}/members`}
            className="mt-3 flex min-h-10 max-w-md items-center gap-2 rounded-[var(--radius-sm)] border border-border bg-background px-3 py-2 text-sm font-medium text-link transition-token hover:border-border-strong hover:bg-surface-hover hover:text-link-hover focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-inset"
          >
            <UsersRound className="h-4 w-4 shrink-0" aria-hidden />
            <span className="flex-1">Members</span>
            <span className="text-xs tabular-nums text-foreground-muted"><OverviewCount value={info.member_count} /></span>
            <ChevronRight className="h-4 w-4 shrink-0" aria-hidden />
          </Link>
        </div>
      </Panel>

    </aside>
  );
}

function OverviewContextHeading({
  id,
  icon: Icon,
  tone,
  children,
}: {
  id: string;
  icon: LucideIcon;
  tone: TonalIconTone;
  children: ReactNode;
}) {
  return (
    <div className="flex min-h-12 items-center gap-2.5 border-b border-border bg-background px-4 py-2.5">
      <TonalIcon tone={tone} size="sm"><Icon aria-hidden /></TonalIcon>
      <h2 id={id} className="min-w-0 text-sm font-semibold text-foreground">{children}</h2>
    </div>
  );
}

function OverviewContextRow({
  label,
  value,
}: {
  label: string;
  value: ReactNode;
}) {
  return (
    <div className="flex min-h-9 items-start justify-between gap-3 py-1.5 text-sm">
      <dt className="text-foreground-muted">{label}</dt>
      <dd className="min-w-0 break-words text-right font-medium text-foreground [overflow-wrap:anywhere]">{value}</dd>
    </div>
  );
}

function VaultContextSkeleton() {
  return (
    <aside
      className="flex min-w-0 flex-col gap-3"
      aria-label="Loading vault details"
      aria-busy="true"
    >
      <Panel variant="workspace" flush className="rounded-[var(--radius-sm)]" aria-hidden>
        <div className="flex min-h-12 items-center gap-2.5 border-b border-border bg-background px-4 py-2.5">
          <div className="h-7 w-7 animate-pulse rounded bg-surface-muted" />
          <div className="h-4 w-24 animate-pulse rounded bg-surface-muted" />
        </div>
        <div className="space-y-3 p-4">
          <div className="h-16 animate-pulse rounded bg-surface-muted" />
          <div className="h-9 animate-pulse rounded bg-surface-muted" />
        </div>
      </Panel>
      <Panel variant="workspace" flush className="rounded-[var(--radius-sm)]" aria-hidden>
        <div className="flex min-h-12 items-center gap-2.5 border-b border-border bg-background px-4 py-2.5">
          <div className="h-7 w-7 animate-pulse rounded bg-surface-muted" />
          <div className="h-4 w-36 animate-pulse rounded bg-surface-muted" />
        </div>
        <div className="space-y-3 p-4">
          {Array.from({ length: 2 }, (_, i) => (
            <div key={i} className="flex h-6 justify-between">
              <div className="h-3 w-24 animate-pulse rounded bg-surface-muted" />
              <div className="h-3 w-6 animate-pulse rounded bg-surface-muted" />
            </div>
          ))}
          <div className="h-4 animate-pulse rounded bg-surface-muted" />
          <div className="h-10 animate-pulse rounded bg-surface-muted" />
        </div>
      </Panel>
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
