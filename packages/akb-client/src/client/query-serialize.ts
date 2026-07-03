import { baseOperator, formatAstValue, formatFilterValue } from "./query-values.js";
import type { MutationState, OrderNode, QueryNode, QueryState } from "./query-types.js";

const JSON_PATH_COLUMN_RE = /^([a-z][a-z0-9_]*)(?:(->>|#>>)([^:]+))?(?:::([a-z]+))?$/;

export function serializeUrl(state: QueryState): string {
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

export function serializeMutationUrl(state: QueryState, mutation: MutationState): string {
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

function appendNodeParam(params: URLSearchParams, node: QueryNode): void {
  if (node.type === "filter") {
    params.append(node.column, `${node.operator}.${formatFilterValue(node.operator, node.value)}`);
    return;
  }
  params.append(node.op, `(${node.expression ?? node.nodes?.map(serializeGroupNode).join(",") ?? ""})`);
}

function serializeGroupNode(node: QueryNode): string {
  if (node.type === "filter") {
    return `${node.column}.${node.operator}.${formatFilterValue(node.operator, node.value)}`;
  }
  return `${node.op}(${node.expression ?? node.nodes?.map(serializeGroupNode).join(",") ?? ""})`;
}

function serializeOrder(order: OrderNode): string {
  return `${order.column}.${order.ascending ? "asc" : "desc"}`;
}

export function mutationMethod(mutation: MutationState): "POST" | "PATCH" | "DELETE" {
  if (mutation.type === "update") return "PATCH";
  if (mutation.type === "delete") return "DELETE";
  return "POST";
}

export function mutationPreferHeader(state: QueryState, mutation: MutationState): string {
  const parts = [state.select ? "return=representation" : "return=minimal"];
  if (mutation.type === "upsert" && mutation.ignoreDuplicates) {
    parts.unshift("resolution=ignore-duplicates");
  }
  return parts.join(", ");
}

export function serializeAst(state: QueryState): object {
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

export function serializeMutationAst(state: QueryState, mutation: MutationState): object {
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

function serializeAstFilterRoot(nodes: QueryNode[]): object | null {
  if (nodes.length === 0) return null;
  const astNodes = nodes.map(serializeAstNode);
  return astNodes.length === 1 ? astNodes[0] : { and: astNodes };
}

function serializeAstNode(node: QueryNode): object {
  if (node.type === "filter") {
    return serializeAstCondition(node.column, node.operator, node.value);
  }
  return {
    [node.op]: node.expression ? parseGroupExpression(node.expression) : node.nodes?.map(serializeAstNode) ?? [],
  };
}

function serializeAstOrder(order: OrderNode): object {
  return {
    ...serializeAstOperand(order.column),
    dir: order.ascending ? "asc" : "desc",
  };
}

function serializeAstCondition(column: string, operator: string, value: unknown): object {
  return {
    ...serializeAstOperand(column),
    op: operator,
    val: formatAstValue(operator, value),
  };
}

function serializeAstOperand(
  column: string,
): { col: string } | { jsonb: { col: string; path: string[]; cast?: string } } {
  const match = JSON_PATH_COLUMN_RE.exec(column.trim());
  if (!match || !match[2]) return { col: column };
  const [, base, arrow, rawPath, cast] = match;
  const path = arrow === "#>>" ? parseJsonPathList(rawPath) : [rawPath];
  return { jsonb: { col: base, path, ...(cast ? { cast } : {}) } };
}

function parseGroupExpression(expression: string): object[] {
  return splitTopLevel(expression).map((part) => parseGroupPart(part.trim())).filter(Boolean);
}

function parseGroupPart(part: string): object {
  const nested = parseNestedGroup(part);
  if (nested) return { [nested.op]: parseGroupExpression(nested.expression) };
  const split = splitBoolCondition(part);
  if (!split) throw new TypeError(`Invalid boolean filter expression: ${part}`);
  return serializeAstCondition(split.column, split.operator, parseRawFilterValue(split.operator, split.value));
}

function parseNestedGroup(part: string): { op: "and" | "or"; expression: string } | null {
  for (const op of ["and", "or"] as const) {
    const prefix = `${op}(`;
    if (part.startsWith(prefix) && part.endsWith(")")) {
      return { op, expression: part.slice(prefix.length, -1) };
    }
  }
  return null;
}

function splitBoolCondition(part: string): { column: string; operator: string; value: string } | null {
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

function splitOperatorValue(value: string): { operator: string; value: string } | null {
  const dot = value.indexOf(".");
  if (dot === -1) return null;
  return { operator: value.slice(0, dot), value: value.slice(dot + 1) };
}

function splitTopLevel(value: string): string[] {
  const parts: string[] = [];
  let buffer: string[] = [];
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

function parseJsonPathList(raw: string): string[] {
  const text = raw.trim();
  if (!text.startsWith("{") || !text.endsWith("}")) return [text];
  return text.slice(1, -1).split(",").map((part) => part.trim()).filter(Boolean);
}

function parseRawFilterValue(operator: string, value: string): unknown {
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
