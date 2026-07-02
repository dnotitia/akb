/**
 * Create a typed fetch wrapper over an AKB client request function.
 *
 * Runtime work stays deliberately small: the parent client owns bearer auth,
 * X-Akb-Claims, and {data,error}; this helper substitutes OpenAPI path
 * parameters and forwards the final route through that boundary.
 *
 * @param {Pick<import("../index.js").AkbClient, "request">} client
 * @returns {import("./fetch.js").AkbTypedFetch}
 */
export function createTypedFetch(client) {
  return async function typedFetch(method, path, init = {}) {
    const { path: pathParams, query, json, ...requestInit } = init;
    const route = appendQuery(stripApiPrefix(resolvePathTemplate(String(path), pathParams)), query);
    const body = json === undefined ? requestInit.body : JSON.stringify(json);
    return await client.request(route, {
      ...requestInit,
      method: String(method).toUpperCase(),
      body,
    });
  };
}

/**
 * @param {string} path
 * @param {Record<string, string | number | boolean> | undefined} params
 * @returns {string}
 */
function resolvePathTemplate(path, params) {
  return path.replace(/\{([^}]+)\}/g, (_match, key) => {
    const value = params?.[key];
    if (value === undefined || value === null) {
      throw new TypeError(`Missing path parameter: ${key}`);
    }
    return encodeURIComponent(String(value));
  });
}

/**
 * @param {string} path
 * @returns {string}
 */
function stripApiPrefix(path) {
  return path.startsWith("/api/v1/") ? path.slice("/api/v1".length) : path;
}

/**
 * @param {string} path
 * @param {Record<string, string | number | boolean | null | undefined | Array<string | number | boolean>> | undefined} query
 * @returns {string}
 */
function appendQuery(path, query) {
  if (!query) return path;
  const params = new URLSearchParams();
  for (const [key, value] of Object.entries(query)) {
    if (value === null || value === undefined) continue;
    const values = Array.isArray(value) ? value : [value];
    for (const item of values) params.append(key, String(item));
  }
  const suffix = params.toString();
  return suffix ? `${path}${path.includes("?") ? "&" : "?"}${suffix}` : path;
}
