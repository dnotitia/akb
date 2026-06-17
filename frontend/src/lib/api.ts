const API_BASE = "/api/v1";

let _token: string | null = null;

export function setToken(t: string | null) {
  _token = t;
  if (t) localStorage.setItem("akb_token", t);
  else localStorage.removeItem("akb_token");
}

export function getToken(): string | null {
  if (!_token) _token = localStorage.getItem("akb_token");
  return _token;
}

// ── SSO session marker (optional Keycloak) ──
// Records whether the *current* session was obtained via Keycloak SSO, so
// "Sign out" can also end the Keycloak session (RP-initiated logout) —
// otherwise the live KC session would silently re-authenticate on the next
// SSO click. Local-auth sessions never set this, so their logout is
// unaffected. We also keep the KC id_token to pass as id_token_hint for a
// prompt-free logout.
const SSO_FLAG = "akb_sso";
const KC_HINT = "akb_kc_id_token";

export function markSsoSession(kcIdToken?: string) {
  localStorage.setItem(SSO_FLAG, "1");
  if (kcIdToken) localStorage.setItem(KC_HINT, kcIdToken);
}

export function clearSsoSession() {
  localStorage.removeItem(SSO_FLAG);
  localStorage.removeItem(KC_HINT);
}

export function isSsoSession(): boolean {
  return localStorage.getItem(SSO_FLAG) === "1";
}

/** Backend RP-initiated logout endpoint (bounces to KC end_session → /auth). */
export function keycloakLogoutUrl(): string {
  const hint = localStorage.getItem(KC_HINT);
  return `${API_BASE}/auth/keycloak/logout${hint ? `?id_token_hint=${encodeURIComponent(hint)}` : ""}`;
}

/**
 * Error thrown by `api()` when the server returns a structured 4xx/5xx body
 * (FastAPI `HTTPException(detail=dict)`). Inherits from `Error` so existing
 * call sites that catch `Error` continue to work; new code can narrow with
 * `if (e instanceof ApiError) e.detail.foo`.
 */
export class ApiError<T = unknown> extends Error {
  status: number;
  detail: T;
  constructor(message: string, status: number, detail: T) {
    super(message);
    this.name = "ApiError";
    this.status = status;
    this.detail = detail;
  }
}

async function api<T>(path: string, opts?: RequestInit): Promise<T> {
  const token = getToken();
  const headers: Record<string, string> = {
    "Content-Type": "application/json",
    ...((opts?.headers as Record<string, string>) || {}),
  };
  if (token) headers["Authorization"] = `Bearer ${token}`;
  const res = await fetch(`${API_BASE}${path}`, { ...opts, headers });
  if (res.status === 401) {
    setToken(null);
    if (!location.pathname.startsWith("/auth")) location.href = "/auth?next=" + encodeURIComponent(location.pathname + location.search);
    throw new Error("Unauthorized");
  }
  if (!res.ok) {
    const body = await res.json().catch(() => ({}));
    if (body && typeof body.detail === "object" && body.detail !== null) {
      // FastAPI returns {detail: {...}} for HTTPException(detail=dict).
      // Preserve the structured payload so callers can render its fields.
      const detail = body.detail as { message?: string };
      throw new ApiError(
        detail.message || `${res.status} ${res.statusText}`,
        res.status,
        body.detail,
      );
    }
    throw new Error(body.error || body.detail || `${res.status} ${res.statusText}`);
  }
  return res.json();
}

async function apiText(path: string, opts?: RequestInit): Promise<string> {
  const token = getToken();
  const headers: Record<string, string> = {
    ...((opts?.headers as Record<string, string>) || {}),
  };
  if (token) headers["Authorization"] = `Bearer ${token}`;
  const res = await fetch(`${API_BASE}${path}`, { ...opts, headers });
  if (res.status === 401) {
    setToken(null);
    if (!location.pathname.startsWith("/auth")) location.href = "/auth?next=" + encodeURIComponent(location.pathname + location.search);
    throw new Error("Unauthorized");
  }
  if (!res.ok) {
    const body = await res.text().catch(() => "");
    throw new Error(body || `${res.status} ${res.statusText}`);
  }
  return res.text();
}

// ── Auth (no token) ──
/**
 * Safely parse an auth response. If the body isn't valid JSON (empty 401,
 * HTML error page from nginx, 502 from proxy, network blip), synthesize
 * `{ error }` so the form can show a readable message rather than the
 * browser-native "Failed to execute 'json' on 'Response'…" exception.
 */
async function parseAuthResponse(r: Response): Promise<any> {
  const text = await r.text().catch(() => "");
  if (!text) {
    return r.ok
      ? {}
      : { error: `${r.status} ${r.statusText || "Request failed"}` };
  }
  try {
    return JSON.parse(text);
  } catch {
    return {
      error: r.ok
        ? "Invalid server response"
        : `${r.status} ${r.statusText || "Request failed"}`,
    };
  }
}

export const authRegister = (
  username: string,
  email: string,
  password: string,
  display_name?: string,
) =>
  fetch(`${API_BASE}/auth/register`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ username, email, password, display_name }),
  }).then(parseAuthResponse);

export const authLogin = (username: string, password: string) =>
  fetch(`${API_BASE}/auth/login`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ username, password }),
  }).then(parseAuthResponse);

export interface AuthConfig {
  keycloak: { enabled: boolean; login_url: string | null };
}

/** Public auth config — drives whether the optional SSO button shows.
 * Falls back to SSO-disabled if the endpoint is unreachable/old. */
export const getAuthConfig = (): Promise<AuthConfig> =>
  fetch(`${API_BASE}/auth/config`)
    .then((r) => (r.ok ? r.json() : { keycloak: { enabled: false, login_url: null } }))
    .catch(() => ({ keycloak: { enabled: false, login_url: null } }));

/** Redeem the one-time SSO code from the Keycloak callback for an AKB JWT. */
export const keycloakExchange = (code: string) =>
  fetch(`${API_BASE}/auth/keycloak/exchange`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ code }),
  }).then(parseAuthResponse);

// ── Auth (token) ──
export const getMe = () => api<any>("/auth/me");
export const createPAT = (name: string, scopes?: string[], expires_days?: number) =>
  api<any>("/auth/tokens", { method: "POST", body: JSON.stringify({ name, scopes, expires_days }) });
export const listPATs = () => api<{ tokens: any[] }>("/auth/tokens");
export const revokePAT = (id: string) => api<any>(`/auth/tokens/${id}`, { method: "DELETE" });

// ── Vaults ──
export interface VaultTemplateCollection {
  path: string;
  name: string;
}

export interface VaultTemplateSummary {
  name: string;
  display_name: string;
  description: string;
  collection_count: number;
  collections: VaultTemplateCollection[];
}

export const listVaultTemplates = () =>
  api<VaultTemplateSummary[]>("/vaults/templates");

export const listVaults = () => api<{ vaults: any[] }>("/my/vaults");
export const createVault = (
  name: string,
  description?: string,
  template?: string,
) => {
  const params = new URLSearchParams({ name });
  if (description) params.set("description", description);
  if (template) params.set("template", template);
  return api<any>(`/vaults?${params}`, { method: "POST" });
};
export const getVaultInfo = (vault: string) => api<any>(`/vaults/${vault}/info`);
export const getVaultMembers = (vault: string) => api<{ members: any[] }>(`/vaults/${vault}/members`);
export const grantAccess = (vault: string, user: string, role: string) =>
  api<any>(`/vaults/${vault}/grant`, { method: "POST", body: JSON.stringify({ user, role }) });
export const revokeAccess = (vault: string, user: string) =>
  api<any>(`/vaults/${vault}/revoke`, { method: "POST", body: JSON.stringify({ user }) });
export const transferOwnership = (vault: string, new_owner: string) =>
  api<any>(`/vaults/${vault}/transfer`, { method: "POST", body: JSON.stringify({ new_owner }) });
export const archiveVault = (vault: string) =>
  api<any>(`/vaults/${vault}/archive`, { method: "POST" });
export const unarchiveVault = (vault: string) =>
  api<any>(`/vaults/${vault}/unarchive`, { method: "POST" });
export const updateVault = (
  vault: string,
  patch: { description?: string; public_access?: string },
) => api<any>(`/vaults/${vault}`, { method: "PATCH", body: JSON.stringify(patch) });
export const deleteVaultPermanent = (vault: string) =>
  api<any>(`/vaults/${vault}`, { method: "DELETE" });

// ── Collections ──
export interface CollectionRowSummary {
  path: string;
  name: string;
  summary: string | null;
  doc_count: number;
}

export interface CollectionCreateResult {
  ok: true;
  created: boolean;
  collection: CollectionRowSummary;
}

export interface CollectionDeleteResult {
  ok: true;
  collection: string;
  deleted_docs: number;
  deleted_files: number;
  deleted_sub_collections: number;
}

export interface CollectionNotEmptyDetail {
  message: string;
  doc_count: number;
  file_count: number;
  sub_collection_count: number;
}

export const createCollection = (vault: string, path: string, summary?: string) =>
  api<CollectionCreateResult>(`/collections/${encodeURIComponent(vault)}`, {
    method: "POST",
    body: JSON.stringify({ path, summary }),
  });

export const deleteCollection = (vault: string, path: string, recursive: boolean) => {
  // Path may contain '/' — backend uses {path:path} catch-all. Encode segments
  // individually so '/' stays as a separator.
  const segs = path.split("/").map(encodeURIComponent).join("/");
  const qs = recursive ? "?recursive=true" : "";
  return api<CollectionDeleteResult>(
    `/collections/${encodeURIComponent(vault)}/${segs}${qs}`,
    { method: "DELETE" },
  );
};

// ── Documents ──
export const putDocument = (data: any) =>
  api<any>("/documents", { method: "POST", body: JSON.stringify(data) });
export const getDocument = (vault: string, id: string, version?: string) => {
  const path = `/documents/${vault}/${encodeURIComponent(id)}`;
  return api<any>(
    version ? `${path}?version=${encodeURIComponent(version)}` : path,
  );
};
export const updateDocument = (vault: string, id: string, data: any) =>
  api<any>(`/documents/${vault}/${encodeURIComponent(id)}`, { method: "PATCH", body: JSON.stringify(data) });
export const deleteDocument = (vault: string, id: string) =>
  api<any>(`/documents/${vault}/${encodeURIComponent(id)}`, { method: "DELETE" });

// ── Browse ──
export const browseVault = (vault: string, collection?: string, depth = 1) => {
  const p = new URLSearchParams({ depth: String(depth) });
  if (collection) p.set("collection", collection);
  return api<{ vault: string; path: string; items: any[] }>(`/browse/${vault}?${p}`);
};

// ── Search ──
// `total` is the legacy alias of `returned` (kept until the SPA / agent
// prompts stop reading it). New fields per backend PR #39:
//   - returned: items in `results` after limit + rerank
//   - total_matches: deduped prefetch-pool size, NOT a corpus-wide count
//     (vector ANN is top-K; see backend SearchResponse docstring)
//   - truncated / hint: set when the prefetch pool filled, meaning the
//     corpus may contain more hits than the response surfaces (#77 / 0.2.5).
export interface SearchResponse {
  query: string;
  total: number;
  returned: number;
  total_matches: number;
  truncated?: boolean;
  hint?: string | null;
  // Set when the retrieval index hit a transient failure (vector-store outage
  // or a degraded leg): results may be incomplete/empty — NOT a genuine
  // zero-match (backend issue #189). `degradation_reason` names the cause.
  degraded?: boolean;
  degradation_reason?: string | null;
  results: any[];
}
export const searchDocs = (query: string, vault?: string, limit = 10) => {
  const p = new URLSearchParams({ q: query, limit: String(limit) });
  if (vault) p.set("vault", vault);
  return api<SearchResponse>(`/search?${p}`);
};

export interface GrepMatch {
  section: string | null;
  text: string;
}
export interface GrepDoc {
  uri: string;
  vault: string;
  path: string;
  title: string;
  matches: GrepMatch[];
}
// Grep response (default mode). `total_*` reflect the full ILIKE scan
// across the corpus; `returned_*` reflect what fit under `limit` in
// `results`. `truncated=true` + `hint` are set when the corpus has more
// matches than the response surfaces — switch to count_only or
// files_with_matches at the agent / caller level (backend #76 / 0.2.4).
export interface GrepResponse {
  pattern: string;
  regex: boolean;
  returned_docs?: number;
  returned_matches?: number;
  total_docs: number;
  total_matches: number;
  truncated?: boolean;
  hint?: string | null;
  results: GrepDoc[];
}
export const grepDocs = (query: string, vault?: string, limit = 20) =>
  api<GrepResponse>(`/grep?${(() => {
    const p = new URLSearchParams({ q: query, limit: String(limit) });
    if (vault) p.set("vault", vault);
    return p;
  })()}`);

// ── Graph ──
export interface GraphApiNode {
  uri: string;
  name?: string;
  resource_type?: string;
}
export interface GraphApiEdge {
  source: string;
  target: string;
  relation?: string;
}
// Build the canonical URI from (vault, path) before calling REST so
// every call site presents the unified shape to the backend.
// `docUri` lives in `lib/uri.ts` and handles the 0.3.0
// `/coll/<path>/doc/<basename>` form transparently.
import { docUri as _docUri } from "@/lib/uri";

export const getGraph = (vault: string, docPath?: string, hops = 2, limit = 50) => {
  // Backend 0.3.0 renamed the graph traversal radius from `depth`
  // to `hops` to disambiguate it from `browse?depth` (collection-tree
  // depth). The frontend mirrors the rename so call sites stay
  // self-documenting.
  const p = new URLSearchParams({ hops: String(hops), limit: String(limit) });
  if (docPath) p.set("uri", _docUri(vault, docPath));
  else p.set("vault", vault);
  return api<{ nodes: GraphApiNode[]; edges: GraphApiEdge[] }>(`/graph?${p}`);
};

// ── Drill Down ──
export const drillDown = (vault: string, docPath: string, section?: string) => {
  const p = new URLSearchParams({ uri: _docUri(vault, docPath) });
  if (section) p.set("section", section);
  return api<{ sections: any[] }>(`/drill-down?${p}`);
};

// ── Relations ──
export interface RelationRow {
  direction: "outgoing" | "incoming";
  relation: string;
  uri: string;          // the "other" side
  resource_type?: string;
  name?: string;
}
export const getRelations = (vault: string, docPath: string) => {
  const p = new URLSearchParams({ uri: _docUri(vault, docPath) });
  return api<{ uri: string; relations: RelationRow[] }>(`/relations?${p}`);
};

// User-settable link vocabulary (mirrors backend LinkRelationType). `links_to`
// is auto-extracted from markdown and is NOT settable here.
export const RELATION_TYPES = [
  "references",
  "related_to",
  "depends_on",
  "implements",
  "derived_from",
  "attached_to",
] as const;
export type RelationType = (typeof RELATION_TYPES)[number];

// Create a typed relation edge. `source`/`target` are full akb:// URIs and must
// live in the same vault (backend rejects cross-vault links). Needs writer role.
export const createRelation = (
  source: string,
  target: string,
  relation: RelationType,
  metadata?: Record<string, unknown>,
) =>
  api<{ linked: boolean; source: string; target: string; relation: string }>(`/relations`, {
    method: "POST",
    body: JSON.stringify({ source, target, relation, metadata }),
  });

// Remove a relation edge. `relation` is widened to `string` (vs createRelation's
// `RelationType`) on purpose: unlink must also be able to name the read-only
// `links_to` edge, and omitting it drops ALL edges between the two. Returns
// `{ unlinked: <count> }`; a 0 count (nothing matched) is still a 200 success,
// not an error — the UI only deletes edges it already shows, so count ≥ 1.
export const deleteRelation = (source: string, target: string, relation?: string) => {
  const p = new URLSearchParams({ source, target });
  if (relation) p.set("relation", relation);
  return api<{ unlinked: number; source: string; target: string }>(`/relations?${p}`, {
    method: "DELETE",
  });
};

// ── Recent ──
export const getRecent = (vault?: string, limit = 20) => {
  const p = new URLSearchParams({ limit: String(limit) });
  if (vault) p.set("vault", vault);
  return api<{ changes: any[] }>(`/recent?${p}`);
};

export interface ActivityEntry {
  hash?: string;
  agent?: string;
  author?: string;
  /** Resolved human author name (the raw agent/author is the actor's UUID). */
  author_name?: string;
  subject?: string;
  summary?: string;
  timestamp?: string;
  files?: Array<{ path: string; change?: string }>;
}
export const getVaultActivity = (
  vault: string,
  opts?: { author?: string; collection?: string; since?: string; limit?: number },
) => {
  const p = new URLSearchParams({ limit: String(opts?.limit ?? 50) });
  if (opts?.author) p.set("author", opts.author);
  if (opts?.collection) p.set("collection", opts.collection);
  if (opts?.since) p.set("since", opts.since);
  return api<{ vault: string; total: number; activity: ActivityEntry[] }>(
    `/activity/${vault}?${p}`,
  );
};

// ── Document publish helpers (wrap createPublication/listPublications/deletePublication) ──
//
// The user-facing `doc_id` in this module is the URL-shaped doc path
// (e.g. `specs/api.md`). We resolve it via getDocument() to recover the
// canonical `uri`, then match publications by `resource_uri`.
export const publishDoc = async (vault: string, doc_id: string) => {
  const doc = await getDocument(vault, doc_id);
  const { publications } = await listPublications(vault, "document");
  const existing = publications.find((p: any) => p.resource_uri === doc.uri);
  return existing ?? (await createPublication(vault, { resource_type: "document", uri: doc.uri }));
};

export const unpublishDoc = async (vault: string, doc_id: string) => {
  const doc = await getDocument(vault, doc_id);
  const { publications } = await listPublications(vault, "document");
  const matches = publications.filter((p: any) => p.resource_uri === doc.uri);
  for (const p of matches) await deletePublication(vault, p.slug);
  return { deleted: matches.length };
};

// ── Publications (unified public sharing) ──
export interface PublicationResponse {
  resource_type: "document" | "table_query" | "file";
  embed?: boolean;
  title?: string;
  // document fields
  type?: string;
  status?: string;
  summary?: string;
  domain?: string;
  created_by?: string;
  /** Human author name resolved from created_by (a user id) at read time. */
  created_by_name?: string;
  created_at?: string;
  updated_at?: string;
  tags?: string[];
  content?: string;
  content_unavailable?: boolean;
  section_filter?: string | null;
  section_not_found?: boolean;
  // file fields
  name?: string;
  mime_type?: string;
  size_bytes?: number;
  collection?: string;
  download_url?: string;
  url_expires_in?: number;
  // table_query fields
  columns?: string[];
  rows?: Record<string, any>[];
  total?: number;
  query_params?: Record<string, { type?: string; default?: any; required?: boolean }>;
  applied_params?: Record<string, any>;
  mode?: "live" | "snapshot";
  snapshot_at?: string;
}

export interface PublicationError {
  password_required?: boolean;
  expired?: boolean;
  view_limit_reached?: boolean;
  not_found?: boolean;
  message: string;
  status: number;
}

function publicationTokenKey(slug: string) {
  return `akb_publication_token_${slug}`;
}

export function getPublicationToken(slug: string): string | null {
  return sessionStorage.getItem(publicationTokenKey(slug));
}

export function setPublicationToken(slug: string, token: string) {
  sessionStorage.setItem(publicationTokenKey(slug), token);
}

export function clearPublicationToken(slug: string) {
  sessionStorage.removeItem(publicationTokenKey(slug));
}

async function fetchPublic(slug: string, path: string = "", params?: Record<string, string>): Promise<Response> {
  const token = getPublicationToken(slug);
  const search = new URLSearchParams(params || {});
  if (token) search.set("token", token);
  const qs = search.toString();
  const suffix = qs ? `?${qs}` : "";
  return fetch(`${API_BASE}/public/${slug}${path}${suffix}`);
}

export async function getPublication(
  slug: string,
  params?: Record<string, string>,
): Promise<PublicationResponse> {
  const res = await fetchPublic(slug, "", params);
  const body = await res.json().catch(() => ({}));
  if (!res.ok) {
    const err: PublicationError = {
      message: body.detail || body.error || res.statusText,
      status: res.status,
      password_required: body.password_required,
      expired: res.status === 410 && /expired/i.test(body.detail || ""),
      view_limit_reached: res.status === 410 && /view limit/i.test(body.detail || ""),
      not_found: res.status === 404,
    };
    throw err;
  }
  return body;
}

export async function getPublicationMeta(slug: string): Promise<any> {
  const res = await fetchPublic(slug, "/meta");
  if (!res.ok) {
    const body = await res.json().catch(() => ({}));
    throw { message: body.detail || body.error || res.statusText, status: res.status, password_required: body.password_required } as PublicationError;
  }
  return res.json();
}

export async function submitPublicationPassword(slug: string, password: string): Promise<{ token: string; expires_in: number }> {
  const res = await fetch(`${API_BASE}/public/${slug}/auth`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ password }),
  });
  const body = await res.json().catch(() => ({}));
  if (!res.ok) {
    throw new Error(body.detail || body.error || "Invalid password");
  }
  setPublicationToken(slug, body.token);
  return body;
}

export function publicationDownloadUrl(slug: string, params?: Record<string, string>): string {
  const token = getPublicationToken(slug);
  const search = new URLSearchParams(params || {});
  if (token) search.set("token", token);
  const qs = search.toString();
  return `${API_BASE}/public/${slug}/download${qs ? `?${qs}` : ""}`;
}

export function publicationRawUrl(slug: string): string {
  const token = getPublicationToken(slug);
  const qs = token ? `?token=${token}` : "";
  return `${API_BASE}/public/${slug}/raw${qs}`;
}

export function publicationCsvUrl(slug: string, params?: Record<string, string>): string {
  const search = new URLSearchParams(params || {});
  search.set("format", "csv");
  const token = getPublicationToken(slug);
  if (token) search.set("token", token);
  return `${API_BASE}/public/${slug}?${search.toString()}`;
}

// ── Publications CRUD (authenticated) ──
export interface CreatePublicationRequest {
  resource_type: "document" | "table_query" | "file";
  // For document/file publications, pass the canonical akb:// URI.
  // table_query publications still scope by `vault` (route path) and
  // use `query_sql`.
  uri?: string;
  query_sql?: string;
  query_vault_names?: string[];
  query_params?: Record<string, { type?: string; default?: any; required?: boolean }>;
  password?: string;
  max_views?: number;
  expires_in?: string;
  title?: string;
  // Document publications only — render only this heading section.
  section_filter?: string;
  allow_embed?: boolean;
}

// Single canonical publication dict — same shape from every endpoint
// (create, list, snapshot). `slug` is the only identifier we hand
// around; `share_url` is always absolute.
export interface Publication {
  slug: string;
  share_url: string;
  resource_type: "document" | "table_query" | "file";
  resource_uri: string | null;
  vault: string;
  title: string | null;
  mode: "live" | "snapshot";
  expires_at: string | null;
  max_views: number | null;
  view_count: number;
  allow_embed: boolean;
  section_filter: string | null;
  password_protected: boolean;
  created_at: string;
  snapshot_at: string | null;
  // table_query-only:
  query_sql?: string | null;
  query_vault_names?: string[] | null;
  query_params?: Record<string, any> | null;
}

export const createPublication = (vault: string, req: CreatePublicationRequest) =>
  api<Publication>(`/publications/${vault}/create`, { method: "POST", body: JSON.stringify(req) });

export const listPublications = (vault: string, resource_type?: string) => {
  const qs = resource_type ? `?resource_type=${resource_type}` : "";
  return api<{ publications: Publication[] }>(`/publications/${vault}${qs}`);
};

export const deletePublication = (vault: string, slug: string) =>
  api<{ deleted: number }>(`/publications/${vault}/${slug}`, { method: "DELETE" });

export const createPublicationSnapshot = (vault: string, slug: string) =>
  api<Publication>(`/publications/${vault}/${slug}/snapshot`, { method: "POST" });

export const searchUsers = (query?: string) =>
  api<{ users: any[] }>(`/users/search${query ? `?q=${encodeURIComponent(query)}` : ""}`);

// Agent memory is just another vault (`agent-memory-{username}`)
// since v0.5.0 — read/write via the standard documents+browse API.

// ── Admin ──
export interface AdminUser {
  id: string;
  username: string;
  display_name: string | null;
  email: string;
  is_admin: boolean;
  created_at: string;
  owned_vaults: number;
}
export const adminListUsers = () => api<{ users: AdminUser[] }>("/admin/users");
export const adminDeleteUser = (user_id: string) =>
  api<any>(`/admin/users/${user_id}`, { method: "DELETE" });
export const changePassword = (current_password: string, new_password: string) =>
  api<{ ok: true }>("/auth/change-password", {
    method: "POST",
    body: JSON.stringify({ current_password, new_password }),
  });
export const updateProfile = (patch: { display_name?: string; email?: string }) =>
  api<{ updated: true; username: string; display_name: string | null; email: string }>(
    "/auth/me",
    { method: "PATCH", body: JSON.stringify(patch) },
  );
export const adminResetPassword = (userId: string) =>
  api<{ temporary_password: string; username: string }>(
    `/admin/users/${encodeURIComponent(userId)}/reset-password`,
    { method: "POST" },
  );

// ── Provenance / drill-down ──
// Callers pass the doc path under its vault; the helper builds the
// canonical URI before calling the URI-only REST endpoint.
export const getProvenance = (vault: string, docPath: string) => {
  const p = new URLSearchParams({ uri: _docUri(vault, docPath) });
  return api<{ provenance: any }>(`/provenance?${p}`);
};

// ── Help / Skill ──
// Skill seed template (text/markdown)
export const getSkillTemplate = (): Promise<string> =>
  apiText("/help/skill-template");

// Agent-view preview of a vault's skill (used by S6 AGENT segment)
export const getVaultSkillPreview = (vault: string): Promise<string> =>
  apiText(`/help/vault-skill-preview/${encodeURIComponent(vault)}`);
