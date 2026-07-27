import { useEffect, useRef, useState } from "react";
import { Link } from "react-router-dom";
import { ArrowRight, File as FileIcon, FileText, Star, Table as TableIcon } from "lucide-react";
import { getVaultInfo } from "@/lib/api";
import { Panel } from "@/components/ui/panel";
import { Badge } from "@/components/ui/badge";
import { VaultChip } from "@/components/ui/vault-chip";
import { RoleBadge } from "@/components/status-badge";
import { RelativeTime } from "@/components/ui/relative-time";
import { recentTone } from "@/lib/recent";

export interface VaultRow {
  id: string;
  name: string;
  description?: string;
  role?: "owner" | "admin" | "writer" | "reader";
  status?: string;
}

interface VaultMetrics {
  document_count?: number;
  table_count?: number;
  file_count?: number;
  last_activity?: string;
}

/**
 * Optional favorite affordance. Capability-shaped (not a `showFavorite`
 * boolean) so <VaultList> stays decoupled from where favorites live — the
 * owner (Home) supplies the read + toggle, and a future server-synced
 * `is_pinned` provider is invisible to this row UI.
 */
export interface VaultFavoriteControl {
  isFavorite: (id: string) => boolean;
  onToggle: (vault: VaultRow) => void;
}

// Cap concurrent /vaults/{v}/info calls — each one fans out into ~10 pooled
// COUNT queries server-side, so an unbounded forEach risks pool exhaustion.
const VAULT_INFO_CONCURRENCY = 5;

/**
 * The shared vault directory list — rounded rows with name/description, content
 * counts, last-activity, role badge, and an Open affordance. Owns the bounded
 * per-vault /info enrichment (fetched once per name, skipped if already known),
 * so both the Home preview and the /vault index render an identical, live list.
 */
export function VaultList({
  vaults,
  favoriteControl,
}: {
  vaults: VaultRow[];
  favoriteControl?: VaultFavoriteControl;
}) {
  const [metrics, setMetrics] = useState<Record<string, VaultMetrics>>({});
  const fetched = useRef<Set<string>>(new Set());

  useEffect(() => {
    let cancelled = false;
    const todo = vaults.filter((v) => !fetched.current.has(v.name));
    if (todo.length === 0) return;
    todo.forEach((v) => fetched.current.add(v.name));
    void (async () => {
      for (let i = 0; i < todo.length; i += VAULT_INFO_CONCURRENCY) {
        if (cancelled) return;
        await Promise.all(
          todo.slice(i, i + VAULT_INFO_CONCURRENCY).map((v) =>
            getVaultInfo(v.name)
              .then((info) => {
                if (cancelled) return;
                setMetrics((prev) => ({
                  ...prev,
                  [v.name]: {
                    document_count: info?.document_count,
                    table_count: info?.table_count,
                    file_count: info?.file_count,
                    last_activity: info?.last_activity,
                  },
                }));
              })
              .catch(() => {}),
          ),
        );
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [vaults]);

  return (
    <Panel className="mt-3">
      <ol className="divide-y divide-border stagger">
        {vaults.map((v) => {
          const m = metrics[v.name];
          const lastActivity = m?.last_activity;
          return (
            <li
              key={v.id}
              className="group relative flex items-stretch bg-surface hover:bg-surface-muted transition-colors"
            >
              <Link
                to={`/vault/${v.name}`}
                className="grid grid-cols-[auto_minmax(0,1fr)_auto] items-center gap-x-3 gap-y-1 flex-1 min-w-0 px-4 py-3 focus:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-inset"
              >
                <VaultChip name={v.name} size="md" />
                <div className="min-w-0 pr-4">
                  <div className="flex items-baseline gap-2 flex-wrap mb-0.5">
                    <span className="text-base font-semibold text-foreground group-hover:text-primary transition-colors">
                      {v.name}
                    </span>
                    {v.status === "archived" && <Badge variant="archived">archived</Badge>}
                  </div>
                  {v.description && (
                    <p
                      className="text-xs text-foreground-muted leading-relaxed line-clamp-1"
                      title={v.description}
                    >
                      {v.description}
                    </p>
                  )}
                </div>
                <div className="flex items-center gap-3 shrink-0">
                  <VaultStatsCell m={m} />
                  {m ? (
                    <RelativeTime
                      iso={lastActivity}
                      fallback="—"
                      className="w-[56px] justify-end text-right whitespace-nowrap"
                    />
                  ) : (
                    <span
                      className="h-3 w-[56px] rounded bg-surface-muted animate-pulse"
                      aria-hidden
                    />
                  )}
                  {v.role && <RoleBadge role={v.role} />}
                  <ArrowRight
                    className="h-4 w-4 shrink-0 text-foreground-muted opacity-40 group-hover:opacity-100 group-hover:translate-x-0.5 group-hover:text-primary transition-all"
                    aria-hidden
                  />
                </div>
              </Link>
              {favoriteControl && (
                <FavoriteStar
                  name={v.name}
                  favorite={favoriteControl.isFavorite(v.id)}
                  onToggle={() => favoriteControl.onToggle(v)}
                />
              )}
            </li>
          );
        })}
      </ol>
    </Panel>
  );
}

/**
 * Favorite toggle — a sibling of the row's <Link> (never nested, so a click
 * pins/unpins instead of navigating). Kept visible-but-muted rather than
 * hover-only so it works on touch; the star fills + goes primary when pinned.
 * 36×36px target meets the design-system icon-button floor.
 */
function FavoriteStar({
  name,
  favorite,
  onToggle,
}: {
  name: string;
  favorite: boolean;
  onToggle: () => void;
}) {
  return (
    <button
      type="button"
      onClick={onToggle}
      aria-pressed={favorite}
      aria-label={favorite ? `Remove ${name} from favorites` : `Add ${name} to favorites`}
      className={`mr-2 grid h-9 w-9 shrink-0 self-center place-items-center rounded-[var(--radius-sm)] transition-colors focus:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2 focus-visible:ring-offset-background ${
        favorite ? "text-primary" : "text-foreground-muted hover:text-foreground"
      }`}
    >
      <Star className={`h-4 w-4 ${favorite ? "fill-current" : ""}`} aria-hidden />
    </button>
  );
}

/**
 * Compact stats cell: an icon/count per non-empty category (document/table/
 * file), with the full breakdown in the cell's tooltip. A skeleton bar while
 * metrics load keeps the row width stable (no pop-in).
 */
function VaultStatsCell({ m }: { m?: VaultMetrics }) {
  if (!m) {
    return (
      <span className="h-3 w-[56px] rounded bg-surface-muted animate-pulse" aria-hidden />
    );
  }
  const d = m.document_count ?? 0;
  const t = m.table_count ?? 0;
  const f = m.file_count ?? 0;
  const title = `${d} document${d === 1 ? "" : "s"} · ${t} table${t === 1 ? "" : "s"} · ${f} file${f === 1 ? "" : "s"}`;
  return (
    <span
      className="coord tabular-nums whitespace-nowrap inline-flex items-center gap-2"
      title={title}
      role="img"
      aria-label={title}
    >
      {d + t + f === 0 ? (
        <span className="text-foreground-muted">—</span>
      ) : (
        <>
          {d > 0 && (
            <span className="inline-flex items-center gap-1">
              <FileText className="h-3 w-3" style={{ color: recentTone("document") }} aria-hidden />
              {d}
            </span>
          )}
          {t > 0 && (
            <span className="inline-flex items-center gap-1">
              <TableIcon className="h-3 w-3" style={{ color: recentTone("table") }} aria-hidden />
              {t}
            </span>
          )}
          {f > 0 && (
            <span className="inline-flex items-center gap-1">
              <FileIcon className="h-3 w-3" style={{ color: recentTone("file") }} aria-hidden />
              {f}
            </span>
          )}
        </>
      )}
    </span>
  );
}
