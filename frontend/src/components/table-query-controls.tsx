import { useEffect, useId, useMemo, useState, type FormEvent } from "react";
import {
  ChevronLeft,
  ChevronRight,
  ChevronsLeft,
  ChevronsRight,
  Filter,
  RefreshCw,
  X,
} from "lucide-react";
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
import { SelectMenu } from "@/components/ui/select-menu";
import { Textarea } from "@/components/ui/textarea";
import type { VaultTableColumnInput } from "@/lib/api";
import {
  TABLE_PAGE_SIZES,
  normalizeTableColumnType,
  tableFilterLabel,
  tableFilterNeedsValue,
  tableFilterOperators,
  validateTableFilter,
  type TableFilter,
  type TableFilterOperator,
  type TablePageSize,
} from "@/lib/table-query-state";

const SYSTEM_COLUMN_NAMES = new Set(["id", "created_by", "created_at", "updated_at"]);

export function TableFilterDialog({
  open,
  onOpenChange,
  columns,
  onAdd,
}: {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  columns: VaultTableColumnInput[];
  onAdd: (filter: TableFilter) => void;
}) {
  const formId = useId();
  const [columnName, setColumnName] = useState("");
  const [operator, setOperator] = useState<TableFilterOperator>("contains");
  const [value, setValue] = useState("");
  const [error, setError] = useState("");

  const preferredColumn = useMemo(
    () => columns.find((column) => !SYSTEM_COLUMN_NAMES.has(column.name)) || columns[0],
    [columns],
  );

  const selectedColumn = useMemo(
    () => columns.find((column) => column.name === columnName) || preferredColumn,
    [columnName, columns, preferredColumn],
  );
  const operatorOptions = useMemo(
    () => (selectedColumn ? tableFilterOperators(selectedColumn) : []),
    [selectedColumn],
  );

  useEffect(() => {
    if (!open || !preferredColumn) return;
    const first = preferredColumn;
    setColumnName(first.name);
    setOperator(tableFilterOperators(first)[0]?.value || "eq");
    setValue("");
    setError("");
  }, [open, preferredColumn]);

  function chooseColumn(nextName: string) {
    const nextColumn = columns.find((column) => column.name === nextName);
    if (!nextColumn) return;
    setColumnName(nextName);
    setOperator(tableFilterOperators(nextColumn)[0]?.value || "eq");
    setValue("");
    setError("");
  }

  function submit(event: FormEvent) {
    event.preventDefault();
    if (!selectedColumn) return;
    const filter = { column: selectedColumn.name, operator, value };
    const validationError = validateTableFilter(filter, selectedColumn);
    if (validationError) {
      setError(validationError);
      return;
    }
    onAdd(filter);
    onOpenChange(false);
  }

  const needsValue = tableFilterNeedsValue(operator);
  const normalizedType = normalizeTableColumnType(selectedColumn?.type);
  const isEnumValue =
    (normalizedType === "enum" || Boolean(selectedColumn?.enum?.length)) &&
    Boolean(selectedColumn?.enum?.length) &&
    operator !== "in";

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="max-w-xl gap-0 overflow-hidden p-0">
        <DialogHeader className="border-b border-border px-5 py-4 pr-14 sm:px-6">
          <DialogTitle className="flex items-center gap-2">
            <Filter className="h-5 w-5 text-primary" aria-hidden />
            Add a filter
          </DialogTitle>
          <DialogDescription>
            Narrow records with a condition that matches the selected column type.
          </DialogDescription>
        </DialogHeader>
        <form id={formId} onSubmit={submit}>
          <div className="grid gap-4 px-5 py-5 sm:grid-cols-2 sm:px-6">
            <div className="space-y-2">
              <Label htmlFor={`${formId}-column`}>Column</Label>
              <SelectMenu
                id={`${formId}-column`}
                value={selectedColumn?.name || ""}
                onValueChange={chooseColumn}
                options={columns.map((column) => ({
                  value: column.name,
                  label: column.name,
                  hint: normalizeTableColumnType(column.type),
                }))}
                mono
                searchable
              />
            </div>
            <div className="space-y-2">
              <Label htmlFor={`${formId}-operator`}>Condition</Label>
              <SelectMenu
                id={`${formId}-operator`}
                value={operator}
                onValueChange={(next) => {
                  setOperator(next as TableFilterOperator);
                  setError("");
                }}
                options={operatorOptions}
              />
            </div>
            {needsValue && selectedColumn && (
              <div className="space-y-2 sm:col-span-2">
                <Label htmlFor={`${formId}-value`}>Value</Label>
                {isEnumValue ? (
                  <SelectMenu
                    id={`${formId}-value`}
                    value={value}
                    onValueChange={(next) => {
                      setValue(next);
                      setError("");
                    }}
                    options={(selectedColumn.enum || []).map((item) => ({
                      value: String(item),
                      label: String(item),
                    }))}
                    placeholder="Select a value"
                  />
                ) : normalizedType === "jsonb" ? (
                  <Textarea
                    id={`${formId}-value`}
                    value={value}
                    onChange={(event) => {
                      setValue(event.target.value);
                      setError("");
                    }}
                    rows={5}
                    className="font-mono text-xs"
                    placeholder={'{"status":"open"}'}
                    aria-invalid={Boolean(error) || undefined}
                    aria-describedby={`${formId}-value-help${error ? ` ${formId}-value-error` : ""}`}
                    autoFocus
                  />
                ) : (
                  <Input
                    id={`${formId}-value`}
                    type={inputTypeFor(normalizedType, operator)}
                    step={stepFor(normalizedType)}
                    value={value}
                    onChange={(event) => {
                      setValue(event.target.value);
                      setError("");
                    }}
                    placeholder={valuePlaceholder(normalizedType, operator)}
                    aria-invalid={Boolean(error) || undefined}
                    aria-describedby={`${formId}-value-help${error ? ` ${formId}-value-error` : ""}`}
                    autoComplete="off"
                    autoFocus
                  />
                )}
                <p id={`${formId}-value-help`} className="text-xs text-foreground-muted">
                  {valueHelp(normalizedType, operator)}
                </p>
                {error && (
                  <p id={`${formId}-value-error`} role="alert" className="text-xs text-destructive">
                    {error}
                  </p>
                )}
              </div>
            )}
          </div>
          <DialogFooter className="border-t border-border bg-surface-2 px-5 py-4 sm:px-6">
            <Button type="button" variant="outline" onClick={() => onOpenChange(false)}>
              Cancel
            </Button>
            <Button type="submit" variant="default" disabled={!selectedColumn}>
              <Filter className="h-4 w-4" aria-hidden />
              Apply filter
            </Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  );
}

export function TableFilterBar({
  filters,
  onOpen,
  onRemove,
  onClear,
  onRefresh,
  refreshing = false,
}: {
  filters: TableFilter[];
  onOpen: () => void;
  onRemove: (index: number) => void;
  onClear: () => void;
  onRefresh: () => void;
  refreshing?: boolean;
}) {
  return (
    <div className="flex min-h-11 shrink-0 flex-wrap items-center gap-2 border-b border-border bg-surface px-3 py-1.5">
      <Button type="button" variant="outline" size="sm" onClick={onOpen}>
        <Filter className="h-3.5 w-3.5" aria-hidden />
        Filters
        {filters.length > 0 && <Badge variant="info-solid">{filters.length}</Badge>}
      </Button>
      {filters.length > 0 ? (
        <>
          <div className="flex min-w-0 flex-1 flex-wrap items-center gap-1.5" aria-label="Active filters">
            {filters.map((filter, index) => (
              <span
                key={`${filter.column}-${filter.operator}-${filter.value}-${index}`}
                className="inline-flex min-w-0 max-w-72 items-center gap-1 rounded-[var(--radius-full)] border border-border bg-surface-selected py-1 pl-2.5 pr-1 text-xs text-surface-selected-foreground"
              >
                <span className="truncate">{tableFilterLabel(filter)}</span>
                <button
                  type="button"
                  onClick={() => onRemove(index)}
                  className="inline-flex h-6 w-6 shrink-0 cursor-pointer items-center justify-center rounded-[var(--radius-full)] hover:bg-surface-hover focus:outline-none focus-visible:ring-2 focus-visible:ring-ring"
                  aria-label={`Remove filter: ${tableFilterLabel(filter)}`}
                >
                  <X className="h-3.5 w-3.5" aria-hidden />
                </button>
              </span>
            ))}
          </div>
          <Button type="button" variant="ghost" size="sm" onClick={onClear}>
            Clear all
          </Button>
        </>
      ) : (
        <span className="text-xs text-foreground-muted">All records</span>
      )}
      <Button
        type="button"
        variant="ghost"
        size="icon"
        className="ml-auto h-8 w-8"
        onClick={onRefresh}
        disabled={refreshing}
        aria-label="Refresh records"
      >
        <RefreshCw className={refreshing ? "h-3.5 w-3.5 animate-spin" : "h-3.5 w-3.5"} aria-hidden />
      </Button>
    </div>
  );
}

export function TablePagination({
  pageIndex,
  pageSize,
  total,
  itemCount,
  onPageChange,
  onPageSizeChange,
}: {
  pageIndex: number;
  pageSize: TablePageSize;
  total: number;
  itemCount: number;
  onPageChange: (pageIndex: number) => void;
  onPageSizeChange: (pageSize: TablePageSize) => void;
}) {
  const pageCount = Math.max(1, Math.ceil(total / pageSize));
  const start = total === 0 ? 0 : pageIndex * pageSize + 1;
  const end = total === 0 ? 0 : Math.min(pageIndex * pageSize + itemCount, total);
  const [pageDraft, setPageDraft] = useState(String(pageIndex + 1));

  useEffect(() => setPageDraft(String(pageIndex + 1)), [pageIndex]);

  function commitPage(event?: FormEvent) {
    event?.preventDefault();
    const parsed = Number(pageDraft);
    const next = Number.isSafeInteger(parsed) ? Math.min(Math.max(parsed, 1), pageCount) : pageIndex + 1;
    setPageDraft(String(next));
    onPageChange(next - 1);
  }

  return (
    <div className="flex min-h-12 shrink-0 flex-wrap items-center gap-3 border-t border-border bg-surface-2 px-3 py-1.5 text-xs text-foreground-muted">
      <span className="min-w-32 tabular-nums" aria-live="polite">
        {start}–{end} of {total}
      </span>
      <div className="ml-auto flex items-center gap-2">
        <Label htmlFor="table-page-size" className="hidden text-xs font-normal sm:inline">
          Rows per page
        </Label>
        <SelectMenu
          id="table-page-size"
          aria-label="Rows per page"
          value={String(pageSize)}
          onValueChange={(next) => onPageSizeChange(Number(next) as TablePageSize)}
          options={TABLE_PAGE_SIZES.map((size) => ({ value: String(size), label: String(size) }))}
          className="h-9 w-20"
        />
        <div className="flex items-center gap-1">
          <Button
            type="button"
            variant="outline"
            size="icon"
            className="hidden h-9 w-9 sm:inline-flex"
            onClick={() => onPageChange(0)}
            disabled={pageIndex === 0}
            aria-label="First page"
          >
            <ChevronsLeft className="h-4 w-4" aria-hidden />
          </Button>
          <Button
            type="button"
            variant="outline"
            size="icon"
            className="h-9 w-9"
            onClick={() => onPageChange(Math.max(0, pageIndex - 1))}
            disabled={pageIndex === 0}
            aria-label="Previous page"
          >
            <ChevronLeft className="h-4 w-4" aria-hidden />
          </Button>
          <form className="flex items-center gap-1.5" onSubmit={commitPage}>
            <Label htmlFor="table-page-number" className="sr-only">
              Page number
            </Label>
            <Input
              id="table-page-number"
              type="number"
              min={1}
              max={pageCount}
              inputMode="numeric"
              value={pageDraft}
              onChange={(event) => setPageDraft(event.target.value)}
              onBlur={() => commitPage()}
              className="h-9 w-14 px-2 text-center tabular-nums"
              aria-label={`Page number, ${pageCount} pages total`}
            />
            <span className="hidden whitespace-nowrap tabular-nums sm:inline">of {pageCount}</span>
          </form>
          <Button
            type="button"
            variant="outline"
            size="icon"
            className="h-9 w-9"
            onClick={() => onPageChange(Math.min(pageCount - 1, pageIndex + 1))}
            disabled={pageIndex >= pageCount - 1}
            aria-label="Next page"
          >
            <ChevronRight className="h-4 w-4" aria-hidden />
          </Button>
          <Button
            type="button"
            variant="outline"
            size="icon"
            className="hidden h-9 w-9 sm:inline-flex"
            onClick={() => onPageChange(pageCount - 1)}
            disabled={pageIndex >= pageCount - 1}
            aria-label="Last page"
          >
            <ChevronsRight className="h-4 w-4" aria-hidden />
          </Button>
        </div>
      </div>
    </div>
  );
}

function inputTypeFor(type: string, operator: TableFilterOperator): string {
  if (operator === "in") return "text";
  if (type === "date") return "date";
  if (["int", "float", "numeric"].includes(type)) return "number";
  return "text";
}

function stepFor(type: string): string | undefined {
  if (type === "int") return "1";
  if (["float", "numeric"].includes(type)) return "any";
  return undefined;
}

function valuePlaceholder(type: string, operator: TableFilterOperator): string {
  if (operator === "in") return "open, pending, resolved";
  if (type === "uuid") return "00000000-0000-4000-8000-000000000000";
  if (type === "timestamp") return "2026-09-02T13:45:00+09:00";
  if (["int", "float", "numeric"].includes(type)) return "0";
  return "Value";
}

function valueHelp(type: string, operator: TableFilterOperator): string {
  if (operator === "in") {
    return "Separate exact values with commas. Use “is” for one value that contains a comma.";
  }
  if (type === "jsonb") return "Enter the JSON object or array that each matching row must contain.";
  if (type === "timestamp") return "Use an ISO 8601 timestamp including a timezone when possible.";
  return "Filtering is applied by the server across the entire table.";
}
