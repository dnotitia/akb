import { baseOperator, formatAstValue, formatFilterValue } from "./query-values.js";

/** @typedef {import("./query-types.js").QueryState} QueryState */
/** @typedef {import("./query-types.js").QueryNode} QueryNode */
/** @typedef {import("./query-types.js").MutationState} MutationState */
/** @typedef {import("./query-types.js").OrderNode} OrderNode */

const JSON_PATH_COLUMN_RE = /^([a-z][a-z0-9_]*)(?:(->>|#>>)([^:]+))?(?:::([a-z]+))?$/;

/**
 * @param {QueryState} state
 * @returns {string}
 */
export function serializeUrl(state) {
  const params = new URLSearchParams();
  if (state.select) params.set("select", state.select);
  for (const node of state.nodes) appendNodeParam(params, node);
  if (state.order.length > 0) {
    params.set("order", state.order.map(serializeOrder).join(","));
  }
  if (state.limit !== null) params.set("limit", String(state.limit));
  if (state.offset !== null) params.set("offset", String(state.offset));
  const suffix = params.toString();
  return suffix ? `?${suffix}` : "";
}

/**
 * @param {QueryState} state
 * @param {MutationState} mutation
 * @returns {string}
 */
export function serializeMutationUrl(state, mutation) {
  const params = new URLSearchParams();
  if (state.select) params.set("select", state.select);
  if (mutation.type === "upsert") params.set("on_conflict", mutation.onConflict || "id");
  if ((mutation.type === "update" || mutation.type === "delete") && state.all) {
    params.set("all", "true");
  }
  if (mutation.type === "update" || mutation.type === "delete") {
    for (const node of state.nodes) appendNodeParam(params, node);
  }
  const suffix = params.toString();
  return suffix ? `?${suffix}` : "";
}

/**
 * @param {URLSearchParams} params
 * @param {QueryNode} node
 * @returns {void}
 */
function appendNodeParam(params, node) {
  if (node.type === "filter") {
    params.append(node.column, `${node.operator}.${formatFilterValue(node.operator, node.value)}`);
    return;
  }
  params.append(node.op, `(${node.expression ?? node.nodes?.map(serializeGroupNode).join(",") ?? ""})`);
}

/**
 * @param {QueryNode} node
 * @returns {string}
 */
function serializeGroupNode(node) {
  if (node.type === "filter") {
    return `${node.column}.${node.operator}.${formatFilterValue(node.operator, node.value)}`;
  }
  return `${node.op}(${node.expression ?? node.nodes?.map(serializeGroupNode).join(",") ?? ""})`;
}

/**
 * @param {OrderNode} order
 * @returns {string}
 */
function serializeOrder(order) {
  return `${order.column}.${order.ascending ? "asc" : "desc"}`;
}

/**
 * @param {MutationState} mutation
 * @returns {"POST" | "PATCH" | "DELETE"}
 */
export function mutationMethod(mutation) {
  if (mutation.type === "update") return "PATCH";
  if (mutation.type === "delete") return "DELETE";
  return "POST";
}

/**
 * @param {QueryState} state
 * @param {MutationState} mutation
 * @returns {string}
 */
export function mutationPreferHeader(state, mutation) {
  const parts = [state.select ? "return=representation" : "return=minimal"];
  if (mutation.type === "upsert" && mutation.ignoreDuplicates) {
    parts.unshift("resolution=ignore-duplicates");
  }
  return parts.join(", ");
}

/**
 * @param {QueryState} state
 * @returns {object}
 */
export function serializeAst(state) {
  const filter = serializeAstFilterRoot(state.nodes);
  return {
    ...(state.select ? { select: state.select } : {}),
    ...(filter ? { filter } : {}),
    ...(state.order.length > 0 ? { order: state.order.map(serializeAstOrder) } : {}),
    ...(state.limit !== null ? { limit: state.limit } : {}),
    ...(state.offset !== null ? { offset: state.offset } : {}),
    ...(state.count ? { count: state.count } : {}),
  };
}

/**
 * @param {QueryState} state
 * @param {MutationState} mutation
 * @returns {object}
 */
export function serializeMutationAst(state, mutation) {
  const filter = serializeAstFilterRoot(state.nodes);
  return {
    ...(mutation.type === "insert" ? { insert: mutation.body } : {}),
    ...(mutation.type === "upsert" ? { insert: mutation.body, on_conflict: mutation.onConflict || "id" } : {}),
    ...(mutation.type === "update" ? { update: mutation.body } : {}),
    ...(mutation.type === "delete" ? { delete: true } : {}),
    ...(filter ? { filter } : {}),
    ...(state.all ? { all: true } : {}),
    ...(state.select ? { returning: state.select } : {}),
    ...(mutation.type === "upsert" && mutation.ignoreDuplicates ? { resolution: "ignore-duplicates" } : {}),
  };
}

/**
 * @param {QueryNode[]} nodes
 * @returns {object | null}
 */
function serializeAstFilterRoot(nodes) {
  if (nodes.length === 0) return null;
  const astNodes = nodes.map(serializeAstNode);
  return astNodes.length === 1 ? astNodes[0] : { and: astNodes };
}

/**
 * @param {QueryNode} node
 * @returns {object}
 */
function serializeAstNode(node) {
  if (node.type === "filter") {
    return serializeAstCondition(node.column, node.operator, node.value);
  }
  return {
    [node.op]: node.expression ? parseGroupExpression(node.expression) : node.nodes?.map(serializeAstNode) ?? [],
  };
}

/**
 * @param {OrderNode} order
 * @returns {object}
 */
function serializeAstOrder(order) {
  return {
    ...serializeAstOperand(order.column),
    dir: order.ascending ? "asc" : "desc",
  };
}

/**
 * @param {string} column
 * @param {string} operator
 * @param {unknown} value
 * @returns {object}
 */
function serializeAstCondition(column, operator, value) {
  return {
    ...serializeAstOperand(column),
    op: operator,
    val: formatAstValue(operator, value),
  };
}

/**
 * @param {string} column
 * @returns {{ col: string } | { jsonb: { col: string, path: string[], cast?: string } }}
 */
function serializeAstOperand(column) {
  const match = JSON_PATH_COLUMN_RE.exec(column.trim());
  if (!match || !match[2]) return { col: column };
  const [, base, arrow, rawPath, cast] = match;
  const path = arrow === "#>>" ? parseJsonPathList(rawPath) : [rawPath];
  return { jsonb: { col: base, path, ...(cast ? { cast } : {}) } };
}

/**
 * @param {string} expression
 * @returns {object[]}
 */
function parseGroupExpression(expression) {
  return splitTopLevel(expression).map((part) => parseGroupPart(part.trim())).filter(Boolean);
}

/**
 * @param {string} part
 * @returns {object}
 */
function parseGroupPart(part) {
  const nested = parseNestedGroup(part);
  if (nested) return { [nested.op]: parseGroupExpression(nested.expression) };
  const split = splitBoolCondition(part);
  if (!split) throw new TypeError(`Invalid boolean filter expression: ${part}`);
  return serializeAstCondition(split.column, split.operator, parseRawFilterValue(split.operator, split.value));
}

/**
 * @param {string} part
 * @returns {{ op: "and" | "or", expression: string } | null}
 */
function parseNestedGroup(part) {
  for (const op of /** @type {const} */ (["and", "or"])) {
    const prefix = `${op}(`;
    if (part.startsWith(prefix) && part.endsWith(")")) {
      return { op, expression: part.slice(prefix.length, -1) };
    }
  }
  return null;
}

/**
 * @param {string} part
 * @returns {{ column: string, operator: string, value: string } | null}
 */
function splitBoolCondition(part) {
  const first = part.indexOf(".");
  if (first === -1) return null;
  const column = part.slice(0, first);
  const opAndValue = part.slice(first + 1);
  const second = opAndValue.indexOf(".");
  if (second === -1) return null;
  const operator = opAndValue.slice(0, second);
  const value = opAndValue.slice(second + 1);
  if (operator === "not") {
    const nested = splitOperatorValue(value);
    if (!nested) return null;
    return { column, operator: `not.${nested.operator}`, value: nested.value };
  }
  return { column, operator, value };
}

/**
 * @param {string} value
 * @returns {{ operator: string, value: string } | null}
 */
function splitOperatorValue(value) {
  const dot = value.indexOf(".");
  if (dot === -1) return null;
  return { operator: value.slice(0, dot), value: value.slice(dot + 1) };
}

/**
 * @param {string} value
 * @returns {string[]}
 */
function splitTopLevel(value) {
  /** @type {string[]} */
  const parts = [];
  /** @type {string[]} */
  let buffer = [];
  let depth = 0;
  let braceDepth = 0;
  for (const ch of value) {
    if (ch === "(") depth += 1;
    else if (ch === ")") depth = Math.max(0, depth - 1);
    else if (ch === "{") braceDepth += 1;
    else if (ch === "}") braceDepth = Math.max(0, braceDepth - 1);
    if (ch === "," && depth === 0 && braceDepth === 0) {
      parts.push(buffer.join(""));
      buffer = [];
    } else {
      buffer.push(ch);
    }
  }
  parts.push(buffer.join(""));
  return parts;
}

/**
 * @param {string} raw
 * @returns {string[]}
 */
function parseJsonPathList(raw) {
  const text = raw.trim();
  if (!text.startsWith("{") || !text.endsWith("}")) return [text];
  return text.slice(1, -1).split(",").map((part) => part.trim()).filter(Boolean);
}

/**
 * @param {string} operator
 * @param {string} value
 * @returns {unknown}
 */
function parseRawFilterValue(operator, value) {
  const op = baseOperator(operator);
  if (op === "is") {
    const lowered = value.toLowerCase();
    if (lowered === "null") return null;
    if (lowered === "true") return true;
    if (lowered === "false") return false;
  }
  if (op === "in") {
    const text = value.trim();
    if (text.startsWith("(") && text.endsWith(")")) {
      return splitTopLevel(text.slice(1, -1)).map((part) => part.trim());
    }
  }
  if (op === "cs") {
    const text = value.trim();
    if (text.startsWith("{") && text.endsWith("}") && !text.includes(":")) {
      return text.slice(1, -1).split(",").map((part) => part.trim()).filter(Boolean);
    }
    try {
      return JSON.parse(text);
    } catch {
      return text;
    }
  }
  return formatAstValue(operator, value);
}
