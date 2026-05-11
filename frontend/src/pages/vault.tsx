import { useEffect, useState } from "react";
import { Link, useParams } from "react-router-dom";
import { ChevronDown, ChevronRight, Settings as SettingsIcon, Users } from "lucide-react";
import { browseVault, getRecent, getVaultInfo } from "@/lib/api";
import { timeAgo } from "@/lib/utils";
import { EmptyState } from "@/components/empty-state";
import { IndexingBadge, RoleBadge, VaultStateBadge } from "@/components/status-badge";
import { useVaultHealth } from "@/hooks/use-vault-health";

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

  useEffect(() => {
    if (!name) return;
    getVaultInfo(name).then(setInfo).catch(() => {});
    getRecent(name, 12).then((d) => setRecent(d.changes || [])).catch(() => {});
    browseVault(name, undefined, 2)
      .then((d) => {
        const items = d.items || [];
        setCounts({
          collections: items.filter((i: any) => i.type === "collection").length,
          documents: items.filter((i: any) => i.type === "document").length,
          tables: items.filter((i: any) => i.type === "table").length,
          files: items.filter((i: any) => i.type === "file").length,
        });
      })
      .catch(() => {});
  }, [name]);

  async function ensureCommitsLoaded(vault: string) {
    if (commitsLoaded) return;
    const t = localStorage.getItem("akb_token") || "";
    try {
      const r = await fetch(`/api/v1/activity/${vault}?limit=20`, {
        headers: { Authorization: `Bearer ${t}` },
      });
      const d = await r.json();
      setActivity(d.activity || []);
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
    <div className="fade-up max-w-[1280px] mx-auto">
      {/* Mono meta line */}
      <div className="coord mb-3">
        VAULT · {name?.toUpperCase()} · akb://{name}
      </div>

      {/* Serif display title */}
      <h1 className="font-serif text-[54px] leading-[0.95] tracking-[-0.03em] text-foreground mb-3">
        {name}<span className="text-foreground-muted">.</span>
      </h1>

      {info?.description && (
        <p className="font-serif-italic text-[17px] leading-[1.55] text-foreground-muted mb-1">
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
        <div className="ml-auto flex items-baseline gap-4">
          <Link
            to={`/vault/${name}/members`}
            className="inline-flex items-baseline gap-1.5 coord hover:text-accent transition-colors focus:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2 focus-visible:ring-offset-background"
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
              className="inline-flex items-baseline gap-1.5 coord hover:text-accent transition-colors focus:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2 focus-visible:ring-offset-background"
            >
              <SettingsIcon className="h-3 w-3 self-center" aria-hidden />
              SETTINGS
            </Link>
          )}
        </div>
      </div>

      {/* Ledger — 4-stat strip */}
      <div className="mt-10 pt-6 border-t border-border">
        <div className="grid grid-cols-4">
          {counts &&
            (
              [
                ["collections", counts.collections, "dirs"],
                ["documents", counts.documents, "md"],
                ["tables", counts.tables, "rows"],
                ["files", counts.files, "bytes"],
              ] as Array<[string, number, string]>
            ).map(([label, value, kind], i) => (
              <div
                key={label}
                className={i < 3 ? "pr-6 border-r border-border" : "pl-6"}
                style={i > 0 ? { paddingLeft: "1.25rem" } : undefined}
              >
                <div className="coord mb-2">{label.toUpperCase()}</div>
                <div className="font-serif text-[36px] leading-none tabular-nums text-foreground mb-2">
                  {String(value).padStart(2, "0")}
                </div>
                <div className="coord">{kind}</div>
              </div>
            ))}
        </div>
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
          <ol className="border border-border bg-surface divide-y divide-border">
            {recent.map((c, i) => (
              <li key={c.doc_id + c.changed_at}>
                <Link
                  to={`/vault/${name}/doc/${encodeURIComponent(c.path || c.doc_id)}`}
                  className="group grid grid-cols-[32px_1fr_auto] items-baseline gap-4 px-3 py-2 hover:bg-surface-muted transition-colors focus:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2 focus-visible:ring-offset-background"
                >
                  <span className="coord tabular-nums">
                    {String(i + 1).padStart(2, "0")}
                  </span>
                  <div className="min-w-0">
                    <div className="text-sm font-medium tracking-tight truncate text-foreground group-hover:text-accent">
                      {c.title}
                    </div>
                    <div className="coord truncate">{c.path}</div>
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
        <button
          onClick={toggleCommits}
          aria-expanded={commitsOpen}
          aria-controls="commit-log-list"
          className="w-full flex items-center gap-2 py-2 text-left text-foreground-muted hover:text-foreground transition-colors focus:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2 focus-visible:ring-offset-background"
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
          <Link
            to={`/vault/${name}/activity`}
            onClick={(e) => e.stopPropagation()}
            className="coord hover:text-accent transition-colors focus:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2 focus-visible:ring-offset-background"
          >
            ↗ FULL ACTIVITY
          </Link>
        </button>

        {commitsOpen && (
          <div
            id="commit-log-list"
            className="mt-2 border border-border bg-surface p-3 overflow-x-auto"
          >
            {!commitsLoaded ? (
              <div className="coord">LOADING…</div>
            ) : activity.length === 0 ? (
              <div className="coord">NO COMMITS</div>
            ) : (
              <ol className="font-mono text-[11px] leading-[1.9]">
                {activity.map((c, i) => {
                  const filePath = c.files?.[0]?.path || c.summary || "";
                  const filesCount = c.files?.length || 0;
                  const change = c.files?.[0]?.change;
                  const primaryDocPath = c.files?.[0]?.path;
                  const link = primaryDocPath
                    ? `/vault/${name}/doc/${encodeURIComponent(primaryDocPath)}`
                    : `/vault/${name}`;
                  return (
                    <li key={i}>
                      <Link
                        to={link}
                        className="group grid grid-cols-[70px_140px_1fr_auto_54px] gap-3 py-1 items-baseline hover:bg-surface-muted -mx-2 px-2 transition-colors focus:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2 focus-visible:ring-offset-surface"
                      >
                        <span className="text-accent">{(c.hash || "").slice(0, 7)}</span>
                        <span className="text-foreground truncate">
                          <span className="text-info">◆ </span>
                          {c.agent || c.author || "unknown"}
                        </span>
                        <span className="text-foreground truncate">
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
