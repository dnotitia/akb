import type { RelationRow } from "@/lib/api";
import { parseUri } from "@/lib/uri";

// Pure helpers for the Relations panel, lifted out of the component so the
// branchy bits (edge direction, URI routing) are unit-testable. A relation row
// from GET /relations carries only the "other side" (`uri`) plus a `direction`.

/**
 * Rebuild the (source, target) pair the unlink endpoint wants, from this
 * document's vantage point. An outgoing edge has this doc as the source; an
 * incoming edge has it as the target.
 */
export function edgeFor(
  row: RelationRow,
  sourceUri: string,
): { source: string; target: string } {
  return row.direction === "incoming"
    ? { source: row.uri, target: sourceUri }
    : { source: sourceUri, target: row.uri };
}

/**
 * Route a relation's other-side URI to its in-app path. Returns "#" when the URI
 * has no resolvable ref (e.g. a vault/coll URI) rather than a broken half-path —
 * a flat regex here once produced `/vault/x/doc/` blank screens for collection
 * docs, so this delegates to the canonical `parseUri`.
 */
export function hrefFor(row: RelationRow, vault: string): string {
  const p = parseUri(row.uri);
  const v = p?.vault ?? vault;
  const ref = p?.id ?? "";
  const kind = p?.kind ?? row.resource_type;
  if (!ref) return "#";
  if (kind === "table") return `/vault/${v}/table/${encodeURIComponent(ref)}`;
  if (kind === "file") return `/vault/${v}/file/${encodeURIComponent(ref)}`;
  return `/vault/${v}/doc/${encodeURIComponent(ref)}`;
}
