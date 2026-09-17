export interface PublishableTableColumn {
  name: string;
  type?: string;
  primary_key?: boolean;
}

export const DEFAULT_TABLE_PUBLICATION_ROWS = 100;
export const MAX_TABLE_PUBLICATION_ROWS = 10_000;

const TABLE_IDENTIFIER = /^[a-z][a-z0-9_]*$/;

export function isPublishableTableIdentifier(value: string): boolean {
  return TABLE_IDENTIFIER.test(value);
}

export function buildTablePublicationQuery({
  table,
  columns,
  selectedColumns,
  rowLimit,
}: {
  table: string;
  columns: PublishableTableColumn[];
  selectedColumns: string[];
  rowLimit: number;
}): string {
  if (!isPublishableTableIdentifier(table)) {
    throw new Error("The table name cannot be published safely.");
  }
  if (
    !Number.isInteger(rowLimit) ||
    rowLimit < 1 ||
    rowLimit > MAX_TABLE_PUBLICATION_ROWS
  ) {
    throw new Error(
      `Row limit must be between 1 and ${MAX_TABLE_PUBLICATION_ROWS.toLocaleString()}.`,
    );
  }

  const selected = new Set(selectedColumns);
  const orderedColumns = columns
    .map((column) => column.name)
    .filter((name) => selected.has(name));
  if (orderedColumns.length === 0) {
    throw new Error("Select at least one column to publish.");
  }
  if (orderedColumns.some((name) => !isPublishableTableIdentifier(name))) {
    throw new Error("One or more columns cannot be published safely.");
  }

  const primaryKeys = columns
    .filter(
      (column) => column.primary_key && isPublishableTableIdentifier(column.name),
    )
    .map((column) => column.name);
  const orderBy = primaryKeys.length ? ` ORDER BY ${primaryKeys.join(", ")}` : "";
  return `SELECT ${orderedColumns.join(", ")} FROM ${table}${orderBy} LIMIT ${rowLimit}`;
}
