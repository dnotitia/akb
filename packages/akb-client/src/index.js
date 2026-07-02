import { AkbError } from "./errors.js";
export { AkbError } from "./errors.js";

export { createTypedFetch } from "./core/fetch.js";
import { createQueryBuilder } from "./client/query-builder.js";

/**
 * Convert one parsed HTTP response body into a `{data,error}` result.
 *
 * @template T
 * @param {Pick<Response, "ok" | "status" | "statusText"> | null} response
 * @param {T | unknown} body
 * @returns {import("./index.js").AkbResult<T>}
 */
export function unwrapAkbResponse(response, body) {
  if (response?.ok) {
    return makeResult(body, null, response);
  }
  return makeResult(null, new AkbError(body, asResponse(response)), response);
}

/**
 * Fetch an AKB REST endpoint and unwrap the HTTP boundary.
 *
 * @template T
 * @param {RequestInfo | URL} input
 * @param {RequestInit} [init]
 * @param {typeof fetch} [fetchImpl]
 * @returns {Promise<import("./index.js").AkbResult<T>>}
 */
export async function akbFetch(input, init = undefined, fetchImpl = globalThis.fetch) {
  if (typeof fetchImpl !== "function") {
    throw new TypeError("A fetch implementation is required.");
  }
  const response = await fetchImpl(input, init);
  const body = await readBody(response);
  return unwrapAkbResponse(response, body);
}

/**
 * Create a scoped AKB REST client. The namespace helpers are intentionally thin
 * request facades; fluent table/storage helpers can build on this contract.
 *
 * @param {import("./index.js").AkbClientConfig | string | URL} configOrUrl
 * @param {import("./index.js").AkbClientOptions} [options]
 * @returns {import("./index.js").AkbClient}
 */
export function createClient(configOrUrl, options = {}) {
  const config = normalizeClientConfig(configOrUrl, options);
  return makeClient(config, {
    defaultVault: config.defaultVault ?? null,
    claims: null,
  });
}

/**
 * @template T
 * @param {T | null} data
 * @param {AkbError | null} error
 * @param {Pick<Response, "ok" | "status" | "statusText"> | null} response
 * @returns {import("./index.js").AkbResult<T>}
 */
function makeResult(data, error, response) {
  return {
    data,
    error,
    response,
    throwOnError() {
      if (error) throw error;
      return this;
    },
  };
}

/**
 * @param {Response} response
 * @returns {Promise<unknown>}
 */
async function readBody(response) {
  const text = await response.text();
  if (!text) return null;
  const contentType = response.headers.get("content-type") ?? "";
  if (contentType.includes("application/json")) {
    return JSON.parse(text);
  }
  try {
    return JSON.parse(text);
  } catch {
    return text;
  }
}

/**
 * @param {Pick<Response, "ok" | "status" | "statusText"> | null | undefined} response
 * @returns {Pick<Response, "ok" | "status" | "statusText"> | null}
 */
function asResponse(response) {
  return response ?? null;
}

/**
 * @param {string} url
 * @returns {string}
 */
function trimTrailingSlash(url) {
  return url.endsWith("/") ? url.slice(0, -1) : url;
}

/**
 * @param {string} baseUrl
 * @param {string | URL} path
 * @returns {string}
 */
function resolveRequestUrl(baseUrl, path) {
  const value = String(path);
  if (/^https?:\/\//i.test(value)) {
    const baseOrigin = httpOrigin(baseUrl);
    const targetOrigin = httpOrigin(value);
    if (baseOrigin && targetOrigin === baseOrigin) return value;
    throw new TypeError("Refusing to send an AKB bearer token to a different origin.");
  }
  return `${baseUrl}${value.startsWith("/") ? value : `/${value}`}`;
}

/**
 * @param {string} value
 * @returns {string | null}
 */
function httpOrigin(value) {
  try {
    return new URL(value).origin;
  } catch {
    return null;
  }
}

/**
 * @param {import("./index.js").AkbClientConfig | string | URL} configOrUrl
 * @param {import("./index.js").AkbClientOptions} options
 * @returns {Required<Pick<import("./index.js").AkbClientConfig, "baseUrl">> & import("./index.js").AkbClientOptions}
 */
function normalizeClientConfig(configOrUrl, options) {
  if (typeof configOrUrl === "string" || configOrUrl instanceof URL) {
    return {
      ...options,
      baseUrl: trimTrailingSlash(String(configOrUrl)),
      token: options.token ?? options.apiKey ?? null,
    };
  }

  if (!configOrUrl || typeof configOrUrl !== "object") {
    throw new TypeError("createClient requires a base URL string or config object.");
  }

  return {
    ...configOrUrl,
    baseUrl: trimTrailingSlash(configOrUrl.baseUrl),
    token: configOrUrl.token ?? configOrUrl.apiKey ?? null,
  };
}

/**
 * @param {Required<Pick<import("./index.js").AkbClientConfig, "baseUrl">> & import("./index.js").AkbClientOptions} config
 * @param {import("./index.js").AkbClientScope} scope
 * @returns {import("./index.js").AkbClient}
 */
function makeClient(config, scope) {
  const fetchImpl = config.fetch ?? globalThis.fetch;

  /** @type {import("./index.js").AkbClient["request"]} */
  async function request(path, init = {}) {
    const requestUrl = resolveRequestUrl(config.baseUrl, path);
    const headers = new Headers(init.headers);
    if (!headers.has("content-type") && init.body !== undefined) {
      headers.set("content-type", "application/json");
    }
    const token = resolveToken(config.token ?? config.apiKey ?? null);
    if (token && !headers.has("authorization")) {
      headers.set("authorization", `Bearer ${token}`);
    }
    if (scope.claims && !headers.has("x-akb-claims")) {
      headers.set("x-akb-claims", JSON.stringify(scope.claims));
    }
    return await akbFetch(requestUrl, { ...init, headers }, fetchImpl);
  }

  const client = {
    request,
    vault(vault) {
      return makeClient(config, { ...scope, defaultVault: vault });
    },
    actingAs(claims) {
      return makeClient(config, { ...scope, claims: normalizeClaims(claims) });
    },
    from(table) {
      return createQueryBuilder({
        baseUrl: config.baseUrl,
        table,
        vault: scope.defaultVault,
        request,
        maxUrlBytes: config.maxUrlBytes ?? 8192,
      });
    },
    search: makeNamespaceStub("search", "/search", request),
    graph: makeNamespaceStub("graph", "/graph", request),
    docs: makeNamespaceStub("docs", "/documents", request),
    storage: makeNamespaceStub("storage", "/files", request),
  };
  return Object.freeze(client);
}

/**
 * @param {string | null | undefined | (() => string | null | undefined)} token
 * @returns {string | null}
 */
function resolveToken(token) {
  const value = typeof token === "function" ? token() : token;
  return typeof value === "string" && value.length > 0 ? value : null;
}

/**
 * @param {import("./index.js").AkbClaims} claims
 * @returns {import("./index.js").AkbClaims}
 */
function normalizeClaims(claims) {
  if (!claims || typeof claims !== "object" || Array.isArray(claims)) {
    throw new TypeError("actingAs requires a claims object.");
  }
  if (typeof claims.sub !== "string" || claims.sub.length === 0) {
    throw new TypeError("actingAs claims require a non-empty sub.");
  }
  if (!claims.app_metadata || typeof claims.app_metadata !== "object" || Array.isArray(claims.app_metadata)) {
    throw new TypeError("actingAs claims require an app_metadata object.");
  }
  if (typeof claims.app_metadata.org_id !== "string" || claims.app_metadata.org_id.length === 0) {
    throw new TypeError("actingAs claims require a non-empty app_metadata.org_id.");
  }
  if (typeof claims.app_metadata.role !== "string" || claims.app_metadata.role.length === 0) {
    throw new TypeError("actingAs claims require a non-empty app_metadata.role.");
  }
  return claims;
}

/**
 * @param {string} name
 * @param {string} prefix
 * @param {import("./index.js").AkbClient["request"]} request
 * @returns {import("./index.js").AkbNamespaceStub}
 */
function makeNamespaceStub(name, prefix, request) {
  return Object.freeze({
    name,
    request(path = "", init = {}) {
      return request(joinPath(prefix, path), init);
    },
  });
}

/**
 * @param {string} prefix
 * @param {string | URL} path
 * @returns {string}
 */
function joinPath(prefix, path) {
  const suffix = String(path);
  if (!suffix) return prefix;
  return `${prefix}${suffix.startsWith("/") ? suffix : `/${suffix}`}`;
}
