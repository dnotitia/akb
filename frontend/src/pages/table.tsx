import * as DropdownMenu from "@radix-ui/react-dropdown-menu";
import { keepPreviousData, useQuery, useQueryClient } from "@tanstack/react-query";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useNavigate, useParams, useSearchParams } from "react-router-dom";
import {
  ArrowDown,
  ArrowUp,
  ArrowUpDown,
  Asterisk,
  Braces,
  CalendarClock,
  Columns3,
  Database,
  Fingerprint,
  Hash,
  Info,
  KeyRound,
  MoreHorizontal,
  PanelRightClose,
  PanelRightOpen,
  Pencil,
  Plus,
  Rows3,
  Table2,
  ToggleLeft,
  Trash2,
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
import { ConfirmDialog } from "@/components/ui/confirm-dialog";
import { LoadingState } from "@/components/ui/loading-state";
import { Skeleton } from "@/components/ui/skeleton";
import { TooltipText } from "@/components/ui/tooltip-text";
import { ResourceActionsMenu } from "@/components/resource-actions-menu";
import { ResourceDeleteDialog } from "@/components/resource-delete-dialog";
import { PublicationSuccessBanner } from "@/components/publication-success-banner";
import { TablePublishDialog } from "@/components/table-publish-dialog";
import { TableRowDialog } from "@/components/table-row-dialog";
import {
  TableFilterBar,
  TableFilterDialog,
  TablePagination,
} from "@/components/table-query-controls";
import { useVaultRefresh } from "@/contexts/vault-refresh-context";
import {
  ApiError,
  deleteVaultTable,
  deleteVaultTableRow,
  getVaultTableRow,
  getVaultInfo,
  insertVaultTableRow,
  listVaultTableRows,
  listVaultTables,
  updateVaultTableRow,
  TableRowConflictError,
  type Publication,
  type VaultTableColumnInput,
  type VaultTableInfo,
} from "@/lib/api";
import { ROLE_RANK, type Role } from "@/lib/roles";
import {
  DEFAULT_TABLE_SORT,
  compileTableFilter,
  parseTableQueryState,
  tableOrder,
  writeTableQueryState,
  type TablePageSize,
  type TableQueryState,
  type TableSort,
} from "@/lib/table-query-state";
import { cn } from "@/lib/utils";

type Column = VaultTableColumnInput;

const SYSTEM_COLUMN_TYPES: Record<string, string> = {
  id: "uuid",
  created_by: "text",
  created_at: "timestamp",
  updated_at: "timestamp",
};

export default function TablePage() {
  const { name: vault, table } = useParams<{ name: string; table: string }>();
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const [searchParams, setSearchParams] = useSearchParams();
  const { refetchTree } = useVaultRefresh();
  const [schemaOpen, setSchemaOpen] = useState(false);
  const [filtersOpen, setFiltersOpen] = useState(false);
  const [publishOpen, setPublishOpen] = useState(false);
  const [deleteOpen, setDeleteOpen] = useState(false);
  const [published, setPublished] = useState<Publication | null>(null);
  const [rowDialogMode, setRowDialogMode] = useState<"create" | "edit" | null>(null);
  const [selectedRow, setSelectedRow] = useState<Record<string, unknown> | null>(null);
  const [rowToDelete, setRowToDelete] = useState<Record<string, unknown> | null>(null);
  const [rowNotice, setRowNotice] = useState("");
  const schemaToggleRef = useRef<HTMLButtonElement | null>(null);
  const schemaCloseRef = useRef<HTMLButtonElement | null>(null);
  const queryState = useMemo(() => parseTableQueryState(searchParams), [searchParams]);

  const setQueryState = useCallback(
    (next: TableQueryState) => {
      setSearchParams(writeTableQueryState(searchParams, next), { replace: true });
    },
    [searchParams, setSearchParams],
  );

  const vaultInfoQuery = useQuery({
    queryKey: ["vault-info", vault],
    queryFn: () => getVaultInfo(vault!),
    enabled: Boolean(vault),
  });
  const catalogQuery = useQuery({
    queryKey: ["vault-tables", vault],
    queryFn: () => listVaultTables(vault!),
    enabled: Boolean(vault),
  });
  const rowsQuery = useQuery({
    queryKey: [
      "vault-table-rows",
      vault,
      table,
      queryState.pageIndex,
      queryState.pageSize,
      queryState.sort,
      queryState.filters,
    ],
    queryFn: async ({ signal }) => {
      const result = await listVaultTableRows(vault!, table!, {
        limit: queryState.pageSize,
        offset: queryState.pageIndex * queryState.pageSize,
        order: tableOrder(queryState.sort),
        filters: queryState.filters.map(compileTableFilter),
        signal,
      });
      return {
        ...result,
        queryPageIndex: queryState.pageIndex,
        queryPageSize: queryState.pageSize,
      };
    },
    enabled: Boolean(vault && table),
    placeholderData: keepPreviousData,
  });

  const info: VaultTableInfo | null =
    catalogQuery.data?.items.find((item) => item.name === table) || null;
  const rows = rowsQuery.data?.items || [];
  const cols = rowsQuery.data?.columns || [];
  const total = rowsQuery.data?.total ?? 0;
  const roleRank = ROLE_RANK[vaultInfoQuery.data?.role as Role] ?? 0;
  const writeRestriction = rowWriteRestriction(vaultInfoQuery.data, roleRank);
  const canManageRows = writeRestriction === null;
  const canPublish = roleRank >= ROLE_RANK.writer && !writeRestriction;
  const canDelete = roleRank >= ROLE_RANK.admin && !writeRestriction;

  const closeSchema = useCallback(() => {
    setSchemaOpen(false);
    window.requestAnimationFrame(() => schemaToggleRef.current?.focus());
  }, []);

  useEffect(() => {
    if (!rowNotice) return;
    const timeout = window.setTimeout(() => setRowNotice(""), 4000);
    return () => window.clearTimeout(timeout);
  }, [rowNotice]);

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

  useEffect(() => {
    if (!rowsQuery.data || rowsQuery.isPlaceholderData) return;
    const pageCount = Math.max(1, Math.ceil(total / queryState.pageSize));
    if (queryState.pageIndex < pageCount) return;
    setQueryState({ ...queryState, pageIndex: pageCount - 1 });
  }, [queryState, rowsQuery.data, rowsQuery.isPlaceholderData, setQueryState, total]);

  const columnByName = useMemo(
    () =>
      new Map<string, Column>([
        ...Object.entries(SYSTEM_COLUMN_TYPES).map(
          ([name, type]) => [name, { name, type }] as const,
        ),
        ...(info?.columns || []).map((column) => [column.name, column] as const),
      ]),
    [info?.columns],
  );

  const rowCount = info?.row_count ?? total;
  const schemaColumnCount = info?.columns?.length || Math.max(0, cols.length - 4);
  const visibleColumnCount = cols.length || schemaColumnCount;
  const primaryKeyCount = info?.columns?.filter((column) => column.primary_key).length || 0;
  const requiredCount =
    info?.columns?.filter((column) => column.required || column.primary_key).length || 0;
  const displayPageIndex = rowsQuery.data?.queryPageIndex ?? queryState.pageIndex;
  const displayPageSize = rowsQuery.data?.queryPageSize ?? queryState.pageSize;
  const pageStart = total === 0 ? 0 : displayPageIndex * displayPageSize + 1;
  const pageEnd = total === 0 ? 0 : Math.min(pageStart + rows.length - 1, total);
  const activeSort = queryState.sort || DEFAULT_TABLE_SORT;
  const availableColumns = Array.from(columnByName.values());
  const queryError = tableRowsError(rowsQuery.error, Boolean(info));

  if (vaultInfoQuery.isPending || catalogQuery.isPending || rowsQuery.isPending) {
    return <TablePageLoading />;
  }

  async function refreshTableData() {
    await Promise.all([rowsQuery.refetch(), catalogQuery.refetch()]);
  }

  function updatePage(pageIndex: number) {
    setQueryState({ ...queryState, pageIndex });
  }

  function updatePageSize(pageSize: TablePageSize) {
    setQueryState({ ...queryState, pageIndex: 0, pageSize });
  }

  function updateSort(column: string) {
    const next: TableSort = {
      column,
      direction:
        activeSort.column === column && activeSort.direction === "asc" ? "desc" : "asc",
    };
    setQueryState({ ...queryState, pageIndex: 0, sort: next });
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
        meta={
          <Badge variant={canManageRows ? "success" : "outline"}>
            {canManageRows ? "Editable" : "Read only"}
          </Badge>
        }
        actions={
          <>
            <div className="mr-1 hidden items-center gap-3 text-xs text-foreground-muted xl:flex">
              <span className="inline-flex items-center gap-1.5 whitespace-nowrap tabular-nums">
                <Rows3 className="h-3.5 w-3.5" aria-hidden />
                {rowCount} rows
              </span>
              <span className="inline-flex items-center gap-1.5 whitespace-nowrap tabular-nums">
                <Columns3 className="h-3.5 w-3.5" aria-hidden />
                {visibleColumnCount} columns
              </span>
            </div>
            <Button
              type="button"
              variant="accent"
              size="sm"
              onClick={() => {
                setSelectedRow(null);
                setRowDialogMode("create");
              }}
              disabled={!canManageRows || !info?.columns?.length}
              title={
                writeRestriction ||
                (!info?.columns?.length ? "Schema metadata is required to add rows" : undefined)
              }
            >
              <Plus className="h-4 w-4" aria-hidden />
              <span className="hidden sm:inline">Add row</span>
            </Button>
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
            {(canPublish || canDelete) && (
              <ResourceActionsMenu
                resourceName={table || "Table"}
                publishLabel={canPublish && info?.columns?.length ? "Publish table" : undefined}
                onPublish={canPublish && info?.columns?.length ? () => setPublishOpen(true) : undefined}
                deleteLabel={canDelete ? "Delete table" : undefined}
                onDelete={canDelete ? () => setDeleteOpen(true) : undefined}
              />
            )}
          </>
        }
      />

      {published && (
        <PublicationSuccessBanner
          vault={vault!}
          publication={published}
          resourceLabel="Table"
          onDismiss={() => setPublished(null)}
        />
      )}

      <div className="relative min-h-0 flex-1 overflow-hidden">
        <ResourceCanvas>
          <ResourceContextBar
            trailing={
              rowNotice ? (
                <span role="status" className="font-medium text-success">
                  {rowNotice}
                </span>
              ) : (
                <span className="inline-flex items-center gap-1.5 whitespace-nowrap tabular-nums">
                  <Rows3 className="h-3.5 w-3.5" aria-hidden />
                  {pageStart}–{pageEnd} of {total}
                </span>
              )
            }
          >
            <div className="flex min-w-0 items-center gap-2 text-xs text-foreground-muted">
              <Info className="h-3.5 w-3.5 shrink-0 text-link" aria-hidden />
              <TooltipText
                tip={info?.description || "Browse records in this Vault table."}
                className="truncate"
              >
                {info?.description || "Browse records in this Vault table."}
              </TooltipText>
              {writeRestriction && (
                <>
                  <span aria-hidden>·</span>
                  <span className="truncate">{writeRestriction}</span>
                </>
              )}
            </div>
          </ResourceContextBar>

          <ResourceViewerFrame
            icon={Database}
            label="Records"
            meta={
              <>
                <span className="tabular-nums">{cols.length} columns</span>
                <span className="hidden sm:inline">
                  {queryState.filters.length > 0 ? `${total} matching` : `${total} rows`}
                </span>
              </>
            }
            bodyClassName="overflow-hidden"
          >
            <div className="flex h-full min-h-0 flex-col">
              <TableFilterBar
                filters={queryState.filters}
                onOpen={() => setFiltersOpen(true)}
                onRemove={(index) =>
                  setQueryState({
                    ...queryState,
                    pageIndex: 0,
                    filters: queryState.filters.filter((_, filterIndex) => filterIndex !== index),
                  })
                }
                onClear={() => setQueryState({ ...queryState, pageIndex: 0, filters: [] })}
                onRefresh={() => void refreshTableData()}
                refreshing={rowsQuery.isFetching}
              />
              {queryError && rowsQuery.data && (
                <div className="shrink-0 border-b border-border p-3">
                  <Alert variant="destructive" title="Records could not be refreshed">
                    {queryError}
                  </Alert>
                </div>
              )}
              {queryError && !rowsQuery.data ? (
                <div className="p-4 sm:p-6">
                  <Alert variant="destructive" title="Records could not be loaded">
                    {queryError}
                  </Alert>
                </div>
              ) : rows.length === 0 ? (
                <div className="flex min-h-64 flex-1 flex-col items-center justify-center px-6 py-12 text-center">
                  <Table2 className="h-9 w-9 text-foreground-muted" aria-hidden />
                  <h2 className="mt-4 text-sm font-semibold text-foreground">
                    {queryState.filters.length > 0 ? "No records match these filters" : "No rows yet"}
                  </h2>
                  <p className="mt-1 max-w-sm text-sm text-foreground-muted">
                    {queryState.filters.length > 0
                      ? "Remove a filter or change its value to broaden the result."
                      : canManageRows
                        ? "The schema is ready. Add the first record to start using this table."
                        : "The schema is ready, but this table does not contain any records."}
                  </p>
                  {queryState.filters.length > 0 ? (
                    <Button
                      type="button"
                      variant="outline"
                      size="sm"
                      className="mt-5"
                      onClick={() => setQueryState({ ...queryState, pageIndex: 0, filters: [] })}
                    >
                      Clear filters
                    </Button>
                  ) : canManageRows && info?.columns?.length ? (
                    <Button
                      type="button"
                      variant="accent"
                      size="sm"
                      className="mt-5"
                      onClick={() => {
                        setSelectedRow(null);
                        setRowDialogMode("create");
                      }}
                    >
                      <Plus className="h-4 w-4" aria-hidden />
                      Add first row
                    </Button>
                  ) : null}
                </div>
              ) : (
                <div
                  role="region"
                  aria-label={`Records for ${table}`}
                  aria-busy={rowsQuery.isFetching || undefined}
                  tabIndex={0}
                  className="min-h-0 flex-1 overflow-auto focus:outline-none focus-visible:ring-2 focus-visible:ring-inset focus-visible:ring-ring"
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
                        {cols.map((columnName) => {
                          const sorted = activeSort.column === columnName;
                          const SortIcon = sorted
                            ? activeSort.direction === "asc"
                              ? ArrowUp
                              : ArrowDown
                            : ArrowUpDown;
                          return (
                            <th
                              key={columnName}
                              scope="col"
                              aria-sort={
                                sorted
                                  ? activeSort.direction === "asc"
                                    ? "ascending"
                                    : "descending"
                                  : "none"
                              }
                              className="sticky top-0 z-[var(--z-raised)] min-w-36 border-b border-r border-border bg-surface-2 p-0 text-left last:border-r-0"
                            >
                              <button
                                type="button"
                                className="flex min-h-11 w-full cursor-pointer items-center justify-between gap-3 px-3 py-2 text-left hover:bg-surface-hover focus:outline-none focus-visible:ring-2 focus-visible:ring-inset focus-visible:ring-ring"
                                onClick={() => updateSort(columnName)}
                                aria-label={`Sort by ${columnName}, ${
                                  sorted ? activeSort.direction : "not currently sorted"
                                }`}
                              >
                                <span className="min-w-0">
                                  <span className="block truncate whitespace-nowrap font-mono text-xs font-medium text-foreground">
                                    {columnName}
                                  </span>
                                  {columnByName.get(columnName)?.type && (
                                    <span className="mt-0.5 block whitespace-nowrap text-xs font-normal text-foreground-muted">
                                      {columnByName.get(columnName)?.type}
                                    </span>
                                  )}
                                </span>
                                <SortIcon
                                  className={cn(
                                    "h-3.5 w-3.5 shrink-0",
                                    sorted ? "text-link" : "text-foreground-muted",
                                  )}
                                  aria-hidden
                                />
                              </button>
                            </th>
                          );
                        })}
                        {canManageRows && (
                          <th
                            scope="col"
                            className="sticky right-0 top-0 z-[var(--z-sticky)] w-12 border-b border-l border-border bg-surface-2 px-2 py-2.5 text-center text-xs font-medium text-foreground-muted"
                          >
                            <span className="sr-only">Row actions</span>
                          </th>
                        )}
                      </tr>
                    </thead>
                    <tbody>
                      {rows.map((row, rowIndex) => (
                        <tr
                          key={typeof row.id === "string" ? row.id : rowIndex}
                          className="group transition-colors hover:bg-surface-hover"
                        >
                          <td className="sticky left-0 z-[var(--z-raised)] border-b border-r border-border bg-surface px-3 py-2 text-xs text-foreground-muted tabular-nums group-hover:bg-surface-hover">
                            {displayPageIndex * displayPageSize + rowIndex + 1}
                          </td>
                          {cols.map((columnName) => {
                            const value = row[columnName];
                            const numeric =
                              typeof value === "number" ||
                              isNumericColumnType(columnByName.get(columnName)?.type);
                            return (
                              <TooltipText key={columnName} asChild tip={formatCellFull(value)}>
                                <td
                                  className={cn(
                                    "max-w-md truncate whitespace-nowrap border-b border-r border-border px-3 py-2 text-foreground last:border-r-0",
                                    numeric && "text-right tabular-nums",
                                    value !== null && typeof value === "object" && "font-mono text-xs",
                                    value === null && "italic text-foreground-muted",
                                  )}
                                >
                                  {formatCell(value)}
                                </td>
                              </TooltipText>
                            );
                          })}
                          {canManageRows && (
                            <td className="sticky right-0 z-[var(--z-raised)] border-b border-l border-border bg-surface px-1 py-1 text-center group-hover:bg-surface-hover">
                              <RowActionsMenu
                                rowNumber={displayPageIndex * displayPageSize + rowIndex + 1}
                                onEdit={() => {
                                  setSelectedRow(row);
                                  setRowDialogMode("edit");
                                }}
                                onDelete={() => setRowToDelete(row)}
                              />
                            </td>
                          )}
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              )}
              {rowsQuery.data && total > 0 && (
                <TablePagination
                  pageIndex={displayPageIndex}
                  pageSize={displayPageSize}
                  total={total}
                  itemCount={rows.length}
                  onPageChange={updatePage}
                  onPageSizeChange={updatePageSize}
                />
              )}
            </div>
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
                    <SchemaStat value={schemaColumnCount} label="Columns" icon={Columns3} />
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
                      {schemaColumnCount} total
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
      <ResourceDeleteDialog
        open={deleteOpen}
        onOpenChange={setDeleteOpen}
        kind="table"
        name={table || "Table"}
        rowCount={rowCount}
        onConfirm={async () => {
          await deleteVaultTable(vault!, table!);
          refetchTree();
          navigate(`/vault/${vault}`);
        }}
      />
      <TablePublishDialog
        open={publishOpen}
        onOpenChange={setPublishOpen}
        vault={vault!}
        table={table!}
        columns={info?.columns || []}
        onPublished={setPublished}
      />
      <TableFilterDialog
        open={filtersOpen}
        onOpenChange={setFiltersOpen}
        columns={availableColumns}
        onAdd={(filter) =>
          setQueryState({
            ...queryState,
            pageIndex: 0,
            filters: [...queryState.filters, filter],
          })
        }
      />
      <TableRowDialog
        open={rowDialogMode !== null}
        onOpenChange={(open) => {
          if (open) return;
          setRowDialogMode(null);
          setSelectedRow(null);
        }}
        mode={rowDialogMode || "create"}
        table={table || "Table"}
        columns={info?.columns || []}
        row={selectedRow}
        onSave={async (values, options) => {
          if (!vault || !table) return;
          if (rowDialogMode === "edit") {
            const rowId = selectedRow?.id;
            if (typeof rowId !== "string") throw new Error("This row does not have a valid id.");
            await updateVaultTableRow(vault, table, rowId, values, {
              expectedUpdatedAt:
                typeof selectedRow?.updated_at === "string" ? selectedRow.updated_at : undefined,
              force: options?.force,
            });
            setRowNotice("Row updated");
          } else {
            await insertVaultTableRow(vault, table, values);
            setRowNotice(
              queryState.filters.length > 0
                ? "Row added · current filters may hide it"
                : "Row added",
            );
            if (queryState.filters.length === 0 && queryState.pageIndex !== 0) {
              setQueryState({ ...queryState, pageIndex: 0 });
            }
          }
          await Promise.all([
            queryClient.invalidateQueries({ queryKey: ["vault-table-rows", vault, table] }),
            queryClient.invalidateQueries({ queryKey: ["vault-tables", vault] }),
          ]);
          refetchTree();
        }}
        onReloadConflict={async () => {
          if (!vault || !table || typeof selectedRow?.id !== "string") return null;
          const latest = await getVaultTableRow(vault, table, selectedRow.id);
          if (latest) setSelectedRow(latest);
          await queryClient.invalidateQueries({ queryKey: ["vault-table-rows", vault, table] });
          return latest;
        }}
      />
      <ConfirmDialog
        open={rowToDelete !== null}
        onOpenChange={(open) => !open && setRowToDelete(null)}
        title="Delete row?"
        description={`This permanently removes ${rowLabel(rowToDelete)} from ${table}. This action cannot be undone.`}
        confirmLabel="Delete row"
        variant="destructive"
        onConfirm={async () => {
          if (!vault || !table || typeof rowToDelete?.id !== "string") {
            throw new Error("This row does not have a valid id.");
          }
          try {
            await deleteVaultTableRow(vault, table, rowToDelete.id, {
              expectedUpdatedAt:
                typeof rowToDelete.updated_at === "string" ? rowToDelete.updated_at : undefined,
            });
          } catch (caught: unknown) {
            if (!(caught instanceof TableRowConflictError)) throw caught;
            const latest = await getVaultTableRow(vault, table, rowToDelete.id);
            if (!latest) {
              setRowNotice("Row was already deleted");
              setRowToDelete(null);
              await queryClient.invalidateQueries({
                queryKey: ["vault-table-rows", vault, table],
              });
              return;
            }
            setRowToDelete(latest);
            throw new Error(
              "This row changed after you opened it. The latest version is now loaded; review it and choose Delete row again.",
              { cause: caught },
            );
          }
          await Promise.all([
            queryClient.invalidateQueries({ queryKey: ["vault-table-rows", vault, table] }),
            queryClient.invalidateQueries({ queryKey: ["vault-tables", vault] }),
          ]);
          refetchTree();
          setRowNotice("Row deleted");
          setRowToDelete(null);
        }}
      />
    </ResourceWorkspace>
  );
}

function rowWriteRestriction(
  vaultInfo: { role?: string; is_archived?: boolean; is_external_git?: boolean } | undefined,
  roleRank: number,
): string | null {
  if (!vaultInfo) return "Permissions could not be verified, so rows are read only.";
  if (vaultInfo.is_archived) return "Archived Vaults are read only.";
  if (vaultInfo.is_external_git) {
    return "This Vault is managed from an external Git source, so rows are read only here.";
  }
  if (roleRank < ROLE_RANK.writer) return "Writer role required to add, edit, or delete rows.";
  return null;
}

function tableRowsError(error: Error | null, tableExists: boolean): string | null {
  if (!error) return null;
  if (tableExists && error instanceof ApiError && [404, 405].includes(error.status)) {
    return "Structured row browsing is not supported by the connected AKB server yet. The table schema is still available.";
  }
  return error.message || "The table records could not be loaded.";
}

function RowActionsMenu({
  rowNumber,
  onEdit,
  onDelete,
}: {
  rowNumber: number;
  onEdit: () => void;
  onDelete: () => void;
}) {
  const itemClass =
    "flex cursor-pointer select-none items-center gap-2 rounded-[var(--radius-sm)] px-3 py-2 text-sm text-foreground outline-none data-[highlighted]:bg-surface-hover";
  return (
    <DropdownMenu.Root>
      <DropdownMenu.Trigger asChild>
        <Button
          type="button"
          variant="ghost"
          size="icon"
          className="h-8 w-8 text-foreground-muted"
          aria-label={`Actions for row ${rowNumber}`}
        >
          <MoreHorizontal className="h-4 w-4" aria-hidden />
        </Button>
      </DropdownMenu.Trigger>
      <DropdownMenu.Portal>
        <DropdownMenu.Content
          align="end"
          sideOffset={6}
          className="z-[var(--z-popover)] min-w-40 overflow-hidden rounded-[var(--radius-md)] border border-border bg-surface p-1 shadow-md"
        >
          <DropdownMenu.Item onSelect={onEdit} className={itemClass}>
            <Pencil className="h-4 w-4 text-foreground-muted" aria-hidden />
            Edit row
          </DropdownMenu.Item>
          <DropdownMenu.Separator className="my-1 h-px bg-border" />
          <DropdownMenu.Item
            onSelect={onDelete}
            className={`${itemClass} text-destructive data-[highlighted]:bg-destructive-soft`}
          >
            <Trash2 className="h-4 w-4" aria-hidden />
            Delete row
          </DropdownMenu.Item>
        </DropdownMenu.Content>
      </DropdownMenu.Portal>
    </DropdownMenu.Root>
  );
}

function rowLabel(row: Record<string, unknown> | null): string {
  if (!row) return "this row";
  const identity = ["title", "name", "label", "id"]
    .map((key) => row[key])
    .find((value) => typeof value === "string" && value.trim());
  if (typeof identity !== "string") return "this row";
  const compact = identity.length > 48 ? `${identity.slice(0, 48)}…` : identity;
  return `“${compact}”`;
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

function isNumericColumnType(type: string | undefined): boolean {
  return /^(int|float|numeric|number)$/.test((type || "").toLowerCase());
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
