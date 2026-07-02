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
  fetch?: typeof fetch;
}

export interface AkbClientOptions extends Omit<AkbClientConfig, "baseUrl"> {}

export type AkbTableMap<Schema> = Schema extends { public: { Tables: infer Tables } }
  ? Tables
  : Record<string, { Row: unknown; Insert: unknown; Update: unknown }>;

export type AkbTableNames<Schema> = string & keyof AkbTableMap<Schema>;

export type AkbTableRow<Schema, TableName extends string> =
  AkbTableMap<Schema> extends Record<TableName, { Row: infer Row }> ? Row : unknown;

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

export interface AkbTableStub<Row = unknown> {
  readonly table: string;
  readonly vault: string | null;
  request<T = AkbSuccessEnvelope>(path?: string | URL, init?: RequestInit): Promise<AkbResult<T>>;
  readonly __row?: Row;
}

export interface AkbClient<Schema = unknown> {
  readonly __schema?: Schema;
  request<T = AkbSuccessEnvelope>(path: string | URL, init?: RequestInit): Promise<AkbResult<T>>;
  vault(vault: string): AkbClient<Schema>;
  actingAs(claims: AkbClaims): AkbClient<Schema>;
  from<TableName extends AkbTableNames<Schema>>(table: TableName): AkbTableStub<AkbTableRow<Schema, TableName>>;
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
