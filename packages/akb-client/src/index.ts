import { AkbError } from "./errors.js";
import { createQueryBuilder } from "./client/query-builder.js";

export { AkbError } from "./errors.js";
export { createTypedFetch } from "./core/fetch.js";

export type {
  AkbOperation,
  AkbOperationResponse,
  AkbPath,
  AkbPathMethod,
  AkbTypedFetch,
  AkbTypedFetchInit,
} from "./core/fetch.js";
export type {
  AkbFileEnvelope,
  AkbSqlEnvelope,
  AkbTableEnvelope,
  AkbTableMigrationEnvelope,
  AkbTableQueryEnvelope,
  AkbTableSchemaEnvelope,
  AkbTableSqlEnvelope,
  AkbVaultTableSchemaEnvelope,
  components,
  operations,
  paths,
} from "./core/schema.gen.js";

export type AkbJsonValue =
  | string
  | number
  | boolean
  | null
  | AkbJsonValue[]
  | { [key: string]: AkbJsonValue };

export interface AkbSuccessEnvelope {
  kind: string;
  [key: string]: AkbJsonValue | undefined;
}

export interface AkbErrorPayload {
  message?: string;
  error?: string;
  detail?: unknown;
  code?: string;
  details?: unknown;
  hint?: string | null;
  password_required?: boolean;
  slug?: string;
  [key: string]: unknown;
}

export interface AkbResult<T, E extends AkbError = AkbError> {
  data: T | null;
  error: E | null;
  response: Pick<Response, "ok" | "status" | "statusText"> | null;
  throwOnError(): AkbThrowingResult<T>;
}

export interface AkbThrowingResult<T> extends AkbResult<T, never> {
  data: T;
  error: null;
}

export interface AkbClientConfig {
  baseUrl: string;
  apiKey?: string | null | (() => string | null | undefined);
  token?: string | null | (() => string | null | undefined);
  defaultVault?: string | null;
  maxUrlBytes?: number;
  fetch?: typeof fetch;
}

export interface AkbClientOptions extends Omit<AkbClientConfig, "baseUrl"> {}

export type AkbTableMap<Schema> = Schema extends { public: { Tables: infer Tables } }
  ? Tables
  : Record<string, { Row: unknown; Insert: unknown; Update: unknown }>;

export type AkbTableNames<Schema> = string & keyof AkbTableMap<Schema>;

export type AkbTableRow<Schema, TableName extends string> =
  AkbTableMap<Schema> extends Record<TableName, { Row: infer Row }> ? Row : unknown;

export type AkbTableInsert<Schema, TableName extends string> =
  AkbTableMap<Schema> extends Record<TableName, { Insert: infer Insert }> ? Insert : AkbTableRow<Schema, TableName>;

export type AkbTableUpdate<Schema, TableName extends string> =
  AkbTableMap<Schema> extends Record<TableName, { Update: infer Update }> ? Update : Partial<AkbTableRow<Schema, TableName>>;

export type AkbSingleResult<Row, Result> =
  [Result] extends [import("./core/schema.gen.js").AkbTableQueryEnvelope<infer Selected>] ? Selected : Result;

export type AkbMaybeSingleResult<Row, Result> =
  [Result] extends [import("./core/schema.gen.js").AkbTableQueryEnvelope<infer Selected>] ? Selected | null : Result;

export type AkbTrim<S extends string> =
  S extends ` ${infer R}` ? AkbTrim<R> :
  S extends `${infer R} ` ? AkbTrim<R> :
  S;

export type AkbPopDepth<T extends unknown[]> = T extends [unknown, ...infer Rest] ? Rest : [];

export type AkbSplitSelect<
  S extends string,
  Current extends string = "",
  Parts extends string[] = [],
  ParenDepth extends unknown[] = [],
  BraceDepth extends unknown[] = [],
> =
  S extends `${infer Ch}${infer Rest}`
    ? Ch extends ","
      ? ParenDepth extends []
        ? BraceDepth extends []
          ? AkbSplitSelect<Rest, "", [...Parts, AkbTrim<Current>], ParenDepth, BraceDepth>
          : AkbSplitSelect<Rest, `${Current}${Ch}`, Parts, ParenDepth, BraceDepth>
        : AkbSplitSelect<Rest, `${Current}${Ch}`, Parts, ParenDepth, BraceDepth>
      : Ch extends "("
        ? AkbSplitSelect<Rest, `${Current}${Ch}`, Parts, [unknown, ...ParenDepth], BraceDepth>
        : Ch extends ")"
          ? AkbSplitSelect<Rest, `${Current}${Ch}`, Parts, AkbPopDepth<ParenDepth>, BraceDepth>
          : Ch extends "{"
            ? AkbSplitSelect<Rest, `${Current}${Ch}`, Parts, ParenDepth, [unknown, ...BraceDepth]>
            : Ch extends "}"
              ? AkbSplitSelect<Rest, `${Current}${Ch}`, Parts, ParenDepth, AkbPopDepth<BraceDepth>>
              : AkbSplitSelect<Rest, `${Current}${Ch}`, Parts, ParenDepth, BraceDepth>
    : [...Parts, AkbTrim<Current>];

export type AkbSelectTokenIsWide<Token extends string> =
  Token extends `${string}(${string}` ? true :
  Token extends `${string}:${string}` ? Token extends `${string}::${string}` ? false : true :
  false;

export type AkbJsonPathBase<Token extends string> =
  Token extends `${infer Base}->>${string}` ? Base :
  Token extends `${infer Base}#>>${string}` ? Base :
  never;

export type AkbSelectTokenIsJsonPath<Row, Token extends string> =
  [AkbJsonPathBase<Token>] extends [never] ? false :
  AkbJsonPathBase<Token> extends keyof Row ? true : false;

export type AkbInvalidSelectToken<Row, Token extends string> =
  Token extends "" | "*" ? never :
  AkbSelectTokenIsWide<Token> extends true ? never :
  AkbSelectTokenIsJsonPath<Row, Token> extends true ? never :
  Token extends keyof Row ? never :
  Token;

export type AkbInvalidSelectTokens<Row, Columns extends string> =
  AkbSplitSelect<Columns>[number] extends infer Token
    ? Token extends string ? AkbInvalidSelectToken<Row, Token> : never
    : never;

export type AkbSelectStringIsWide<Columns extends string> =
  true extends AkbSelectHasWideToken<Columns> ? true : false;

export type AkbSelectStringIsSingleJsonPath<Row, Columns extends string> =
  AkbSelectTokenIsJsonPath<Row, AkbTrim<Columns>> extends true ? true : false;

export type AkbSelectString<Row, Columns extends string> =
  [keyof Row] extends [never] ? Columns :
  string extends Columns ? Columns :
  AkbInvalidSelectTokens<Row, Columns> extends never ? Columns : never;

export type AkbSelectHasWideToken<Columns extends string> =
  AkbSplitSelect<Columns>[number] extends infer Token
    ? Token extends string ? AkbSelectTokenIsWide<Token> : never
    : never;

export type AkbPlainSelectKeys<Row, Columns extends string> =
  Extract<AkbSplitSelect<Columns>[number], keyof Row>;

export type AkbSelectHasStar<Columns extends string> =
  "*" extends AkbSplitSelect<Columns>[number] ? true : false;

export type AkbJsonSelectShape<Row, Columns extends string> = {
  [Token in AkbSplitSelect<Columns>[number] as
    Token extends string
      ? AkbSelectTokenIsJsonPath<Row, Token> extends true ? Token : never
      : never
  ]: string | null;
};

export type AkbJsonSelectTokenShape<Row, Token extends string> = {
  [Key in AkbTrim<Token> as AkbSelectTokenIsJsonPath<Row, Key> extends true ? Key : never]: string | null;
};

export type AkbSelectResult<Row, Columns extends string> =
  [keyof Row] extends [never] ? Row :
  string extends Columns ? Row :
  AkbTrim<Columns> extends "" | "*" ? Row :
  AkbSelectStringIsWide<Columns> extends true ? Row :
  AkbSelectHasStar<Columns> extends true ? Row & AkbJsonSelectShape<Row, Columns> :
  true extends AkbSelectHasWideToken<Columns> ? Row :
  Pick<Row, AkbPlainSelectKeys<Row, Columns>> & AkbJsonSelectShape<Row, Columns>;

export type AkbVaultSqlResult<Row = Record<string, unknown>> =
  | import("./core/schema.gen.js").AkbTableQueryEnvelope<Row>
  | import("./core/schema.gen.js").AkbTableSqlEnvelope;

export interface AkbSqlTag {
  <Row = Record<string, unknown>>(
    strings: TemplateStringsArray,
    ...values: unknown[]
  ): Promise<AkbResult<AkbVaultSqlResult<Row>>>;
}

export interface AkbClaims {
  sub: string;
  app_metadata: {
    org_id: string;
    role: string;
    [key: string]: AkbJsonValue;
  };
  [key: string]: AkbJsonValue | Record<string, AkbJsonValue>;
}

export interface AkbClientScope {
  defaultVault: string | null;
  claims: AkbClaims | null;
}

export interface AkbNamespaceStub {
  readonly name: string;
  request<T = AkbSuccessEnvelope>(path?: string | URL, init?: RequestInit): Promise<AkbResult<T>>;
}

export interface AkbTableStub<
  Row = unknown,
  Result = import("./core/schema.gen.js").AkbTableQueryEnvelope<Row>,
  Insert = Row,
  Update = Partial<Row>,
> {
  readonly table: string;
  readonly vault: string | null;
  request<T = AkbSuccessEnvelope>(path?: string | URL, init?: RequestInit): Promise<AkbResult<T>>;
  select<const Columns extends string = "*">(
    columns?: Columns & AkbSelectString<Row, Columns>,
  ): AkbTableStub<Row, import("./core/schema.gen.js").AkbTableQueryEnvelope<AkbSelectResult<Row, Columns>>, Insert, Update>;
  filter(column: string, operator: string, value: unknown): AkbTableStub<Row, Result, Insert, Update>;
  eq(column: string, value: unknown): AkbTableStub<Row, Result, Insert, Update>;
  neq(column: string, value: unknown): AkbTableStub<Row, Result, Insert, Update>;
  gt(column: string, value: unknown): AkbTableStub<Row, Result, Insert, Update>;
  gte(column: string, value: unknown): AkbTableStub<Row, Result, Insert, Update>;
  lt(column: string, value: unknown): AkbTableStub<Row, Result, Insert, Update>;
  lte(column: string, value: unknown): AkbTableStub<Row, Result, Insert, Update>;
  like(column: string, value: unknown): AkbTableStub<Row, Result, Insert, Update>;
  ilike(column: string, value: unknown): AkbTableStub<Row, Result, Insert, Update>;
  is(column: string, value: unknown): AkbTableStub<Row, Result, Insert, Update>;
  in(column: string, value: readonly unknown[]): AkbTableStub<Row, Result, Insert, Update>;
  cs(column: string, value: unknown): AkbTableStub<Row, Result, Insert, Update>;
  not(column: string, operator: string, value: unknown): AkbTableStub<Row, Result, Insert, Update>;
  or(group: string | AkbFilterGroupCallback): AkbTableStub<Row, Result, Insert, Update>;
  and(group: string | AkbFilterGroupCallback): AkbTableStub<Row, Result, Insert, Update>;
  order(column: string, options?: AkbOrderOptions): AkbTableStub<Row, Result, Insert, Update>;
  limit(count: number): AkbTableStub<Row, Result, Insert, Update>;
  range(from: number, to: number): AkbTableStub<Row, Result, Insert, Update>;
  count(mode?: "exact" | "planned" | "estimated"): AkbTableStub<Row, Result, Insert, Update>;
  insert(values: Insert | readonly Insert[]): AkbTableStub<Row, import("./core/schema.gen.js").AkbTableQueryEnvelope<Row> | null, Insert, Update>;
  update(patch: Update): AkbTableStub<Row, import("./core/schema.gen.js").AkbTableQueryEnvelope<Row> | null, Insert, Update>;
  upsert(values: Insert | readonly Insert[], options?: AkbUpsertOptions): AkbTableStub<Row, import("./core/schema.gen.js").AkbTableQueryEnvelope<Row> | null, Insert, Update>;
  delete(): AkbTableStub<Row, import("./core/schema.gen.js").AkbTableQueryEnvelope<Row> | null, Insert, Update>;
  all(): AkbTableStub<Row, Result, Insert, Update>;
  single(): AkbTableStub<Row, AkbSingleResult<Row, Result>, Insert, Update>;
  maybeSingle(): AkbTableStub<Row, AkbMaybeSingleResult<Row, Result>, Insert, Update>;
  then<TResult1 = AkbResult<Result>, TResult2 = never>(
    onfulfilled?: ((value: AkbResult<Result>) => TResult1 | PromiseLike<TResult1>) | null,
    onrejected?: ((reason: unknown) => TResult2 | PromiseLike<TResult2>) | null,
  ): Promise<TResult1 | TResult2>;
  readonly __row?: Row;
}

export interface AkbUpsertOptions {
  onConflict?: string;
  ignoreDuplicates?: boolean;
}

export interface AkbOrderOptions {
  ascending?: boolean;
}

export interface AkbFilterGroup {
  filter(column: string, operator: string, value: unknown): AkbFilterGroup;
  eq(column: string, value: unknown): AkbFilterGroup;
  neq(column: string, value: unknown): AkbFilterGroup;
  gt(column: string, value: unknown): AkbFilterGroup;
  gte(column: string, value: unknown): AkbFilterGroup;
  lt(column: string, value: unknown): AkbFilterGroup;
  lte(column: string, value: unknown): AkbFilterGroup;
  like(column: string, value: unknown): AkbFilterGroup;
  ilike(column: string, value: unknown): AkbFilterGroup;
  is(column: string, value: unknown): AkbFilterGroup;
  in(column: string, value: readonly unknown[]): AkbFilterGroup;
  cs(column: string, value: unknown): AkbFilterGroup;
  not(column: string, operator: string, value: unknown): AkbFilterGroup;
  or(group: string | AkbFilterGroupCallback): AkbFilterGroup;
  and(group: string | AkbFilterGroupCallback): AkbFilterGroup;
}

export type AkbFilterGroupCallback = (group: AkbFilterGroup) => AkbFilterGroup | void;

export interface AkbClient<Schema = unknown> {
  readonly __schema?: Schema;
  request<T = AkbSuccessEnvelope>(path: string | URL, init?: RequestInit): Promise<AkbResult<T>>;
  vault(vault: string): AkbClient<Schema>;
  actingAs(claims: AkbClaims): AkbClient<Schema>;
  readonly sql: AkbSqlTag;
  from<TableName extends AkbTableNames<Schema>>(table: TableName): AkbTableStub<
    AkbTableRow<Schema, TableName>,
    import("./core/schema.gen.js").AkbTableQueryEnvelope<AkbTableRow<Schema, TableName>>,
    AkbTableInsert<Schema, TableName>,
    AkbTableUpdate<Schema, TableName>
  >;
  readonly search: AkbNamespaceStub;
  readonly graph: AkbNamespaceStub;
  readonly docs: AkbNamespaceStub;
  readonly storage: AkbNamespaceStub;
}

/**
 * Convert one parsed HTTP response body into a `{data,error}` result.
 */
export function unwrapAkbResponse<T = unknown>(
  response: Pick<Response, "ok" | "status" | "statusText"> | null,
  body: T | AkbErrorPayload | unknown,
): AkbResult<T> {
  if (response?.ok) {
    return makeResult<T>(body as T, null, response);
  }
  return makeResult<T>(null, new AkbError(body, asResponse(response)), response);
}

/**
 * Fetch an AKB REST endpoint and unwrap the HTTP boundary.
 */
export async function akbFetch<T = unknown>(
  input: RequestInfo | URL,
  init: RequestInit | undefined = undefined,
  fetchImpl: typeof fetch = globalThis.fetch,
): Promise<AkbResult<T>> {
  if (typeof fetchImpl !== "function") {
    throw new TypeError("A fetch implementation is required.");
  }
  const response = await fetchImpl(input, init);
  const body = await readBody(response);
  return unwrapAkbResponse<T>(response, body);
}

/**
 * Create a scoped AKB REST client. The namespace helpers are intentionally thin
 * request facades; fluent table/storage helpers can build on this contract.
 */
export function createClient<Schema = unknown>(baseUrl: string | URL, options?: AkbClientOptions): AkbClient<Schema>;
export function createClient<Schema = unknown>(config: AkbClientConfig): AkbClient<Schema>;
export function createClient<Schema = unknown>(
  configOrUrl: AkbClientConfig | string | URL,
  options: AkbClientOptions = {},
): AkbClient<Schema> {
  const config = normalizeClientConfig(configOrUrl, options);
  return makeClient(config, {
    defaultVault: config.defaultVault ?? null,
    claims: null,
  }) as AkbClient<Schema>;
}

function makeResult<T>(
  data: T | null,
  error: AkbError | null,
  response: Pick<Response, "ok" | "status" | "statusText"> | null,
): AkbResult<T> {
  return {
    data,
    error,
    response,
    throwOnError() {
      if (error) throw error;
      return this as AkbThrowingResult<T>;
    },
  };
}

async function readBody(response: Response): Promise<unknown> {
  const text = await response.text();
  if (!text) return null;
  const contentType = response.headers.get("content-type") ?? "";
  if (contentType.includes("application/json")) {
    return JSON.parse(text);
  }
  try {
    return JSON.parse(text);
  } catch {
    return text;
  }
}

function asResponse(
  response: Pick<Response, "ok" | "status" | "statusText"> | null | undefined,
): Pick<Response, "ok" | "status" | "statusText"> | null {
  return response ?? null;
}

function trimTrailingSlash(url: string): string {
  return url.endsWith("/") ? url.slice(0, -1) : url;
}

function resolveRequestUrl(baseUrl: string, path: string | URL): string {
  const value = String(path);
  if (/^https?:\/\//i.test(value)) {
    const baseOrigin = httpOrigin(baseUrl);
    const targetOrigin = httpOrigin(value);
    if (baseOrigin && targetOrigin === baseOrigin) return value;
    throw new TypeError("Refusing to send an AKB bearer token to a different origin.");
  }
  return `${baseUrl}${value.startsWith("/") ? value : `/${value}`}`;
}

function httpOrigin(value: string): string | null {
  try {
    return new URL(value).origin;
  } catch {
    return null;
  }
}

function normalizeClientConfig(
  configOrUrl: AkbClientConfig | string | URL,
  options: AkbClientOptions,
): Required<Pick<AkbClientConfig, "baseUrl">> & AkbClientOptions {
  if (typeof configOrUrl === "string" || configOrUrl instanceof URL) {
    return {
      ...options,
      baseUrl: trimTrailingSlash(String(configOrUrl)),
      token: options.token ?? options.apiKey ?? null,
    };
  }

  if (!configOrUrl || typeof configOrUrl !== "object") {
    throw new TypeError("createClient requires a base URL string or config object.");
  }

  return {
    ...configOrUrl,
    baseUrl: trimTrailingSlash(configOrUrl.baseUrl),
    token: configOrUrl.token ?? configOrUrl.apiKey ?? null,
  };
}

function makeClient(
  config: Required<Pick<AkbClientConfig, "baseUrl">> & AkbClientOptions,
  scope: AkbClientScope,
): AkbClient {
  const fetchImpl = config.fetch ?? globalThis.fetch;

  const request = (async (path: string | URL, init: RequestInit = {}): Promise<AkbResult<unknown>> => {
    const requestUrl = resolveRequestUrl(config.baseUrl, path);
    const headers = new Headers(init.headers);
    if (!headers.has("content-type") && init.body !== undefined) {
      headers.set("content-type", "application/json");
    }
    const token = resolveToken(config.token ?? config.apiKey ?? null);
    if (token && !headers.has("authorization")) {
      headers.set("authorization", `Bearer ${token}`);
    }
    if (scope.claims && !headers.has("x-akb-claims")) {
      headers.set("x-akb-claims", JSON.stringify(scope.claims));
    }
    return await akbFetch(requestUrl, { ...init, headers }, fetchImpl);
  }) as AkbClient["request"];

  const client = {
    request,
    vault(vault: string) {
      return makeClient(config, { ...scope, defaultVault: vault });
    },
    actingAs(claims: AkbClaims) {
      return makeClient(config, { ...scope, claims: normalizeClaims(claims) });
    },
    sql(strings: TemplateStringsArray, ...values: unknown[]) {
      if (!scope.defaultVault) {
        throw new TypeError("Select a vault before using raw SQL: client.vault(\"...\").sql`...`.");
      }
      const compiled = compileSqlTemplate(strings, values);
      return request(`/tables/${encodeURIComponent(scope.defaultVault)}/sql`, {
        method: "POST",
        body: JSON.stringify({ sql: compiled.text, params: compiled.params }),
      });
    },
    from(table: string) {
      return createQueryBuilder({
        baseUrl: config.baseUrl,
        table,
        vault: scope.defaultVault,
        request,
        maxUrlBytes: config.maxUrlBytes ?? 8192,
      });
    },
    search: makeNamespaceStub("search", "/search", request),
    graph: makeNamespaceStub("graph", "/graph", request),
    docs: makeNamespaceStub("docs", "/documents", request),
    storage: makeNamespaceStub("storage", "/files", request),
  };
  return Object.freeze(client) as unknown as AkbClient;
}

function resolveToken(
  token: string | null | undefined | (() => string | null | undefined),
): string | null {
  const value = typeof token === "function" ? token() : token;
  return typeof value === "string" && value.length > 0 ? value : null;
}

function normalizeClaims(claims: AkbClaims): AkbClaims {
  if (!claims || typeof claims !== "object" || Array.isArray(claims)) {
    throw new TypeError("actingAs requires a claims object.");
  }
  if (typeof claims.sub !== "string" || claims.sub.length === 0) {
    throw new TypeError("actingAs claims require a non-empty sub.");
  }
  if (!claims.app_metadata || typeof claims.app_metadata !== "object" || Array.isArray(claims.app_metadata)) {
    throw new TypeError("actingAs claims require an app_metadata object.");
  }
  if (typeof claims.app_metadata.org_id !== "string" || claims.app_metadata.org_id.length === 0) {
    throw new TypeError("actingAs claims require a non-empty app_metadata.org_id.");
  }
  if (typeof claims.app_metadata.role !== "string" || claims.app_metadata.role.length === 0) {
    throw new TypeError("actingAs claims require a non-empty app_metadata.role.");
  }
  return claims;
}

function compileSqlTemplate(
  strings: TemplateStringsArray,
  values: unknown[],
): { text: string; params: unknown[] } {
  if (!isTemplateStringsArray(strings)) {
    throw new TypeError("client.sql must be used as a tagged template.");
  }
  if (values.length !== strings.length - 1) {
    throw new TypeError("Tagged SQL template interpolation count is invalid.");
  }

  let text = "";
  for (let index = 0; index < strings.length; index += 1) {
    text += strings[index];
    if (index < values.length) {
      text += `$${index + 1}`;
    }
  }
  return { text, params: Array.from(values) };
}

function isTemplateStringsArray(value: unknown): value is TemplateStringsArray {
  if (!Array.isArray(value)) return false;
  const raw = (value as { raw?: unknown }).raw;
  return Array.isArray(raw)
    && Object.isFrozen(value)
    && Object.isFrozen(raw)
    && value.length === raw.length;
}

function makeNamespaceStub(
  name: string,
  prefix: string,
  request: AkbClient["request"],
): AkbNamespaceStub {
  return Object.freeze({
    name,
    request(path: string | URL = "", init: RequestInit = {}) {
      return request(joinPath(prefix, path), init);
    },
  }) as AkbNamespaceStub;
}

function joinPath(prefix: string, path: string | URL): string {
  const suffix = String(path);
  if (!suffix) return prefix;
  return `${prefix}${suffix.startsWith("/") ? suffix : `/${suffix}`}`;
}
