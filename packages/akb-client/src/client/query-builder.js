import { AkbError } from "../errors.js";
import { compileMutation, compileQuery, unsupportedMutationModifiers } from "./query-compiler.js";
import { applyResultMode, clientResult } from "./query-result.js";

/** @typedef {import("./query-types.js").QueryBuilderOptions} QueryBuilderOptions */
/** @typedef {import("./query-types.js").QueryState} QueryState */
/** @typedef {import("./query-types.js").QueryNode} QueryNode */

const MAX_BOOLEAN_DEPTH = 3;

/**
 * @param {QueryBuilderOptions} options
 * @returns {import("../index.js").AkbTableStub}
 */
export function createQueryBuilder(options) {
  return /** @type {import("../index.js").AkbTableStub} */ (new QueryBuilder(options, emptyState()));
}

/**
 * Named-operator shortcuts shared by {@link QueryBuilder} and {@link GroupBuilder}.
 *
 * Each shortcut delegates to `filter()`, which the two subclasses implement with
 * different semantics (immutable clone vs. mutable push). The base class stays
 * abstract: `filter()` must be overridden.
 */
class FilterBuilder {
  /**
   * @param {string} column
   * @param {string} operator
   * @param {unknown} value
   * @returns {this}
   */
  filter(column, operator, value) {
    throw new Error("filter() must be implemented by a subclass.");
  }

  /** @param {string} column @param {unknown} value @returns {this} */
  eq(column, value) { return this.filter(column, "eq", value); }
  /** @param {string} column @param {unknown} value @returns {this} */
  neq(column, value) { return this.filter(column, "neq", value); }
  /** @param {string} column @param {unknown} value @returns {this} */
  gt(column, value) { return this.filter(column, "gt", value); }
  /** @param {string} column @param {unknown} value @returns {this} */
  gte(column, value) { return this.filter(column, "gte", value); }
  /** @param {string} column @param {unknown} value @returns {this} */
  lt(column, value) { return this.filter(column, "lt", value); }
  /** @param {string} column @param {unknown} value @returns {this} */
  lte(column, value) { return this.filter(column, "lte", value); }
  /** @param {string} column @param {unknown} value @returns {this} */
  like(column, value) { return this.filter(column, "like", value); }
  /** @param {string} column @param {unknown} value @returns {this} */
  ilike(column, value) { return this.filter(column, "ilike", value); }
  /** @param {string} column @param {unknown} value @returns {this} */
  is(column, value) { return this.filter(column, "is", value); }
  /** @param {string} column @param {readonly unknown[]} value @returns {this} */
  in(column, value) { return this.filter(column, "in", Array.from(value)); }
  /** @param {string} column @param {unknown} value @returns {this} */
  cs(column, value) { return this.filter(column, "cs", value); }

  /** @param {string} column @param {string} operator @param {unknown} value @returns {this} */
  not(column, operator, value) {
    return this.filter(column, `not.${operator}`, value);
  }
}

class QueryBuilder extends FilterBuilder {
  /** @type {Promise<import("../index.js").AkbResult<unknown>> | null} */
  #promise = null;

  /**
   * @param {QueryBuilderOptions} options
   * @param {QueryState} state
   */
  constructor(options, state) {
    super();
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
   * @returns {this}
   */
  filter(column, operator, value) {
    return /** @type {this} */ (this.#appendNode(filterNode(column, operator, value)));
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
   * @param {unknown} values
   * @returns {QueryBuilder}
   */
  insert(values) {
    return this.#clone({ mutation: { type: "insert", body: values } });
  }

  /**
   * @param {unknown} patch
   * @returns {QueryBuilder}
   */
  update(patch) {
    return this.#clone({ mutation: { type: "update", body: patch } });
  }

  /**
   * @param {unknown} values
   * @param {{ onConflict?: string, ignoreDuplicates?: boolean }} [options]
   * @returns {QueryBuilder}
   */
  upsert(values, options = {}) {
    return this.#clone({
      mutation: {
        type: "upsert",
        body: values,
        onConflict: options.onConflict ?? "id",
        ignoreDuplicates: options.ignoreDuplicates === true,
      },
    });
  }

  /**
   * @returns {QueryBuilder}
   */
  delete() {
    return this.#clone({ mutation: { type: "delete" } });
  }

  /**
   * @returns {QueryBuilder}
   */
  all() {
    return this.#clone({ all: true });
  }

  /**
   * @returns {QueryBuilder}
   */
  single() {
    return this.#clone({ resultMode: "single" });
  }

  /**
   * @returns {QueryBuilder}
   */
  maybeSingle() {
    return this.#clone({ resultMode: "maybeSingle" });
  }

  /**
   * @template TResult1
   * @template TResult2
   * @param {((value: import("../index.js").AkbResult<unknown>) => TResult1 | PromiseLike<TResult1>) | null} [onfulfilled]
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
   * @returns {Promise<import("../index.js").AkbResult<unknown>>}
   */
  async #execute() {
    const result = this.#state.mutation
      ? await this.#executeMutation()
      : await this.#executeRead();
    return applyResultMode(result, this.#state.resultMode);
  }

  /**
   * @returns {Promise<import("../index.js").AkbResult<import("../core/schema.gen.js").AkbTableQueryEnvelope>>}
   */
  async #executeRead() {
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
   * @returns {Promise<import("../index.js").AkbResult<unknown>>}
   */
  async #executeMutation() {
    if (!this.#state.mutation) {
      throw new TypeError("Mutation execution requires a write verb.");
    }
    const unsupportedModifiers = unsupportedMutationModifiers(this.#state);
    if (unsupportedModifiers.length > 0) {
      return clientResult(
        null,
        new AkbError({
          message: `Write builders do not support ${unsupportedModifiers.join(", ")} modifiers.`,
          code: "unsupported_write_modifier",
          details: { modifiers: unsupportedModifiers },
        }),
        null,
      );
    }
    const rowsPath = this.#rowsPath();
    const compiled = compileMutation(
      this.#state,
      this.#state.mutation,
      this.#options.maxUrlBytes,
      rowsPath,
      `${this.#options.baseUrl}${rowsPath}`,
    );
    return await this.#options.request(compiled.path, {
      method: compiled.method,
      headers: compiled.headers,
      ...(compiled.body === undefined ? {} : { body: JSON.stringify(compiled.body) }),
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

class GroupBuilder extends FilterBuilder {
  /**
   * @param {number} depth
   */
  constructor(depth = 1) {
    super();
    this.depth = depth;
    /** @type {QueryNode[]} */
    this.nodes = [];
  }

  /** @param {string} column @param {string} operator @param {unknown} value @returns {this} */
  filter(column, operator, value) {
    this.nodes.push(filterNode(column, operator, value));
    return this;
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
    mutation: null,
    all: false,
    resultMode: "rows",
  };
}

/**
 * @param {string} column
 * @param {string} operator
 * @param {unknown} value
 * @returns {import("./query-types.js").FilterNode}
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
 * @returns {import("./query-types.js").GroupNode}
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
 * @param {string} prefix
 * @param {string | URL} path
 * @returns {string}
 */
function joinPath(prefix, path) {
  const suffix = String(path);
  if (!suffix) return prefix;
  return `${prefix}${suffix.startsWith("/") ? suffix : `/${suffix}`}`;
}
