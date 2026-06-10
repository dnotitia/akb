import { useEffect, useRef, useState } from "react";
import { Link } from "react-router-dom";
import { ArrowRight, File as FileIcon, FileText, Table as TableIcon } from "lucide-react";
import { getVaultInfo } from "@/lib/api";
import { Panel } from "@/components/ui/panel";
import { Badge } from "@/components/ui/badge";
import { RoleBadge } from "@/components/status-badge";
import { timeAgo } from "@/lib/utils";

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

// Cap concurrent /vaults/{v}/info calls — each one fans out into ~10 pooled
// COUNT queries server-side, so an unbounded forEach risks pool exhaustion.
const VAULT_INFO_CONCURRENCY = 5;

/**
 * The shared vault directory list — rounded rows with name/description, content
 * counts, last-activity, role badge, and an Open affordance. Owns the bounded
 * per-vault /info enrichment (fetched once per name, skipped if already known),
 * so both the Home preview and the /vault index render an identical, live list.
 */
export function VaultList({ vaults }: { vaults: VaultRow[] }) {
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
            <li key={v.id}>
              <Link
                to={`/vault/${v.name}`}
                className="group grid grid-cols-[minmax(0,1fr)_auto] items-baseline gap-x-4 gap-y-1 px-4 py-3 bg-surface hover:bg-surface-muted transition-colors focus:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2 focus-visible:ring-offset-background"
              >
                <div className="min-w-0 pr-4">
                  <div className="flex items-baseline gap-2 flex-wrap mb-1">
                    <span className="font-mono text-base font-semibold text-foreground group-hover:text-primary transition-colors">
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
                <div className="flex items-center gap-3 shrink-0 self-baseline">
                  <VaultStatsCell m={m} />
                  <span className="coord tabular-nums whitespace-nowrap w-[56px] text-right">
                    {lastActivity ? timeAgo(lastActivity) : "—"}
                  </span>
                  {v.role && <RoleBadge role={v.role} />}
                  <ArrowRight
                    className="h-4 w-4 shrink-0 text-foreground-muted opacity-40 group-hover:opacity-100 group-hover:translate-x-0.5 group-hover:text-primary transition-all"
                    aria-hidden
                  />
                </div>
              </Link>
            </li>
          );
        })}
      </ol>
    </Panel>
  );
}

/**
 * Compact stats cell: icon + count per non-empty category. A skeleton bar while
 * metrics load (stable row width, no pop-in); "—" when the vault has no content.
 */
function VaultStatsCell({ m }: { m?: VaultMetrics }) {
  if (!m) {
    return (
      <span className="h-3 w-12 rounded bg-surface-muted animate-pulse self-baseline" aria-hidden />
    );
  }
  const d = m.document_count ?? 0;
  const t = m.table_count ?? 0;
  const f = m.file_count ?? 0;
  const title = `${d} document${d === 1 ? "" : "s"} · ${t} table${t === 1 ? "" : "s"} · ${f} file${f === 1 ? "" : "s"}`;
  if (d + t + f === 0) {
    return (
      <span className="coord tabular-nums whitespace-nowrap self-baseline" title={title}>
        —
      </span>
    );
  }
  return (
    <span
      className="coord tabular-nums whitespace-nowrap self-baseline inline-flex items-center gap-2"
      title={title}
    >
      {d > 0 && (
        <span className="inline-flex items-center gap-1">
          <FileText className="h-3 w-3" aria-hidden />
          {d}
        </span>
      )}
      {t > 0 && (
        <span className="inline-flex items-center gap-1">
          <TableIcon className="h-3 w-3" aria-hidden />
          {t}
        </span>
      )}
      {f > 0 && (
        <span className="inline-flex items-center gap-1">
          <FileIcon className="h-3 w-3" aria-hidden />
          {f}
        </span>
      )}
    </span>
  );
}
