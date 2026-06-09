import { useEffect, useState } from "react";
import { Link, useParams } from "react-router-dom";
import { ChevronDown, ChevronRight, FilePlus, Settings as SettingsIcon, Users } from "lucide-react";
import { useQuery } from "@tanstack/react-query";
import { browseVault, getDocument, getRecent, getVaultActivity, getVaultInfo } from "@/lib/api";
import { timeAgo } from "@/lib/utils";
import { Alert } from "@/components/ui/alert";
import { EmptyState } from "@/components/empty-state";
import { IndexingBadge, RoleBadge, VaultStateBadge } from "@/components/status-badge";
import { useVaultHealth } from "@/hooks/use-vault-health";
import { SkillStatusChip } from "@/components/skill/skill-status-chip";
import { StatTile } from "@/components/ui/stat-tile";

interface VaultInfo {
  name: string;
  description?: string;
  role?: "owner" | "admin" | "writer" | "reader";
  is_archived?: boolean;
  is_external_git?: boolean;
  public_access?: "none" | "reader" | "writer";
  member_count?: number;
}

interface Counts {
  collections: number;
  documents: number;
  tables: number;
  files: number;
}

interface RecentRow {
  doc_id: string;
  vault: string;
  path: string;
  title: string;
  commit?: string;
  changed_at?: string;
}

interface ActivityRow {
  hash?: string;
  agent?: string;
  author?: string;
  subject?: string;
  summary?: string;
  timestamp?: string;
  files?: Array<{ path: string; change?: string }>;
}

export default function VaultPage() {
  const { name } = useParams<{ name: string }>();
  const [info, setInfo] = useState<VaultInfo | null>(null);
  const [counts, setCounts] = useState<Counts | null>(null);
  const [recent, setRecent] = useState<RecentRow[]>([]);
  const [activity, setActivity] = useState<ActivityRow[]>([]);
  const [commitsOpen, setCommitsOpen] = useState(false);
  const [commitsLoaded, setCommitsLoaded] = useState(false);
  const [loadError, setLoadError] = useState(false);

  const vaultHealth = useVaultHealth(name);
  // Same shape as the global header: `pending` from the backend includes
  // retry-exhausted (abandoned) chunks, so subtract them to get the
  // "actively indexing" count and surface abandoned separately.
  const vUpsert = vaultHealth?.vector_store?.backfill?.upsert;
  const vaultAbandoned: number = vUpsert?.abandoned || 0;
  const vaultPending: number | null = vaultHealth
    ? Math.max(
        0,
        (vUpsert?.pending || 0) - vaultAbandoned,
      ) + (vaultHealth.metadata_backfill?.pending || 0)
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

  useEffect(() => {
    if (!name) return;
    let alive = true;
    // Reset stale state from previous param before re-fetch resolves.
    setInfo(null);
    setCounts(null);
    setRecent([]);
    setActivity([]);
    setCommitsLoaded(false);
    setCommitsOpen(false);
    setLoadError(false);
    // A failed overview load must surface (not read as an empty vault); a
    // fast vault switch must not clobber the newer vault (alive guard).
    getVaultInfo(name)
      .then((d) => alive && setInfo(d))
      .catch(() => alive && setLoadError(true));
    getRecent(name, 12)
      .then((d) => alive && setRecent(d.changes || []))
      .catch(() => alive && setLoadError(true));
    browseVault(name, undefined, 2)
      .then((d) => {
        if (!alive) return;
        const items = d.items || [];
        setCounts({
          collections: items.filter((i: any) => i.type === "collection").length,
          documents: items.filter((i: any) => i.type === "document").length,
          tables: items.filter((i: any) => i.type === "table").length,
          files: items.filter((i: any) => i.type === "file").length,
        });
      })
      .catch(() => alive && setLoadError(true));
    return () => { alive = false; };
  }, [name]);

  async function ensureCommitsLoaded(vault: string) {
    if (commitsLoaded) return;
    // Use the api() layer (401 redirect + ApiError) instead of a raw fetch.
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

  return (
    <div className="fade-up">
      {loadError && (
        <Alert variant="destructive" className="mb-4">
          Couldn't load this vault's overview. Reload the page to retry.
        </Alert>
      )}
      {/* Mono meta line (.coord already uppercases — no JS .toUpperCase()) */}
      <div className="coord mb-3">
        VAULT · {name} · akb://{name}
      </div>

      {/* Display title */}
      <h1 className="font-display text-[44px] leading-[1.0] tracking-tight text-foreground mb-3">
        {name}<span className="text-accent">.</span>
      </h1>

      {info?.description && (
        <p className="text-[16px] leading-[1.55] text-foreground-muted mb-1 max-w-2xl">
          {info.description}
        </p>
      )}

      {/* Meta badges row */}
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
        <div className="ml-auto flex items-baseline gap-4">
          {(info?.role === "writer" ||
            info?.role === "admin" ||
            info?.role === "owner") && (
            <Link
              to={`/vault/${name}/doc/new`}
              className="inline-flex items-baseline gap-1.5 coord hover:text-link transition-colors focus:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2 focus-visible:ring-offset-background"
            >
              <FilePlus className="h-3 w-3 self-center" aria-hidden />
              NEW DOC
            </Link>
          )}
          <Link
            to={`/vault/${name}/members`}
            className="inline-flex items-baseline gap-1.5 coord hover:text-link transition-colors focus:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2 focus-visible:ring-offset-background"
          >
            <Users className="h-3 w-3 self-center" aria-hidden />
            MEMBERS
            {info?.member_count !== undefined && (
              <span className="tabular-nums">[{info.member_count}]</span>
            )}
          </Link>
          {info?.role === "owner" && (
            <Link
              to={`/vault/${name}/settings`}
              className="inline-flex items-baseline gap-1.5 coord hover:text-link transition-colors focus:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2 focus-visible:ring-offset-background"
            >
              <SettingsIcon className="h-3 w-3 self-center" aria-hidden />
              SETTINGS
            </Link>
          )}
        </div>
      </div>

      {/* Ledger — 4-stat tiles */}
      <div className="mt-10 grid grid-cols-2 sm:grid-cols-4 gap-3">
        {counts &&
          (
            [
              ["collections", counts.collections, "dirs"],
              ["documents", counts.documents, "md"],
              ["tables", counts.tables, "rows"],
              ["files", counts.files, "bytes"],
            ] as Array<[string, number, string]>
          ).map(([label, value, kind]) => (
            <StatTile key={label} label={label} value={value} kind={kind} />
          ))}
      </div>

      {/* Recent writes — primary */}
      <section className="mt-10" aria-labelledby="recent-heading">
        <div className="flex items-baseline gap-3 pb-3 border-b border-border mb-3">
          <span id="recent-heading" className="coord-ink">§ RECENT</span>
          <span className="coord tabular-nums">[{recent.length}]</span>
        </div>

        {recent.length === 0 ? (
          <EmptyState
            title="No activity yet"
            description="Documents written via agent will appear here."
          />
        ) : (
          <ol className="rounded-[var(--radius-lg)] border border-border bg-surface divide-y divide-border overflow-hidden shadow-sm">
            {recent.map((c, i) => (
              <li key={`${c.doc_id}:${c.commit ?? ""}:${i}`}>
                <Link
                  to={
                    `/vault/${name}/doc/${encodeURIComponent(c.path || c.doc_id)}` +
                    (c.commit ? `?commit=${encodeURIComponent(c.commit)}` : "")
                  }
                  className="group grid grid-cols-[32px_1fr_auto] items-baseline gap-4 px-3 py-2 hover:bg-surface-hover transition-colors focus:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2 focus-visible:ring-offset-background"
                >
                  <span className="coord tabular-nums">
                    {String(i + 1).padStart(2, "0")}
                  </span>
                  <div className="min-w-0">
                    <div title={c.title} className="text-sm font-medium tracking-tight truncate text-foreground group-hover:text-link">
                      {c.title}
                    </div>
                    <div title={c.path} className="coord truncate">{c.path}</div>
                  </div>
                  <div className="flex items-baseline gap-3 shrink-0">
                    <span className="coord font-mono tabular-nums">
                      {c.commit?.slice(0, 7)}
                    </span>
                    <span className="coord tabular-nums w-[52px] text-right">
                      {timeAgo(c.changed_at)}
                    </span>
                  </div>
                </Link>
              </li>
            ))}
          </ol>
        )}
      </section>

      {/* Commit log — collapsible (secondary detail) */}
      <section className="mt-8 mb-10" aria-labelledby="commit-log-heading">
        {/* Disclosure button + the FULL ACTIVITY link are SIBLINGS — a <Link>
            nested inside a <button> is invalid HTML and breaks keyboard nav. */}
        <div className="flex items-center gap-2 py-2">
          <button
            onClick={toggleCommits}
            aria-expanded={commitsOpen}
            aria-controls="commit-log-list"
            className="flex flex-1 items-center gap-2 text-left text-foreground-muted hover:text-foreground transition-colors rounded-[var(--radius-sm)] focus:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2 focus-visible:ring-offset-background"
          >
            {commitsOpen ? (
              <ChevronDown className="h-3 w-3" aria-hidden />
            ) : (
              <ChevronRight className="h-3 w-3" aria-hidden />
            )}
            <span id="commit-log-heading" className="coord-ink">§ COMMIT LOG</span>
            {commitsLoaded && (
              <span className="coord tabular-nums">[{activity.length}]</span>
            )}
            <span className="coord ml-auto">HEAD · MAIN</span>
          </button>
          <Link
            to={`/vault/${name}/activity`}
            className="coord shrink-0 hover:text-link transition-colors rounded-[var(--radius-sm)] focus:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2 focus-visible:ring-offset-background"
          >
            ↗ FULL ACTIVITY
          </Link>
        </div>

        {commitsOpen && (
          <div
            id="commit-log-list"
            className="mt-2 rounded-[var(--radius-lg)] border border-border bg-surface p-3 overflow-x-auto shadow-sm"
          >
            {!commitsLoaded ? (
              <div className="coord" role="status" aria-live="polite">LOADING…</div>
            ) : activity.length === 0 ? (
              <div className="coord" role="status">NO COMMITS</div>
            ) : (
              <ol className="font-mono text-[11px] leading-[1.9]">
                {activity.map((c, i) => {
                  const filePath = c.files?.[0]?.path || c.summary || "";
                  const filesCount = c.files?.length || 0;
                  const change = c.files?.[0]?.change;
                  const primaryDocPath = c.files?.[0]?.path;
                  const link = primaryDocPath
                    ? `/vault/${name}/doc/${encodeURIComponent(primaryDocPath)}` +
                      (c.hash ? `?commit=${encodeURIComponent(c.hash)}` : "")
                    : `/vault/${name}`;
                  return (
                    <li key={i}>
                      <Link
                        to={link}
                        className="group grid grid-cols-[70px_140px_1fr_auto_54px] gap-3 py-1 items-baseline hover:bg-surface-hover -mx-2 px-2 transition-colors focus:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2 focus-visible:ring-offset-surface"
                      >
                        <span className="text-foreground-muted">{(c.hash || "").slice(0, 7)}</span>
                        <span title={c.agent || c.author || "unknown"} className="text-foreground truncate">
                          <span className="text-info">◆ </span>
                          {c.agent || c.author || "unknown"}
                        </span>
                        <span title={c.subject || filePath} className="text-foreground truncate">
                          {c.subject || filePath}
                          {filesCount > 1 && (
                            <span className="text-foreground-muted"> · +{filesCount - 1}</span>
                          )}
                        </span>
                        <span className="text-foreground-muted text-[10px] tabular-nums text-right">
                          {change || ""}
                        </span>
                        <span className="text-foreground-muted text-[10px] tabular-nums text-right">
                          {timeAgo(c.timestamp)}
                        </span>
                      </Link>
                    </li>
                  );
                })}
              </ol>
            )}
          </div>
        )}
      </section>
    </div>
  );
}
