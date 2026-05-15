// Parsing helpers for the canonical `akb://{vault}/{type}/{identifier}`
// URI scheme. Backend `app/services/uri_service.py` has the matching
// Python parser — keep both in sync.
//
// Why a regex instead of `.split("/doc/")`: plain split is wrong when
// the doc path itself contains `/doc/` (e.g. an `archive/doc/...` doc
// inside an `archive` collection). The regex anchors on the first
// `/(doc|table|file)/` after the vault segment.

const URI_RE = /^akb:\/\/([^/]+)\/(doc|table|file)\/(.+)$/;

export interface ParsedUri {
  vault: string;
  kind: "doc" | "table" | "file";
  /** Path for `doc`, table name for `table`, UUID for `file`. */
  id: string;
}

export function parseUri(uri: string | null | undefined): ParsedUri | null {
  if (!uri) return null;
  const m = uri.match(URI_RE);
  if (!m) return null;
  return { vault: m[1], kind: m[2] as ParsedUri["kind"], id: m[3] };
}

export function parseDocUri(uri: string | null | undefined): ParsedUri | null {
  const p = parseUri(uri);
  return p && p.kind === "doc" ? p : null;
}

export function parseFileUri(uri: string | null | undefined): ParsedUri | null {
  const p = parseUri(uri);
  return p && p.kind === "file" ? p : null;
}
