import { useEffect, useId, useMemo, useRef, useState, type FormEvent } from "react";
import { Braces, Plus, Save, TableRowsSplit } from "lucide-react";
import { Alert } from "@/components/ui/alert";
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
import { TableRowConflictError, type VaultTableColumnInput } from "@/lib/api";
import { cn } from "@/lib/utils";

type RowMode = "create" | "edit";

interface TableRowDialogProps {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  mode: RowMode;
  table: string;
  columns: VaultTableColumnInput[];
  row?: Record<string, unknown> | null;
  onSave: (
    values: Record<string, unknown>,
    options?: { force?: boolean },
  ) => Promise<void>;
  onReloadConflict?: () => Promise<Record<string, unknown> | null>;
}

const UNSET_OPTION = "__akb_unset__";
const OMIT = Symbol("omit");
const SYSTEM_COLUMNS = new Set(["id", "created_by", "created_at", "updated_at"]);

export function TableRowDialog({
  open,
  onOpenChange,
  mode,
  table,
  columns,
  row,
  onSave,
  onReloadConflict,
}: TableRowDialogProps) {
  const formRef = useRef<HTMLFormElement>(null);
  const formId = useId();
  const [values, setValues] = useState<Record<string, string>>({});
  const [nullColumns, setNullColumns] = useState<Set<string>>(new Set());
  const [touched, setTouched] = useState<Set<string>>(new Set());
  const [errors, setErrors] = useState<Record<string, string>>({});
  const [saving, setSaving] = useState(false);
  const [submitError, setSubmitError] = useState("");
  const [conflict, setConflict] = useState(false);
  const [pendingValues, setPendingValues] = useState<Record<string, unknown> | null>(null);

  const editableColumns = useMemo(
    () => columns.filter((column) => !isSystemColumn(column.name)),
    [columns],
  );

  useEffect(() => {
    if (!open) return;
    const initialValues: Record<string, string> = {};
    const initialNulls = new Set<string>();
    for (const column of editableColumns) {
      const current = row?.[column.name];
      if (mode === "edit" && current === null) initialNulls.add(column.name);
      initialValues[column.name] = mode === "edit" ? valueForInput(current, column.type) : "";
    }
    setValues(initialValues);
    setNullColumns(initialNulls);
    setTouched(new Set());
    setErrors({});
    setSubmitError("");
    setConflict(false);
    setPendingValues(null);
  }, [editableColumns, mode, open, row]);

  function updateValue(column: VaultTableColumnInput, value: string) {
    setValues((current) => ({ ...current, [column.name]: value }));
    setErrors((current) => ({ ...current, [column.name]: "" }));
    setSubmitError("");
  }

  function toggleNull(column: VaultTableColumnInput, checked: boolean) {
    setNullColumns((current) => {
      const next = new Set(current);
      if (checked) next.add(column.name);
      else next.delete(column.name);
      return next;
    });
    setErrors((current) => ({ ...current, [column.name]: "" }));
    setSubmitError("");
  }

  function validateField(column: VaultTableColumnInput) {
    const result = parseField(
      column,
      values[column.name] ?? "",
      nullColumns.has(column.name),
      mode,
    );
    setErrors((current) => ({
      ...current,
      [column.name]: typeof result === "string" ? result : "",
    }));
  }

  async function submit(event: FormEvent) {
    event.preventDefault();
    setSubmitError("");
    const nextErrors: Record<string, string> = {};
    const payload: Record<string, unknown> = {};

    for (const column of editableColumns) {
      const parsed = parseField(
        column,
        values[column.name] ?? "",
        nullColumns.has(column.name),
        mode,
      );
      if (typeof parsed === "string") {
        nextErrors[column.name] = parsed;
        continue;
      }
      if (parsed.value === OMIT) continue;
      if (mode === "edit" && valuesEqual(parsed.value, row?.[column.name])) continue;
      payload[column.name] = parsed.value;
    }

    setTouched(new Set(editableColumns.map((column) => column.name)));
    setErrors(nextErrors);
    if (Object.keys(nextErrors).length > 0) {
      requestAnimationFrame(() => {
        formRef.current?.querySelector<HTMLElement>("[aria-invalid='true']")?.focus();
      });
      return;
    }
    if (mode === "edit" && Object.keys(payload).length === 0) {
      setSubmitError("No row values have changed.");
      return;
    }

    setPendingValues(payload);
    await save(payload);
  }

  async function save(payload: Record<string, unknown>, force = false) {
    setSaving(true);
    setSubmitError("");
    try {
      await onSave(payload, { force });
      setConflict(false);
      onOpenChange(false);
    } catch (caught: unknown) {
      if (caught instanceof TableRowConflictError) {
        setConflict(true);
      } else {
        setSubmitError(
          caught instanceof Error
            ? caught.message
            : `The row could not be ${mode === "create" ? "added" : "updated"}.`,
        );
      }
    } finally {
      setSaving(false);
    }
  }

  async function reloadCurrentValues() {
    if (!onReloadConflict) return;
    setSaving(true);
    setSubmitError("");
    try {
      const latest = await onReloadConflict();
      if (!latest) {
        setConflict(false);
        setSubmitError("This row no longer exists. Close the editor to return to the table.");
        return;
      }
      const nextValues: Record<string, string> = {};
      const nextNulls = new Set<string>();
      for (const column of editableColumns) {
        const current = latest[column.name];
        if (current === null) nextNulls.add(column.name);
        nextValues[column.name] = valueForInput(current, column.type);
      }
      setValues(nextValues);
      setNullColumns(nextNulls);
      setTouched(new Set());
      setErrors({});
      setPendingValues(null);
      setConflict(false);
    } catch (caught: unknown) {
      setSubmitError(caught instanceof Error ? caught.message : "The current row could not be loaded.");
    } finally {
      setSaving(false);
    }
  }

  return (
    <Dialog open={open} onOpenChange={(next) => !saving && onOpenChange(next)}>
      <DialogContent
        className="max-w-3xl gap-0 overflow-hidden p-0"
        onEscapeKeyDown={(event) => saving && event.preventDefault()}
        onPointerDownOutside={(event) => saving && event.preventDefault()}
      >
        <DialogHeader className="border-b border-border px-5 py-4 pr-14 sm:px-6">
          <DialogTitle className="flex items-center gap-2">
            {mode === "create" ? (
              <Plus className="h-5 w-5 text-accent-strong" aria-hidden />
            ) : (
              <TableRowsSplit className="h-5 w-5 text-primary" aria-hidden />
            )}
            {mode === "create" ? "Add row" : "Edit row"}
          </DialogTitle>
          <DialogDescription>
            {mode === "create"
              ? `Add one record to ${table}. Empty optional fields use their database default.`
              : `Update this record in ${table}. System identity and audit fields stay unchanged.`}
          </DialogDescription>
        </DialogHeader>

        <form
          id={formId}
          ref={formRef}
          className="flex min-h-0 flex-1 flex-col overflow-hidden"
          onSubmit={(event) => void submit(event)}
        >
          <div className="min-h-0 flex-1 overflow-y-auto px-5 py-5 sm:px-6 rail-scroll">
            {editableColumns.length === 0 ? (
              <Alert variant="warning" title="No editable columns">
                This table only exposes system-managed fields, so a row form cannot be generated.
              </Alert>
            ) : (
              <fieldset disabled={saving} className="grid gap-4 sm:grid-cols-2">
                <legend className="sr-only">Row values</legend>
                {editableColumns.map((column, index) => {
                  const error = touched.has(column.name) ? errors[column.name] : "";
                  const nullable = !column.required && !column.primary_key;
                  const isNull = nullColumns.has(column.name);
                  const fieldId = `${formId}-${column.name}`;
                  return (
                    <div
                      key={column.name}
                      className={cn(
                        "min-w-0 rounded-[var(--radius-md)] border border-border bg-background p-3",
                        isWideField(column.type) && "sm:col-span-2",
                      )}
                    >
                      <div className="mb-2 flex min-w-0 items-start justify-between gap-3">
                        <div className="min-w-0">
                          <Label htmlFor={fieldId} className="block truncate font-mono text-xs">
                            {column.name}
                          </Label>
                          <p className="mt-0.5 text-xs text-foreground-muted">
                            {friendlyType(column.type)}
                            {column.required ? " · required" : " · optional"}
                            {column.default !== undefined && column.default !== null
                              ? " · has default"
                              : ""}
                          </p>
                        </div>
                        {nullable && (
                          <label className="inline-flex min-h-8 shrink-0 cursor-pointer items-center gap-2 text-xs text-foreground-muted">
                            <input
                              type="checkbox"
                              checked={isNull}
                              onChange={(event) => toggleNull(column, event.target.checked)}
                              className="h-4 w-4 accent-primary"
                            />
                            Null
                          </label>
                        )}
                      </div>
                      <RowField
                        id={fieldId}
                        column={column}
                        value={values[column.name] ?? ""}
                        disabled={isNull || saving}
                        invalid={Boolean(error)}
                        describedBy={`${fieldId}-help${error ? ` ${fieldId}-error` : ""}`}
                        autoFocus={index === 0}
                        onChange={(value) => updateValue(column, value)}
                        onBlur={() => {
                          setTouched((current) => new Set(current).add(column.name));
                          validateField(column);
                        }}
                      />
                      <p id={`${fieldId}-help`} className="mt-1.5 text-xs text-foreground-muted">
                        {fieldHelp(column, mode)}
                      </p>
                      {error && (
                        <p id={`${fieldId}-error`} role="alert" className="mt-1 text-xs text-destructive">
                          {error}
                        </p>
                      )}
                    </div>
                  );
                })}
              </fieldset>
            )}
            {conflict && (
              <Alert variant="warning" title="This row has newer changes" className="mt-4">
                <p>
                  Another update was saved after you opened this row. Your draft is still here.
                </p>
                <div className="mt-3 flex flex-wrap gap-2">
                  <Button
                    type="button"
                    variant="outline"
                    size="sm"
                    onClick={() => void reloadCurrentValues()}
                    disabled={saving || !onReloadConflict}
                  >
                    Reload current values
                  </Button>
                  <Button
                    type="button"
                    variant="default"
                    size="sm"
                    onClick={() => pendingValues && void save(pendingValues, true)}
                    disabled={saving || !pendingValues}
                  >
                    Overwrite anyway
                  </Button>
                </div>
              </Alert>
            )}
            {submitError && (
              <Alert variant="destructive" title="Row could not be saved" className="mt-4">
                {submitError}
              </Alert>
            )}
          </div>

          <DialogFooter className="shrink-0 border-t border-border bg-surface-2 px-5 py-4 sm:px-6">
            <Button type="button" variant="outline" onClick={() => onOpenChange(false)} disabled={saving}>
              Cancel
            </Button>
            <Button
              type="submit"
              variant={mode === "create" ? "accent" : "default"}
              loading={saving}
              disabled={editableColumns.length === 0}
            >
              {!saving && (mode === "create" ? <Plus className="h-4 w-4" aria-hidden /> : <Save className="h-4 w-4" aria-hidden />)}
              {saving ? "Saving row…" : mode === "create" ? "Add row" : "Save changes"}
            </Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  );
}

function RowField({
  id,
  column,
  value,
  disabled,
  invalid,
  describedBy,
  autoFocus,
  onChange,
  onBlur,
}: {
  id: string;
  column: VaultTableColumnInput;
  value: string;
  disabled: boolean;
  invalid: boolean;
  describedBy: string;
  autoFocus: boolean;
  onChange: (value: string) => void;
  onBlur: () => void;
}) {
  const normalized = normalizeType(column.type);
  const common = {
    id,
    disabled,
    "aria-invalid": invalid || undefined,
    "aria-describedby": describedBy,
  };

  if (normalized === "boolean") {
    return (
      <SelectMenu
        {...common}
        value={value || UNSET_OPTION}
        onValueChange={(next) => {
          onChange(next === UNSET_OPTION ? "" : next);
        }}
        options={[
          { value: UNSET_OPTION, label: column.required ? "Select a value" : "Not set" },
          { value: "true", label: "True" },
          { value: "false", label: "False" },
        ]}
      />
    );
  }

  if (normalized === "enum" && column.enum?.length) {
    return (
      <SelectMenu
        {...common}
        value={value || UNSET_OPTION}
        onValueChange={(next) => {
          onChange(next === UNSET_OPTION ? "" : next);
        }}
        options={[
          { value: UNSET_OPTION, label: column.required ? "Select a value" : "Not set" },
          ...column.enum.map((option) => ({ value: String(option), label: String(option) })),
        ]}
      />
    );
  }

  if (normalized === "jsonb" || normalized === "text[]") {
    return (
      <div className="relative">
        <Braces className="pointer-events-none absolute left-3 top-3 h-4 w-4 text-foreground-muted" aria-hidden />
        <Textarea
          {...common}
          value={value}
          onChange={(event) => onChange(event.target.value)}
          onBlur={onBlur}
          autoFocus={autoFocus}
          rows={normalized === "jsonb" ? 5 : 3}
          spellCheck={false}
          className="pl-9 font-mono text-xs"
          placeholder={normalized === "jsonb" ? '{"status":"open"}' : '["one","two"]'}
        />
      </div>
    );
  }

  const inputType = normalized === "date" ? "date" : normalized === "int" || normalized === "float" || normalized === "numeric" ? "number" : "text";
  return (
    <Input
      {...common}
      type={inputType}
      step={normalized === "int" ? "1" : normalized === "float" || normalized === "numeric" ? "any" : undefined}
      inputMode={normalized === "int" ? "numeric" : normalized === "float" || normalized === "numeric" ? "decimal" : undefined}
      value={value}
      onChange={(event) => onChange(event.target.value)}
      onBlur={onBlur}
      autoFocus={autoFocus}
      autoComplete="off"
      spellCheck={normalized === "text"}
      className={cn(normalized === "uuid" || normalized === "timestamp" ? "font-mono text-xs" : undefined)}
      placeholder={placeholderFor(column)}
    />
  );
}

function parseField(
  column: VaultTableColumnInput,
  rawValue: string,
  isNull: boolean,
  mode: RowMode,
): { value: unknown | typeof OMIT } | string {
  if (isNull) {
    if (column.required || column.primary_key) return `${column.name} cannot be null.`;
    return { value: null };
  }

  const normalized = normalizeType(column.type);
  const empty = rawValue === "";
  if (empty && mode === "create" && !column.required) return { value: OMIT };
  if (empty && mode === "create" && column.default !== undefined && column.default !== null) {
    return { value: OMIT };
  }
  if (empty && (column.required || normalized !== "text")) {
    return `${column.name} requires a ${friendlyType(column.type).toLowerCase()} value.`;
  }

  if (normalized === "boolean") {
    if (rawValue !== "true" && rawValue !== "false") return `${column.name} must be true or false.`;
    return { value: rawValue === "true" };
  }
  if (normalized === "int") {
    const value = Number(rawValue);
    if (!Number.isSafeInteger(value)) return `${column.name} must be a whole number.`;
    return { value };
  }
  if (normalized === "float" || normalized === "numeric") {
    const value = Number(rawValue);
    if (!Number.isFinite(value)) return `${column.name} must be a valid number.`;
    // Keep arbitrary-precision NUMERIC input as text. The backend accepts a
    // decimal string and avoids the binary-float expansion visible with JSON
    // numbers such as 0.87. FLOAT remains an actual JSON number.
    return { value: normalized === "numeric" ? rawValue : value };
  }
  if (normalized === "jsonb") {
    try {
      return { value: JSON.parse(rawValue) };
    } catch {
      return `${column.name} must contain valid JSON.`;
    }
  }
  if (normalized === "text[]") {
    try {
      const value = JSON.parse(rawValue);
      if (!Array.isArray(value) || value.some((item) => typeof item !== "string")) {
        return `${column.name} must be a JSON array of strings.`;
      }
      return { value };
    } catch {
      return `${column.name} must be a JSON array of strings.`;
    }
  }
  if (normalized === "uuid" && rawValue && !/^[0-9a-f]{8}-[0-9a-f]{4}-[1-8][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i.test(rawValue)) {
    return `${column.name} must be a valid UUID.`;
  }
  return { value: rawValue };
}

function valueForInput(value: unknown, type: string): string {
  if (value === null || value === undefined) return "";
  const normalized = normalizeType(type);
  if (normalized === "jsonb" || normalized === "text[]") {
    return JSON.stringify(value, null, 2);
  }
  if (normalized === "boolean") return value ? "true" : "false";
  return String(value);
}

function valuesEqual(left: unknown, right: unknown): boolean {
  if (left === right) return true;
  try {
    return JSON.stringify(left) === JSON.stringify(right);
  } catch {
    return false;
  }
}

function normalizeType(type: string | undefined): string {
  const normalized = (type || "text").toLowerCase();
  if (normalized === "json") return "jsonb";
  if (normalized === "number") return "numeric";
  return normalized;
}

function friendlyType(type: string | undefined): string {
  const normalized = normalizeType(type);
  const labels: Record<string, string> = {
    text: "Text",
    int: "Integer",
    float: "Float",
    numeric: "Number",
    boolean: "Boolean",
    uuid: "UUID",
    date: "Date",
    timestamp: "Timestamp",
    jsonb: "JSON",
    "text[]": "Text list",
    enum: "Choice",
  };
  return labels[normalized] || normalized;
}

function isSystemColumn(name: string): boolean {
  return SYSTEM_COLUMNS.has(name);
}

function isWideField(type: string | undefined): boolean {
  const normalized = normalizeType(type);
  return normalized === "jsonb" || normalized === "text[]";
}

function placeholderFor(column: VaultTableColumnInput): string {
  if (column.default !== undefined && column.default !== null) return `Default: ${String(column.default)}`;
  const normalized = normalizeType(column.type);
  if (normalized === "uuid") return "00000000-0000-4000-8000-000000000000";
  if (normalized === "timestamp") return "2026-08-31T13:45:00+09:00";
  return column.required ? "Required value" : "Optional";
}

function fieldHelp(column: VaultTableColumnInput, mode: RowMode): string {
  if (mode === "create" && column.default !== undefined && column.default !== null) {
    return `Leave empty to use ${String(column.default)}.`;
  }
  const normalized = normalizeType(column.type);
  if (normalized === "jsonb") return "Enter a JSON object, array, string, number, boolean, or null.";
  if (normalized === "text[]") return "Use JSON array syntax; every item must be text.";
  if (normalized === "timestamp") return "Use an ISO 8601 timestamp including a timezone when possible.";
  return column.required ? "A value is required by the table schema." : "Optional field.";
}
