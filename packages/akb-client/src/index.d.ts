export type AkbJsonValue =
  | string
  | number
  | boolean
  | null
  | AkbJsonValue[]
  | { [key: string]: AkbJsonValue };

export type {
  AkbOperation,
  AkbOperationResponse,
  AkbPath,
  AkbPathMethod,
  AkbTypedFetch,
  AkbTypedFetchInit,
} from "./core/fetch.js";
export { createTypedFetch } from "./core/fetch.js";
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

export class AkbError extends Error {
  code: string;
  details: unknown;
  hint: string | null;
  status: number;
  payload: Record<string, unknown>;
  response: Pick<Response, "ok" | "status" | "statusText"> | null;
  constructor(payload: unknown, response?: Pick<Response, "ok" | "status" | "statusText"> | null);
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
  [Result] extends [import("./core/schema.gen.js").AkbTableQueryEnvelope<Row>] ? Row : Result;

export type AkbMaybeSingleResult<Row, Result> =
  [Result] extends [import("./core/schema.gen.js").AkbTableQueryEnvelope<Row>] ? Row | null : Result;

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
  select(columns?: string): AkbTableStub<Row, import("./core/schema.gen.js").AkbTableQueryEnvelope<Row>, Insert, Update>;
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

export function unwrapAkbResponse<T = unknown>(
  response: Pick<Response, "ok" | "status" | "statusText"> | null,
  body: T | AkbErrorPayload | unknown,
): AkbResult<T>;

export function akbFetch<T = unknown>(
  input: RequestInfo | URL,
  init?: RequestInit,
  fetchImpl?: typeof fetch,
): Promise<AkbResult<T>>;

export function createClient<Schema = unknown>(baseUrl: string | URL, options?: AkbClientOptions): AkbClient<Schema>;
export function createClient<Schema = unknown>(config: AkbClientConfig): AkbClient<Schema>;
