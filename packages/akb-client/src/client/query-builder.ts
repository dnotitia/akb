import { AkbError } from "../errors.js";
import { compileMutation, compileQuery, unsupportedMutationModifiers } from "./query-compiler.js";
import { applyResultMode, clientResult } from "./query-result.js";
import type { FilterNode, GroupNode, QueryBuilderOptions, QueryNode, QueryState } from "./query-types.js";
import type { AkbFilterGroup, AkbFilterGroupCallback, AkbResult, AkbTableStub } from "../index.js";

const MAX_BOOLEAN_DEPTH = 3;

export function createQueryBuilder(options: QueryBuilderOptions): AkbTableStub {
  return new QueryBuilder(options, emptyState()) as unknown as AkbTableStub;
}

/**
 * Named-operator shortcuts shared by {@link QueryBuilder} and {@link GroupBuilder}.
 *
 * Each shortcut delegates to `filter()`, which the two subclasses implement with
 * different semantics (immutable clone vs. mutable push). The base class stays
 * abstract: `filter()` must be overridden.
 */
class FilterBuilder {
  filter(column: string, operator: string, value: unknown): this {
    throw new Error("filter() must be implemented by a subclass.");
  }

  eq(column: string, value: unknown): this { return this.filter(column, "eq", value); }
  neq(column: string, value: unknown): this { return this.filter(column, "neq", value); }
  gt(column: string, value: unknown): this { return this.filter(column, "gt", value); }
  gte(column: string, value: unknown): this { return this.filter(column, "gte", value); }
  lt(column: string, value: unknown): this { return this.filter(column, "lt", value); }
  lte(column: string, value: unknown): this { return this.filter(column, "lte", value); }
  like(column: string, value: unknown): this { return this.filter(column, "like", value); }
  ilike(column: string, value: unknown): this { return this.filter(column, "ilike", value); }
  is(column: string, value: unknown): this { return this.filter(column, "is", value); }
  in(column: string, value: readonly unknown[]): this { return this.filter(column, "in", Array.from(value)); }
  cs(column: string, value: unknown): this { return this.filter(column, "cs", value); }

  not(column: string, operator: string, value: unknown): this {
    return this.filter(column, `not.${operator}`, value);
  }
}

class QueryBuilder extends FilterBuilder {
  table: string;
  vault: string | null;

  #promise: Promise<AkbResult<unknown>> | null = null;
  #options: QueryBuilderOptions;
  #state: QueryState;

  constructor(options: QueryBuilderOptions, state: QueryState) {
    super();
    this.table = options.table;
    this.vault = options.vault;
    this.#options = options;
    this.#state = state;
  }

  request<T = unknown>(path: string | URL = "", init: RequestInit = {}): Promise<AkbResult<T>> {
    const prefix = this.#tablePath();
    return this.#options.request(joinPath(prefix, path), init) as Promise<AkbResult<T>>;
  }

  select(columns = "*"): QueryBuilder {
    return this.#clone({ select: columns });
  }

  filter(column: string, operator: string, value: unknown): this {
    return this.#appendNode(filterNode(column, operator, value)) as this;
  }

  or(group: string | AkbFilterGroupCallback): QueryBuilder {
    return this.#appendNode(groupNode("or", group));
  }

  and(group: string | AkbFilterGroupCallback): QueryBuilder {
    return this.#appendNode(groupNode("and", group));
  }

  order(column: string, options: { ascending?: boolean } = {}): QueryBuilder {
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

  limit(count: number): QueryBuilder {
    assertNonNegativeInteger(count, "limit");
    return this.#clone({ limit: count, offset: null, range: null });
  }

  range(from: number, to: number): QueryBuilder {
    assertNonNegativeInteger(from, "range from");
    assertNonNegativeInteger(to, "range to");
    if (to < from) throw new RangeError("range to must be greater than or equal to range from.");
    return this.#clone({ limit: null, offset: null, range: { from, to } });
  }

  count(mode: "exact" | "planned" | "estimated" = "exact"): QueryBuilder {
    return this.#clone({ count: mode });
  }

  insert(values: unknown): QueryBuilder {
    return this.#clone({ mutation: { type: "insert", body: values } });
  }

  update(patch: unknown): QueryBuilder {
    return this.#clone({ mutation: { type: "update", body: patch } });
  }

  upsert(values: unknown, options: { onConflict?: string; ignoreDuplicates?: boolean } = {}): QueryBuilder {
    return this.#clone({
      mutation: {
        type: "upsert",
        body: values,
        onConflict: options.onConflict ?? "id",
        ignoreDuplicates: options.ignoreDuplicates === true,
      },
    });
  }

  delete(): QueryBuilder {
    return this.#clone({ mutation: { type: "delete" } });
  }

  all(): QueryBuilder {
    return this.#clone({ all: true });
  }

  single(): QueryBuilder {
    return this.#clone({ resultMode: "single" });
  }

  maybeSingle(): QueryBuilder {
    return this.#clone({ resultMode: "maybeSingle" });
  }

  then<TResult1 = AkbResult<unknown>, TResult2 = never>(
    onfulfilled?: ((value: AkbResult<unknown>) => TResult1 | PromiseLike<TResult1>) | null,
    onrejected?: ((reason: unknown) => TResult2 | PromiseLike<TResult2>) | null,
  ): Promise<TResult1 | TResult2> {
    if (!this.#promise) this.#promise = this.#execute();
    return this.#promise.then(onfulfilled, onrejected);
  }

  #clone(patch: Partial<QueryState>): QueryBuilder {
    return new QueryBuilder(this.#options, { ...this.#state, ...patch });
  }

  #appendNode(node: QueryNode): QueryBuilder {
    return this.#clone({ nodes: [...this.#state.nodes, node] });
  }

  async #execute(): Promise<AkbResult<unknown>> {
    const result = this.#state.mutation
      ? await this.#executeMutation()
      : await this.#executeRead();
    return applyResultMode(result, this.#state.resultMode);
  }

  async #executeRead(): Promise<AkbResult<unknown>> {
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

  async #executeMutation(): Promise<AkbResult<unknown>> {
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

  #rowsPath(): string {
    return `${this.#tablePath()}/rows`;
  }

  #tablePath(): string {
    if (!this.#options.vault) {
      throw new TypeError("Select a vault before using table helpers: client.vault(\"...\").from(\"...\").");
    }
    return `/tables/${encodeURIComponent(this.#options.vault)}/${encodeURIComponent(this.#options.table)}`;
  }
}

class GroupBuilder extends FilterBuilder {
  depth: number;
  nodes: QueryNode[];

  constructor(depth = 1) {
    super();
    this.depth = depth;
    this.nodes = [];
  }

  filter(column: string, operator: string, value: unknown): this {
    this.nodes.push(filterNode(column, operator, value));
    return this;
  }

  or(group: string | AkbFilterGroupCallback): GroupBuilder {
    this.nodes.push(groupNode("or", group, this.depth + 1));
    return this;
  }

  and(group: string | AkbFilterGroupCallback): GroupBuilder {
    this.nodes.push(groupNode("and", group, this.depth + 1));
    return this;
  }
}

function emptyState(): QueryState {
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

function filterNode(column: string, operator: string, value: unknown): FilterNode {
  if (!column) throw new TypeError("filter column is required.");
  if (!operator) throw new TypeError("filter operator is required.");
  return { type: "filter", column, operator, value };
}

function groupNode(op: "and" | "or", group: string | AkbFilterGroupCallback, depth = 1): GroupNode {
  if (typeof group === "string") return { type: "group", op, expression: group };
  if (depth > MAX_BOOLEAN_DEPTH) {
    throw new RangeError(`Boolean filter depth cannot exceed ${MAX_BOOLEAN_DEPTH}.`);
  }
  const builder = new GroupBuilder(depth);
  const returned = group(builder as unknown as AkbFilterGroup);
  const resolved = returned instanceof GroupBuilder ? returned : builder;
  return { type: "group", op, nodes: resolved.nodes };
}

function assertNonNegativeInteger(value: number, label: string): void {
  if (!Number.isInteger(value) || value < 0) {
    throw new RangeError(`${label} must be a non-negative integer.`);
  }
}

function joinPath(prefix: string, path: string | URL): string {
  const suffix = String(path);
  if (!suffix) return prefix;
  return `${prefix}${suffix.startsWith("/") ? suffix : `/${suffix}`}`;
}
