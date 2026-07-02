export class AkbError extends Error {
  /**
   * @param {unknown} payload
   * @param {Pick<Response, "ok" | "status" | "statusText"> | null} [response]
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
 * @param {Response | Pick<Response, "ok" | "status" | "statusText"> | null} response
 * @returns {string | null}
 */
function responseStatusMessage(response) {
  if (!response || typeof response.status !== "number") return null;
  return `${response.status} ${response.statusText || "Request failed"}`;
}
