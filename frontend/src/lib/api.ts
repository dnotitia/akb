const API_BASE = "/api/v1";

export type AuthMode = "local" | "sso";
export type PublicAuthMode = AuthMode | "hybrid";

let _token: string | null = null;
let _authMode: AuthMode | null = null;
let _authSessionGeneration = 0;
const SAFE_AUTH_METHODS = new Set(["GET", "HEAD", "OPTIONS"]);
const LOCAL_TOKEN_STORAGE_KEY = "akb_token";
const LEGACY_SSO_SESSION_KEY = "akb_legacy_sso";
const LEGACY_SSO_ID_TOKEN_KEY = "akb_legacy_kc_id_token";

const PRIVATE_ASSET_CACHE_MAX_ENTRIES = 32;
const PRIVATE_ASSET_CACHE_MAX_BYTES = 64 * 1024 * 1024;
const privateAssetCache = new Map<string, Blob>();
let privateAssetCacheBytes = 0;
type AssetFlight = {
  controller: AbortController;
  promise: Promise<Blob>;
  subscribers: number;
  settled: boolean;
};
const privateAssetFlights = new Map<string, AssetFlight>();

export function clearPrivateAssetCache() {
  privateAssetCache.clear();
  privateAssetCacheBytes = 0;
  for (const flight of privateAssetFlights.values()) flight.controller.abort();
  privateAssetFlights.clear();
}

type StoredTokenRead =
  | { available: true; value: string | null }
  | { available: false; value: null };

function readStoredToken(): StoredTokenRead {
  try {
    return { available: true, value: localStorage.getItem(LOCAL_TOKEN_STORAGE_KEY) };
  } catch {
    // Cookie-backed SSO must remain usable when storage is blocked by browser
    // policy. Local mode can still retain a token in memory for this tab.
    return { available: false, value: null };
  }
}

function writeStoredToken(token: string | null) {
  try {
    if (token) localStorage.setItem(LOCAL_TOKEN_STORAGE_KEY, token);
    else localStorage.removeItem(LOCAL_TOKEN_STORAGE_KEY);
  } catch {
    // Best effort only; `_token` remains the current tab's local carrier.
  }
}

export function setToken(t: string | null) {
  const stored = readStoredToken();
  if (_token !== t || (stored.available && stored.value !== t)) {
    _authSessionGeneration += 1;
    clearPrivateAssetCache();
  }
  _token = t;
  writeStoredToken(t);
}

/**
 * Temporary compatibility marker for the pre-browser-session Keycloak flow.
 * The legacy backend exchanges a one-time callback code for the same Bearer
 * JWT used by local login, so transport remains `local`; this marker exists
 * only to choose the matching RP-initiated logout route. Session storage keeps
 * the optional Keycloak hint scoped to the tab that completed the redirect.
 */
export function markLegacySsoSession(idToken?: unknown) {
  try {
    sessionStorage.setItem(LEGACY_SSO_SESSION_KEY, "1");
    if (
      typeof idToken === "string" &&
      idToken.length > 0 &&
      idToken.length <= 16_384 &&
      !hasControlCharacters(idToken)
    ) {
      sessionStorage.setItem(LEGACY_SSO_ID_TOKEN_KEY, idToken);
    } else {
      sessionStorage.removeItem(LEGACY_SSO_ID_TOKEN_KEY);
    }
  } catch {
    // The Bearer session remains usable when storage is blocked; only the
    // seamless Keycloak logout hint is lost.
  }
}

export function clearLegacySsoSession() {
  try {
    sessionStorage.removeItem(LEGACY_SSO_SESSION_KEY);
    sessionStorage.removeItem(LEGACY_SSO_ID_TOKEN_KEY);
  } catch {
    // Best effort; the AKB Bearer token is cleared independently.
  }
}

function legacySsoLogoutUrl(): string | null {
  try {
    if (sessionStorage.getItem(LEGACY_SSO_SESSION_KEY) !== "1") return null;
    const hint = sessionStorage.getItem(LEGACY_SSO_ID_TOKEN_KEY);
    const search = new URLSearchParams();
    if (hint) search.set("id_token_hint", hint);
    const query = search.toString();
    return `${API_BASE}/auth/keycloak/logout${query ? `?${query}` : ""}`;
  } catch {
    return null;
  }
}

/**
 * Select the browser credential carrier from the server-owned auth policy.
 * A stale local JWT is destroyed as soon as an SSO policy is accepted, so an
 * explicit Authorization header can never shadow the HttpOnly SSO session.
 */
export function configureAuthTransport(mode: AuthMode | null) {
  if (_authMode !== mode) {
    _authMode = mode;
    _authSessionGeneration += 1;
    clearPrivateAssetCache();
  }
  const stored = readStoredToken();
  if (mode === "sso" && (_token !== null || (stored.available && stored.value !== null))) {
    setToken(null);
  }
}

function privateAssetCacheKey(
  sessionGeneration: number,
  fileId: string,
  vault: string,
  source?: { document: string; commit: string },
) {
  return [sessionGeneration, vault, source?.document ?? "live", source?.commit ?? "live", fileId].join("\u0000");
}

function readPrivateAssetCache(key: string): Blob | null {
  const blob = privateAssetCache.get(key);
  if (!blob) return null;
  // Map insertion order is the LRU order.
  privateAssetCache.delete(key);
  privateAssetCache.set(key, blob);
  return blob;
}

function writePrivateAssetCache(key: string, blob: Blob) {
  if (blob.size > PRIVATE_ASSET_CACHE_MAX_BYTES) return;
  const previous = privateAssetCache.get(key);
  if (previous) privateAssetCacheBytes -= previous.size;
  privateAssetCache.delete(key);
  privateAssetCache.set(key, blob);
  privateAssetCacheBytes += blob.size;
  while (
    privateAssetCache.size > PRIVATE_ASSET_CACHE_MAX_ENTRIES ||
    privateAssetCacheBytes > PRIVATE_ASSET_CACHE_MAX_BYTES
  ) {
    const oldest = privateAssetCache.entries().next().value as [string, Blob] | undefined;
    if (!oldest) break;
    privateAssetCache.delete(oldest[0]);
    privateAssetCacheBytes -= oldest[1].size;
  }
}

function waitForAssetFlight(flight: AssetFlight, signal?: AbortSignal): Promise<Blob> {
  if (!signal) return flight.promise;
  if (signal.aborted) return Promise.reject(new DOMException("Aborted", "AbortError"));
  return new Promise((resolve, reject) => {
    const abort = () => reject(new DOMException("Aborted", "AbortError"));
    signal.addEventListener("abort", abort, { once: true });
    flight.promise.then(resolve, reject).finally(() => {
      signal.removeEventListener("abort", abort);
    });
  });
}

export function getToken(): string | null {
  if (_authMode === "sso") return null;
  const stored = readStoredToken();
  if (stored.available && stored.value !== _token) {
    _token = stored.value;
    _authSessionGeneration += 1;
    clearPrivateAssetCache();
  }
  return _token;
}

function ssoCsrfCookieName(): string {
  return window.location.protocol === "https:"
    ? "__Host-akb_sso_csrf"
    : "akb_dev_sso_csrf";
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

function withAuthCarrier(headers: HeadersInit | undefined, method: string): HeadersInit {
  const unsafeMethod = !SAFE_AUTH_METHODS.has(method.toUpperCase());
  if (headers instanceof Headers || Array.isArray(headers)) {
    const merged = new Headers(headers);
    if (_authMode === "local" && !merged.has("Authorization")) {
      const token = getToken();
      if (token) merged.set("Authorization", `Bearer ${token}`);
    }
    if (_authMode === "sso" && unsafeMethod && !merged.has("Authorization")) {
      const csrf = cookieValue(ssoCsrfCookieName());
      if (csrf) merged.set("X-AKB-CSRF", csrf);
    }
    return merged;
  }
  const merged = { ...(headers || {}) } as Record<string, string>;
  const hasHeader = (name: string) =>
    Object.keys(merged).some((candidate) => candidate.toLowerCase() === name.toLowerCase());
  if (_authMode === "local" && !hasHeader("Authorization")) {
    const token = getToken();
    if (token) merged.Authorization = `Bearer ${token}`;
  }
  if (_authMode === "sso" && unsafeMethod && !hasHeader("Authorization")) {
    const csrf = cookieValue(ssoCsrfCookieName());
    if (csrf) merged["X-AKB-CSRF"] = csrf;
  }
  return merged;
}

function expireUnauthorizedSession(redirect: boolean) {
  if (_authMode === "local" || getToken() !== null) {
    setToken(null);
    clearLegacySsoSession();
  } else {
    _authSessionGeneration += 1;
    clearPrivateAssetCache();
  }
  if (redirect && !location.pathname.startsWith("/auth")) {
    location.href =
      "/auth?next=" + encodeURIComponent(location.pathname + location.search);
  }
}

export async function authenticatedFetch(
  input: RequestInfo | URL,
  init?: RequestInit,
  { unauthorized = "expire-and-redirect" }: {
    unauthorized?: "expire-and-redirect" | "expire" | "preserve-session";
  } = {},
): Promise<Response> {
  const rawUrl =
    typeof input === "string"
      ? input
      : input instanceof URL
        ? input.href
        : input.url;
  const resolvedUrl = new URL(rawUrl, window.location.href);
  if (resolvedUrl.origin !== window.location.origin) {
    throw new Error("Authenticated requests must remain same-origin");
  }
  const requestMethod = init?.method || (input instanceof Request ? input.method : "GET");
  const requestHeaders = init?.headers || (input instanceof Request ? input.headers : undefined);
  const res = await fetch(input, {
    ...init,
    credentials: "same-origin",
    headers: withAuthCarrier(requestHeaders, requestMethod),
  });
  if (res.status === 401) {
    if (unauthorized !== "preserve-session") {
      expireUnauthorizedSession(unauthorized === "expire-and-redirect");
    }
    throw new Error("Unauthorized");
  }
  return res;
}

async function throwJsonApiError(res: Response): Promise<never> {
  const body = await res.json().catch(() => ({}));
  if (body && typeof body.detail === "object" && body.detail !== null) {
    const detail = body.detail as { message?: string };
    throw new ApiError(
      detail.message || `${res.status} ${res.statusText}`,
      res.status,
      body.detail,
    );
  }
  throw new Error(body.error || body.detail || `${res.status} ${res.statusText}`);
}

async function api<T>(
  path: string,
  opts?: RequestInit,
  unauthorized: "expire-and-redirect" | "expire" | "preserve-session" = "expire-and-redirect",
): Promise<T> {
  const headers: Record<string, string> = {
    "Content-Type": "application/json",
    ...((opts?.headers as Record<string, string>) || {}),
  };
  const res = await authenticatedFetch(
    `${API_BASE}${path}`,
    { ...opts, headers },
    { unauthorized },
  );
  if (!res.ok) {
    await throwJsonApiError(res);
  }
  return res.json();
}

async function apiText(path: string, opts?: RequestInit): Promise<string> {
  const res = await authenticatedFetch(`${API_BASE}${path}`, opts);
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

export interface AuthProviderOption {
  provider_type: string;
  alias: string;
  display_name: string;
  login_url: string | null;
}

export interface AuthConfig {
  available: boolean;
  schema_version: 1 | 2 | null;
  auth_mode: PublicAuthMode | null;
  local_auth: { enabled: boolean };
  keycloak: {
    enabled: boolean;
    browser_session_ready: boolean;
  };
  providers: AuthProviderOption[];
  mcp_oauth: { enabled: boolean };
}

export const AUTH_CONFIG_UNAVAILABLE: AuthConfig = {
  available: false,
  schema_version: null,
  auth_mode: null,
  local_auth: { enabled: false },
  keycloak: {
    enabled: false,
    browser_session_ready: false,
  },
  providers: [],
  mcp_oauth: { enabled: false },
};

function record(value: unknown): Record<string, unknown> | null {
  return typeof value === "object" && value !== null && !Array.isArray(value)
    ? value as Record<string, unknown>
    : null;
}

function hasExactKeys(
  value: Record<string, unknown> | null,
  expected: readonly string[],
): value is Record<string, unknown> {
  if (!value) return false;
  const actual = Object.keys(value).sort();
  const exact = [...expected].sort();
  return actual.length === exact.length && actual.every((key, index) => key === exact[index]);
}

function hasControlCharacters(value: string): boolean {
  return Array.from(value).some((character) => {
    const code = character.charCodeAt(0);
    return code < 0x20 || code === 0x7f;
  });
}

function parseV2AuthConfig(value: unknown): AuthConfig | null {
  const root = record(value);
  const localAuth = record(root?.local_auth);
  const keycloak = record(root?.keycloak);
  const mcpOauth = record(root?.mcp_oauth);
  const mode = root?.auth_mode;
  const providerValues = root?.providers;
  if (
    !hasExactKeys(root, ["schema_version", "auth_mode", "local_auth", "keycloak", "providers", "mcp_oauth"]) ||
    !hasExactKeys(localAuth, ["enabled"]) ||
    !hasExactKeys(keycloak, ["enabled", "browser_session_ready"]) ||
    !hasExactKeys(mcpOauth, ["enabled"]) ||
    root?.schema_version !== 2 ||
    (mode !== "local" && mode !== "sso") ||
    typeof localAuth?.enabled !== "boolean" ||
    typeof keycloak?.enabled !== "boolean" ||
    typeof keycloak?.browser_session_ready !== "boolean" ||
    !Array.isArray(providerValues) ||
    providerValues.length > 100 ||
    typeof mcpOauth?.enabled !== "boolean" ||
    localAuth.enabled !== (mode === "local") ||
    keycloak.enabled !== (mode === "sso") ||
    (keycloak.browser_session_ready && !keycloak.enabled) ||
    (mode === "local" && providerValues.length !== 0)
  ) {
    return null;
  }
  const aliases = new Set<string>();
  const providers: AuthProviderOption[] = [];
  for (const value of providerValues) {
    const provider = record(value);
    if (!hasExactKeys(provider, ["provider_type", "alias", "display_name", "login_url"])) {
      return null;
    }
    const providerType = provider.provider_type;
    const alias = provider.alias;
    const displayName = provider.display_name;
    const loginUrl = provider.login_url;
    if (
      typeof providerType !== "string" ||
      !/^[a-z0-9][a-z0-9-]{0,79}$/.test(providerType) ||
      typeof alias !== "string" ||
      !/^[a-z0-9][a-z0-9._-]{0,62}$/.test(alias) ||
      aliases.has(alias) ||
      typeof displayName !== "string" ||
      !displayName.trim() ||
      displayName.length > 80 ||
      hasControlCharacters(displayName) ||
      (loginUrl !== null && typeof loginUrl !== "string") ||
      (!keycloak.browser_session_ready && loginUrl !== null) ||
      (keycloak.browser_session_ready && loginUrl !== `/api/v1/auth/sso/${alias}/login`)
    ) {
      return null;
    }
    aliases.add(alias);
    providers.push({
      provider_type: providerType,
      alias,
      display_name: displayName,
      login_url: loginUrl,
    });
  }
  return {
    available: true,
    schema_version: 2,
    auth_mode: mode,
    local_auth: { enabled: localAuth.enabled },
    keycloak: {
      enabled: keycloak.enabled,
      browser_session_ready: keycloak.browser_session_ready,
    },
    providers,
    mcp_oauth: { enabled: mcpOauth.enabled },
  };
}

/**
 * Exact adapter for the last unversioned hybrid contract shipped before the
 * server-custodied SSO transition. Do not broaden this parser: unknown legacy
 * shapes must continue to fail closed instead of being guessed into policy.
 */
function parseLegacyHybridAuthConfig(value: unknown): AuthConfig | null {
  const root = record(value);
  const localAuth = record(root?.local_auth);
  const keycloak = record(root?.keycloak);
  const mcpOauth = record(root?.mcp_oauth);
  const keycloakEnabled = keycloak?.enabled;
  const loginUrl = keycloak?.login_url;
  const ssoOnly = keycloak?.sso_only;
  const enrollmentMode = keycloak?.enrollment_mode;
  if (
    !hasExactKeys(root, ["local_auth", "keycloak", "mcp_oauth"]) ||
    !hasExactKeys(localAuth, ["enabled"]) ||
    !hasExactKeys(keycloak, ["enabled", "enrollment_mode", "login_url", "sso_only"]) ||
    !hasExactKeys(mcpOauth, ["enabled"]) ||
    typeof localAuth.enabled !== "boolean" ||
    typeof keycloakEnabled !== "boolean" ||
    typeof ssoOnly !== "boolean" ||
    !["open", "invite_only", "disabled"].includes(String(enrollmentMode)) ||
    typeof mcpOauth.enabled !== "boolean" ||
    loginUrl !== (keycloakEnabled ? "/api/v1/auth/keycloak/login" : null) ||
    (ssoOnly && !keycloakEnabled)
  ) {
    return null;
  }

  // `sso_only` was a server-owned presentation policy. Its recovery escape is
  // intentionally not revived: when set, the compatibility client exposes no
  // local form even if the old payload also reports local_auth.enabled=true.
  const localEnabled = localAuth.enabled && !ssoOnly;
  if (!localEnabled && !keycloakEnabled) return null;

  return {
    available: true,
    schema_version: 1,
    auth_mode: keycloakEnabled ? "hybrid" : "local",
    local_auth: { enabled: localEnabled },
    keycloak: {
      enabled: keycloakEnabled,
      // Legacy Keycloak exchanges into a Bearer JWT. It is intentionally not
      // represented as the v2 HttpOnly browser-session transport.
      browser_session_ready: false,
    },
    providers: keycloakEnabled
      ? [{
          provider_type: "legacy-keycloak-oidc",
          alias: "legacy-keycloak",
          display_name: "SSO",
          login_url: "/api/v1/auth/keycloak/login",
        }]
      : [],
    mcp_oauth: { enabled: mcpOauth.enabled },
  };
}

export function parseAuthConfig(value: unknown): AuthConfig {
  return (
    parseV2AuthConfig(value) ??
    parseLegacyHybridAuthConfig(value) ??
    AUTH_CONFIG_UNAVAILABLE
  );
}

/** Versioned public capabilities. Any transport/schema error is deny-all. */
export async function getAuthConfig(): Promise<AuthConfig> {
  try {
    const response = await fetch(`${API_BASE}/auth/config`);
    if (!response.ok) return AUTH_CONFIG_UNAVAILABLE;
    const config = parseAuthConfig(await response.json());
    if (config.available && config.auth_mode !== null) {
      configureAuthTransport(config.auth_mode === "hybrid" ? "local" : config.auth_mode);
    }
    return config;
  } catch {
    return AUTH_CONFIG_UNAVAILABLE;
  }
}

export interface AdminAuthConfig {
  available: boolean;
  schema_version: 1 | null;
  auth_mode: AuthMode | null;
  local: { enabled: boolean; login_url: string | null };
  keycloak: { enabled: boolean; login_url: string | null };
}

export const ADMIN_AUTH_CONFIG_UNAVAILABLE: AdminAuthConfig = {
  available: false,
  schema_version: null,
  auth_mode: null,
  local: { enabled: false, login_url: null },
  keycloak: { enabled: false, login_url: null },
};

export function parseAdminAuthConfig(value: unknown): AdminAuthConfig {
  const root = record(value);
  const local = record(root?.local);
  const keycloak = record(root?.keycloak);
  const mode = root?.auth_mode;
  const localUrl = local?.login_url;
  const keycloakUrl = keycloak?.login_url;
  if (
    !hasExactKeys(root, ["schema_version", "auth_mode", "local", "keycloak"]) ||
    !hasExactKeys(local, ["enabled", "login_url"]) ||
    !hasExactKeys(keycloak, ["enabled", "login_url"]) ||
    root?.schema_version !== 1 ||
    (mode !== "local" && mode !== "sso") ||
    typeof local?.enabled !== "boolean" ||
    typeof keycloak?.enabled !== "boolean" ||
    (localUrl !== null && typeof localUrl !== "string") ||
    (keycloakUrl !== null && typeof keycloakUrl !== "string") ||
    local.enabled !== (mode === "local") ||
    keycloak.enabled !== (mode === "sso") ||
    localUrl !== (mode === "local" ? "/api/v1/admin/auth/local/login" : null) ||
    keycloakUrl !== (mode === "sso" ? "/api/v1/admin/auth/keycloak/login" : null)
  ) {
    return ADMIN_AUTH_CONFIG_UNAVAILABLE;
  }
  return {
    available: true,
    schema_version: 1,
    auth_mode: mode,
    local: { enabled: local.enabled, login_url: localUrl },
    keycloak: { enabled: keycloak.enabled, login_url: keycloakUrl },
  };
}

export async function getAdminAuthConfig(): Promise<AdminAuthConfig> {
  try {
    const response = await fetch(`${API_BASE}/admin/auth/config`);
    if (!response.ok) return ADMIN_AUTH_CONFIG_UNAVAILABLE;
    const config = parseAdminAuthConfig(await response.json());
    if (config.available && config.auth_mode !== null) {
      configureAuthTransport(config.auth_mode);
    }
    return config;
  } catch {
    return ADMIN_AUTH_CONFIG_UNAVAILABLE;
  }
}

export interface AdminSession {
  schema_version: 1;
  auth_mode: AuthMode;
  user: {
    id: string;
    username: string;
    email: string;
    display_name: string | null;
    is_admin: true;
  };
}

function parseAdminSession(value: unknown): AdminSession {
  const root = record(value);
  const user = record(root?.user);
  const mode = root?.auth_mode;
  if (
    !hasExactKeys(root, ["schema_version", "auth_mode", "user"]) ||
    !hasExactKeys(user, ["id", "username", "email", "display_name", "is_admin"]) ||
    root?.schema_version !== 1 ||
    (mode !== "local" && mode !== "sso") ||
    typeof user?.id !== "string" ||
    typeof user?.username !== "string" ||
    typeof user?.email !== "string" ||
    (user?.display_name !== null && typeof user?.display_name !== "string") ||
    user?.is_admin !== true
  ) {
    throw new Error("Invalid product-admin session response");
  }
  return {
    schema_version: 1,
    auth_mode: mode,
    user: {
      id: user.id,
      username: user.username,
      email: user.email,
      display_name: user.display_name as string | null,
      is_admin: true,
    },
  };
}

function adminHeaders(headers?: HeadersInit): Headers {
  const result = new Headers(headers);
  const token = getToken();
  if (token) result.set("Authorization", `Bearer ${token}`);
  return result;
}

function cookieValue(name: string): string | null {
  const prefix = `${encodeURIComponent(name)}=`;
  for (const entry of document.cookie.split(";")) {
    const candidate = entry.trim();
    if (candidate.startsWith(prefix)) {
      try {
        return decodeURIComponent(candidate.slice(prefix.length));
      } catch {
        return null;
      }
    }
  }
  return null;
}

function adminCsrfCookieName(): string {
  return window.location.protocol === "https:"
    ? "__Host-akb_admin_csrf"
    : "akb_dev_admin_csrf";
}

export async function getAdminSession(): Promise<AdminSession | null> {
  const response = await fetch(`${API_BASE}/admin/auth/session`, {
    credentials: "same-origin",
    headers: adminHeaders(),
  });
  if (response.status === 401 || response.status === 403) return null;
  if (!response.ok) await throwJsonApiError(response);
  return parseAdminSession(await response.json());
}

export const adminLocalLogin = (username: string, password: string) =>
  fetch(`${API_BASE}/admin/auth/local/login`, {
    method: "POST",
    credentials: "same-origin",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ username, password }),
  }).then(parseAuthResponse);

export async function adminLogout(): Promise<{ logout_url: string }> {
  const headers = adminHeaders();
  const csrf = cookieValue(adminCsrfCookieName());
  if (csrf) headers.set("X-AKB-Admin-CSRF", csrf);
  const response = await fetch(`${API_BASE}/admin/auth/logout`, {
    method: "POST",
    credentials: "same-origin",
    headers,
  });
  if (!response.ok) await throwJsonApiError(response);
  const payload = record(await response.json());
  if (
    !hasExactKeys(payload, ["logout_url"]) ||
    typeof payload.logout_url !== "string" ||
    !payload.logout_url ||
    payload.logout_url.length > 4096 ||
    hasControlCharacters(payload.logout_url)
  ) {
    throw new Error("Invalid product-admin logout response");
  }
  let navigation: URL;
  try {
    navigation = new URL(payload.logout_url, window.location.href);
  } catch {
    throw new Error("Invalid product-admin logout response");
  }
  if (
    !["http:", "https:"].includes(navigation.protocol) ||
    navigation.username !== "" ||
    navigation.password !== "" ||
    navigation.hash !== ""
  ) {
    throw new Error("Invalid product-admin logout response");
  }
  return {
    logout_url:
      navigation.origin === window.location.origin
        ? `${navigation.pathname}${navigation.search}`
        : navigation.href,
  };
}

export type AdminSsoProviderState =
  | "configured_disabled"
  | "enabled"
  | "configuration_error";

export interface AdminSsoProvider {
  provider_type: string;
  alias: string;
  display_name: string;
  state: AdminSsoProviderState;
  enabled: boolean;
  issuer: string | null;
  discovery_url: string | null;
  client_id: string | null;
  client_secret_configured: boolean;
  redirect_uri: string;
  capabilities: {
    supports_logout: boolean;
    supports_identity_migration: boolean;
  };
}

export interface AdminSsoCatalog {
  schema_version: 1;
  auth_mode: "sso";
  control_mode: "direct" | "delegated";
  supported_provider_types: string[];
  providers: AdminSsoProvider[];
}

function parseAdminSsoProvider(value: unknown): AdminSsoProvider {
  const provider = record(value);
  const capabilities = record(provider?.capabilities);
  const state = provider?.state;
  if (
    !hasExactKeys(provider, [
      "provider_type", "alias", "display_name", "state", "enabled", "issuer",
      "discovery_url", "client_id", "client_secret_configured",
      "redirect_uri", "capabilities",
    ]) ||
    !hasExactKeys(capabilities, ["supports_logout", "supports_identity_migration"]) ||
    typeof provider.provider_type !== "string" ||
    !/^[a-z0-9][a-z0-9-]{0,79}$/.test(provider.provider_type) ||
    typeof provider.alias !== "string" ||
    !/^[a-z0-9][a-z0-9._-]{0,62}$/.test(provider.alias) ||
    typeof provider.display_name !== "string" ||
    !provider.display_name.trim() ||
    provider.display_name.length > 80 ||
    hasControlCharacters(provider.display_name) ||
    (state !== "configured_disabled" && state !== "enabled" && state !== "configuration_error") ||
    typeof provider.enabled !== "boolean" ||
    (state === "enabled" && provider.enabled !== true) ||
    (state === "configured_disabled" && provider.enabled !== false) ||
    (provider.issuer !== null && (
      typeof provider.issuer !== "string" ||
      provider.issuer.length > 2048 ||
      hasControlCharacters(provider.issuer)
    )) ||
    (provider.discovery_url !== null && (
      typeof provider.discovery_url !== "string" ||
      provider.discovery_url.length > 2048 ||
      hasControlCharacters(provider.discovery_url)
    )) ||
    (provider.client_id !== null && (
      typeof provider.client_id !== "string" ||
      provider.client_id.length > 255 ||
      hasControlCharacters(provider.client_id)
    )) ||
    typeof provider.client_secret_configured !== "boolean" || // pragma: allowlist secret
    typeof provider.redirect_uri !== "string" ||
    !provider.redirect_uri ||
    provider.redirect_uri.length > 2048 ||
    hasControlCharacters(provider.redirect_uri) ||
    ((state === "enabled" || state === "configured_disabled") && (
      provider.issuer === null ||
      provider.discovery_url === null ||
      provider.client_id === null ||
      provider.client_secret_configured !== true // pragma: allowlist secret
    )) ||
    typeof capabilities.supports_logout !== "boolean" ||
    typeof capabilities.supports_identity_migration !== "boolean"
  ) {
    throw new Error("Invalid SSO provider response");
  }
  return {
    provider_type: provider.provider_type,
    alias: provider.alias,
    display_name: provider.display_name,
    state,
    enabled: provider.enabled,
    issuer: provider.issuer as string | null,
    discovery_url: provider.discovery_url as string | null,
    client_id: provider.client_id as string | null,
    client_secret_configured: provider.client_secret_configured,
    redirect_uri: provider.redirect_uri,
    capabilities: {
      supports_logout: capabilities.supports_logout,
      supports_identity_migration: capabilities.supports_identity_migration,
    },
  };
}

export function parseAdminSsoCatalog(value: unknown): AdminSsoCatalog {
  const root = record(value);
  const types = root?.supported_provider_types;
  const values = root?.providers;
  if (
    !hasExactKeys(root, [
      "schema_version", "auth_mode", "control_mode",
      "supported_provider_types", "providers",
    ]) ||
    root.schema_version !== 1 ||
    root.auth_mode !== "sso" ||
    (root.control_mode !== "direct" && root.control_mode !== "delegated") ||
    !Array.isArray(types) ||
    types.length > 32 ||
    types.some((type) => typeof type !== "string" || !/^[a-z0-9][a-z0-9-]{0,79}$/.test(type)) ||
    new Set(types).size !== types.length ||
    !Array.isArray(values) ||
    values.length > 100 ||
    (root.control_mode === "delegated" && values.length !== 0)
  ) {
    throw new Error("Invalid SSO provider catalog response");
  }
  const providers = values.map(parseAdminSsoProvider);
  if (
    new Set(providers.map((provider) => provider.alias)).size !== providers.length ||
    providers.some((provider) => !types.includes(provider.provider_type))
  ) {
    throw new Error("Invalid SSO provider catalog response");
  }
  return {
    schema_version: 1,
    auth_mode: "sso",
    control_mode: root.control_mode,
    supported_provider_types: types,
    providers,
  };
}

function adminSsoHeaders({ mutation = false }: { mutation?: boolean } = {}): Headers {
  const headers = adminHeaders();
  if (mutation) {
    headers.set("Content-Type", "application/json");
    const csrf = cookieValue(adminCsrfCookieName());
    if (csrf) headers.set("X-AKB-Admin-CSRF", csrf);
  }
  return headers;
}

export async function getAdminSsoCatalog(): Promise<AdminSsoCatalog> {
  const response = await fetch(`${API_BASE}/admin/sso/providers`, {
    credentials: "same-origin",
    headers: adminSsoHeaders(),
  });
  if (!response.ok) await throwJsonApiError(response);
  return parseAdminSsoCatalog(await response.json());
}

export interface ConfigureAdminSsoProvider {
  provider_type: string;
  display_name: string;
  issuer: string;
  discovery_url: string;
  client_id: string;
  client_secret?: string;
}

async function parseAdminSsoMutation(response: Response): Promise<AdminSsoProvider> {
  if (!response.ok) await throwJsonApiError(response);
  const payload = record(await response.json());
  if (!hasExactKeys(payload, ["provider"])) {
    throw new Error("Invalid SSO provider mutation response");
  }
  return parseAdminSsoProvider(payload.provider);
}

export function configureAdminSsoProvider(
  alias: string,
  input: ConfigureAdminSsoProvider,
): Promise<AdminSsoProvider> {
  return fetch(`${API_BASE}/admin/sso/providers/${encodeURIComponent(alias)}`, {
    method: "PUT",
    credentials: "same-origin",
    headers: adminSsoHeaders({ mutation: true }),
    body: JSON.stringify(input),
  }).then(parseAdminSsoMutation);
}

export function setAdminSsoProviderEnabled(
  alias: string,
  enabled: boolean,
): Promise<AdminSsoProvider> {
  const verb = enabled ? "enable" : "disable";
  return fetch(
    `${API_BASE}/admin/sso/providers/${encodeURIComponent(alias)}/${verb}`,
    {
      method: "POST",
      credentials: "same-origin",
      headers: adminSsoHeaders({ mutation: true }),
    },
  ).then(parseAdminSsoMutation);
}

// ── Auth (ordinary browser session) ──
export interface CurrentUser {
  user_id: string;
  username: string;
  email: string;
  display_name: string | null;
  is_admin: boolean;
  auth_method: string;
  key_class: string | null;
}

export const getMe = (
  { redirectOnUnauthorized = true }: { redirectOnUnauthorized?: boolean } = {},
) => api<CurrentUser>(
  "/auth/me",
  undefined,
  redirectOnUnauthorized ? "expire-and-redirect" : "expire",
);

export interface OrdinaryLogoutResult {
  mode: AuthMode;
  logout_url: string;
}

export async function logoutOrdinarySession(): Promise<OrdinaryLogoutResult> {
  const mode = _authMode === "sso" ? "sso" : "local";
  if (mode === "local") {
    const legacyLogout = legacySsoLogoutUrl();
    setToken(null);
    clearLegacySsoSession();
    return legacyLogout
      ? { mode: "sso", logout_url: legacyLogout }
      : { mode, logout_url: "/auth" };
  }
  const response = await authenticatedFetch(
    `${API_BASE}/auth/logout`,
    { method: "POST" },
    { unauthorized: "preserve-session" },
  );
  if (!response.ok) await throwJsonApiError(response);
  const payload = record(await response.json());
  if (
    !hasExactKeys(payload, ["logout_url"]) ||
    typeof payload.logout_url !== "string" ||
    !payload.logout_url ||
    payload.logout_url.length > 4096 ||
    hasControlCharacters(payload.logout_url)
  ) {
    throw new Error("Invalid SSO logout response");
  }
  let navigation: URL;
  try {
    navigation = new URL(payload.logout_url);
  } catch {
    throw new Error("Invalid SSO logout response");
  }
  if (
    !["http:", "https:"].includes(navigation.protocol) ||
    navigation.username !== "" ||
    navigation.password !== "" ||
    navigation.hash !== ""
  ) {
    throw new Error("Invalid SSO logout response");
  }
  _authSessionGeneration += 1;
  clearPrivateAssetCache();
  return { mode, logout_url: navigation.href };
}

/** Redeem the legacy one-time Keycloak callback code for an AKB Bearer JWT. */
export const keycloakExchange = (code: string) =>
  fetch(`${API_BASE}/auth/keycloak/exchange`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ code }),
  }).then(parseAuthResponse);

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
  deleted_tables?: number;
}

export interface CollectionNotEmptyDetail {
  message: string;
  doc_count: number;
  file_count: number;
  sub_collection_count: number;
  table_count?: number;
}

export const createCollection = (vault: string, path: string, summary?: string) =>
  api<CollectionCreateResult>(`/collections/${encodeURIComponent(vault)}`, {
    method: "POST",
    body: JSON.stringify({ path, summary }),
  });

/**
 * Update collection metadata on backends that implement the collection PATCH
 * contract. Older AKB servers only expose POST + DELETE and answer 404/405;
 * callers must keep their view usable and surface an upgrade hint instead of
 * treating that compatibility response as a generic failure.
 */
export const updateCollection = (vault: string, path: string, summary: string | null) => {
  const segs = path.split("/").map(encodeURIComponent).join("/");
  return api<CollectionCreateResult>(
    `/collections/${encodeURIComponent(vault)}/${segs}`,
    {
      method: "PATCH",
      body: JSON.stringify({ summary }),
    },
  );
};

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

// ── Editor image assets ──
export interface AssetUploadResponse {
  id: string;
  url: string;
  name: string;
  mime_type: string;
  size_bytes: number;
}

/**
 * Upload an editor image as a raw request body. This deliberately does not use
 * `api()`: that helper always injects `Content-Type: application/json`, while
 * the asset endpoint validates the image MIME from this header.
 */
export async function uploadAsset(
  vault: string,
  file: File,
  signal?: AbortSignal,
): Promise<AssetUploadResponse> {
  const params = new URLSearchParams({ filename: file.name });
  const headers: Record<string, string> = { "Content-Type": file.type };

  const res = await authenticatedFetch(
    `${API_BASE}/assets/${encodeURIComponent(vault)}?${params}`,
    { method: "POST", headers, body: file, signal },
  );
  if (!res.ok) {
    await throwJsonApiError(res);
  }
  return res.json();
}

/** Fetch a private asset with the app's Bearer credential for blob rendering. */
export async function getAssetBlob(
  fileId: string,
  vault: string,
  signal?: AbortSignal,
  source?: { document: string; commit: string },
): Promise<Blob> {
  const fetchBlob = async (requestSignal?: AbortSignal) => {
    const params = new URLSearchParams({ vault });
    if (source) {
      params.set("document", source.document);
      params.set("commit", source.commit);
    }
    const res = await authenticatedFetch(
      `/api/assets/${encodeURIComponent(fileId)}?${params}`,
      { signal: requestSignal },
    );
    if (!res.ok) throw new Error(`Image unavailable (${res.status})`);
    return res.blob();
  };

  // An HttpOnly SSO cookie can be replaced by another tab without a storage
  // event. Do not serve a persistent private blob cache until the foreground
  // identity recheck in Layout has observed that change.
  if (_authMode === "sso") return fetchBlob(signal);

  // The cache is scoped to an in-memory auth generation, never raw credential
  // material. Login, logout, mode changes, and 401 expiry all rotate it.
  const cacheKey = privateAssetCacheKey(_authSessionGeneration, fileId, vault, source);
  const cached = readPrivateAssetCache(cacheKey);
  if (cached) return cached;

  let flight = privateAssetFlights.get(cacheKey);
  if (!flight) {
    const controller = new AbortController();
    const entry: AssetFlight = {
      controller,
      subscribers: 0,
      settled: false,
      promise: Promise.resolve(new Blob()),
    };
    entry.promise = (async () => {
      const blob = await fetchBlob(controller.signal);
      writePrivateAssetCache(cacheKey, blob);
      return blob;
    })().finally(() => {
      entry.settled = true;
      if (privateAssetFlights.get(cacheKey) === entry) {
        privateAssetFlights.delete(cacheKey);
      }
    });
    flight = entry;
    privateAssetFlights.set(cacheKey, flight);
  }
  flight.subscribers += 1;
  try {
    return await waitForAssetFlight(flight, signal);
  } finally {
    flight.subscribers -= 1;
    if (!flight.settled && flight.subscribers === 0) flight.controller.abort();
  }
}

/** Best-effort cleanup for an upload that never reached a document commit. */
export async function discardAsset(vault: string, fileId: string): Promise<void> {
  const res = await authenticatedFetch(
    `${API_BASE}/assets/${encodeURIComponent(vault)}/${encodeURIComponent(fileId)}`,
    { method: "DELETE", keepalive: true },
    // Cleanup is best effort and may race with logout.  Its 401 must not
    // invalidate an otherwise live session or abort unrelated image reads.
    { unauthorized: "preserve-session" },
  );
  if (res.ok) return;
  await throwJsonApiError(res);
}

// ── Vault files ──
export type FileUploadStage = "preparing" | "uploading" | "confirming";

export interface VaultFileUploadOptions {
  collection?: string;
  description?: string;
  onStageChange?: (stage: FileUploadStage) => void;
}

export interface VaultFileUploadResult {
  kind: "file";
  uri: string;
  vault: string;
  name: string;
  collection?: string | null;
  mime_type: string;
  size_bytes: number;
  content_hash?: string;
  version?: string | null;
}

export interface VaultFileDeleteResult {
  kind: "file";
  uri?: string | null;
  vault: string;
  collection?: string | null;
  name: string;
  deleted: true;
}

/**
 * Complete the backend's presigned file flow from the browser: reserve the
 * file record, PUT the bytes directly to object storage, then certify the
 * upload. The stage callback keeps the modal honest during the multi-request
 * operation without exposing storage implementation details in its copy.
 */
export async function uploadVaultFile(
  vault: string,
  file: File,
  options: VaultFileUploadOptions = {},
): Promise<VaultFileUploadResult> {
  const mimeType = file.type || "application/octet-stream";
  const params = new URLSearchParams({
    filename: file.name,
    mime_type: mimeType,
  });
  const collection = options.collection?.trim();
  const description = options.description?.trim();
  if (collection) params.set("collection", collection);
  if (description) params.set("description", description);

  options.onStageChange?.("preparing");
  const prepare = await authenticatedFetch(
    `${API_BASE}/files/${encodeURIComponent(vault)}/upload?${params}`,
    { method: "POST" },
  );
  if (!prepare.ok) await throwJsonApiError(prepare);
  const reservation = (await prepare.json()) as {
    uri: string;
    upload_url: string;
    deduplicated?: boolean;
  };
  const fileIdMatch = reservation.uri?.match(/\/file\/([^/?#]+)$/);
  const fileId = fileIdMatch ? decodeURIComponent(fileIdMatch[1]) : "";
  if (!fileId || !reservation.upload_url) {
    throw new Error("The server returned an incomplete upload reservation.");
  }

  try {
    if (!reservation.deduplicated) {
      options.onStageChange?.("uploading");
      const transfer = await fetch(reservation.upload_url, {
        method: "PUT",
        headers: { "Content-Type": mimeType },
        body: file,
      });
      if (!transfer.ok) {
        throw new Error(`File transfer failed (${transfer.status}).`);
      }
    }

    options.onStageChange?.("confirming");
    const confirm = await authenticatedFetch(
      `${API_BASE}/files/${encodeURIComponent(vault)}/${encodeURIComponent(fileId)}/confirm`,
      { method: "POST" },
    );
    if (!confirm.ok) await throwJsonApiError(confirm);
    return confirm.json();
  } catch (error) {
    // Best-effort rollback: pending reservations are otherwise cleaned by the
    // backend worker, but immediate cleanup lets a user retry the same file now.
    try {
      await authenticatedFetch(
        `${API_BASE}/files/${encodeURIComponent(vault)}/${encodeURIComponent(fileId)}`,
        { method: "DELETE" },
        { unauthorized: "preserve-session" },
      );
    } catch {
      // The original transfer/confirmation error is the useful one to surface.
    }
    throw error;
  }
}

export const deleteVaultFile = (vault: string, fileId: string) =>
  api<VaultFileDeleteResult>(
    `/files/${encodeURIComponent(vault)}/${encodeURIComponent(fileId)}`,
    { method: "DELETE" },
  );

// ── Vault tables ──
export interface VaultTableColumnInput {
  name: string;
  type: string;
  required?: boolean;
  unique?: boolean;
}

export interface VaultTableCreateInput {
  name: string;
  description?: string;
  collection?: string;
  columns: VaultTableColumnInput[];
}

export interface VaultTableCreateResult {
  kind?: "table";
  uri?: string;
  vault?: string;
  name?: string;
  table?: string;
  created?: boolean;
}

export interface VaultTableDeleteResult {
  kind: "table";
  uri: string;
  vault: string;
  collection?: string | null;
  name: string;
  deleted: true;
}

export const createVaultTable = (
  vault: string,
  input: VaultTableCreateInput,
) =>
  api<VaultTableCreateResult>(`/tables/${encodeURIComponent(vault)}`, {
    method: "POST",
    body: JSON.stringify({
      ...input,
      description: input.description?.trim() || "",
      collection: input.collection?.trim() || undefined,
    }),
  });

export const deleteVaultTable = (vault: string, tableName: string) =>
  api<VaultTableDeleteResult>(
    `/tables/${encodeURIComponent(vault)}/${encodeURIComponent(tableName)}`,
    { method: "DELETE" },
  );

// ── Browse ──
export const browseVault = (vault: string, collection?: string, depth = 1) => {
  const p = new URLSearchParams({ depth: String(depth) });
  if (collection) p.set("collection", collection);
  return api<{
    vault: string;
    path: string;
    context?: {
      type: "vault" | "collection";
      uri: string;
      name: string;
      path: string;
      summary?: string | null;
      description?: string | null;
    } | null;
    items: any[];
  }>(`/browse/${vault}?${p}`);
};

export interface KnowledgeImportResult {
  format: string;
  vault: string;
  created: number;
  skipped: number;
  failed: number;
  uris: string[];
  skipped_paths: string[];
  reserved: string[];
  errors: Array<{ path: string; error: string }>;
}

/** Import an OKF-compatible ZIP without forcing the JSON content type used by
 *  `api()`. The browser owns the multipart boundary for this FormData body. */
export async function importKnowledgeBundle(
  vault: string,
  file: File,
): Promise<KnowledgeImportResult> {
  const body = new FormData();
  body.append("file", file);
  const res = await authenticatedFetch(
    `${API_BASE}/vaults/${encodeURIComponent(vault)}/import?format=okf&status=active`,
    { method: "POST", body },
  );
  if (!res.ok) await throwJsonApiError(res);
  return res.json();
}

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
// Scope a search to one or more vaults. The backend `vault` query param is
// repeatable (`?vault=a&vault=b`); omit it for all accessible vaults.
const vaultScopeParams = (vaults?: string[] | string): string[] =>
  (Array.isArray(vaults) ? vaults : vaults ? [vaults] : []).filter(Boolean);

export const searchDocs = (query: string, vaults?: string[] | string, limit = 10) => {
  const p = new URLSearchParams({ q: query, limit: String(limit) });
  for (const v of vaultScopeParams(vaults)) p.append("vault", v);
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
export const grepDocs = (query: string, vaults?: string[] | string, limit = 20) => {
  const p = new URLSearchParams({ q: query, limit: String(limit) });
  for (const v of vaultScopeParams(vaults)) p.append("vault", v);
  return api<GrepResponse>(`/grep?${p}`);
};

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
  // 'implicit' (parsed from doc body) vs 'explicit' (akb_link). Present on
  // overview + BFS responses; optional so legacy callers stay valid.
  kind?: "implicit" | "explicit";
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
  // Focused graph search can start from documents, tables, or files. Existing
  // document callers pass a vault-relative path; typed resources pass their
  // canonical URI so the same backend endpoint can center the BFS correctly.
  if (docPath) p.set("uri", docPath.startsWith("akb://") ? docPath : _docUri(vault, docPath));
  else p.set("vault", vault);
  return api<{ nodes: GraphApiNode[]; edges: GraphApiEdge[] }>(`/graph?${p}`);
};

// Whole-vault overview: the top-`topK` highest-degree nodes + induced edges,
// with honest totals so the UI can show "showing N of M" instead of a silent
// recency cap. Backed by GET /graph/overview (degree-ranked, deterministic).
export interface GraphOverviewResponse {
  nodes: GraphApiNode[];
  edges: GraphApiEdge[];
  nodes_total: number;
  edges_total: number;
  returned: number;
  truncated: boolean;
  // Count of unlinked resources appended as degree-0 isolated nodes (so a vault
  // with no relations still renders its resources, governed by the "Hide
  // orphans" toggle). nodes_total/returned/truncated describe the CONNECTED
  // graph only; `nodes` additionally holds the orphans (len(nodes) = returned +
  // orphans_returned). INFORMATIONAL — the canvas recomputes its own orphan
  // count from edge connectivity (graph.tsx), it does not read this field.
  orphans_returned?: number;
  // True when the orphan set was capped (orphan_limit) — more unlinked
  // resources exist than were returned. Parallel to `truncated` for the
  // connected graph.
  orphans_truncated?: boolean;
}
export const getGraphOverview = (vault: string, topK = 200) => {
  const p = new URLSearchParams({ vault, top_k: String(topK) });
  return api<GraphOverviewResponse>(`/graph/overview?${p}`);
};

// KB-health audit: over-connected hubs + orphan documents (no relations).
// Backed by GET /graph/health.
export interface GraphHealthNode {
  uri: string;
  name: string;
  resource_type?: string;
  degree?: number;
}
export interface GraphHealthResponse {
  hubs: GraphHealthNode[];
  orphans: { count: number; sample: GraphHealthNode[] };
}
export const getGraphHealth = (vault: string, hubThreshold = 5, limit = 20) => {
  const p = new URLSearchParams({
    vault,
    hub_threshold: String(hubThreshold),
    limit: String(limit),
  });
  return api<GraphHealthResponse>(`/graph/health?${p}`);
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
  kind: "implicit" | "explicit";
  name?: string;
}
export const getRelations = (vault: string, docPath: string) => {
  // The relations endpoint accepts any typed resource URI. Document callers
  // still pass a vault-relative path; graph callers can pass a canonical
  // table/file URI without it being accidentally rewritten as a doc URI.
  const uri = docPath.startsWith("akb://") ? docPath : _docUri(vault, docPath);
  const p = new URLSearchParams({ uri });
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
  /** Native activity responses use `date`; legacy/imported responses may use `timestamp`. */
  date?: string;
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
  summary?: string;
  domain?: string;
  /** Human author name resolved from the doc's creator at read time. The raw
   *  created_by identifier and internal status/created_at are not exposed to
   *  anonymous viewers (F8). */
  created_by_name?: string;
  updated_at?: string;
  tags?: string[];
  content?: string;
  content_unavailable?: boolean;
  section_filter?: string | null;
  section_not_found?: boolean;
  // file fields — content is served from same-origin /raw and /download
  // (built from the slug); the resolver no longer returns a presigned
  // download_url / url_expires_in (F4).
  name?: string;
  mime_type?: string;
  size_bytes?: number;
  collection?: string;
  // table_query fields
  columns?: string[];
  rows?: Record<string, any>[];
  total?: number;
  query_params?: Record<string, { type?: string; default?: any; required?: boolean }>;
  applied_params?: Record<string, any>;
  mode?: "live" | "snapshot";
  snapshot_at?: string;
  // Short-lived grant minted by this page open; carried by /raw, /download and
  // CSV so they re-serve this same view without re-counting it.
  view_grant?: string;
  // Bounded proof for rotating a legacy fetch grant during a rolling upgrade.
  view_grant_session?: string;
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

// View-grant: the page-open response mints one; /raw, /download and CSV carry it
// so those re-serves of the SAME view aren't re-counted. Without it the backend
// counts each fetch as its own view (so max_views is a hard cap on every path).
function publicationGrantKey(slug: string) {
  return `akb_publication_grant_${slug}`;
}

function publicationSessionGrantKey(slug: string) {
  return `akb_publication_grant_session_${slug}`;
}

export function getViewGrant(slug: string): string | null {
  return sessionStorage.getItem(publicationGrantKey(slug));
}

export function setViewGrant(slug: string, grant: string) {
  sessionStorage.setItem(publicationGrantKey(slug), grant);
}

function getViewGrantSession(slug: string): string | null {
  return sessionStorage.getItem(publicationSessionGrantKey(slug));
}

function setViewGrantSession(slug: string, grant: string) {
  sessionStorage.setItem(publicationSessionGrantKey(slug), grant);
}

async function fetchPublic(
  slug: string,
  path: string = "",
  params?: Record<string, string>,
  init?: RequestInit,
): Promise<Response> {
  const token = getPublicationToken(slug);
  const grant = getViewGrant(slug);
  const search = new URLSearchParams(params || {});
  if (token) search.set("token", token);
  // Carry any grant from an earlier open so a reload / param-change within the
  // window re-serves this view instead of spending a new one.
  if (grant) search.set("grant", grant);
  const qs = search.toString();
  const suffix = qs ? `?${qs}` : "";
  return fetch(`${API_BASE}/public/${slug}${path}${suffix}`, init);
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
  // This page open spent one view and returned a grant; keep it so the paired
  // /raw + /download re-serves of this view aren't counted again.
  if (body.view_grant) setViewGrant(slug, body.view_grant);
  if (body.view_grant_session) {
    setViewGrantSession(slug, body.view_grant_session);
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

const publicationGrantRefreshes = new Map<string, Promise<string>>();

/** Acquire one newly counted subordinate-fetch window for a publication. */
export function refreshPublicationViewGrant(slug: string): Promise<string> {
  const existing = publicationGrantRefreshes.get(slug);
  if (existing) return existing;

  const refresh = (async () => {
    const search = new URLSearchParams();
    const token = getPublicationToken(slug);
    const renewalGrant = getViewGrantSession(slug) ?? getViewGrant(slug);
    if (token) search.set("token", token);
    if (renewalGrant) search.set("grant", renewalGrant);
    const qs = search.toString();
    const res = await fetch(
      `${API_BASE}/public/${encodeURIComponent(slug)}/grant${qs ? `?${qs}` : ""}`,
      { method: "POST" },
    );
    const body = await res.json().catch(() => ({}));
    if (!res.ok || !body.view_grant) {
      throw new Error(body.detail || body.error || "Publication grant expired");
    }
    setViewGrant(slug, body.view_grant);
    if (body.view_grant_session) {
      setViewGrantSession(slug, body.view_grant_session);
    } else if (body.view_grant.split(".").length === 3) {
      // Compatibility with a bounded-grant backend that predates the separate
      // session field.  Never promote a two-field fetch grant here.
      setViewGrantSession(slug, body.view_grant);
    }
    return body.view_grant;
  })();

  const tracked = refresh.finally(() => {
    if (publicationGrantRefreshes.get(slug) === tracked) {
      publicationGrantRefreshes.delete(slug);
    }
  });
  publicationGrantRefreshes.set(slug, tracked);
  return tracked;
}

export interface PublicationCapabilities {
  can_edit: boolean;
  vault?: string;
  resource_type?: "document" | "table_query" | "file";
}

// Owner capability probe for the public page. SSO's HttpOnly cookie cannot be
// detected in JavaScript, so the request is anonymous/cookie-first and degrades
// to {can_edit:false} on any error without redirecting the public view. (F6.)
export async function publicationCapabilities(slug: string): Promise<PublicationCapabilities> {
  try {
    if (_authMode === null) {
      const config = await getAuthConfig();
      if (!config.available) return { can_edit: false };
    }
    // An SSO session is HttpOnly and intentionally cannot be detected from
    // JavaScript. Probe anonymously/cookie-first and degrade to read-only.
    const res = await authenticatedFetch(
      `${API_BASE}/public/${slug}/capabilities`,
      undefined,
      { unauthorized: "preserve-session" },
    );
    if (!res.ok) return { can_edit: false };
    return await res.json();
  } catch {
    return { can_edit: false };
  }
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
  const grant = getViewGrant(slug);
  const search = new URLSearchParams(params || {});
  if (token) search.set("token", token);
  if (grant) search.set("grant", grant);
  const qs = search.toString();
  return `${API_BASE}/public/${slug}/download${qs ? `?${qs}` : ""}`;
}

export function publicationRawUrl(slug: string): string {
  const token = getPublicationToken(slug);
  const grant = getViewGrant(slug);
  const search = new URLSearchParams();
  if (token) search.set("token", token);
  if (grant) search.set("grant", grant);
  const qs = search.toString();
  return `${API_BASE}/public/${slug}/raw${qs ? `?${qs}` : ""}`;
}

/**
 * Asset-scoped URL for an image embedded by a counted document view.
 *
 * The grant suppresses N+1 view counting. Password-protected publications also
 * require their short-lived password token; a grant never unlocks content.
 */
export function publicationAssetUrl(slug: string, fileId: string): string {
  const token = getPublicationToken(slug);
  const grant = getViewGrant(slug);
  const search = new URLSearchParams();
  if (token) search.set("token", token);
  if (grant) search.set("grant", grant);
  const qs = search.toString();
  return `${API_BASE}/public/${encodeURIComponent(slug)}/assets/${encodeURIComponent(fileId)}${qs ? `?${qs}` : ""}`;
}

export function publicationCsvUrl(slug: string, params?: Record<string, string>): string {
  const search = new URLSearchParams(params || {});
  search.set("format", "csv");
  const token = getPublicationToken(slug);
  const grant = getViewGrant(slug);
  if (token) search.set("token", token);
  if (grant) search.set("grant", grant);
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
