export function documentAuthorName(doc: { created_by_name?: unknown; created_by?: unknown }) {
  return [doc.created_by_name, doc.created_by]
    .filter((value): value is string => typeof value === "string")
    .map(value => value.trim())
    .find(value => value && !/^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i.test(value)
      && !/^(unknown(?: author)?|n\/a|null|undefined)$/i.test(value));
}
