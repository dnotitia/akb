import { useState } from "react";
import {
  getPublication,
  publicationCsvUrl,
  type PublicationResponse,
} from "@/lib/api";

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
          <div className="coord mb-1">TYPE</div>
          <div className="text-sm font-medium">table query</div>
        </div>
        <div>
          <div className="coord mb-1">MODE</div>
          <div className="text-sm font-medium flex items-center gap-1">
            {data.mode === "snapshot" ? (
              <>
                <span className="text-accent">⊛</span> snapshot
              </>
            ) : (
              <>
                <span className="text-accent">●</span> live
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
          <div className="coord mb-1">ROWS</div>
          <div className="font-display-tight text-3xl text-foreground">
            {String(total).padStart(2, "0")}
          </div>
        </div>
        <div>
          <div className="coord mb-1">COLUMNS</div>
          <div className="font-display-tight text-3xl text-foreground">
            {String(cols.length).padStart(2, "0")}
          </div>
        </div>
        <div className="pt-3 border-t border-border space-y-2">
          <a
            href={publicationCsvUrl(slug, params)}
            download
            className="block coord hover:text-accent"
          >
            ↓ DOWNLOAD CSV
          </a>
          <button
            onClick={reload}
            disabled={loading}
            className="block coord hover:text-accent text-left disabled:opacity-50"
          >
            {loading ? "↻ LOADING…" : "↻ REFRESH"}
          </button>
        </div>
      </aside>

      {/* Main column */}
      <div className="min-w-0">
        <div className="coord-spark mb-4">§ TABLE QUERY</div>
        <h1 className="font-display-tight text-5xl lg:text-7xl text-foreground leading-[0.95] tracking-tight mb-8">
          {data.title || "Query results"}
        </h1>

        {hasParams && (
          <div className="border border-border mb-6">
            <div className="border-b border-border px-4 py-2 flex items-baseline justify-between">
              <span className="coord-ink">§ PARAMETERS</span>
              <span className="coord">[{Object.keys(paramDefs).length}]</span>
            </div>
            <form
              className="p-4 flex flex-wrap gap-3 items-end"
              onSubmit={(e) => { e.preventDefault(); reload(); }}
            >
              {Object.entries(paramDefs).map(([name, def]) => (
                <div key={name} className="flex flex-col gap-1">
                  <label className="coord">
                    {name} ({def.type || "text"})
                    {def.required && <span className="text-accent ml-1">*</span>}
                  </label>
                  <input
                    type={def.type === "number" || def.type === "int" ? "number" : "text"}
                    value={params[name] ?? ""}
                    onChange={(e) =>
                      setParams((p) => ({ ...p, [name]: e.target.value }))
                    }
                    placeholder={def.default !== undefined ? String(def.default) : ""}
                    className="w-44 h-9 px-2 border border-border bg-transparent font-mono text-xs focus:outline-none focus:border-accent"
                  />
                </div>
              ))}
              <button
                type="submit"
                disabled={loading}
                className="h-9 px-4 border border-border bg-foreground text-background coord-ink hover:bg-accent hover:border-accent disabled:opacity-40"
                style={{ color: "var(--color-paper)" }}
              >
                {loading ? "RUNNING…" : "→ APPLY"}
              </button>
            </form>
          </div>
        )}

        {error && (
          <div className="border border-destructive p-3 mb-6">
            <div className="coord-spark mb-1" style={{ color: "var(--color-destructive)" }}>
              ⚠ QUERY FAILED
            </div>
            <p className="text-sm">{error}</p>
          </div>
        )}

        {/* Table */}
        <div className="border border-border overflow-x-auto">
          <table className="w-full text-sm">
            <thead className="bg-foreground text-background">
              <tr>
                <th className="coord-ink px-3 py-2 text-left border-r border-paper/20" style={{ color: "var(--color-paper)" }}>
                  #
                </th>
                {cols.map((c) => (
                  <th
                    key={c}
                    className="px-3 py-2 text-left font-mono text-[11px] uppercase tracking-wider border-r border-paper/20 last:border-r-0 whitespace-nowrap"
                  >
                    {c}
                  </th>
                ))}
              </tr>
            </thead>
            <tbody>
              {(data.rows || []).map((row, i) => (
                <tr
                  key={i}
                  className={`border-t border-border hover:bg-accent/5 transition-colors`}
                >
                  <td className="coord px-3 py-2 border-r border-border">
                    {String(i + 1).padStart(2, "0")}
                  </td>
                  {cols.map((c) => (
                    <td
                      key={c}
                      className="px-3 py-2 font-mono text-[12px] border-r border-border last:border-r-0 whitespace-nowrap"
                    >
                      {formatCell(row[c])}
                    </td>
                  ))}
                </tr>
              ))}
              {(!data.rows || data.rows.length === 0) && (
                <tr>
                  <td
                    colSpan={cols.length + 1}
                    className="px-3 py-12 text-center coord"
                  >
                    — NO ROWS —
                  </td>
                </tr>
              )}
            </tbody>
          </table>
        </div>
      </div>
    </div>
  );
}

function formatCell(v: any): string {
  if (v === null || v === undefined) return "—";
  if (typeof v === "object") return JSON.stringify(v);
  return String(v);
}
