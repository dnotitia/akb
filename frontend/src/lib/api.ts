const API_BASE = "/api/v1";

let _token: string | null = null;

export function setToken(t: string | null) {
  _token = t;
  t ? localStorage.setItem("akb_token", t) : localStorage.removeItem("akb_token");
}

export function getToken(): string | null {
  if (!_token) _token = localStorage.getItem("akb_token");
  return _token;
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
    if (!location.pathname.startsWith("/auth")) location.href = "/auth";
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
export const getDocument = (vault: string, id: string) =>
  api<any>(`/documents/${vault}/${encodeURIComponent(id)}`);
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
export const searchDocs = (query: string, vault?: string, limit = 10) => {
  const p = new URLSearchParams({ q: query, limit: String(limit) });
  if (vault) p.set("vault", vault);
  return api<{ query: string; total: number; results: any[] }>(`/search?${p}`);
};

export interface GrepMatch {
  section: string | null;
  text: string;
}
export interface GrepDoc {
  doc_id: string;
  vault: string;
  path: string;
  title: string;
  matches: GrepMatch[];
}
export const grepDocs = (query: string, vault?: string, limit = 20) => {
  const p = new URLSearchParams({ q: query, limit: String(limit) });
  if (vault) p.set("vault", vault);
  return api<{
    pattern: string;
    regex: boolean;
    total_docs: number;
    total_matches: number;
    results: GrepDoc[];
  }>(`/grep?${p}`);
};

// ── Graph ──
export const getGraph = (vault: string, docId?: string, depth = 2, limit = 50) => {
  const p = new URLSearchParams({ depth: String(depth), limit: String(limit) });
  if (docId) p.set("doc_id", docId);
  return api<{ nodes: any[]; edges: any[] }>(`/graph/${vault}?${p}`);
};

// ── Drill Down ──
export const drillDown = (vault: string, docId: string, section?: string) => {
  const p = new URLSearchParams();
  if (section) p.set("section", section);
  return api<{ sections: any[] }>(`/drill-down/${vault}/${docId}?${p}`);
};

// ── Relations ──
export const getRelations = (vault: string, docId: string) =>
  api<{ relations: any[] }>(`/relations/${vault}/${docId}`);

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

// ── Users ──
// ── Document publish helpers (wrap createPublication/listPublications/deletePublication) ──
//
// publications.document_id is the underlying UUID. The user-facing doc_id can
// be a UUID, a "d-XXXXXXXX" hash from metadata, or a path substring — we
// resolve it via getDocument() once and then match exclusively by UUID.
//
// All publication objects use `publication_id` (not `id`) — backend normalizes
// this in publication_service._row_to_dict so frontend can rely on it everywhere.
export const publishDoc = async (vault: string, doc_id: string) => {
  // Idempotent: reuse the first existing publication for this doc, else create one.
  const doc = await getDocument(vault, doc_id);
  const { publications } = await listPublications(vault, "document");
  const existing = publications.find((s: any) => s.document_id === doc.id);
  if (existing) {
    return {
      published: true,
      public_url: `/p/${existing.slug}`,
      slug: existing.slug,
      publication_id: existing.publication_id,
    };
  }
  const result = await createPublication(vault, { resource_type: "document", doc_id });
  return {
    published: true,
    public_url: result.public_url,
    slug: result.slug,
    publication_id: result.publication_id,
  };
};

export const unpublishDoc = async (vault: string, doc_id: string) => {
  const doc = await getDocument(vault, doc_id);
  const { publications } = await listPublications(vault, "document");
  const matches = publications.filter((s: any) => s.document_id === doc.id);
  let deleted = 0;
  for (const s of matches) {
    await deletePublication(vault, s.publication_id);
    deleted++;
  }
  return { published: false, deleted_publications: deleted };
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
  doc_id?: string;
  file_id?: string;
  query_sql?: string;
  query_vault_names?: string[];
  query_params?: Record<string, { type?: string; default?: any; required?: boolean }>;
  password?: string;
  max_views?: number;
  expires_in?: string;
  title?: string;
  mode?: "live" | "snapshot";
  section?: string;
  allow_embed?: boolean;
}

export const createPublication = (vault: string, req: CreatePublicationRequest) =>
  api<any>(`/publications/${vault}/create`, { method: "POST", body: JSON.stringify(req) });

export const listPublications = (vault: string, resource_type?: string) => {
  const qs = resource_type ? `?resource_type=${resource_type}` : "";
  return api<{ publications: any[] }>(`/publications/${vault}${qs}`);
};

export const deletePublication = (vault: string, publication_id: string) =>
  api<any>(`/publications/${vault}/${publication_id}`, { method: "DELETE" });

export const createPublicationSnapshot = (vault: string, publication_id: string) =>
  api<any>(`/publications/${vault}/${publication_id}/snapshot`, { method: "POST" });

export const searchUsers = (query?: string) =>
  api<{ users: any[] }>(`/users/search${query ? `?q=${encodeURIComponent(query)}` : ""}`);

// ── Memory ──
export interface Memory {
  memory_id: string;
  category: string;
  content: string;
  source?: string;
  created_at?: string;
  updated_at?: string;
}
export const recallMemories = (category?: string, limit = 100) => {
  const p = new URLSearchParams({ limit: String(limit) });
  if (category) p.set("category", category);
  return api<{ memories: Memory[]; total: number }>(`/memory?${p}`);
};
export const forgetMemory = (memory_id: string) =>
  api<{ forgotten: boolean }>(`/memory/${memory_id}`, { method: "DELETE" });
export const forgetCategory = (category: string) =>
  api<{ forgotten: number; category: string }>(`/memory/category/${category}`, {
    method: "DELETE",
  });

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
export const adminResetPassword = (userId: string) =>
  api<{ temporary_password: string; username: string }>(
    `/admin/users/${encodeURIComponent(userId)}/reset-password`,
    { method: "POST" },
  );
