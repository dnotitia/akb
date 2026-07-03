import { baseOperator } from "./query-values.js";
import {
  mutationMethod,
  mutationPreferHeader,
  serializeAst,
  serializeMutationAst,
  serializeMutationUrl,
  serializeUrl,
} from "./query-serialize.js";
import type { MutationState, QueryNode, QueryState } from "./query-types.js";

export function compileQuery(
  state: QueryState,
  maxUrlBytes: number,
  rowsUrlPrefix: string,
):
  | { method: "GET"; query: string; headers: Headers }
  | { method: "POST"; body: object; headers: Headers } {
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

export function compileMutation(
  state: QueryState,
  mutation: MutationState,
  maxUrlBytes: number,
  rowsPath: string,
  rowsUrlPrefix: string,
): { method: "POST" | "PATCH" | "DELETE"; path: string; headers: Headers; body?: unknown } {
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

export function unsupportedMutationModifiers(state: QueryState): string[] {
  const modifiers: string[] = [];
  if (state.order.length > 0) modifiers.push("order");
  if (state.limit !== null) modifiers.push("limit");
  if (state.range) modifiers.push("range");
  if (state.count) modifiers.push("count");
  return modifiers;
}

function containsNestedGroup(nodes: QueryNode[]): boolean {
  return nodes.some((node) => {
    if (node.type !== "group" || !node.nodes) return false;
    return node.nodes.some((child) => child.type === "group") || containsNestedGroup(node.nodes);
  });
}

function containsUrlUnsafeArrayValue(nodes: QueryNode[]): boolean {
  return nodes.some((node) => {
    if (node.type === "group") return node.nodes ? containsUrlUnsafeArrayValue(node.nodes) : false;
    return Array.isArray(node.value) && arrayNeedsAstFallback(node.operator, node.value);
  });
}

function containsUrlUnsafeBooleanValue(nodes: QueryNode[], insideGroup = false): boolean {
  return nodes.some((node) => {
    if (node.type === "group") {
      return node.nodes ? containsUrlUnsafeBooleanValue(node.nodes, true) : false;
    }
    return insideGroup && scalarNeedsBooleanAstFallback(node.value);
  });
}

function arrayNeedsAstFallback(operator: string, value: readonly unknown[]): boolean {
  const op = baseOperator(operator);
  if (op !== "in" && op !== "cs") return false;
  if (op === "cs" && value.some((item) => typeof item !== "string")) return true;
  return value.some((item) => typeof item === "string" && /[(),{}]/.test(item));
}

function scalarNeedsBooleanAstFallback(value: unknown): boolean {
  return typeof value === "string" && /[(),{}]/.test(value);
}

function byteLength(value: string): number {
  return new TextEncoder().encode(value).byteLength;
}
