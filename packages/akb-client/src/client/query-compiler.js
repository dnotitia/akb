import { baseOperator } from "./query-values.js";
import {
  mutationMethod,
  mutationPreferHeader,
  serializeAst,
  serializeMutationAst,
  serializeMutationUrl,
  serializeUrl,
} from "./query-serialize.js";

/** @typedef {import("./query-types.js").QueryState} QueryState */
/** @typedef {import("./query-types.js").QueryNode} QueryNode */
/** @typedef {import("./query-types.js").MutationState} MutationState */

/**
 * @param {QueryState} state
 * @param {number} maxUrlBytes
 * @param {string} rowsUrlPrefix
 * @returns {{ method: "GET", query: string, headers: Headers } | { method: "POST", body: object, headers: Headers }}
 */
export function compileQuery(state, maxUrlBytes, rowsUrlPrefix) {
  const headers = new Headers();
  if (state.count) headers.set("prefer", `count=${state.count}`);
  if (state.range) headers.set("range", `${state.range.from}-${state.range.to}`);
  const query = serializeUrl(state);
  const canUseUrl = byteLength(`${rowsUrlPrefix}${query}`) <= maxUrlBytes
    && !containsNestedGroup(state.nodes)
    && !containsUrlUnsafeArrayValue(state.nodes)
    && !containsUrlUnsafeBooleanValue(state.nodes);
  if (canUseUrl) {
    return { method: "GET", query, headers };
  }
  return { method: "POST", body: serializeAst(state), headers };
}

/**
 * @param {QueryState} state
 * @param {MutationState} mutation
 * @param {number} maxUrlBytes
 * @param {string} rowsPath
 * @param {string} rowsUrlPrefix
 * @returns {{ method: "POST" | "PATCH" | "DELETE", path: string, headers: Headers, body?: object | unknown }}
 */
export function compileMutation(state, mutation, maxUrlBytes, rowsPath, rowsUrlPrefix) {
  const headers = new Headers();
  headers.set("prefer", mutationPreferHeader(state, mutation));
  const query = serializeMutationUrl(state, mutation);
  const canUseRows = byteLength(`${rowsUrlPrefix}${query}`) <= maxUrlBytes
    && !containsNestedGroup(state.nodes)
    && !containsUrlUnsafeArrayValue(state.nodes)
    && !containsUrlUnsafeBooleanValue(state.nodes);

  if (canUseRows) {
    return {
      method: mutationMethod(mutation),
      path: `${rowsPath}${query}`,
      headers,
      ...(mutation.body === undefined ? {} : { body: mutation.body }),
    };
  }

  return {
    method: "POST",
    path: `${rowsPath.slice(0, -"/rows".length)}/query`,
    headers,
    body: serializeMutationAst(state, mutation),
  };
}

/**
 * @param {QueryState} state
 * @returns {string[]}
 */
export function unsupportedMutationModifiers(state) {
  /** @type {string[]} */
  const modifiers = [];
  if (state.order.length > 0) modifiers.push("order");
  if (state.limit !== null) modifiers.push("limit");
  if (state.range) modifiers.push("range");
  if (state.count) modifiers.push("count");
  return modifiers;
}

/**
 * @param {QueryNode[]} nodes
 * @returns {boolean}
 */
function containsNestedGroup(nodes) {
  return nodes.some((node) => {
    if (node.type !== "group" || !node.nodes) return false;
    return node.nodes.some((child) => child.type === "group") || containsNestedGroup(node.nodes);
  });
}

/**
 * @param {QueryNode[]} nodes
 * @returns {boolean}
 */
function containsUrlUnsafeArrayValue(nodes) {
  return nodes.some((node) => {
    if (node.type === "group") return node.nodes ? containsUrlUnsafeArrayValue(node.nodes) : false;
    return Array.isArray(node.value) && arrayNeedsAstFallback(node.operator, node.value);
  });
}

/**
 * @param {QueryNode[]} nodes
 * @param {boolean} [insideGroup]
 * @returns {boolean}
 */
function containsUrlUnsafeBooleanValue(nodes, insideGroup = false) {
  return nodes.some((node) => {
    if (node.type === "group") {
      return node.nodes ? containsUrlUnsafeBooleanValue(node.nodes, true) : false;
    }
    return insideGroup && scalarNeedsBooleanAstFallback(node.value);
  });
}

/**
 * @param {string} operator
 * @param {readonly unknown[]} value
 * @returns {boolean}
 */
function arrayNeedsAstFallback(operator, value) {
  const op = baseOperator(operator);
  if (op !== "in" && op !== "cs") return false;
  if (op === "cs" && value.some((item) => typeof item !== "string")) return true;
  return value.some((item) => typeof item === "string" && /[(),{}]/.test(item));
}

/**
 * @param {unknown} value
 * @returns {boolean}
 */
function scalarNeedsBooleanAstFallback(value) {
  return typeof value === "string" && /[(),{}]/.test(value);
}

/**
 * @param {string} value
 * @returns {number}
 */
function byteLength(value) {
  return new TextEncoder().encode(value).byteLength;
}
