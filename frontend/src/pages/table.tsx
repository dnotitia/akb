import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useParams } from "react-router-dom";
import {
  Asterisk,
  Braces,
  CalendarClock,
  Columns3,
  Database,
  Fingerprint,
  Hash,
  Info,
  KeyRound,
  PanelRightClose,
  PanelRightOpen,
  Rows3,
  Table2,
  ToggleLeft,
  Type,
  X,
} from "lucide-react";
import {
  ResourceCanvas,
  ResourceContextBar,
  ResourceViewerFrame,
  ResourceWorkspace,
  ResourceWorkspaceHeader,
} from "@/components/resource-workspace";
import { Alert } from "@/components/ui/alert";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { LoadingState } from "@/components/ui/loading-state";
import { Skeleton } from "@/components/ui/skeleton";
import { TooltipText } from "@/components/ui/tooltip-text";
import { authenticatedFetch } from "@/lib/api";
import { cn } from "@/lib/utils";

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
  const [rows, setRows] = useState<Record<string, unknown>[]>([]);
  const [cols, setCols] = useState<string[]>([]);
  const [total, setTotal] = useState(0);
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(true);
  const [infoLoading, setInfoLoading] = useState(true);
  const [schemaOpen, setSchemaOpen] = useState(false);
  const schemaToggleRef = useRef<HTMLButtonElement | null>(null);
  const schemaCloseRef = useRef<HTMLButtonElement | null>(null);
  const limit = 50;

  const closeSchema = useCallback(() => {
    setSchemaOpen(false);
    window.requestAnimationFrame(() => schemaToggleRef.current?.focus());
  }, []);

  useEffect(() => {
    if (!vault || !table) return;
    let cancelled = false;
    setInfo(null);
    setRows([]);
    setCols([]);
    setTotal(0);
    setError("");
    setLoading(true);
    setInfoLoading(true);

    authenticatedFetch(`/api/v1/tables/${encodeURIComponent(vault)}`)
      .then((response) => (response.ok ? response.json().catch(() => null) : null))
      .then((data) => {
        if (cancelled || !data) return;
        const found = (data.items || []).find((item: TableInfo) => item.name === table);
        if (found) setInfo(found);
      })
      .catch(() => {})
      .finally(() => {
        if (!cancelled) setInfoLoading(false);
      });

    // Table identifiers are restricted to ^[a-z][a-z0-9_]*$. Keep the bare
    // name so the backend SQL rewriter maps it to the vault-scoped relation.
    authenticatedFetch(`/api/v1/tables/${encodeURIComponent(vault)}/sql`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ sql: `SELECT * FROM ${table} LIMIT ${limit}` }),
    })
      .then((response) => response.json())
      .then((data) => {
        if (cancelled) return;
        if (data.error || data.detail) {
          setError(data.error || data.detail);
          return;
        }
        setRows(data.items || []);
        setCols(data.columns || []);
        setTotal(data.total ?? data.items?.length ?? 0);
      })
      .catch((queryError) => {
        if (!cancelled) setError(String(queryError));
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });

    return () => {
      cancelled = true;
    };
  }, [vault, table]);

  useEffect(() => {
    if (!schemaOpen) return;
    const focusFrame = window.requestAnimationFrame(() => schemaCloseRef.current?.focus());
    const handleKeyDown = (event: KeyboardEvent) => {
      if (event.key !== "Escape") return;
      event.preventDefault();
      closeSchema();
    };
    window.addEventListener("keydown", handleKeyDown);
    return () => {
      window.cancelAnimationFrame(focusFrame);
      window.removeEventListener("keydown", handleKeyDown);
    };
  }, [closeSchema, schemaOpen]);

  const columnByName = useMemo(
    () => new Map((info?.columns || []).map((column) => [column.name, column])),
    [info?.columns],
  );

  const rowCount = info?.row_count ?? total;
  const columnCount = info?.columns?.length || cols.length;
  const primaryKeyCount = info?.columns?.filter((column) => column.primary_key).length || 0;
  const requiredCount =
    info?.columns?.filter((column) => column.required || column.primary_key).length || 0;
  const sampled = rowCount > rows.length;

  if (loading || infoLoading) {
    return <TablePageLoading />;
  }

  return (
    <ResourceWorkspace label="Table workspace">
      <ResourceWorkspaceHeader
        icon={Table2}
        iconTone="data"
        title={table || "Table"}
        subtitle={
          <>
            Table <span aria-hidden>·</span>{" "}
            <span className="font-medium text-foreground">{vault}</span>
          </>
        }
        meta={<Badge variant="outline">Read only</Badge>}
        actions={
          <>
            <div className="mr-1 hidden items-center gap-3 text-xs text-foreground-muted xl:flex">
              <span className="inline-flex items-center gap-1.5 whitespace-nowrap tabular-nums">
                <Rows3 className="h-3.5 w-3.5" aria-hidden />
                {rowCount} rows
              </span>
              <span className="inline-flex items-center gap-1.5 whitespace-nowrap tabular-nums">
                <Columns3 className="h-3.5 w-3.5" aria-hidden />
                {columnCount} columns
              </span>
            </div>
            <Button
              ref={schemaToggleRef}
              type="button"
              variant="outline"
              size="sm"
              aria-controls="table-schema-panel"
              aria-expanded={schemaOpen}
              onClick={() => (schemaOpen ? closeSchema() : setSchemaOpen(true))}
            >
              {schemaOpen ? (
                <PanelRightClose className="h-4 w-4" aria-hidden />
              ) : (
                <PanelRightOpen className="h-4 w-4" aria-hidden />
              )}
              <span className="hidden sm:inline">Schema</span>
            </Button>
          </>
        }
      />

      <div className="relative min-h-0 flex-1 overflow-hidden">
        <ResourceCanvas>
          <ResourceContextBar
            trailing={
              <span className="inline-flex items-center gap-1.5 whitespace-nowrap tabular-nums">
                <Rows3 className="h-3.5 w-3.5" aria-hidden />
                {sampled ? `${rows.length} of ${rowCount}` : `${rows.length} rows`}
              </span>
            }
          >
            <div className="flex min-w-0 items-center gap-2 text-xs text-foreground-muted">
              <Info className="h-3.5 w-3.5 shrink-0 text-link" aria-hidden />
              <TooltipText
                tip={info?.description || "A read-only sample of this Vault table."}
                className="truncate"
              >
                {info?.description || "A read-only sample of this Vault table."}
              </TooltipText>
            </div>
          </ResourceContextBar>

          <ResourceViewerFrame
            icon={Database}
            label="Data preview"
            meta={
              <>
                <span className="tabular-nums">{cols.length} columns</span>
                <span className="hidden sm:inline">
                  {sampled ? `First ${limit} rows` : "Complete result"}
                </span>
              </>
            }
            bodyClassName="overflow-hidden"
          >
            {error ? (
              <div className="p-4 sm:p-6">
                <Alert variant="destructive" title="Query failed">
                  <span className="font-mono">{error}</span>
                </Alert>
              </div>
            ) : rows.length === 0 ? (
              <div className="flex min-h-64 flex-col items-center justify-center px-6 py-12 text-center">
                <Table2 className="h-9 w-9 text-foreground-muted" aria-hidden />
                <h2 className="mt-4 text-sm font-semibold text-foreground">No rows yet</h2>
                <p className="mt-1 max-w-sm text-sm text-foreground-muted">
                  The schema is ready, but this table does not contain any records.
                </p>
              </div>
            ) : (
              <div
                role="region"
                aria-label={`Preview rows for ${table}`}
                tabIndex={0}
                className="h-full overflow-auto focus:outline-none focus-visible:ring-2 focus-visible:ring-inset focus-visible:ring-ring"
              >
                <table className="min-w-full border-separate border-spacing-0 text-sm">
                  <thead>
                    <tr>
                      <th
                        scope="col"
                        className="sticky left-0 top-0 z-[var(--z-sticky)] w-12 border-b border-r border-border bg-surface-2 px-3 py-2.5 text-left text-xs font-medium text-foreground-muted"
                      >
                        #
                      </th>
                      {cols.map((columnName) => (
                        <th
                          key={columnName}
                          scope="col"
                          className="sticky top-0 z-[var(--z-raised)] min-w-36 border-b border-r border-border bg-surface-2 px-3 py-2.5 text-left last:border-r-0"
                        >
                          <span className="block whitespace-nowrap font-mono text-xs font-medium text-foreground">
                            {columnName}
                          </span>
                          {columnByName.get(columnName)?.type && (
                            <span className="mt-0.5 block whitespace-nowrap text-xs font-normal text-foreground-muted">
                              {columnByName.get(columnName)?.type}
                            </span>
                          )}
                        </th>
                      ))}
                    </tr>
                  </thead>
                  <tbody>
                    {rows.map((row, rowIndex) => (
                      <tr key={rowIndex} className="group transition-colors hover:bg-surface-hover">
                        <td className="sticky left-0 z-[var(--z-raised)] border-b border-r border-border bg-surface px-3 py-2 text-xs text-foreground-muted tabular-nums group-hover:bg-surface-hover">
                          {rowIndex + 1}
                        </td>
                        {cols.map((columnName) => {
                          const value = row[columnName];
                          return (
                            <TooltipText key={columnName} asChild tip={formatCellFull(value)}>
                              <td
                                className={cn(
                                  "max-w-md truncate whitespace-nowrap border-b border-r border-border px-3 py-2 text-foreground last:border-r-0",
                                  typeof value === "number" && "text-right tabular-nums",
                                  value !== null && typeof value === "object" && "font-mono text-xs",
                                  value === null && "italic text-foreground-muted",
                                )}
                              >
                                {formatCell(value)}
                              </td>
                            </TooltipText>
                          );
                        })}
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}
          </ResourceViewerFrame>
        </ResourceCanvas>

        {schemaOpen && (
          <button
            type="button"
            aria-label="Close table schema"
            onClick={closeSchema}
            className="absolute inset-0 z-[var(--z-raised)] bg-black/40 lg:hidden"
          />
        )}
        <aside
          id="table-schema-panel"
          aria-label="Table schema"
          aria-hidden={!schemaOpen}
          inert={!schemaOpen}
          className={cn(
            "absolute inset-y-0 right-0 z-[var(--z-overlay)] flex w-full max-w-xl flex-col overflow-hidden border-l border-border bg-surface shadow-xl transition-transform duration-[var(--duration-base)] ease-[var(--ease-out)] lg:w-[30rem]",
            schemaOpen ? "translate-x-0" : "pointer-events-none translate-x-full",
          )}
        >
          <div className="flex h-14 shrink-0 items-center justify-between border-b border-border px-4">
            <div>
              <h2 className="text-sm font-semibold text-foreground">Table schema</h2>
              <p className="text-xs text-foreground-muted">Columns and constraints</p>
            </div>
            <Button
              ref={schemaCloseRef}
              type="button"
              variant="ghost"
              size="icon"
              aria-label="Close table schema"
              onClick={closeSchema}
            >
              <X className="h-4 w-4" aria-hidden />
            </Button>
          </div>
          <div className="min-h-0 flex-1 overflow-y-auto p-4 rail-scroll">
            {info?.columns?.length ? (
              <>
                <section aria-labelledby="schema-overview-title">
                  <div className="mb-2 flex items-center justify-between gap-3">
                    <h3 id="schema-overview-title" className="text-xs font-semibold text-foreground">
                      At a glance
                    </h3>
                    <span className="max-w-48 truncate font-mono text-xs text-foreground-muted">
                      {table}
                    </span>
                  </div>
                  <dl className="grid grid-cols-3 overflow-hidden rounded-[var(--radius-md)] border border-border bg-surface-2/60">
                    <SchemaStat value={columnCount} label="Columns" icon={Columns3} />
                    <SchemaStat value={primaryKeyCount} label="Primary keys" icon={KeyRound} />
                    <SchemaStat value={requiredCount} label="Required" icon={Asterisk} />
                  </dl>
                </section>

                <section className="mt-5" aria-labelledby="schema-columns-title">
                  <div className="mb-2 flex items-end justify-between gap-3">
                    <div>
                      <h3 id="schema-columns-title" className="text-xs font-semibold text-foreground">
                        Column dictionary
                      </h3>
                      <p className="mt-0.5 text-xs text-foreground-muted">Stored order and constraints</p>
                    </div>
                    <span className="text-xs text-foreground-muted tabular-nums">
                      {columnCount} total
                    </span>
                  </div>

                  <div className="overflow-x-auto rounded-[var(--radius-md)] border border-border">
                    <table className="w-full min-w-[26rem] table-fixed border-separate border-spacing-0 text-xs">
                      <colgroup>
                        <col className="w-10" />
                        <col />
                        <col className="w-28" />
                        <col className="w-32" />
                      </colgroup>
                      <thead>
                        <tr className="bg-surface-2 text-left text-foreground-muted">
                          <th scope="col" className="border-b border-r border-border px-2 py-2 font-medium">
                            #
                          </th>
                          <th scope="col" className="border-b border-r border-border px-3 py-2 font-medium">
                            Column
                          </th>
                          <th scope="col" className="border-b border-r border-border px-3 py-2 font-medium">
                            Data type
                          </th>
                          <th scope="col" className="border-b border-border px-3 py-2 font-medium">
                            Constraints
                          </th>
                        </tr>
                      </thead>
                      <tbody className="[&>tr:last-child>*]:border-b-0">
                        {info.columns.map((column, index) => (
                          <SchemaColumn key={column.name} column={column} index={index} />
                        ))}
                      </tbody>
                    </table>
                  </div>
                </section>
              </>
            ) : (
              <Alert variant="info" title="Schema unavailable">
                Column metadata was not included in the table catalog response.
              </Alert>
            )}
          </div>
        </aside>
      </div>
    </ResourceWorkspace>
  );
}

function SchemaStat({
  value,
  label,
  icon: Icon,
}: {
  value: number;
  label: string;
  icon: typeof Columns3;
}) {
  return (
    <div className="border-r border-border px-3 py-3 last:border-r-0">
      <dt className="flex items-center gap-1.5 text-xs text-foreground-muted">
        <Icon className="h-3.5 w-3.5" aria-hidden />
        <span className="truncate">{label}</span>
      </dt>
      <dd className="mt-1 text-lg font-semibold leading-none text-foreground tabular-nums">{value}</dd>
    </div>
  );
}

function SchemaColumn({ column, index }: { column: Column; index: number }) {
  const dataType = column.type || "text";
  const TypeIcon = columnTypeIcon(dataType);
  const isRequired = column.required || column.primary_key;

  return (
    <tr
      className={cn(
        "align-top text-foreground",
        column.primary_key && "bg-surface-selected",
      )}
    >
      <td className="border-b border-r border-border px-2 py-3 text-right text-foreground-muted tabular-nums">
        {index + 1}
      </td>
      <th
        scope="row"
        className="border-b border-r border-border px-3 py-3 text-left font-normal"
      >
        <code className="block break-all font-mono text-xs font-semibold text-foreground">
          {column.name}
        </code>
      </th>
      <td className="border-b border-r border-border px-3 py-2.5">
        <span className="inline-flex h-7 max-w-full items-center gap-1.5 rounded-[var(--radius-sm)] border border-border bg-surface px-2 text-foreground">
          <TypeIcon className="h-3.5 w-3.5 shrink-0 text-foreground-muted" aria-hidden />
          <span className="truncate font-mono" title={dataType}>
            {dataType}
          </span>
        </span>
      </td>
      <td className="border-b border-border px-3 py-2.5">
        <div className="flex flex-col items-start gap-1.5 text-foreground-muted">
          {column.primary_key && (
            <span className="inline-flex items-center gap-1 font-medium text-link">
              <KeyRound className="h-3 w-3" aria-hidden />
              Primary key
            </span>
          )}
          <span className="inline-flex items-center gap-1">
            {isRequired ? (
              <Asterisk className="h-3 w-3" aria-hidden />
            ) : (
              <span className="h-2 w-2 rounded-full border border-border-strong" aria-hidden />
            )}
            {isRequired ? "Required" : "Nullable"}
          </span>
        </div>
      </td>
    </tr>
  );
}

function columnTypeIcon(type: string) {
  const normalized = type.toLowerCase();
  if (/bool/.test(normalized)) return ToggleLeft;
  if (/date|time/.test(normalized)) return CalendarClock;
  if (/int|decimal|numeric|float|double|real|serial/.test(normalized)) return Hash;
  if (/json|array|map|struct/.test(normalized)) return Braces;
  if (/uuid/.test(normalized)) return Fingerprint;
  return Type;
}

function TablePageLoading() {
  return (
    <LoadingState
      label="Loading rows"
      className="flex h-full min-h-0 flex-col overflow-hidden bg-background"
    >
      <div className="flex h-full min-h-0 flex-col overflow-hidden">
        <header className="flex h-16 shrink-0 items-center gap-3 border-b border-border bg-surface px-3 sm:px-4 lg:px-5">
          <Skeleton className="hidden h-9 w-9 shrink-0 rounded-[var(--radius-md)] sm:block" />
          <div className="min-w-0 flex-1 space-y-2">
            <Skeleton className="h-5 w-2/3 max-w-48 rounded-[var(--radius-sm)]" />
            <Skeleton className="h-3 w-1/2 max-w-36 rounded-[var(--radius-sm)]" />
          </div>
          <Skeleton className="h-8 w-20 rounded-[var(--radius-md)]" />
        </header>
        <div className="flex min-h-0 flex-1 flex-col overflow-hidden p-2 sm:p-3">
          <Skeleton className="mb-3 h-11 w-full shrink-0 rounded-[var(--radius-lg)]" />
          <div className="flex min-h-0 flex-1 flex-col overflow-hidden rounded-[var(--radius-lg)] border border-border bg-surface">
            <div className="flex h-11 shrink-0 items-center gap-3 border-b border-border bg-surface-2/60 px-3">
              <Skeleton className="h-4 w-28 rounded-[var(--radius-sm)]" />
              <Skeleton className="ml-auto h-3 w-24 rounded-[var(--radius-sm)]" />
            </div>
            <div className="min-h-0 flex-1 space-y-2 p-3">
              <Skeleton className="h-11 w-full rounded-[var(--radius-sm)]" />
              <Skeleton className="h-10 w-full rounded-[var(--radius-sm)]" />
              <Skeleton className="h-10 w-full rounded-[var(--radius-sm)]" />
              <Skeleton className="h-10 w-full rounded-[var(--radius-sm)]" />
              <Skeleton className="h-10 w-full rounded-[var(--radius-sm)]" />
            </div>
          </div>
        </div>
      </div>
    </LoadingState>
  );
}

function formatCellFull(value: unknown): string {
  if (value === null || value === undefined) return "null";
  if (typeof value === "number" && !Number.isFinite(value)) return "—";
  if (typeof value === "object") return JSON.stringify(value);
  return String(value);
}

function formatCell(value: unknown): string {
  const full = formatCellFull(value);
  return full.length > 120 ? `${full.slice(0, 120)}…` : full;
}
