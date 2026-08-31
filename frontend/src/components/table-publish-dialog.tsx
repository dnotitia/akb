import { useEffect, useMemo, useRef, useState, type FormEvent } from "react";
import { Camera, Columns3, Database, Globe2, RefreshCw, Rows3 } from "lucide-react";
import {
  PublicationAccessFields,
} from "@/components/publish-options-dialog";
import {
  emptyPublicationAccessOptions,
  publicationAccessError,
  publicationAccessPayload,
  type PublicationAccessOptions,
} from "@/lib/publication-options";
import { Alert } from "@/components/ui/alert";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import {
  createPublication,
  createPublicationSnapshot,
  deletePublication,
  previewTablePublicationQuery,
  type Publication,
  type TablePublicationPreview,
} from "@/lib/api";
import {
  buildTablePublicationQuery,
  DEFAULT_TABLE_PUBLICATION_ROWS,
  isPublishableTableIdentifier,
  MAX_TABLE_PUBLICATION_ROWS,
  type PublishableTableColumn,
} from "@/lib/table-publication";

type PublicationMode = "live" | "snapshot";

const PREVIEW_ROW_LIMIT = 5;

export function TablePublishDialog({
  open,
  onOpenChange,
  vault,
  table,
  columns,
  onPublished,
}: {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  vault: string;
  table: string;
  columns: PublishableTableColumn[];
  onPublished: (publication: Publication) => void;
}) {
  const safeColumnNames = useMemo(
    () =>
      columns
        .filter((column) => isPublishableTableIdentifier(column.name))
        .map((column) => column.name),
    [columns],
  );
  const [title, setTitle] = useState(table);
  const [mode, setMode] = useState<PublicationMode>("live");
  const [selectedColumns, setSelectedColumns] = useState<string[]>(safeColumnNames);
  const [rowLimit, setRowLimit] = useState(String(DEFAULT_TABLE_PUBLICATION_ROWS));
  const [access, setAccess] = useState<PublicationAccessOptions>(
    emptyPublicationAccessOptions,
  );
  const [preview, setPreview] = useState<TablePublicationPreview | null>(null);
  const [previewSql, setPreviewSql] = useState("");
  const [previewing, setPreviewing] = useState(false);
  const [publishing, setPublishing] = useState(false);
  const [error, setError] = useState("");
  const errorRef = useRef<HTMLDivElement | null>(null);

  useEffect(() => {
    if (!open) return;
    setTitle(table);
    setMode("live");
    setSelectedColumns(safeColumnNames);
    setRowLimit(String(DEFAULT_TABLE_PUBLICATION_ROWS));
    setAccess(emptyPublicationAccessOptions());
    setPreview(null);
    setPreviewSql("");
    setError("");
  }, [open, safeColumnNames, table]);

  useEffect(() => {
    if (error) errorRef.current?.focus();
  }, [error]);

  const parsedLimit = Number(rowLimit);
  const rowLimitError =
    !/^\d+$/.test(rowLimit.trim()) ||
    !Number.isInteger(parsedLimit) ||
    parsedLimit < 1 ||
    parsedLimit > MAX_TABLE_PUBLICATION_ROWS
      ? `Enter a whole number from 1 to ${MAX_TABLE_PUBLICATION_ROWS.toLocaleString()}.`
      : null;
  const titleError = title.trim() ? null : "Enter a title for the public link.";
  const accessError = publicationAccessError(access);

  const query = useMemo(() => {
    if (rowLimitError) return { sql: "", error: rowLimitError };
    try {
      return {
        sql: buildTablePublicationQuery({
          table,
          columns,
          selectedColumns,
          rowLimit: parsedLimit,
        }),
        error: null,
      };
    } catch (caught: unknown) {
      return {
        sql: "",
        error: caught instanceof Error ? caught.message : "The query cannot be generated.",
      };
    }
  }, [columns, parsedLimit, rowLimitError, selectedColumns, table]);

  const previewIsCurrent = Boolean(preview && query.sql && previewSql === query.sql);
  const busy = previewing || publishing;
  const publishDisabled = Boolean(
    busy || titleError || accessError || query.error || !previewIsCurrent,
  );

  function invalidatePreview() {
    setPreview(null);
    setPreviewSql("");
    setError("");
  }

  async function handlePreview() {
    if (query.error || !query.sql) {
      setError(query.error || "The preview query is unavailable.");
      return;
    }
    setPreviewing(true);
    setError("");
    try {
      const result = await previewTablePublicationQuery(vault, query.sql);
      setPreview(result);
      setPreviewSql(query.sql);
    } catch (caught: unknown) {
      setPreview(null);
      setPreviewSql("");
      setError(caught instanceof Error ? caught.message : "The preview query failed.");
    } finally {
      setPreviewing(false);
    }
  }

  async function handlePublish(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const validationError = titleError || accessError || query.error;
    if (validationError) {
      setError(validationError);
      return;
    }
    if (!previewIsCurrent || !query.sql) {
      setError("Preview the current column and row selection before publishing.");
      return;
    }

    setPublishing(true);
    setError("");
    try {
      const normalizedTitle = title.trim();
      // Table links are intentionally multi-instance. Reusing a matching SQL
      // string could silently discard a newly requested password, expiry, or
      // view limit, so every confirmed submission creates a distinct link.
      const created = await createPublication(vault, {
        resource_type: "table_query",
        query_sql: query.sql,
        query_vault_names: [vault],
        title: normalizedTitle,
        ...publicationAccessPayload(access),
      });
      let ready = created;
      if (mode === "snapshot") {
        try {
          ready = await createPublicationSnapshot(vault, created.slug);
        } catch (caught: unknown) {
          let cleanupFailed = false;
          try {
            await deletePublication(vault, created.slug);
          } catch {
            cleanupFailed = true;
          }
          const reason = caught instanceof Error ? caught.message : "Snapshot creation failed.";
          throw new Error(
            cleanupFailed
              ? `${reason} The temporary live link could not be removed; revoke it from Publish immediately.`
              : `${reason} The temporary live link was removed.`,
            { cause: caught },
          );
        }
      }
      onPublished(ready);
      onOpenChange(false);
    } catch (caught: unknown) {
      setError(caught instanceof Error ? caught.message : "Failed to publish the table.");
    } finally {
      setPublishing(false);
    }
  }

  return (
    <Dialog open={open} onOpenChange={(next) => !busy && onOpenChange(next)}>
      <DialogContent className="max-w-5xl">
        <form onSubmit={handlePublish} className="contents">
          <DialogHeader>
            <DialogTitle>Publish table</DialogTitle>
            <DialogDescription>
              Choose exactly what becomes public, verify the server result, then create the link.
            </DialogDescription>
          </DialogHeader>

          <Alert variant="warning" title="No sign-in required">
            The selected columns and rows become available to anyone with the link. Table writes are never allowed.
          </Alert>

          {error && (
            <div ref={errorRef} tabIndex={-1}>
              <Alert variant="destructive" title="Publication failed">
                {error}
              </Alert>
            </div>
          )}

          <div className="grid min-h-0 gap-4 lg:grid-cols-[minmax(0,0.9fr)_minmax(0,1.1fr)]">
            <div className="space-y-4 rounded-[var(--radius-lg)] border border-border bg-surface p-4">
              <div>
                <Label htmlFor="table-publication-title" className="mb-1.5 block text-xs text-foreground">
                  Public title
                </Label>
                <Input
                  id="table-publication-title"
                  value={title}
                  disabled={busy}
                  onChange={(event) => setTitle(event.target.value)}
                  aria-invalid={Boolean(titleError) || undefined}
                  aria-describedby={titleError ? "table-publication-title-error" : undefined}
                />
                {titleError && (
                  <p id="table-publication-title-error" className="mt-1.5 text-xs text-destructive">
                    {titleError}
                  </p>
                )}
              </div>

              <fieldset disabled={busy}>
                <legend className="mb-1.5 text-xs font-medium text-foreground">Delivery</legend>
                <div className="grid grid-cols-2 gap-px overflow-hidden rounded-[var(--radius-md)] border border-border bg-border">
                  <ModeButton
                    active={mode === "live"}
                    icon={Globe2}
                    label="Live"
                    description="Current rows on each visit"
                    onClick={() => setMode("live")}
                  />
                  <ModeButton
                    active={mode === "snapshot"}
                    icon={Camera}
                    label="Snapshot"
                    description="Frozen at publish time"
                    onClick={() => setMode("snapshot")}
                  />
                </div>
              </fieldset>

              <div>
                <div className="mb-1.5 flex items-center justify-between gap-3">
                  <Label className="text-xs text-foreground">Public columns</Label>
                  <span className="text-xs tabular-nums text-foreground-muted">
                    {selectedColumns.length} of {safeColumnNames.length}
                  </span>
                </div>
                <div className="max-h-48 overflow-y-auto rounded-[var(--radius-md)] border border-border rail-scroll">
                  {columns.map((column) => {
                    const safe = isPublishableTableIdentifier(column.name);
                    const checked = selectedColumns.includes(column.name);
                    return (
                      <label
                        key={column.name}
                        className="flex min-h-9 items-center gap-2 border-b border-border px-3 py-2 last:border-b-0 hover:bg-surface-hover"
                      >
                        <input
                          type="checkbox"
                          checked={checked}
                          disabled={busy || !safe}
                          onChange={(event) => {
                            invalidatePreview();
                            setSelectedColumns((current) =>
                              event.target.checked
                                ? [...current, column.name]
                                : current.filter((name) => name !== column.name),
                            );
                          }}
                          className="h-4 w-4 shrink-0 cursor-pointer rounded-[var(--radius-sm)] accent-[var(--color-primary)] focus:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2 focus-visible:ring-offset-surface disabled:cursor-not-allowed disabled:opacity-50"
                        />
                        <span className="min-w-0 flex-1 truncate font-mono text-xs text-foreground">
                          {column.name}
                        </span>
                        {column.primary_key && <Badge variant="outline">Primary key</Badge>}
                        {column.type && <span className="text-xs text-foreground-muted">{column.type}</span>}
                      </label>
                    );
                  })}
                </div>
                <div className="mt-1 flex items-center gap-1">
                  <Button
                    type="button"
                    variant="ghost"
                    size="sm"
                    disabled={busy || selectedColumns.length === safeColumnNames.length}
                    onClick={() => {
                      invalidatePreview();
                      setSelectedColumns(safeColumnNames);
                    }}
                  >
                    Select all
                  </Button>
                  <Button
                    type="button"
                    variant="ghost"
                    size="sm"
                    disabled={busy || selectedColumns.length === 0}
                    onClick={() => {
                      invalidatePreview();
                      setSelectedColumns([]);
                    }}
                  >
                    Clear
                  </Button>
                </div>
              </div>

              <div>
                <Label htmlFor="table-publication-limit" className="mb-1.5 block text-xs text-foreground">
                  Maximum public rows
                </Label>
                <Input
                  id="table-publication-limit"
                  type="number"
                  min={1}
                  max={MAX_TABLE_PUBLICATION_ROWS}
                  step={1}
                  inputMode="numeric"
                  value={rowLimit}
                  disabled={busy}
                  onChange={(event) => {
                    invalidatePreview();
                    setRowLimit(event.target.value);
                  }}
                  aria-invalid={Boolean(rowLimitError) || undefined}
                  aria-describedby="table-publication-limit-help"
                />
                <p
                  id="table-publication-limit-help"
                  className={rowLimitError ? "mt-1.5 text-xs text-destructive" : "mt-1.5 text-xs text-foreground-muted"}
                >
                  {rowLimitError ||
                    `Up to ${MAX_TABLE_PUBLICATION_ROWS.toLocaleString()} rows. Default: ${DEFAULT_TABLE_PUBLICATION_ROWS}.`}
                </p>
              </div>
            </div>

            <TablePublicationPreviewPanel
              table={table}
              selectedColumnCount={selectedColumns.length}
              rowLimit={rowLimitError ? null : parsedLimit}
              ordered={columns.some((column) => column.primary_key)}
              preview={previewIsCurrent ? preview : null}
              previewing={previewing}
            />
          </div>

          <details className="rounded-[var(--radius-lg)] border border-border bg-surface">
            <summary className="flex min-h-10 cursor-pointer list-none items-center justify-between gap-3 rounded-[var(--radius-lg)] px-4 py-2 text-sm font-medium text-foreground focus:outline-none focus-visible:ring-2 focus-visible:ring-inset focus-visible:ring-ring">
              <span>Optional link controls</span>
              <span className="text-xs font-normal text-foreground-muted">Password · expiry · view limit</span>
            </summary>
            <div className="border-t border-border p-4">
              <PublicationAccessFields
                value={access}
                onChange={setAccess}
                idPrefix="table-publication"
                disabled={busy}
              />
            </div>
          </details>

          <DialogFooter>
            <Button
              type="button"
              variant="outline"
              onClick={() => onOpenChange(false)}
              disabled={busy}
            >
              Cancel
            </Button>
            <Button
              type="button"
              variant="outline"
              loading={previewing}
              disabled={publishing || Boolean(query.error)}
              onClick={() => void handlePreview()}
            >
              {!previewing && <RefreshCw className="h-4 w-4" aria-hidden />}
              {previewing ? "Previewing…" : previewIsCurrent ? "Refresh preview" : "Preview query"}
            </Button>
            <Button
              type="submit"
              variant="accent"
              loading={publishing}
              disabled={publishDisabled}
            >
              {publishing ? "Publishing…" : mode === "snapshot" ? "Publish snapshot" : "Publish live table"}
            </Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  );
}

function ModeButton({
  active,
  icon: Icon,
  label,
  description,
  onClick,
}: {
  active: boolean;
  icon: typeof Globe2;
  label: string;
  description: string;
  onClick: () => void;
}) {
  return (
    <button
      type="button"
      aria-pressed={active}
      onClick={onClick}
      className={`min-h-16 cursor-pointer px-3 py-2 text-left transition-token focus:outline-none focus-visible:ring-2 focus-visible:ring-inset focus-visible:ring-ring disabled:cursor-not-allowed disabled:opacity-50 ${
        active ? "bg-surface-selected text-surface-selected-foreground" : "bg-surface text-foreground hover:bg-surface-hover"
      }`}
    >
      <span className="flex items-center gap-2 text-sm font-medium">
        <Icon className="h-4 w-4" aria-hidden />
        {label}
      </span>
      <span className="mt-0.5 block text-xs text-foreground-muted">{description}</span>
    </button>
  );
}

function TablePublicationPreviewPanel({
  table,
  selectedColumnCount,
  rowLimit,
  ordered,
  preview,
  previewing,
}: {
  table: string;
  selectedColumnCount: number;
  rowLimit: number | null;
  ordered: boolean;
  preview: TablePublicationPreview | null;
  previewing: boolean;
}) {
  const rows = preview?.items.slice(0, PREVIEW_ROW_LIMIT) || [];
  const columns = preview?.columns || [];
  return (
    <section
      aria-labelledby="table-publication-preview-title"
      aria-busy={previewing}
      className="flex min-h-80 min-w-0 flex-col overflow-hidden rounded-[var(--radius-lg)] border border-border bg-surface"
    >
      <div className="flex min-h-11 items-center gap-3 border-b border-border bg-surface-2 px-3 py-2">
        <Database className="h-4 w-4 shrink-0 text-link" aria-hidden />
        <div className="min-w-0 flex-1">
          <h3 id="table-publication-preview-title" className="text-sm font-semibold text-foreground">
            Public result preview
          </h3>
          <p className="truncate text-xs text-foreground-muted">
            {table} · {selectedColumnCount} columns · up to {rowLimit?.toLocaleString() || "—"} rows
          </p>
        </div>
        {preview && <Badge variant="success">Verified</Badge>}
      </div>

      {previewing ? (
        <div role="status" aria-live="polite" className="flex min-h-64 flex-1 flex-col items-center justify-center gap-3 px-6 text-center">
          <RefreshCw className="h-6 w-6 animate-spin text-link" aria-hidden />
          <p className="text-sm font-medium text-foreground">Running the bounded query…</p>
          <p className="text-xs text-foreground-muted">The server is checking access and result shape.</p>
        </div>
      ) : !preview ? (
        <div className="flex min-h-64 flex-1 flex-col items-center justify-center px-6 py-10 text-center">
          <Rows3 className="h-8 w-8 text-foreground-muted" aria-hidden />
          <h4 className="mt-3 text-sm font-semibold text-foreground">Preview required</h4>
          <p className="mt-1 max-w-sm text-sm text-foreground-muted">
            Run the generated query before publishing. Changing columns or the row limit requires another preview.
          </p>
          {ordered && (
            <p className="mt-3 inline-flex items-center gap-1.5 text-xs text-foreground-muted">
              <Columns3 className="h-3.5 w-3.5" aria-hidden />
              Results are ordered by the table primary key.
            </p>
          )}
        </div>
      ) : rows.length === 0 ? (
        <div className="flex min-h-64 flex-1 flex-col items-center justify-center px-6 text-center">
          <Rows3 className="h-8 w-8 text-foreground-muted" aria-hidden />
          <h4 className="mt-3 text-sm font-semibold text-foreground">Verified empty result</h4>
          <p className="mt-1 text-sm text-foreground-muted">
            The public link is valid, but the query currently returns no rows.
          </p>
        </div>
      ) : (
        <div role="region" aria-label="Public table preview rows" tabIndex={0} className="min-h-64 flex-1 overflow-auto focus:outline-none focus-visible:ring-2 focus-visible:ring-inset focus-visible:ring-ring">
          <table className="min-w-full border-separate border-spacing-0 text-xs">
            <thead>
              <tr>
                {columns.map((column) => (
                  <th
                    key={column}
                    scope="col"
                    className="sticky top-0 z-[var(--z-raised)] min-w-32 border-b border-r border-border bg-surface-2 px-3 py-2 text-left font-mono font-medium text-foreground last:border-r-0"
                  >
                    {column}
                  </th>
                ))}
              </tr>
            </thead>
            <tbody>
              {rows.map((row, rowIndex) => (
                <tr key={rowIndex} className="hover:bg-surface-hover">
                  {columns.map((column) => (
                    <td
                      key={column}
                      className="max-w-56 truncate whitespace-nowrap border-b border-r border-border px-3 py-2 text-foreground last:border-r-0"
                      title={formatPreviewCell(row[column])}
                    >
                      {formatPreviewCell(row[column])}
                    </td>
                  ))}
                </tr>
              ))}
            </tbody>
          </table>
          <p className="border-t border-border bg-surface-2 px-3 py-2 text-right text-xs tabular-nums text-foreground-muted">
            Showing {rows.length} of {preview.items.length.toLocaleString()} returned rows
          </p>
        </div>
      )}
    </section>
  );
}

function formatPreviewCell(value: unknown): string {
  if (value === null || value === undefined) return "—";
  if (typeof value === "object") return JSON.stringify(value);
  return String(value);
}
