import { useEffect, useState } from "react";
import { useParams } from "react-router-dom";
import { EmptyState } from "@/components/empty-state";

interface Column {
  name: string;
  type?: string;
  required?: boolean;
  primary_key?: boolean;
}
interface TableInfo {
  name: string;
  description?: string;
  row_count: number;
  columns: Column[];
}

export default function TablePage() {
  const { name: vault, table } = useParams<{ name: string; table: string }>();
  const [info, setInfo] = useState<TableInfo | null>(null);
  const [rows, setRows] = useState<Record<string, any>[]>([]);
  const [cols, setCols] = useState<string[]>([]);
  const [total, setTotal] = useState(0);
  const [error, setError] = useState("");
  const [limit] = useState(50);

  useEffect(() => {
    if (!vault || !table) return;
    const t = localStorage.getItem("akb_token") || "";
    fetch(`/api/v1/tables/${vault}`, { headers: { Authorization: `Bearer ${t}` } })
      .then((r) => r.json())
      .then((d) => {
        const found = (d.tables || []).find((x: any) => x.name === table);
        if (found) setInfo(found);
      })
      .catch(() => {});
    fetch(`/api/v1/tables/${vault}/sql`, {
      method: "POST",
      headers: { "Content-Type": "application/json", Authorization: `Bearer ${t}` },
      body: JSON.stringify({ sql: `SELECT * FROM ${table} LIMIT ${limit}` }),
    })
      .then((r) => r.json())
      .then((d) => {
        if (d.error || d.detail) {
          setError(d.error || d.detail);
          return;
        }
        setRows(d.rows || []);
        setCols(d.columns || []);
        setTotal(d.total ?? d.rows?.length ?? 0);
      })
      .catch((e) => setError(String(e)));
  }, [vault, table, limit]);

  return (
    <div className="min-w-0 fade-up max-w-[1280px] mx-auto">
      <div className="coord mb-3">
        VAULT · {vault?.toUpperCase()} · TABLE · {table?.toUpperCase()}
      </div>

      <header className="flex items-baseline justify-between flex-wrap gap-x-4 gap-y-2 pb-3 border-b border-border">
        <h1 className="font-mono text-[28px] font-semibold tracking-tight text-foreground break-all min-w-0">
          {table}
        </h1>
        <div className="flex items-center gap-4 coord tabular-nums shrink-0">
          {info && (
            <>
              <span>
                <span className="text-foreground font-medium">{info.row_count}</span> rows
              </span>
              <span>
                <span className="text-foreground font-medium">{info.columns?.length || 0}</span> cols
              </span>
            </>
          )}
          <span>Preview {limit}</span>
        </div>
      </header>

      {info?.description && (
        <p className="font-serif-italic text-[17px] leading-[1.55] text-foreground-muted mt-3">
          {info.description}
        </p>
      )}

      {/* Schema — compact inline list */}
      {info?.columns && info.columns.length > 0 && (
        <section className="mt-6" aria-labelledby="schema-heading">
          <div className="flex items-baseline gap-3 mb-2">
            <span id="schema-heading" className="coord-ink">§ SCHEMA</span>
            <span className="coord tabular-nums">[{info.columns.length}]</span>
          </div>
          <div className="flex flex-wrap gap-x-4 gap-y-1 text-xs font-mono">
            {info.columns.map((c) => (
              <div key={c.name} className="flex items-baseline gap-1.5">
                <span className="text-foreground font-medium">{c.name}</span>
                <span className="text-foreground-muted">{(c.type || "text").toLowerCase()}</span>
                {c.primary_key && <span className="coord-spark">PK</span>}
                {c.required && !c.primary_key && (
                  <span className="text-accent" aria-label="required">*</span>
                )}
              </div>
            ))}
          </div>
        </section>
      )}

      {error && (
        <div
          role="alert"
          aria-live="polite"
          className="border border-destructive p-3 mt-6"
        >
          <div className="coord-spark mb-1 text-destructive">⚠ QUERY FAILED</div>
          <p className="text-sm text-destructive font-mono">{error}</p>
        </div>
      )}

      {/* Rows preview */}
      <section className="mt-6" aria-labelledby="preview-heading">
        <div className="flex items-baseline gap-3 mb-2">
          <span id="preview-heading" className="coord-ink">§ PREVIEW</span>
          <span className="coord tabular-nums">
            {total} ROW{total === 1 ? "" : "S"}
            {info?.row_count && info.row_count > total ? ` of ${info.row_count}` : ""}
          </span>
        </div>

        {rows.length === 0 && !error ? (
          <EmptyState title="No rows" />
        ) : (
          <div
            role="region"
            aria-label={`Preview rows for ${table}`}
            tabIndex={0}
            className="border border-border bg-surface overflow-x-auto focus:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2 focus-visible:ring-offset-background"
          >
            <table className="w-full text-sm">
              <thead>
                <tr className="border-b border-border">
                  <th
                    scope="col"
                    className="px-3 py-2 text-left font-mono text-[10px] uppercase tracking-wider text-foreground-muted border-r border-border w-10"
                  >
                    #
                  </th>
                  {cols.map((c) => (
                    <th
                      key={c}
                      scope="col"
                      className="px-3 py-2 text-left font-mono text-[10px] uppercase tracking-wider text-foreground-muted border-r border-border last:border-r-0 whitespace-nowrap"
                    >
                      {c}
                    </th>
                  ))}
                </tr>
              </thead>
              <tbody className="divide-y divide-border">
                {rows.map((row, i) => (
                  <tr key={i} className="hover:bg-surface-muted transition-colors">
                    <td className="coord px-3 py-1.5 border-r border-border tabular-nums">
                      {String(i + 1).padStart(2, "0")}
                    </td>
                    {cols.map((c) => (
                      <td
                        key={c}
                        className="px-3 py-1.5 font-mono text-[12px] text-foreground border-r border-border last:border-r-0 whitespace-nowrap max-w-xs truncate tabular-nums"
                        title={String(row[c] ?? "")}
                      >
                        {formatCell(row[c])}
                      </td>
                    ))}
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </section>

      {total >= limit && info?.row_count && info.row_count > limit && (
        <p className="coord mt-3">
          Preview limited to {limit} rows · use <code className="font-mono text-foreground">akb_sql</code> via agent for full-range queries.
        </p>
      )}
    </div>
  );
}

function formatCell(v: any): string {
  if (v === null || v === undefined) return "—";
  if (typeof v === "number" && !Number.isFinite(v)) return "—";
  if (typeof v === "object") return JSON.stringify(v);
  const s = String(v);
  return s.length > 80 ? s.slice(0, 80) + "…" : s;
}
