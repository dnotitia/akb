import { useState } from "react";
import { Alert } from "@/components/ui/alert";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { TooltipText } from "@/components/ui/tooltip-text";
import {
  getPublication,
  publicationCsvUrl,
  type PublicationResponse,
} from "@/lib/api";

// Cap rendered rows so a large shared result set can't mount millions of DOM
// nodes; the full set is available via the CSV download.
const ROW_RENDER_CAP = 500;

interface Props {
  slug: string;
  initialData: PublicationResponse;
}

export function TableViewer({ slug, initialData }: Props) {
  const [data, setData] = useState<PublicationResponse>(initialData);
  const [params, setParams] = useState<Record<string, string>>(() => {
    const init: Record<string, string> = {};
    if (initialData.query_params) {
      for (const [k, v] of Object.entries(initialData.query_params)) {
        if (v.default !== undefined && v.default !== null) {
          init[k] = String(v.default);
        }
      }
    }
    const urlParams = new URLSearchParams(window.location.search);
    urlParams.forEach((value, key) => {
      if (key !== "format" && key !== "token" && key !== "password") {
        init[key] = value;
      }
    });
    return init;
  });
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");

  async function reload() {
    setLoading(true);
    setError("");
    try {
      const result = await getPublication(slug, params);
      setData(result);
      const url = new URL(window.location.href);
      for (const [k, v] of Object.entries(params)) {
        if (v) url.searchParams.set(k, v);
      }
      window.history.replaceState({}, "", url.toString());
    } catch (e: any) {
      setError(e.message || "Query failed");
    } finally {
      setLoading(false);
    }
  }

  const paramDefs = data.query_params || {};
  const hasParams = Object.keys(paramDefs).length > 0;
  const total = data.total ?? data.rows?.length ?? 0;
  const cols = data.columns || [];

  return (
    <div className="grid grid-cols-1 lg:grid-cols-[180px_1fr] gap-8">
      {/* Left rail */}
      <aside className="lg:sticky lg:top-8 lg:self-start space-y-5">
        <div>
          <div className="coord mb-1">Type</div>
          <div className="text-sm font-medium">table query</div>
        </div>
        <div>
          <div className="coord mb-1">Mode</div>
          <div className="text-sm font-medium flex items-center gap-1">
            {data.mode === "snapshot" ? (
              <>
                <span className="text-info" aria-hidden>⊛</span> snapshot
              </>
            ) : (
              <>
                <span className="text-success" aria-hidden>●</span> live
              </>
            )}
          </div>
          {data.mode === "snapshot" && data.snapshot_at && (
            <div className="coord mt-1">
              @ {new Date(data.snapshot_at).toLocaleString("en-US")}
            </div>
          )}
        </div>
        <div>
          <div className="coord mb-1">Rows</div>
          <div className="font-display-tight text-3xl text-foreground">
            {String(total).padStart(2, "0")}
          </div>
        </div>
        <div>
          <div className="coord mb-1">Columns</div>
          <div className="font-display-tight text-3xl text-foreground">
            {String(cols.length).padStart(2, "0")}
          </div>
        </div>
        <div className="pt-3 border-t border-border space-y-2">
          <a
            href={publicationCsvUrl(slug, params)}
            download
            className="block coord hover:text-link rounded-[var(--radius-sm)] focus:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2 focus-visible:ring-offset-background"
          >
            ↓ Download CSV
          </a>
          <button
            onClick={reload}
            disabled={loading}
            className="block coord hover:text-link text-left disabled:opacity-50 rounded-[var(--radius-sm)] focus:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2 focus-visible:ring-offset-background"
          >
            {loading ? "↻ Loading…" : "↻ Refresh"}
          </button>
          <span className="sr-only" role="status" aria-live="polite">{loading ? "Loading results" : ""}</span>
        </div>
      </aside>

      {/* Main column */}
      <div className="min-w-0">
        <div className="coord-spark mb-4">Table query</div>
        <h1 className="font-display text-3xl font-semibold tracking-tight text-foreground mb-8">
          {data.title || "Query results"}
        </h1>

        {hasParams && (
          <div className="rounded-[var(--radius-lg)] border border-border overflow-hidden shadow-sm mb-6">
            <div className="border-b border-border bg-surface-2 px-4 py-2 flex items-baseline justify-between">
              <span className="coord-ink">Parameters</span>
              <span className="coord">[{Object.keys(paramDefs).length}]</span>
            </div>
            <form
              className="p-4 flex flex-wrap gap-3 items-end"
              onSubmit={(e) => { e.preventDefault(); reload(); }}
            >
              {Object.entries(paramDefs).map(([name, def]) => (
                <div key={name} className="flex flex-col gap-1">
                  <Label htmlFor={`param-${name}`} className="coord">
                    {name} ({def.type || "text"})
                    {def.required && <span className="text-accent-strong ml-1" aria-label="required">*</span>}
                  </Label>
                  <Input
                    id={`param-${name}`}
                    type={def.type === "number" || def.type === "int" ? "number" : "text"}
                    value={params[name] ?? ""}
                    onChange={(e) =>
                      setParams((p) => ({ ...p, [name]: e.target.value }))
                    }
                    placeholder={def.default !== undefined ? String(def.default) : ""}
                    className="w-44 h-9 font-mono text-xs"
                  />
                </div>
              ))}
              <Button type="submit" variant="accent" loading={loading} className="h-9">
                {loading ? "Running…" : "Apply"}
              </Button>
            </form>
          </div>
        )}

        {error && (
          <Alert variant="destructive" title="Query failed" className="mb-6">{error}</Alert>
        )}

        {/* Table */}
        <div className="rounded-[var(--radius-lg)] border border-border overflow-hidden shadow-sm overflow-x-auto">
          <table className="w-full text-sm" aria-label={data.title || "Query results"}>
            <thead className="bg-surface-2 text-foreground">
              <tr>
                <th scope="col" className="coord-ink px-3 py-2 text-left border-r border-border">
                  #
                </th>
                {cols.map((c) => (
                  <th
                    key={c}
                    scope="col"
                    className="px-3 py-2 text-left font-mono text-[11px] tracking-wide text-foreground-muted border-r border-border last:border-r-0 whitespace-nowrap"
                  >
                    {c}
                  </th>
                ))}
              </tr>
            </thead>
            <tbody>
              {(data.rows || []).slice(0, ROW_RENDER_CAP).map((row, i) => (
                <tr
                  key={i}
                  className="border-t border-border hover:bg-surface-hover transition-colors"
                >
                  <td className="coord px-3 py-2 border-r border-border tabular-nums">
                    {String(i + 1).padStart(2, "0")}
                  </td>
                  {cols.map((c) => (
                    <TooltipText key={c} asChild tip={formatCellFull(row[c])}>
                      <td
                        className={`px-3 py-2 font-mono text-[12px] border-r border-border last:border-r-0 whitespace-nowrap max-w-xs truncate ${typeof row[c] === "number" ? "tabular-nums" : ""}`}
                      >
                        {formatCell(row[c])}
                      </td>
                    </TooltipText>
                  ))}
                </tr>
              ))}
              {(!data.rows || data.rows.length === 0) && (
                <tr>
                  <td
                    colSpan={cols.length + 1}
                    className="px-3 py-12 text-center coord"
                  >
                    — no rows —
                  </td>
                </tr>
              )}
            </tbody>
          </table>
        </div>
        {(data.rows?.length || 0) > ROW_RENDER_CAP && (
          <p className="coord mt-3">
            Showing first {ROW_RENDER_CAP} of {total} rows — download the CSV for the full result.
          </p>
        )}
      </div>
    </div>
  );
}

function formatCellFull(v: any): string {
  if (v === null || v === undefined) return "—";
  if (typeof v === "object") return JSON.stringify(v);
  return String(v);
}

// Display cap — a single huge cell shouldn't hold a multi-KB text node; the
// full value is still available via the cell's title tooltip.
function formatCell(v: any): string {
  const s = formatCellFull(v);
  return s.length > 200 ? s.slice(0, 200) + "…" : s;
}
