export class AkbError extends Error {
  /**
   * @param {unknown} payload
   * @param {Response | null} [response]
   */
  constructor(payload, response = null) {
    const body = objectPayload(payload);
    const message = stringValue(body.message)
      ?? stringValue(body.error)
      ?? stringValue(body.detail)
      ?? responseStatusMessage(response)
      ?? "AKB request failed";
    super(message);
    this.name = "AkbError";
    this.code = stringValue(body.code) ?? "unknown";
    this.details = body.details ?? null;
    this.hint = stringValue(body.hint);
    this.status = typeof response?.status === "number" ? response.status : 0;
    this.payload = body;
    this.response = response;
  }
}

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
 * Create a small REST client. This is intentionally only the boundary layer;
 * fluent table/storage helpers can build on top without changing the contract.
 *
 * @param {import("./index.js").AkbClientConfig} config
 * @returns {import("./index.js").AkbClient}
 */
export function createClient(config) {
  const baseUrl = trimTrailingSlash(config.baseUrl);
  const fetchImpl = config.fetch ?? globalThis.fetch;
  return {
    async request(path, init = {}) {
      const requestUrl = resolveRequestUrl(baseUrl, path);
      const headers = new Headers(init.headers);
      if (!headers.has("content-type") && init.body !== undefined) {
        headers.set("content-type", "application/json");
      }
      const token = typeof config.token === "function" ? config.token() : config.token;
      if (token && !headers.has("authorization")) {
        headers.set("authorization", `Bearer ${token}`);
      }
      return await akbFetch(requestUrl, { ...init, headers }, fetchImpl);
    },
  };
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
 * @param {unknown} value
 * @returns {Record<string, unknown>}
 */
function objectPayload(value) {
  return value && typeof value === "object" ? /** @type {Record<string, unknown>} */ (value) : {};
}

/**
 * @param {unknown} value
 * @returns {string | null}
 */
function stringValue(value) {
  return typeof value === "string" && value.length > 0 ? value : null;
}

/**
 * @param {Response | null} response
 * @returns {string | null}
 */
function responseStatusMessage(response) {
  if (!response || typeof response.status !== "number") return null;
  return `${response.status} ${response.statusText || "Request failed"}`;
}

/**
 * @param {Pick<Response, "ok" | "status" | "statusText"> | null | undefined} response
 * @returns {Response | null}
 */
function asResponse(response) {
  if (!response || !("headers" in response)) return null;
  return /** @type {Response} */ (response);
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
