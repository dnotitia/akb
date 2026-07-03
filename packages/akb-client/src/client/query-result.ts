import { AkbError } from "../errors.js";
import type { AkbResult, AkbThrowingResult } from "../index.js";

export function applyResultMode(
  result: AkbResult<unknown>,
  mode: "rows" | "single" | "maybeSingle",
): AkbResult<unknown> {
  if (mode === "rows") return result;
  if (result.error) return result;
  const tableQuery = tableQueryData(result.data);
  if (!tableQuery) {
    return clientResult(
      null,
      clientResultError("single() expects a table_query result.", "invalid_single_result"),
      result.response,
    );
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
    clientResultError(
      `${label} expected ${mode === "single" ? "exactly one row" : "zero or one rows"} but received ${total}.`,
      "invalid_single_result",
    ),
    result.response,
  );
}

function tableQueryData(data: unknown): { items: unknown[]; total?: number } | null {
  if (!data || typeof data !== "object") return null;
  const items = (data as { items?: unknown }).items;
  if (!Array.isArray(items)) return null;
  const total = (data as { total?: unknown }).total;
  return { items, ...(typeof total === "number" ? { total } : {}) };
}

export function clientResult(
  data: unknown,
  error: Error | null,
  response: Pick<Response, "ok" | "status" | "statusText"> | null,
): AkbResult<unknown> {
  return {
    data,
    error: error as AkbError | null,
    response,
    throwOnError() {
      if (error) throw error;
      return this as AkbThrowingResult<unknown>;
    },
  };
}

function clientResultError(message: string, code: string): Error {
  return new AkbError({ message, code });
}
