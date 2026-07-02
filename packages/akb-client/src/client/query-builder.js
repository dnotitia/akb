const NAMED_OPERATORS = new Set([
  "cs",
  "eq",
  "gt",
  "gte",
  "ilike",
  "in",
  "is",
  "like",
  "lt",
  "lte",
  "neq",
]);

const MAX_BOOLEAN_DEPTH = 3;
const JSON_PATH_COLUMN_RE = /^([a-z][a-z0-9_]*)(?:(->>|#>>)([^:]+))?(?:::([a-z]+))?$/;

/**
 * @typedef {{
 *   baseUrl: string,
 *   table: string,
 *   vault: string | null,
 *   request: import("../index.js").AkbClient["request"],
 *   maxUrlBytes: number,
 * }} QueryBuilderOptions
 *
 * @typedef {{ type: "filter", column: string, operator: string, value: unknown }} FilterNode
 * @typedef {{ type: "group", op: "and" | "or", expression?: string, nodes?: QueryNode[] }} GroupNode
 * @typedef {FilterNode | GroupNode} QueryNode
 * @typedef {{ column: string, ascending: boolean }} OrderNode
 * @typedef {{
 *   select: string | null,
 *   nodes: QueryNode[],
 *   order: OrderNode[],
 *   limit: number | null,
 *   offset: number | null,
 *   range: { from: number, to: number } | null,
 *   count: "exact" | "planned" | "estimated" | null,
 * }} QueryState
 */

/**
 * @param {QueryBuilderOptions} options
 * @returns {import("../index.js").AkbTableStub}
 */
export function createQueryBuilder(options) {
  return new QueryBuilder(options, emptyState());
}

class QueryBuilder {
  /** @type {Promise<import("../index.js").AkbResult<import("../core/schema.gen.js").AkbTableQueryEnvelope>> | null} */
  #promise = null;

  /**
   * @param {QueryBuilderOptions} options
   * @param {QueryState} state
   */
  constructor(options, state) {
    this.table = options.table;
    this.vault = options.vault;
    this.#options = options;
    this.#state = state;
  }

  /** @type {QueryBuilderOptions} */
  #options;

  /** @type {QueryState} */
  #state;

  /**
   * @template T
   * @param {string | URL} [path]
   * @param {RequestInit} [init]
   * @returns {Promise<import("../index.js").AkbResult<T>>}
   */
  request(path = "", init = {}) {
    const prefix = this.#tablePath();
    return /** @type {Promise<import("../index.js").AkbResult<T>>} */ (
      this.#options.request(joinPath(prefix, path), init)
    );
  }

  /**
   * @param {string} [columns]
   * @returns {QueryBuilder}
   */
  select(columns = "*") {
    return this.#clone({ select: columns });
  }

  /**
   * @param {string} column
   * @param {string} operator
   * @param {unknown} value
   * @returns {QueryBuilder}
   */
  filter(column, operator, value) {
    return this.#appendNode(filterNode(column, operator, value));
  }

  /** @param {string} column @param {unknown} value @returns {QueryBuilder} */
  eq(column, value) { return this.filter(column, "eq", value); }
  /** @param {string} column @param {unknown} value @returns {QueryBuilder} */
  neq(column, value) { return this.filter(column, "neq", value); }
  /** @param {string} column @param {unknown} value @returns {QueryBuilder} */
  gt(column, value) { return this.filter(column, "gt", value); }
  /** @param {string} column @param {unknown} value @returns {QueryBuilder} */
  gte(column, value) { return this.filter(column, "gte", value); }
  /** @param {string} column @param {unknown} value @returns {QueryBuilder} */
  lt(column, value) { return this.filter(column, "lt", value); }
  /** @param {string} column @param {unknown} value @returns {QueryBuilder} */
  lte(column, value) { return this.filter(column, "lte", value); }
  /** @param {string} column @param {unknown} value @returns {QueryBuilder} */
  like(column, value) { return this.filter(column, "like", value); }
  /** @param {string} column @param {unknown} value @returns {QueryBuilder} */
  ilike(column, value) { return this.filter(column, "ilike", value); }
  /** @param {string} column @param {unknown} value @returns {QueryBuilder} */
  is(column, value) { return this.filter(column, "is", value); }
  /** @param {string} column @param {readonly unknown[]} value @returns {QueryBuilder} */
  in(column, value) { return this.filter(column, "in", Array.from(value)); }
  /** @param {string} column @param {unknown} value @returns {QueryBuilder} */
  cs(column, value) { return this.filter(column, "cs", value); }

  /**
   * @param {string} column
   * @param {string} operator
   * @param {unknown} value
   * @returns {QueryBuilder}
   */
  not(column, operator, value) {
    return this.filter(column, `not.${operator}`, value);
  }

  /**
   * @param {string | import("../index.js").AkbFilterGroupCallback} group
   * @returns {QueryBuilder}
   */
  or(group) {
    return this.#appendNode(groupNode("or", group));
  }

  /**
   * @param {string | import("../index.js").AkbFilterGroupCallback} group
   * @returns {QueryBuilder}
   */
  and(group) {
    return this.#appendNode(groupNode("and", group));
  }

  /**
   * @param {string} column
   * @param {{ ascending?: boolean }} [options]
   * @returns {QueryBuilder}
   */
  order(column, options = {}) {
    return this.#clone({
      order: [
        ...this.#state.order,
        {
          column,
          ascending: options.ascending !== false,
        },
      ],
    });
  }

  /**
   * @param {number} count
   * @returns {QueryBuilder}
   */
  limit(count) {
    assertNonNegativeInteger(count, "limit");
    return this.#clone({ limit: count, offset: null, range: null });
  }

  /**
   * @param {number} from
   * @param {number} to
   * @returns {QueryBuilder}
   */
  range(from, to) {
    assertNonNegativeInteger(from, "range from");
    assertNonNegativeInteger(to, "range to");
    if (to < from) throw new RangeError("range to must be greater than or equal to range from.");
    return this.#clone({ limit: null, offset: null, range: { from, to } });
  }

  /**
   * @param {"exact" | "planned" | "estimated"} [mode]
   * @returns {QueryBuilder}
   */
  count(mode = "exact") {
    return this.#clone({ count: mode });
  }

  /**
   * @template TResult1
   * @template TResult2
   * @param {((value: import("../index.js").AkbResult<import("../core/schema.gen.js").AkbTableQueryEnvelope>) => TResult1 | PromiseLike<TResult1>) | null} [onfulfilled]
   * @param {((reason: unknown) => TResult2 | PromiseLike<TResult2>) | null} [onrejected]
   * @returns {Promise<TResult1 | TResult2>}
   */
  then(onfulfilled, onrejected) {
    if (!this.#promise) this.#promise = this.#execute();
    return this.#promise.then(onfulfilled, onrejected);
  }

  /**
   * @param {Partial<QueryState>} patch
   * @returns {QueryBuilder}
   */
  #clone(patch) {
    return new QueryBuilder(this.#options, { ...this.#state, ...patch });
  }

  /**
   * @param {QueryNode} node
   * @returns {QueryBuilder}
   */
  #appendNode(node) {
    return this.#clone({ nodes: [...this.#state.nodes, node] });
  }

  /**
   * @returns {Promise<import("../index.js").AkbResult<import("../core/schema.gen.js").AkbTableQueryEnvelope>>}
   */
  async #execute() {
    const rowsPath = this.#rowsPath();
    const compiled = compileQuery(
      this.#state,
      this.#options.maxUrlBytes,
      `${this.#options.baseUrl}${rowsPath}`,
    );
    if (compiled.method === "GET") {
      return await this.#options.request(`${rowsPath}${compiled.query}`, {
        method: "GET",
        headers: compiled.headers,
      });
    }
    return await this.#options.request(`${this.#tablePath()}/query`, {
      method: "POST",
      headers: compiled.headers,
      body: JSON.stringify(compiled.body),
    });
  }

  /**
   * @returns {string}
   */
  #rowsPath() {
    return `${this.#tablePath()}/rows`;
  }

  /**
   * @returns {string}
   */
  #tablePath() {
    if (!this.#options.vault) {
      throw new TypeError("Select a vault before using table helpers: client.vault(\"...\").from(\"...\").");
    }
    return `/tables/${encodeURIComponent(this.#options.vault)}/${encodeURIComponent(this.#options.table)}`;
  }
}

class GroupBuilder {
  /**
   * @param {number} depth
   */
  constructor(depth = 1) {
    this.depth = depth;
    /** @type {QueryNode[]} */
    this.nodes = [];
  }

  /** @param {string} column @param {string} operator @param {unknown} value @returns {GroupBuilder} */
  filter(column, operator, value) {
    this.nodes.push(filterNode(column, operator, value));
    return this;
  }
  /** @param {string} column @param {unknown} value @returns {GroupBuilder} */
  eq(column, value) { return this.filter(column, "eq", value); }
  /** @param {string} column @param {unknown} value @returns {GroupBuilder} */
  neq(column, value) { return this.filter(column, "neq", value); }
  /** @param {string} column @param {unknown} value @returns {GroupBuilder} */
  gt(column, value) { return this.filter(column, "gt", value); }
  /** @param {string} column @param {unknown} value @returns {GroupBuilder} */
  gte(column, value) { return this.filter(column, "gte", value); }
  /** @param {string} column @param {unknown} value @returns {GroupBuilder} */
  lt(column, value) { return this.filter(column, "lt", value); }
  /** @param {string} column @param {unknown} value @returns {GroupBuilder} */
  lte(column, value) { return this.filter(column, "lte", value); }
  /** @param {string} column @param {unknown} value @returns {GroupBuilder} */
  like(column, value) { return this.filter(column, "like", value); }
  /** @param {string} column @param {unknown} value @returns {GroupBuilder} */
  ilike(column, value) { return this.filter(column, "ilike", value); }
  /** @param {string} column @param {unknown} value @returns {GroupBuilder} */
  is(column, value) { return this.filter(column, "is", value); }
  /** @param {string} column @param {readonly unknown[]} value @returns {GroupBuilder} */
  in(column, value) { return this.filter(column, "in", Array.from(value)); }
  /** @param {string} column @param {unknown} value @returns {GroupBuilder} */
  cs(column, value) { return this.filter(column, "cs", value); }

  /** @param {string} column @param {string} operator @param {unknown} value @returns {GroupBuilder} */
  not(column, operator, value) {
    return this.filter(column, `not.${operator}`, value);
  }

  /** @param {string | import("../index.js").AkbFilterGroupCallback} group @returns {GroupBuilder} */
  or(group) {
    this.nodes.push(groupNode("or", group, this.depth + 1));
    return this;
  }

  /** @param {string | import("../index.js").AkbFilterGroupCallback} group @returns {GroupBuilder} */
  and(group) {
    this.nodes.push(groupNode("and", group, this.depth + 1));
    return this;
  }
}

/**
 * @returns {QueryState}
 */
function emptyState() {
  return {
    select: null,
    nodes: [],
    order: [],
    limit: null,
    offset: null,
    range: null,
    count: null,
  };
}

/**
 * @param {string} column
 * @param {string} operator
 * @param {unknown} value
 * @returns {FilterNode}
 */
function filterNode(column, operator, value) {
  if (!column) throw new TypeError("filter column is required.");
  if (!operator) throw new TypeError("filter operator is required.");
  return { type: "filter", column, operator, value };
}

/**
 * @param {"and" | "or"} op
 * @param {string | import("../index.js").AkbFilterGroupCallback} group
 * @param {number} [depth]
 * @returns {GroupNode}
 */
function groupNode(op, group, depth = 1) {
  if (typeof group === "string") return { type: "group", op, expression: group };
  if (depth > MAX_BOOLEAN_DEPTH) {
    throw new RangeError(`Boolean filter depth cannot exceed ${MAX_BOOLEAN_DEPTH}.`);
  }
  const builder = new GroupBuilder(depth);
  const returned = group(builder);
  const resolved = returned instanceof GroupBuilder ? returned : builder;
  return { type: "group", op, nodes: resolved.nodes };
}

/**
 * @param {QueryState} state
 * @param {number} maxUrlBytes
 * @param {string} rowsUrlPrefix
 * @returns {{ method: "GET", query: string, headers: Headers } | { method: "POST", body: object, headers: Headers }}
 */
function compileQuery(state, maxUrlBytes, rowsUrlPrefix) {
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
 * @returns {string}
 */
function serializeUrl(state) {
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
 * @param {QueryState} state
 * @returns {object}
 */
function serializeAst(state) {
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
 * @param {unknown} value
 * @returns {string}
 */
function formatFilterValue(operator, value) {
  const op = baseOperator(operator);
  if (Array.isArray(value)) {
    const inner = value.map(formatScalar).join(",");
    return op === "cs" ? `{${inner}}` : `(${inner})`;
  }
  if (value && typeof value === "object") return JSON.stringify(value);
  return formatScalar(value);
}

/**
 * @param {string} operator
 * @param {unknown} value
 * @returns {unknown}
 */
function formatAstValue(operator, value) {
  const op = baseOperator(operator);
  if ((op === "like" || op === "ilike") && typeof value === "string") {
    return value.replaceAll("*", "%");
  }
  return value;
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

/**
 * @param {string} operator
 * @returns {string}
 */
function baseOperator(operator) {
  return operator.startsWith("not.") ? operator.slice(4) : operator;
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
 * @param {unknown} value
 * @returns {string}
 */
function formatScalar(value) {
  if (value === null) return "null";
  if (typeof value === "boolean" || typeof value === "number") return String(value);
  return String(value);
}

/**
 * @param {number} value
 * @param {string} label
 * @returns {void}
 */
function assertNonNegativeInteger(value, label) {
  if (!Number.isInteger(value) || value < 0) {
    throw new RangeError(`${label} must be a non-negative integer.`);
  }
}

/**
 * @param {string} value
 * @returns {number}
 */
function byteLength(value) {
  return new TextEncoder().encode(value).byteLength;
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
