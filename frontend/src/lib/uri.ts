// Parsing helpers for the canonical AKB URI scheme. Backend
// `app/services/uri_service.py` defines the authoritative shape;
// this file mirrors its behaviour for the React client.
//
// As of 0.3.0 the scheme is location-aware — every URI carries an
// optional `/coll/<collection_path>` segment that names its
// containing collection. There are five recognised forms:
//
//   akb://{vault}                                       vault root
//   akb://{vault}/coll/{coll_path}                      collection
//   akb://{vault}/{type}/{identifier}                   root-level typed resource
//   akb://{vault}/coll/{coll_path}/{type}/{identifier}  resource inside a collection
//
// `type` is one of `doc | table | file`. For docs, `identifier` is the
// basename (not the full path) — the collection part lives in the
// `/coll/...` segment. The `id` field returned here mirrors what
// `app.services.uri_service.split_uri` returns for that type:
//
//   doc   → full vault-relative path (`coll_path/basename` if present, else basename)
//   table → table name
//   file  → file UUID
//   coll  → collection path
//   vault → empty string
//
// The matching order matters: the in-collection typed pattern must
// run first so `akb://V/coll/X/doc/Y.md` is not mis-classified as a
// `coll` URI whose path happens to contain `/doc/`.

export type UriKind = "doc" | "table" | "file" | "coll" | "vault";

export interface ParsedUri {
  vault: string;
  kind: UriKind;
  /** Containing-collection path (null for vault-root resources, vault, and coll itself). */
  collection: string | null;
  /** Type-specific identifier — see comment above for the shape per kind. */
  id: string;
}

// Patterns share fragments — order matters in `parseUri`.
const RE_IN_COLL = /^akb:\/\/([^/]+)\/coll\/([^/]+(?:\/[^/]+)*)\/(doc|table|file)\/(.+)$/;
const RE_COLL_ONLY = /^akb:\/\/([^/]+)\/coll\/([^/]+(?:\/[^/]+)*)\/?$/;
const RE_TYPED_ROOT = /^akb:\/\/([^/]+)\/(doc|table|file)\/(.+)$/;
const RE_VAULT_ONLY = /^akb:\/\/([^/]+)\/?$/;

export function parseUri(uri: string | null | undefined): ParsedUri | null {
  if (!uri) return null;

  let m = uri.match(RE_IN_COLL);
  if (m) {
    const [, vault, coll, kind, idRaw] = m;
    const id = stripTrailingSlash(idRaw);
    if (id === null) return null;
    return {
      vault,
      kind: kind as UriKind,
      collection: coll,
      // For docs the public `id` is the full vault-relative path so
      // existing callers (which receive `documents.path`) keep
      // working. For table/file the basename IS the identifier.
      id: kind === "doc" ? `${coll}/${id}` : id,
    };
  }

  m = uri.match(RE_COLL_ONLY);
  if (m) {
    const [, vault, coll] = m;
    const path = coll.replace(/\/+$/, "");
    if (!path) return null;
    return { vault, kind: "coll", collection: path, id: path };
  }

  m = uri.match(RE_TYPED_ROOT);
  if (m) {
    const [, vault, kind, idRaw] = m;
    const id = stripTrailingSlash(idRaw);
    if (id === null) return null;
    return { vault, kind: kind as UriKind, collection: null, id };
  }

  m = uri.match(RE_VAULT_ONLY);
  if (m) return { vault: m[1], kind: "vault", collection: null, id: "" };

  return null;
}

function stripTrailingSlash(id: string): string | null {
  if (id.endsWith("/")) {
    const trimmed = id.replace(/\/+$/, "");
    return trimmed || null;
  }
  return id;
}

export function parseDocUri(uri: string | null | undefined): ParsedUri | null {
  const p = parseUri(uri);
  return p && p.kind === "doc" ? p : null;
}

export function parseFileUri(uri: string | null | undefined): ParsedUri | null {
  const p = parseUri(uri);
  return p && p.kind === "file" ? p : null;
}

export function parseTableUri(uri: string | null | undefined): ParsedUri | null {
  const p = parseUri(uri);
  return p && p.kind === "table" ? p : null;
}

export function parseCollUri(uri: string | null | undefined): ParsedUri | null {
  const p = parseUri(uri);
  return p && p.kind === "coll" ? p : null;
}

// ── Builders — mirror the backend helpers ─────────────────────

export function vaultUri(vault: string): string {
  return `akb://${vault}`;
}

export function collUri(vault: string, collectionPath: string): string {
  return `akb://${vault}/coll/${collectionPath}`;
}

export function docUri(vault: string, path: string): string {
  // `path` is the document's full vault-relative path. Mirror the
  // Python helper exactly — split off the parent directory as the
  // collection prefix.
  const idx = path.lastIndexOf("/");
  if (idx === -1) return `akb://${vault}/doc/${path}`;
  const coll = path.slice(0, idx);
  const basename = path.slice(idx + 1);
  return `akb://${vault}/coll/${coll}/doc/${basename}`;
}

export function tableUri(vault: string, name: string, collection?: string | null): string {
  if (collection) return `akb://${vault}/coll/${collection}/table/${name}`;
  return `akb://${vault}/table/${name}`;
}

export function fileUri(vault: string, fileId: string, collection?: string | null): string {
  if (collection) return `akb://${vault}/coll/${collection}/file/${fileId}`;
  return `akb://${vault}/file/${fileId}`;
}
