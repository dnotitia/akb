import { AkbError } from "../errors.js";

/**
 * @param {import("../index.js").AkbResult<unknown>} result
 * @param {"rows" | "single" | "maybeSingle"} mode
 * @returns {import("../index.js").AkbResult<unknown>}
 */
export function applyResultMode(result, mode) {
  if (mode === "rows") return result;
  if (result.error) return result;
  const tableQuery = tableQueryData(result.data);
  if (!tableQuery) {
    return clientResult(null, clientResultError("single() expects a table_query result.", "invalid_single_result"), result.response);
  }
  const total = typeof tableQuery.total === "number" ? tableQuery.total : tableQuery.items.length;
  if (total === 1 && tableQuery.items.length === 1) {
    return clientResult(tableQuery.items[0], null, result.response);
  }
  if (mode === "maybeSingle" && total === 0) {
    return clientResult(null, null, result.response);
  }
  const label = mode === "single" ? "single()" : "maybeSingle()";
  return clientResult(
    null,
    clientResultError(`${label} expected ${mode === "single" ? "exactly one row" : "zero or one rows"} but received ${total}.`, "invalid_single_result"),
    result.response,
  );
}

/**
 * @param {unknown} data
 * @returns {{ items: unknown[], total?: number } | null}
 */
function tableQueryData(data) {
  if (!data || typeof data !== "object") return null;
  const items = /** @type {{ items?: unknown }} */ (data).items;
  if (!Array.isArray(items)) return null;
  const total = /** @type {{ total?: unknown }} */ (data).total;
  return { items, ...(typeof total === "number" ? { total } : {}) };
}

/**
 * @param {unknown} data
 * @param {Error | null} error
 * @param {Pick<Response, "ok" | "status" | "statusText"> | null} response
 * @returns {import("../index.js").AkbResult<unknown>}
 */
export function clientResult(data, error, response) {
  return {
    data,
    error: /** @type {import("../index.js").AkbError | null} */ (error),
    response,
    throwOnError() {
      if (error) throw error;
      return /** @type {import("../index.js").AkbThrowingResult<unknown>} */ (this);
    },
  };
}

/**
 * @param {string} message
 * @param {string} code
 * @returns {Error}
 */
function clientResultError(message, code) {
  return new AkbError({ message, code });
}
