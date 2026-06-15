import { type ReactNode, useEffect, useState } from "react";
import { Link, useParams } from "react-router-dom";
import {
  AlertTriangle,
  BookText,
  ChevronDown,
  ChevronRight,
  FileClock,
  FilePlus,
  Plug,
  Settings as SettingsIcon,
  Sparkles,
  Table as TableIcon,
  Users,
} from "lucide-react";
import { useQuery } from "@tanstack/react-query";
import { getDocument, getRecent, getVaultActivity, getVaultInfo } from "@/lib/api";
import { RelativeTime } from "@/components/ui/relative-time";
import { recentIcon, recentTone } from "@/lib/recent";
import { Alert } from "@/components/ui/alert";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Panel } from "@/components/ui/panel";
import { EmptyState } from "@/components/empty-state";
import { IndexingBadge, RoleBadge, VaultStateBadge } from "@/components/status-badge";
import { useVaultHealth } from "@/hooks/use-vault-health";
import { SkillStatusChip } from "@/components/skill/skill-status-chip";
import { StatTile } from "@/components/ui/stat-tile";

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
  const m =
    map[change.toLowerCase()] ?? {
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
      className="rounded-[var(--radius-lg)] border border-border bg-surface shadow-sm px-4 py-3.5"
      aria-hidden
    >
      <div className="h-3 w-16 rounded bg-surface-muted animate-pulse mb-2" />
      <div className="h-7 w-10 rounded bg-surface-muted animate-pulse" />
    </div>
  );
}

export default function VaultPage() {
  const { name } = useParams<{ name: string }>();
  const [info, setInfo] = useState<VaultInfo | null>(null);
  const [infoError, setInfoError] = useState(false);
  const [recent, setRecent] = useState<RecentRow[]>([]);
  const [recentLoading, setRecentLoading] = useState(true);
  const [recentError, setRecentError] = useState(false);
  const [activity, setActivity] = useState<ActivityRow[]>([]);
  const [commitsOpen, setCommitsOpen] = useState(false);
  const [commitsLoaded, setCommitsLoaded] = useState(false);

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
    queryKey: ["document", name, "overview/vault-skill.md"],
    queryFn: () => getDocument(name!, "overview/vault-skill.md"),
    retry: false,
    enabled: !!name,
  });
  const skillExists = !skillQuery.isError && !!skillQuery.data;
  const skillLineCount = skillExists
    ? (skillQuery.data!.content || "").split("\n").length
    : undefined;
  const about = skillExists ? aboutExcerpt(skillQuery.data!.content) : "";

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
    setCommitsLoaded(false);
    setCommitsOpen(false);
    loadInfo(name, isAlive);
    loadRecent(name, isAlive);
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

  async function ensureCommitsLoaded(vault: string) {
    if (commitsLoaded) return;
    try {
      const r = await getVaultActivity(vault, { limit: 20 });
      setActivity(r.activity || []);
    } catch {
      setActivity([]);
    } finally {
      setCommitsLoaded(true);
    }
  }

  function toggleCommits() {
    const next = !commitsOpen;
    setCommitsOpen(next);
    if (next && name) ensureCommitsLoaded(name);
  }

  const canWrite =
    info?.role === "writer" || info?.role === "admin" || info?.role === "owner";

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

  return (
    <div className="fade-up">
      {infoError && (
        <Alert variant="destructive" className="mb-4">
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

      {/* Meta line — one identity only (the H1 below states the name; the
          breadcrumb says "where am I"), so don't print the name a third time:
          just the label + the canonical mono URI. */}
      <div className="coord mb-3">
        Vault · <span className="font-mono">akb://{name}</span>
      </div>

      {/* Display title */}
      <h1 className="font-display text-3xl font-semibold tracking-tight text-foreground mb-3">
        {name}
      </h1>

      {info?.description ? (
        <p className="text-base leading-[1.55] text-foreground-muted mb-1 max-w-2xl">
          {info.description}
        </p>
      ) : info === null && !infoError ? (
        <div
          className="mb-1 h-4 max-w-md rounded bg-surface-muted animate-pulse"
          aria-hidden
        />
      ) : null}

      {/* Vitality line — who owns it, how old, how alive. Display names are
          sans (not mono); last_active_user is a raw UUID server-side so it's
          intentionally omitted until the endpoint resolves it. Segments join
          with a "·" only BETWEEN present items (no dangling leading dot when,
          e.g., the owner has no display name yet). */}
      {(() => {
        if (!info) return null;
        const segs: ReactNode[] = [];
        if (info.owner_display_name)
          segs.push(
            <>
              Owned by{" "}
              <span className="text-foreground">{info.owner_display_name}</span>
            </>,
          );
        if (info.created_at)
          segs.push(<>Created <RelativeTime iso={info.created_at} /></>);
        if (info.last_activity)
          segs.push(<>Last active <RelativeTime iso={info.last_activity} /></>);
        if (!segs.length) return null;
        return (
          <div className="coord mt-1.5 flex flex-wrap items-center gap-x-2 gap-y-1">
            {segs.map((s, i) => (
              <span key={i} className="flex items-center gap-x-2">
                {i > 0 && <span aria-hidden>·</span>}
                <span>{s}</span>
              </span>
            ))}
          </div>
        );
      })()}

      {/* Identity pills — what this vault IS. */}
      <div className="flex flex-wrap items-center gap-2 mt-4">
        {info?.role && <RoleBadge role={info.role} />}
        <VaultStateBadge
          archived={info?.is_archived}
          externalGit={info?.is_external_git}
          publicAccess={info?.public_access}
        />
        <IndexingBadge pending={vaultPending} abandoned={vaultAbandoned} />
        {!skillQuery.isLoading && (
          <SkillStatusChip vault={name!} defined={skillExists} lineCount={skillLineCount} />
        )}
      </div>

      {/* Actions — what you can DO. Secondary nav on the left, the single
          marquee CTA on the right; kept distinct from the identity pills above
          so "what it is" never reads as "what to do". */}
      <div className="flex flex-wrap items-center gap-x-5 gap-y-2 mt-4">
        <Link
          to={`/vault/${name}/members`}
          className="inline-flex items-center gap-1.5 min-h-[36px] coord hover:text-link transition-colors focus:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2 focus-visible:ring-offset-background rounded-[var(--radius-sm)]"
        >
          <Users className="h-3 w-3" aria-hidden />
          Members
          {info?.member_count !== undefined && (
            <span className="tabular-nums">[{fmt(info.member_count)}]</span>
          )}
        </Link>
        {info?.role === "owner" && (
          <Link
            to={`/vault/${name}/settings`}
            className="inline-flex items-center gap-1.5 min-h-[36px] coord hover:text-link transition-colors focus:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2 focus-visible:ring-offset-background rounded-[var(--radius-sm)]"
          >
            <SettingsIcon className="h-3 w-3" aria-hidden />
            Settings
          </Link>
        )}
        {canWrite && !info?.is_archived && !isEmpty && (
          <Button asChild variant="accent" size="md" className="ml-auto">
            <Link to={`/vault/${name}/doc/new`}>
              <FilePlus className="h-4 w-4" aria-hidden />
              New document
            </Link>
          </Button>
        )}
      </div>

      {/* Archived vaults are hard read-only server-side, so the write CTA above
          is withheld — say why rather than routing into a guaranteed failure. */}
      {info?.is_archived && (
        <Alert variant="info" className="mt-4">
          This vault is archived — content is read-only. Existing documents stay
          browsable; new writes are disabled.
        </Alert>
      )}

      {/* About — the vault-skill doc is the best answer to "what is this vault
          for", so surface its opening on the read-first landing. */}
      {skillExists && about && (
        <div className="mt-6 rounded-[var(--radius-lg)] border border-border bg-surface/60 px-4 py-3">
          <div className="flex items-baseline justify-between gap-3 mb-1.5">
            <span className="coord-ink">About this vault</span>
            <Link
              to={`/vault/${name}/doc/${encodeURIComponent("overview/vault-skill.md")}`}
              className="coord shrink-0 inline-flex items-center min-h-[28px] hover:text-link transition-colors rounded-[var(--radius-sm)] focus:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2 focus-visible:ring-offset-background"
            >
              Vault skill ↗
            </Link>
          </div>
          <p className="text-sm leading-relaxed text-foreground-muted line-clamp-2">
            {about}
          </p>
        </div>
      )}

      {/* Empty vault → an onboarding hero instead of a barren wall of dimmed
          zeros + empty lists. Otherwise: stats → tables → recent → commit log. */}
      {info && isEmpty ? (
        <VaultEmptyOnboarding
          name={name!}
          canWrite={canWrite}
          skillDefined={skillExists}
        />
      ) : (
      <>
      {/* Ledger — 4-stat tiles read from the authoritative, depth-safe counts
          in /info. Skeletons reserve the height so the tiles don't pop in. The
          label already names the category, so no jargon unit row; 0 recedes. */}
      <div className="mt-10 grid grid-cols-2 sm:grid-cols-4 gap-3">
        {info ? (
          (
            [
              ["Collections", info.collection_count ?? 0],
              ["Documents", info.document_count ?? 0],
              ["Tables", info.table_count ?? 0],
              ["Files", info.file_count ?? 0],
            ] as Array<[string, number]>
          ).map(([label, value]) => (
            <StatTile key={label} label={label} value={fmt(value)} dimZero />
          ))
        ) : infoError ? null : (
          Array.from({ length: 4 }).map((_, i) => <StatTileSkeleton key={i} />)
        )}
      </div>

      {info?.tables && info.tables.length > 0 && (
        <TablesBand name={name!} tables={info.tables} />
      )}

      {/* Recent writes — primary. Same grammar as the Home dashboard's Recent
          activity (loading flag → skeleton, type-tinted leading chip,
          fresh-token spark, card-hover lift) so a change reads identically
          across the app. Single-vault context here, so no per-row VaultChip;
          the git commit ref stays (demoted) since the href is commit-pinned. */}
      <section
        className="mt-10"
        aria-labelledby="recent-heading"
        aria-busy={recentLoading}
      >
        <div className="flex items-baseline gap-3 pb-3 border-b border-border mb-3">
          <h2 id="recent-heading" className="text-xl font-semibold tracking-tight">
            Recent activity
          </h2>
          {!recentLoading && !recentError && recent.length > 0 && (
            <Badge variant="default" className="tabular-nums">
              {recent.length}
            </Badge>
          )}
        </div>
        <span className="sr-only" role="status" aria-live="polite">
          {recentLoading
            ? "Loading recent activity"
            : recentError
              ? "Could not load recent activity"
              : `${recent.length} recent change${recent.length === 1 ? "" : "s"}`}
        </span>

        {recentLoading ? (
          <Panel aria-hidden>
            <ul className="divide-y divide-border">
              {Array.from({ length: 5 }).map((_, i) => (
                <li key={i} className="flex items-center gap-3 px-3 py-2.5">
                  <span className="h-5 w-5 rounded bg-surface-muted shrink-0" />
                  <span className="h-3 flex-1 rounded bg-surface-muted" />
                  <span className="h-2.5 w-14 rounded bg-surface-muted" />
                </li>
              ))}
            </ul>
          </Panel>
        ) : recentError ? (
          <EmptyState
            icon={
              <span className="feature-tile feat-neutral h-14 w-14">
                <AlertTriangle className="h-6 w-6" aria-hidden />
              </span>
            }
            title="Couldn't load recent activity"
            description="Something went wrong fetching this vault's latest changes."
            action={
              <Button variant="outline" size="sm" onClick={() => name && loadRecent(name)}>
                Retry
              </Button>
            }
          />
        ) : recent.length === 0 ? (
          <EmptyState
            icon={
              <span className="feature-tile feat-knowledge h-14 w-14">
                <FileClock className="h-6 w-6" aria-hidden />
              </span>
            }
            title="Nothing written yet"
            description="Document writes in this vault will appear here."
          />
        ) : (
          <Panel inset={false}>
            {/* inset={false} so a hovered row's lift + shadow aren't clipped;
                the end rows are re-rounded to keep the divided-panel look. */}
            <ol className="divide-y divide-border stagger [&>li:first-child>a]:rounded-t-[var(--radius-lg)] [&>li:last-child>a]:rounded-b-[var(--radius-lg)]">
              {recent.map((c, i) => {
                const Icon = recentIcon(c.type);
                const tone = recentTone(c.type);
                return (
                  <li key={`${c.doc_id}:${c.commit ?? ""}:${i}`}>
                    <Link
                      to={
                        `/vault/${name}/doc/${encodeURIComponent(c.path || c.doc_id)}` +
                        (c.commit ? `?commit=${encodeURIComponent(c.commit)}` : "")
                      }
                      className="group card-hover relative z-0 hover:z-10 grid grid-cols-[20px_minmax(0,1fr)_auto] items-center gap-x-3 px-3 py-2.5 bg-surface hover:bg-surface-muted focus:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2 focus-visible:ring-offset-background"
                    >
                      <span
                        className="inline-flex h-5 w-5 items-center justify-center rounded-[var(--radius-sm)] shrink-0"
                        style={{
                          color: tone,
                          backgroundColor: `color-mix(in srgb, ${tone} 12%, transparent)`,
                        }}
                        aria-hidden
                      >
                        <Icon className="h-3 w-3" aria-hidden />
                      </span>
                      <div className="min-w-0">
                        <div
                          title={c.title}
                          className="text-sm font-medium tracking-tight truncate text-foreground group-hover:text-link transition-colors"
                        >
                          {c.title}
                        </div>
                        <div title={c.path} className="coord truncate">
                          {c.path}
                        </div>
                      </div>
                      <div className="flex items-center gap-3 shrink-0">
                        {c.commit && (
                          <span
                            className="coord font-mono tabular-nums"
                            title={`commit ${c.commit}`}
                          >
                            {c.commit.slice(0, 7)}
                          </span>
                        )}
                        <RelativeTime
                          iso={c.changed_at}
                          className="w-[60px] justify-end text-right"
                        />
                      </div>
                    </Link>
                  </li>
                );
              })}
            </ol>
          </Panel>
        )}
      </section>

      {/* Commit log — collapsible (secondary detail). The disclosure label is a
          real <h2> wrapping the button (accordion pattern) so it shows in the
          screen-reader heading list. */}
      <section className="mt-8 mb-10" aria-labelledby="commit-log-heading">
        {/* Disclosure button + the full-history link are SIBLINGS — a <Link>
            nested inside a <button> is invalid HTML and breaks keyboard nav. */}
        <div className="flex items-center gap-2 py-2">
          <h2 className="flex-1 min-w-0">
            <button
              onClick={toggleCommits}
              aria-expanded={commitsOpen}
              aria-controls="commit-log-list"
              className="flex w-full items-center gap-2 min-h-[36px] -my-1 text-left text-foreground-muted hover:text-foreground transition-colors rounded-[var(--radius-sm)] focus:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2 focus-visible:ring-offset-background"
            >
              {commitsOpen ? (
                <ChevronDown className="h-3 w-3" aria-hidden />
              ) : (
                <ChevronRight className="h-3 w-3" aria-hidden />
              )}
              <span id="commit-log-heading" className="coord-ink">
                Commit log
              </span>
              {commitsLoaded && (
                <span className="coord tabular-nums">[{fmt(activity.length)}]</span>
              )}
            </button>
          </h2>
          <Link
            to={`/vault/${name}/activity`}
            className="coord shrink-0 inline-flex items-center min-h-[36px] -my-1 px-1 hover:text-link transition-colors rounded-[var(--radius-sm)] focus:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2 focus-visible:ring-offset-background"
          >
            ↗ Full commit history
          </Link>
        </div>

        {commitsOpen && (
          <div
            id="commit-log-list"
            className="mt-2 rounded-[var(--radius-lg)] border border-border bg-surface p-3 overflow-x-auto shadow-sm"
          >
            <div className="coord mb-2 pb-2 border-b border-border">Latest commits</div>
            {!commitsLoaded ? (
              <div className="coord" role="status" aria-live="polite">
                Loading…
              </div>
            ) : activity.length === 0 ? (
              <div className="coord" role="status">
                No commits
              </div>
            ) : (
              <ol className="text-xs leading-relaxed min-w-[520px]">
                {activity.map((c, i) => {
                  const filePath = c.files?.[0]?.path || c.summary || "";
                  const filesCount = c.files?.length || 0;
                  const change = c.files?.[0]?.change;
                  const author = c.author_name || c.agent || c.author || "unknown";
                  const primaryDocPath = c.files?.[0]?.path;
                  const link = primaryDocPath
                    ? `/vault/${name}/doc/${encodeURIComponent(primaryDocPath)}` +
                      (c.hash ? `?commit=${encodeURIComponent(c.hash)}` : "")
                    : `/vault/${name}`;
                  return (
                    <li key={i}>
                      <Link
                        to={link}
                        className="group grid grid-cols-[68px_140px_1fr_20px_56px] gap-3 py-1 items-baseline hover:bg-surface-hover -mx-2 px-2 transition-colors focus:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2 focus-visible:ring-offset-surface"
                      >
                        <span className="font-mono text-[11px] text-foreground-muted tabular-nums">
                          {(c.hash || "").slice(0, 7)}
                        </span>
                        <span title={author} className="text-foreground truncate">
                          {author}
                        </span>
                        <span
                          title={c.subject || filePath}
                          className="text-foreground truncate"
                        >
                          {c.subject || filePath}
                          {filesCount > 1 && (
                            <span className="text-foreground-muted"> · +{filesCount - 1}</span>
                          )}
                        </span>
                        <span className="text-center">{changeMark(change)}</span>
                        <RelativeTime iso={c.timestamp} className="text-right" />
                      </Link>
                    </li>
                  );
                })}
              </ol>
            )}
          </div>
        )}
      </section>
      </>
      )}
    </div>
  );
}

const TABLES_PREVIEW = 6;

/** Tables-at-a-glance — name + row/column counts per table, linking to the
 *  table viewer. Surfaces the schema /info already pre-loads instead of
 *  collapsing it to a single count tile. */
function TablesBand({ name, tables }: { name: string; tables: TableMeta[] }) {
  const shown = tables.slice(0, TABLES_PREVIEW);
  const more = tables.length - shown.length;
  return (
    <section className="mt-8" aria-labelledby="tables-heading">
      <div className="flex items-baseline gap-3 pb-3 border-b border-border mb-3">
        <h2 id="tables-heading" className="text-xl font-semibold tracking-tight">
          Tables
        </h2>
        <Badge variant="default" className="tabular-nums">
          {fmt(tables.length)}
        </Badge>
      </div>
      <ul className="grid grid-cols-1 sm:grid-cols-2 gap-2">
        {shown.map((t) => {
          const rows = t.row_count ?? 0;
          const cols = t.columns?.length ?? 0;
          return (
            <li key={t.name}>
              <Link
                to={`/vault/${name}/table/${encodeURIComponent(t.name)}`}
                className="group card-hover flex items-center justify-between gap-3 rounded-[var(--radius-lg)] border border-border bg-surface px-3 py-2.5 shadow-sm hover:bg-surface-muted focus:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2 focus-visible:ring-offset-background"
              >
                <span className="inline-flex items-center gap-2 min-w-0">
                  <span
                    className="inline-flex h-5 w-5 items-center justify-center rounded-[var(--radius-sm)] shrink-0"
                    style={{
                      color: "var(--color-cat-3)",
                      backgroundColor:
                        "color-mix(in srgb, var(--color-cat-3) 12%, transparent)",
                    }}
                    aria-hidden
                  >
                    <TableIcon className="h-3 w-3" aria-hidden />
                  </span>
                  <span
                    title={t.name}
                    className="truncate text-sm font-medium text-foreground group-hover:text-link transition-colors"
                  >
                    {t.name}
                  </span>
                </span>
                <span className="coord tabular-nums shrink-0 whitespace-nowrap">
                  {fmt(rows)} {rows === 1 ? "row" : "rows"} · {cols}{" "}
                  {cols === 1 ? "col" : "cols"}
                </span>
              </Link>
            </li>
          );
        })}
      </ul>
      {more > 0 && (
        <p className="coord mt-2">
          +{fmt(more)} more {more === 1 ? "table" : "tables"} in the sidebar tree
        </p>
      )}
    </section>
  );
}

/** First-run hero for a brand-new (fully empty) vault — owns the single marquee
 *  orange CTA (the header's is withheld while empty), plus quiet next-step
 *  cards for writers. Readers get the message without the CTAs. */
function VaultEmptyOnboarding({
  name,
  canWrite,
  skillDefined,
}: {
  name: string;
  canWrite: boolean;
  skillDefined: boolean;
}) {
  return (
    <div className="mt-10">
      <EmptyState
        icon={
          <span className="feature-tile feat-knowledge h-14 w-14">
            <Sparkles className="h-6 w-6" aria-hidden />
          </span>
        }
        title="This vault is just getting started"
        description={
          canWrite
            ? "Nothing here yet — write the first document, describe what it's for, or point an agent at it."
            : "No content yet. A writer or an agent can add the first documents."
        }
        action={
          canWrite ? (
            <Button asChild variant="accent" size="md">
              <Link to={`/vault/${name}/doc/new`}>
                <FilePlus className="h-4 w-4" aria-hidden />
                New document
              </Link>
            </Button>
          ) : undefined
        }
      />
      {canWrite && (
        <div className="mt-4 grid grid-cols-1 sm:grid-cols-2 gap-3">
          <OnboardStep
            icon={BookText}
            to={
              skillDefined
                ? `/vault/${name}/doc/${encodeURIComponent("overview/vault-skill.md")}`
                : `/vault/${name}/doc/new`
            }
            title={skillDefined ? "Edit the vault skill" : "Describe this vault"}
            body="Tell agents what this vault is for and how to use it."
          />
          <OnboardStep
            icon={Plug}
            to="/settings?tab=tokens"
            title="Connect an agent"
            body="Mint a token and wire up your MCP client to write here."
          />
        </div>
      )}
    </div>
  );
}

function OnboardStep({
  icon: Icon,
  to,
  title,
  body,
}: {
  icon: typeof BookText;
  to: string;
  title: string;
  body: string;
}) {
  return (
    <Link
      to={to}
      className="group card-hover flex items-start gap-3 rounded-[var(--radius-lg)] border border-border bg-surface px-4 py-3 shadow-sm hover:bg-surface-muted focus:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2 focus-visible:ring-offset-background"
    >
      <span className="inline-flex h-8 w-8 items-center justify-center rounded-[var(--radius-md)] bg-surface-muted text-foreground-muted shrink-0 group-hover:text-link transition-colors">
        <Icon className="h-4 w-4" aria-hidden />
      </span>
      <span className="min-w-0">
        <span className="block text-sm font-medium text-foreground group-hover:text-link transition-colors">
          {title}
        </span>
        <span className="block coord mt-0.5">{body}</span>
      </span>
    </Link>
  );
}
