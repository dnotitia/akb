import type { SearchOptions } from "./api";

export const SEARCH_FILTER_KEYS = [
  "source",
  "doc_type",
  "tag",
  "collection",
  "include_archived",
  "archive_scope",
  "regex",
  "case_sensitive",
] as const;

export function readSearchOptions(params: URLSearchParams): SearchOptions {
  const source = params.get("source");
  const archiveScope = params.get("archive_scope");
  return {
    collection: params.get("collection") || undefined,
    source_type:
      source === "document" || source === "file" || source === "table"
        ? source
        : undefined,
    doc_types: [...new Set(params.getAll("doc_type").filter(Boolean))],
    tags: [...new Set(params.getAll("tag").filter(Boolean))],
    include_archived: params.get("include_archived") === "true",
    ...(archiveScope === "unarchived" ||
    archiveScope === "archived" ||
    archiveScope === "all"
      ? { archive_scope: archiveScope }
      : {}),
    regex: params.get("regex") === "true",
    case_sensitive: params.get("case_sensitive") === "true",
  };
}

export function setSearchValues(
  params: URLSearchParams,
  key: string,
  values: string[],
): URLSearchParams {
  const next = new URLSearchParams(params);
  next.delete(key);
  for (const value of [...new Set(values.filter(Boolean))])
    next.append(key, value);
  return next;
}
