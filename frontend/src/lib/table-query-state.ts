import type { VaultTableColumnInput } from "@/lib/api";

export const TABLE_PAGE_SIZES = [25, 50, 100] as const;
export type TablePageSize = (typeof TABLE_PAGE_SIZES)[number];
export type TableSortDirection = "asc" | "desc";

export type TableFilterOperator =
  | "eq"
  | "neq"
  | "contains"
  | "starts_with"
  | "gt"
  | "gte"
  | "lt"
  | "lte"
  | "in"
  | "is_null"
  | "not_null"
  | "is_true"
  | "is_false"
  | "json_contains";

export interface TableFilter {
  column: string;
  operator: TableFilterOperator;
  value: string;
}

export interface TableSort {
  column: string;
  direction: TableSortDirection;
}

export interface TableQueryState {
  pageIndex: number;
  pageSize: TablePageSize;
  sort: TableSort | null;
  filters: TableFilter[];
}

export interface TableApiFilter {
  column: string;
  expression: string;
}

export interface TableFilterOperatorOption {
  value: TableFilterOperator;
  label: string;
}

export const DEFAULT_TABLE_QUERY_STATE: TableQueryState = {
  pageIndex: 0,
  pageSize: 50,
  sort: null,
  filters: [],
};

export const DEFAULT_TABLE_SORT: TableSort = {
  column: "created_at",
  direction: "desc",
};

const FILTER_OPERATORS = new Set<TableFilterOperator>([
  "eq",
  "neq",
  "contains",
  "starts_with",
  "gt",
  "gte",
  "lt",
  "lte",
  "in",
  "is_null",
  "not_null",
  "is_true",
  "is_false",
  "json_contains",
]);

const OPERATOR_LABELS: Record<TableFilterOperator, string> = {
  eq: "is",
  neq: "is not",
  contains: "contains",
  starts_with: "starts with",
  gt: "is greater than",
  gte: "is at least",
  lt: "is less than",
  lte: "is at most",
  in: "is one of",
  is_null: "is empty",
  not_null: "is not empty",
  is_true: "is true",
  is_false: "is false",
  json_contains: "contains JSON",
};

const TEXT_OPERATORS: TableFilterOperator[] = [
  "contains",
  "eq",
  "neq",
  "starts_with",
  "in",
  "is_null",
  "not_null",
];
const NUMBER_DATE_OPERATORS: TableFilterOperator[] = [
  "eq",
  "neq",
  "gt",
  "gte",
  "lt",
  "lte",
  "is_null",
  "not_null",
];

export function normalizeTableColumnType(type: string | undefined): string {
  const normalized = (type || "text").toLowerCase();
  if (normalized === "number") return "numeric";
  if (normalized === "bool") return "boolean";
  if (normalized === "json") return "jsonb";
  return normalized;
}

export function tableFilterOperators(column: VaultTableColumnInput): TableFilterOperatorOption[] {
  const type = normalizeTableColumnType(column.type);
  let operators: TableFilterOperator[];
  if (type === "boolean") {
    operators = ["is_true", "is_false", "is_null", "not_null"];
  } else if (type === "enum" || Boolean(column.enum?.length)) {
    operators = ["eq", "neq", "in", "is_null", "not_null"];
  } else if (["int", "float", "numeric", "date", "timestamp"].includes(type)) {
    operators = NUMBER_DATE_OPERATORS;
  } else if (type === "uuid") {
    operators = ["eq", "neq", "in", "is_null", "not_null"];
  } else if (type === "jsonb") {
    operators = ["json_contains", "is_null", "not_null"];
  } else if (type === "text[]") {
    // The current row API exposes null checks for SQL arrays but does not yet
    // provide a typed array-containment operator. Avoid offering a control
    // that would compile to a query PostgreSQL cannot type safely.
    operators = ["is_null", "not_null"];
  } else {
    operators = TEXT_OPERATORS;
  }
  return operators.map((value) => ({ value, label: OPERATOR_LABELS[value] }));
}

export function tableFilterNeedsValue(operator: TableFilterOperator): boolean {
  return !["is_null", "not_null", "is_true", "is_false"].includes(operator);
}

export function validateTableFilter(
  filter: TableFilter,
  column: VaultTableColumnInput,
): string | null {
  if (!tableFilterNeedsValue(filter.operator)) return null;
  const value = filter.value.trim();
  if (!value) return "Enter a value for this filter.";

  const type = normalizeTableColumnType(column.type);
  const values = filter.operator === "in" ? splitListValue(value) : [value];
  if (filter.operator === "in" && values.length === 0) {
    return "Enter at least one comma-separated value.";
  }
  if (["int", "float", "numeric"].includes(type)) {
    if (values.some((item) => !Number.isFinite(Number(item)))) {
      return "Enter a valid number.";
    }
    if (type === "int" && values.some((item) => !Number.isSafeInteger(Number(item)))) {
      return "Enter a whole number.";
    }
  }
  if (type === "uuid") {
    const uuidPattern = /^[0-9a-f]{8}-[0-9a-f]{4}-[1-8][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i;
    if (values.some((item) => !uuidPattern.test(item))) return "Enter a valid UUID.";
  }
  if (filter.operator === "json_contains") {
    try {
      JSON.parse(value);
    } catch {
      return "Enter valid JSON.";
    }
  }
  return null;
}

export function compileTableFilter(filter: TableFilter): TableApiFilter {
  const value = filter.value.trim();
  let expression: string;
  switch (filter.operator) {
    case "contains":
      expression = `ilike.*${value}*`;
      break;
    case "starts_with":
      expression = `ilike.${value}*`;
      break;
    case "in":
      expression = `in.(${splitListValue(value).join(",")})`;
      break;
    case "is_null":
      expression = "is.null";
      break;
    case "not_null":
      expression = "not.is.null";
      break;
    case "is_true":
      expression = "is.true";
      break;
    case "is_false":
      expression = "is.false";
      break;
    case "json_contains": {
      // UI-authored values are validated before they enter query state. A
      // manually edited or stale URL can still carry malformed JSON, though;
      // pass that through so the row API can return a recoverable query error
      // instead of throwing while React builds the request.
      try {
        expression = `cs.${JSON.stringify(JSON.parse(value))}`;
      } catch {
        expression = `cs.${value}`;
      }
      break;
    }
    default:
      expression = `${filter.operator}.${value}`;
  }
  return { column: filter.column, expression };
}

export function tableOrder(sort: TableSort | null): string {
  const active = sort || DEFAULT_TABLE_SORT;
  if (active.column === "id") return `id.${active.direction}`;
  return `${active.column}.${active.direction},id.${active.direction}`;
}

export function tableFilterLabel(filter: TableFilter): string {
  const operator = OPERATOR_LABELS[filter.operator];
  if (!tableFilterNeedsValue(filter.operator)) return `${filter.column} ${operator}`;
  const value = filter.value.trim();
  const compact = value.length > 36 ? `${value.slice(0, 36)}…` : value;
  return `${filter.column} ${operator} “${compact}”`;
}

export function parseTableQueryState(search: URLSearchParams): TableQueryState {
  const rawPage = Number(search.get("page"));
  const rawSize = Number(search.get("size"));
  const pageSize = TABLE_PAGE_SIZES.includes(rawSize as TablePageSize)
    ? (rawSize as TablePageSize)
    : DEFAULT_TABLE_QUERY_STATE.pageSize;
  const sort = parseSort(search.get("sort"));
  const filters = search.getAll("f").flatMap((raw) => {
    const parsed = parseFilter(raw);
    return parsed ? [parsed] : [];
  });
  return {
    pageIndex: Number.isSafeInteger(rawPage) && rawPage > 0 ? rawPage - 1 : 0,
    pageSize,
    sort,
    filters,
  };
}

export function writeTableQueryState(
  current: URLSearchParams,
  state: TableQueryState,
): URLSearchParams {
  const next = new URLSearchParams(current);
  for (const key of ["page", "size", "sort", "f"]) next.delete(key);
  if (state.pageIndex > 0) next.set("page", String(state.pageIndex + 1));
  if (state.pageSize !== DEFAULT_TABLE_QUERY_STATE.pageSize) {
    next.set("size", String(state.pageSize));
  }
  if (state.sort) next.set("sort", `${state.sort.column}.${state.sort.direction}`);
  for (const filter of state.filters) next.append("f", serializeFilter(filter));
  return next;
}

function parseSort(raw: string | null): TableSort | null {
  if (!raw) return null;
  const match = /^([a-z][a-z0-9_]*)\.(asc|desc)$/.exec(raw);
  if (!match) return null;
  return { column: match[1], direction: match[2] as TableSortDirection };
}

function serializeFilter(filter: TableFilter): string {
  return `${filter.column}:${filter.operator}:${filter.value}`;
}

function parseFilter(raw: string): TableFilter | null {
  const first = raw.indexOf(":");
  const second = raw.indexOf(":", first + 1);
  if (first <= 0 || second <= first) return null;
  const column = raw.slice(0, first);
  const operator = raw.slice(first + 1, second) as TableFilterOperator;
  const value = raw.slice(second + 1);
  if (!/^[a-z][a-z0-9_]*$/.test(column) || !FILTER_OPERATORS.has(operator)) return null;
  if (tableFilterNeedsValue(operator) && !value.trim()) return null;
  return { column, operator, value };
}

function splitListValue(value: string): string[] {
  return value
    .split(",")
    .map((item) => item.trim())
    .filter(Boolean);
}
