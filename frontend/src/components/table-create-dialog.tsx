import { useEffect, useRef, useState, type FormEvent, type RefObject } from "react";
import { FolderOpen, Plus, Table2, Trash2 } from "lucide-react";
import {
  createVaultTable,
  type VaultTableColumnInput,
} from "@/lib/api";
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

type EditableColumn = VaultTableColumnInput & { key: number };

const IDENTIFIER = /^[a-z][a-z0-9_]*$/;
const RESERVED_COLUMNS = new Set(["id", "created_at", "updated_at", "created_by"]);
const COLUMN_TYPES = [
  { value: "text", label: "Text" },
  { value: "int", label: "Integer" },
  { value: "float", label: "Float" },
  { value: "numeric", label: "Numeric" },
  { value: "boolean", label: "Boolean" },
  { value: "uuid", label: "UUID" },
  { value: "date", label: "Date" },
  { value: "timestamp", label: "Timestamp" },
  { value: "jsonb", label: "JSON" },
  { value: "text[]", label: "Text list" },
] as const;

const firstColumn = (): EditableColumn => ({
  key: 1,
  name: "",
  type: "text",
  required: false,
  unique: false,
});

export function TableCreateDialog({
  open,
  onOpenChange,
  vault,
  initialCollection = "",
  onCreated,
  returnFocusRef,
}: {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  vault: string;
  initialCollection?: string;
  onCreated: (tableName: string) => void;
  returnFocusRef?: RefObject<HTMLElement | null>;
}) {
  const formRef = useRef<HTMLFormElement>(null);
  const nextColumnKey = useRef(2);
  const [name, setName] = useState("");
  const [description, setDescription] = useState("");
  const [collection, setCollection] = useState("");
  const [columns, setColumns] = useState<EditableColumn[]>(() => [firstColumn()]);
  const [submitted, setSubmitted] = useState(false);
  const [creating, setCreating] = useState(false);
  const [error, setError] = useState("");

  useEffect(() => {
    if (open) {
      setCollection(initialCollection.trim());
      return;
    }
    setName("");
    setDescription("");
    setCollection("");
    setColumns([firstColumn()]);
    nextColumnKey.current = 2;
    setSubmitted(false);
    setError("");
  }, [open, initialCollection]);

  const tableNameError = identifierError(name, "Table name");
  const columnErrors = columns.map((column, index) =>
    columnError(column.name, index, columns),
  );
  const invalid = Boolean(tableNameError || columnErrors.some(Boolean));

  function updateColumn(key: number, patch: Partial<EditableColumn>) {
    setColumns((current) =>
      current.map((column) =>
        column.key === key ? { ...column, ...patch } : column,
      ),
    );
    setError("");
  }

  function addColumn() {
    const key = nextColumnKey.current++;
    setColumns((current) => [
      ...current,
      { key, name: "", type: "text", required: false, unique: false },
    ]);
    requestAnimationFrame(() => {
      document.getElementById(`table-column-name-${key}`)?.focus();
    });
  }

  async function submit(event: FormEvent) {
    event.preventDefault();
    setSubmitted(true);
    setError("");
    if (invalid) {
      requestAnimationFrame(() => {
        formRef.current
          ?.querySelector<HTMLElement>("[aria-invalid='true']")
          ?.focus();
      });
      return;
    }

    setCreating(true);
    try {
      await createVaultTable(vault, {
        name: name.trim(),
        description,
        collection,
        columns: columns.map(({ key: _key, ...column }) => ({
          ...column,
          name: column.name.trim(),
        })),
      });
      onCreated(name.trim());
      onOpenChange(false);
    } catch (caught: unknown) {
      setError(
        caught instanceof Error
          ? caught.message
          : "The table could not be created. Check the schema and try again.",
      );
    } finally {
      setCreating(false);
    }
  }

  return (
    <Dialog open={open} onOpenChange={(next) => !creating && onOpenChange(next)}>
      <DialogContent
        className="max-w-3xl gap-0 p-0"
        onEscapeKeyDown={(event) => creating && event.preventDefault()}
        onPointerDownOutside={(event) => creating && event.preventDefault()}
        onCloseAutoFocus={(event) => {
          if (!returnFocusRef?.current) return;
          event.preventDefault();
          returnFocusRef.current.focus();
        }}
      >
        <DialogHeader className="border-b border-border px-6 py-5 pr-14">
          <DialogTitle className="flex items-center gap-2">
            <Table2 className="h-5 w-5 text-primary" aria-hidden />
            Create a table
          </DialogTitle>
          <DialogDescription>
            Define a small, queryable schema for structured knowledge in {vault}.
          </DialogDescription>
        </DialogHeader>

        <form ref={formRef} onSubmit={(event) => void submit(event)}>
          <div className="space-y-5 p-6">
            <div className="grid gap-4 sm:grid-cols-2">
              <div>
                <Label htmlFor="table-name" className="mb-1.5 block">
                  Table name <span className="text-foreground-muted">*</span>
                </Label>
                <Input
                  id="table-name"
                  autoFocus
                  value={name}
                  onChange={(event) => {
                    setName(event.target.value);
                    setError("");
                  }}
                  placeholder="incident_reports"
                  autoComplete="off"
                  spellCheck={false}
                  aria-invalid={submitted && Boolean(tableNameError)}
                  aria-describedby="table-name-help table-name-error"
                  disabled={creating}
                  className="font-mono"
                />
                <p id="table-name-help" className="mt-1.5 text-xs text-foreground-muted">
                  Lowercase letters, numbers, and underscores.
                </p>
                {submitted && tableNameError && (
                  <p id="table-name-error" role="alert" className="mt-1 text-xs text-destructive">
                    {tableNameError}
                  </p>
                )}
              </div>

              <div>
                <Label htmlFor="table-collection" className="mb-1.5 block">
                  Collection <span className="text-foreground-muted">(optional)</span>
                </Label>
                <div className="relative">
                  <FolderOpen
                    className="pointer-events-none absolute left-3 top-1/2 h-4 w-4 -translate-y-1/2 text-foreground-muted"
                    aria-hidden
                  />
                  <Input
                    id="table-collection"
                    value={collection}
                    onChange={(event) => setCollection(event.target.value)}
                    placeholder="operations/data"
                    className="pl-9"
                    disabled={creating}
                  />
                </div>
                <p className="mt-1.5 text-xs text-foreground-muted">
                  Leave empty to place it at the Vault root.
                </p>
              </div>
            </div>

            <div>
              <Label htmlFor="table-description" className="mb-1.5 block">
                Description <span className="text-foreground-muted">(optional)</span>
              </Label>
              <Textarea
                id="table-description"
                value={description}
                onChange={(event) => setDescription(event.target.value)}
                placeholder="What each row represents and when to use this table"
                rows={2}
                disabled={creating}
                className="resize-y"
              />
            </div>

            <fieldset>
              <div className="mb-2 flex items-center justify-between gap-3">
                <div>
                  <legend className="text-sm font-semibold text-foreground">Columns</legend>
                  <p className="mt-0.5 text-xs text-foreground-muted">
                    AKB adds id, created_at, updated_at, and created_by automatically.
                  </p>
                </div>
                <Button
                  type="button"
                  variant="outline"
                  size="sm"
                  onClick={addColumn}
                  disabled={creating}
                >
                  <Plus className="h-4 w-4" aria-hidden />
                  Add column
                </Button>
              </div>

              <div className="space-y-2">
                {columns.map((column, index) => {
                  const fieldError = submitted ? columnErrors[index] : "";
                  return (
                    <div
                      key={column.key}
                      className="rounded-[var(--radius-md)] border border-border bg-background p-3"
                    >
                      <div className="mb-2 flex items-center justify-between gap-3">
                        <span className="text-xs font-semibold text-foreground">
                          Column {index + 1}
                        </span>
                        <Button
                          type="button"
                          variant="ghost"
                          size="icon"
                          aria-label={`Remove column ${index + 1}`}
                          onClick={() =>
                            setColumns((current) =>
                              current.filter((item) => item.key !== column.key),
                            )
                          }
                          disabled={columns.length === 1 || creating}
                          className="h-8 w-8 text-foreground-muted hover:text-destructive"
                        >
                          <Trash2 className="h-4 w-4" aria-hidden />
                        </Button>
                      </div>
                      <div className="grid gap-3 sm:grid-cols-[minmax(0,1fr)_11rem_auto] sm:items-end">
                        <div>
                          <Label
                            htmlFor={`table-column-name-${column.key}`}
                            className="mb-1.5 block text-xs"
                          >
                            Name
                          </Label>
                          <Input
                            id={`table-column-name-${column.key}`}
                            value={column.name}
                            onChange={(event) =>
                              updateColumn(column.key, { name: event.target.value })
                            }
                            placeholder="status"
                            autoComplete="off"
                            spellCheck={false}
                            aria-invalid={Boolean(fieldError)}
                            aria-describedby={`table-column-error-${column.key}`}
                            disabled={creating}
                            className="font-mono"
                          />
                        </div>
                        <div>
                          <Label
                            htmlFor={`table-column-type-${column.key}`}
                            className="mb-1.5 block text-xs"
                          >
                            Type
                          </Label>
                          <SelectMenu
                            id={`table-column-type-${column.key}`}
                            value={column.type}
                            onValueChange={(value) =>
                              updateColumn(column.key, { type: value })
                            }
                            options={[...COLUMN_TYPES]}
                            disabled={creating}
                          />
                        </div>
                        <div className="flex min-h-10 items-center gap-4 sm:pb-0.5">
                          <label className="inline-flex min-h-8 cursor-pointer items-center gap-2 text-xs text-foreground">
                            <input
                              type="checkbox"
                              checked={Boolean(column.required)}
                              onChange={(event) =>
                                updateColumn(column.key, { required: event.target.checked })
                              }
                              disabled={creating}
                              className="h-4 w-4 accent-primary"
                            />
                            Required
                          </label>
                          <label className="inline-flex min-h-8 cursor-pointer items-center gap-2 text-xs text-foreground">
                            <input
                              type="checkbox"
                              checked={Boolean(column.unique)}
                              onChange={(event) =>
                                updateColumn(column.key, { unique: event.target.checked })
                              }
                              disabled={creating}
                              className="h-4 w-4 accent-primary"
                            />
                            Unique
                          </label>
                        </div>
                      </div>
                      {fieldError && (
                        <p
                          id={`table-column-error-${column.key}`}
                          role="alert"
                          className="mt-1.5 text-xs text-destructive"
                        >
                          {fieldError}
                        </p>
                      )}
                    </div>
                  );
                })}
              </div>
            </fieldset>

            {error && (
              <Alert variant="destructive" title="Table creation failed">
                {error}
              </Alert>
            )}
          </div>

          <DialogFooter className="border-t border-border bg-surface-2 px-6 py-4">
            <Button
              type="button"
              variant="outline"
              onClick={() => onOpenChange(false)}
              disabled={creating}
            >
              Cancel
            </Button>
            <Button type="submit" variant="accent" loading={creating}>
              {!creating && <Table2 className="h-4 w-4" aria-hidden />}
              {creating ? "Creating table…" : "Create table"}
            </Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  );
}

function identifierError(value: string, label: string): string {
  const trimmed = value.trim();
  if (!trimmed) return `${label} is required.`;
  if (!IDENTIFIER.test(trimmed)) {
    return `${label} must start with a lowercase letter and use only letters, numbers, or underscores.`;
  }
  return "";
}

function columnError(
  value: string,
  index: number,
  columns: EditableColumn[],
): string {
  const base = identifierError(value, `Column ${index + 1} name`);
  if (base) return base;
  const normalized = value.trim();
  if (RESERVED_COLUMNS.has(normalized)) {
    return `“${normalized}” is added automatically. Choose another name.`;
  }
  if (
    columns.some(
      (column, candidate) =>
        candidate !== index && column.name.trim() === normalized,
    )
  ) {
    return `Column name “${normalized}” is used more than once.`;
  }
  return "";
}
