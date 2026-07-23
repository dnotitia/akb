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
  AkbDocumentEnvelope,
  AkbDocumentWriteEnvelope,
  AkbCollectionCreateEnvelope,
  AkbCollectionDeleteEnvelope,
  AkbCollectionSummary,
  AkbActivityEnvelope,
  AkbActivityEntry,
  AkbActivityFileChange,
  AkbDocumentDiffEnvelope,
  AkbFileEnvelope,
  AkbDocumentHistoryEnvelope,
  AkbDocumentHistoryEntry,
  AkbDrillDownEnvelope,
  AkbGrepEnvelope,
  AkbGraphEdge,
  AkbGraphEnvelope,
  AkbGraphHealthEnvelope,
  AkbGraphNeighborsEnvelope,
  AkbGraphNode,
  AkbGraphOverviewEnvelope,
  AkbProvenanceEnvelope,
  AkbRecentChangesEnvelope,
  AkbRecentDocumentChange,
  AkbRelation,
  AkbRelationLinkEnvelope,
  AkbRelationType,
  AkbRelationUnlinkEnvelope,
  AkbRelationsEnvelope,
  AkbWritableRelationType,
  AkbSearchEnvelope,
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

export type AkbSuccessEnvelope = import("./core/schema.gen.js").AkbSuccessEnvelope;

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

export interface AkbSearchOptions {
  mode?: "hybrid";
  rerank?: boolean;
  vault?: string | readonly string[];
  collection?: string | null;
  type?: string | null;
  tags?: readonly string[] | null;
  limit?: number;
  includeArchived?: boolean;
  sourceUris?: readonly string[] | null;
}

export interface AkbDrillDownOptions {
  section?: string | null;
}

export interface AkbGrepOptions {
  vault?: string | readonly string[];
  collection?: string | null;
  regex?: boolean;
  caseSensitive?: boolean;
  limit?: number;
  countOnly?: boolean;
  filesWithMatches?: boolean;
}

export type AkbDocumentStatus = "draft" | "active" | "archived";

export interface AkbDocumentVaultOptions {
  vault?: string | null;
}

export interface AkbCreateCollectionInput {
  path: string;
  summary?: string | null;
}

export interface AkbDeleteCollectionOptions {
  recursive?: boolean;
}

export interface AkbDocumentGetOptions extends AkbDocumentVaultOptions {
  version?: string | null;
}

export interface AkbDocumentHistoryOptions {
  vault?: string;
  limit?: number;
}

export interface AkbDocumentDiffOptions {
  vault?: string;
  commit: string;
}

export interface AkbActivityListOptions {
  vault?: string;
  collection?: string | null;
  author?: string | null;
  since?: string | null;
  limit?: number;
}

export interface AkbActivityRecentOptions {
  vault?: string;
  limit?: number;
}

export interface AkbDocumentBrowseOptions extends AkbDocumentVaultOptions {
  collection?: string | null;
  depth?: number;
  includeHashes?: boolean;
  includeArchived?: boolean;
}

export interface AkbDocumentPutInput extends AkbDocumentVaultOptions {
  collection: string;
  title: string;
  content: string;
  type?: string;
  status?: AkbDocumentStatus;
  tags?: readonly string[];
  domain?: string | null;
  summary?: string | null;
  dependsOn?: readonly string[];
  relatedTo?: readonly string[];
  slug?: string | null;
}

export interface AkbDocumentUpdateInput {
  content?: string | null;
  title?: string | null;
  type?: string | null;
  status?: AkbDocumentStatus | null;
  tags?: readonly string[] | null;
  domain?: string | null;
  summary?: string | null;
  dependsOn?: readonly string[] | null;
  relatedTo?: readonly string[] | null;
  message?: string | null;
  expectedCommit?: string | null;
  expectedContentHash?: string | null;
}

export interface AkbStorageVaultOptions {
  vault?: string | null;
}

export interface AkbStoragePresignUploadOptions extends AkbStorageVaultOptions {
  collection?: string | null;
  description?: string | null;
  mimeType?: string | null;
  /**
   * sha256 of the bytes being uploaded. Sent to `initiate_upload` as well as
   * `confirm`, which makes the upload idempotent: re-uploading the same bytes
   * to the same path resolves to the file that is already there rather than
   * creating a duplicate. Omit to keep the original one-file-per-call
   * behaviour.
   */
  contentHash?: string | null;
}

export interface AkbStorageUploadOptions extends AkbStoragePresignUploadOptions {
  confirm?: boolean;
  hashAlgorithm?: string | null;
  headers?: HeadersInit;
}

export interface AkbStorageConfirmOptions extends AkbStorageVaultOptions {
  contentHash?: string | null;
  hashAlgorithm?: string | null;
}

export interface AkbStorageListOptions extends AkbStorageVaultOptions {
  collection?: string | null;
  limit?: number;
}

export interface AkbStorageRefOptions extends AkbStorageVaultOptions {
  lookupLimit?: number;
}

export interface AkbStorageDownloadUrlOptions extends AkbStorageRefOptions {
  bytes?: false;
}

export interface AkbStorageDownloadBytesOptions extends AkbStorageRefOptions {
  bytes: true;
  fetchInit?: RequestInit;
}

export interface AkbStorageDownload {
  kind: "file_download";
  file: import("./core/schema.gen.js").AkbFileEnvelope;
  bytes: ArrayBuffer;
}

export interface AkbSearchFacade extends AkbNamespaceStub {
  (
    query: string,
    options?: AkbSearchOptions,
  ): Promise<AkbResult<import("./core/schema.gen.js").AkbSearchEnvelope>>;
  drillDown(
    uri: string,
    options?: AkbDrillDownOptions,
  ): Promise<AkbResult<import("./core/schema.gen.js").AkbDrillDownEnvelope>>;
  grep(
    pattern: string,
    options?: AkbGrepOptions,
  ): Promise<AkbResult<import("./core/schema.gen.js").AkbGrepEnvelope>>;
}

export interface AkbGraphNeighborsOptions {
  hops?: 1 | 2 | 3 | 4 | 5;
  limit?: number;
}

export interface AkbGraphOverviewOptions {
  topK?: number;
}

export interface AkbGraphHealthOptions {
  hubThreshold?: number;
  limit?: number;
}

export interface AkbGraphRelationsOptions {
  direction?: "incoming" | "outgoing" | "both";
  type?: import("./core/schema.gen.js").AkbRelationType;
}

export interface AkbGraphLinkInput {
  source: string;
  target: string;
  relation: import("./core/schema.gen.js").AkbWritableRelationType;
  metadata?: Record<string, AkbJsonValue>;
}

export interface AkbGraphUnlinkInput {
  source: string;
  target: string;
  relation?: import("./core/schema.gen.js").AkbWritableRelationType;
}

export interface AkbGraphFacade extends AkbNamespaceStub {
  neighbors(
    uri: string,
    options?: AkbGraphNeighborsOptions,
  ): Promise<AkbResult<import("./core/schema.gen.js").AkbGraphNeighborsEnvelope>>;
  overview(
    options?: AkbGraphOverviewOptions,
  ): Promise<AkbResult<import("./core/schema.gen.js").AkbGraphOverviewEnvelope>>;
  health(
    options?: AkbGraphHealthOptions,
  ): Promise<AkbResult<import("./core/schema.gen.js").AkbGraphHealthEnvelope>>;
  relations(
    uri: string,
    options?: AkbGraphRelationsOptions,
  ): Promise<AkbResult<import("./core/schema.gen.js").AkbRelationsEnvelope>>;
  link(
    input: AkbGraphLinkInput,
  ): Promise<AkbResult<import("./core/schema.gen.js").AkbRelationLinkEnvelope>>;
  unlink(
    input: AkbGraphUnlinkInput,
  ): Promise<AkbResult<import("./core/schema.gen.js").AkbRelationUnlinkEnvelope>>;
  provenance(
    uri: string,
  ): Promise<AkbResult<import("./core/schema.gen.js").AkbProvenanceEnvelope>>;
}

export interface AkbDocsFacade extends AkbNamespaceStub {
  createCollection(
    input: AkbCreateCollectionInput,
  ): Promise<AkbResult<import("./core/schema.gen.js").AkbCollectionCreateEnvelope>>;
  deleteCollection(
    path: string,
    options?: AkbDeleteCollectionOptions,
  ): Promise<AkbResult<import("./core/schema.gen.js").AkbCollectionDeleteEnvelope>>;
  get(
    docId: string,
    options?: AkbDocumentGetOptions,
  ): Promise<AkbResult<import("./core/schema.gen.js").AkbDocumentEnvelope>>;
  history(
    docId: string,
    options?: AkbDocumentHistoryOptions,
  ): Promise<AkbResult<import("./core/schema.gen.js").AkbDocumentHistoryEnvelope>>;
  diff(
    docId: string,
    options: AkbDocumentDiffOptions,
  ): Promise<AkbResult<import("./core/schema.gen.js").AkbDocumentDiffEnvelope>>;
  put(
    input: AkbDocumentPutInput,
  ): Promise<AkbResult<import("./core/schema.gen.js").AkbDocumentWriteEnvelope>>;
  update(
    docId: string,
    input: AkbDocumentUpdateInput,
    options?: AkbDocumentVaultOptions,
  ): Promise<AkbResult<import("./core/schema.gen.js").AkbDocumentWriteEnvelope>>;
  delete(
    docId: string,
    options?: AkbDocumentVaultOptions,
  ): Promise<AkbResult<import("./core/schema.gen.js").AkbDocumentEnvelope>>;
  browse(
    options?: AkbDocumentBrowseOptions,
  ): Promise<AkbResult<import("./core/schema.gen.js").AkbDocumentEnvelope>>;
}

export interface AkbActivityFacade extends AkbNamespaceStub {
  list(
    options?: AkbActivityListOptions,
  ): Promise<AkbResult<import("./core/schema.gen.js").AkbActivityEnvelope>>;
  recent(
    options?: AkbActivityRecentOptions,
  ): Promise<AkbResult<import("./core/schema.gen.js").AkbRecentChangesEnvelope>>;
}

export interface AkbStorageFacade extends AkbNamespaceStub {
  presignUpload(
    path: string,
    options?: AkbStoragePresignUploadOptions,
  ): Promise<AkbResult<import("./core/schema.gen.js").AkbFileEnvelope>>;
  upload(
    path: string,
    file: BodyInit,
    options?: AkbStorageUploadOptions,
  ): Promise<AkbResult<import("./core/schema.gen.js").AkbFileEnvelope>>;
  confirm(
    fileRef: string,
    options?: AkbStorageConfirmOptions,
  ): Promise<AkbResult<import("./core/schema.gen.js").AkbFileEnvelope>>;
  download(
    fileRef: string,
    options?: AkbStorageDownloadUrlOptions,
  ): Promise<AkbResult<import("./core/schema.gen.js").AkbFileEnvelope>>;
  download(
    fileRef: string,
    options: AkbStorageDownloadBytesOptions,
  ): Promise<AkbResult<AkbStorageDownload>>;
  list(
    options?: AkbStorageListOptions,
  ): Promise<AkbResult<import("./core/schema.gen.js").AkbFileEnvelope>>;
  delete(
    fileRef: string,
    options?: AkbStorageRefOptions,
  ): Promise<AkbResult<import("./core/schema.gen.js").AkbFileEnvelope>>;
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
  readonly search: AkbSearchFacade;
  readonly graph: AkbGraphFacade;
  readonly docs: AkbDocsFacade;
  readonly activity: AkbActivityFacade;
  readonly storage: AkbStorageFacade;
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
    search: makeSearchFacade(request, scope.defaultVault),
    graph: makeGraphFacade(request, scope.defaultVault),
    docs: makeDocsFacade(request, scope.defaultVault),
    activity: makeActivityFacade(request, scope.defaultVault),
    storage: makeStorageFacade(request, scope.defaultVault, fetchImpl),
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

function makeGraphFacade(
  request: AkbClient["request"],
  defaultVault: string | null,
): AkbGraphFacade {
  const rawRequest = <T = AkbSuccessEnvelope>(
    path: string | URL = "",
    init: RequestInit = {},
  ) => request<T>(joinPath("/graph", path), init);
  const facade = {
    name: "graph",
    request: rawRequest,
    neighbors(uri: string, options: AkbGraphNeighborsOptions = {}) {
      const params = new URLSearchParams({ uri });
      appendOptional(params, "hops", options.hops);
      appendOptional(params, "limit", options.limit);
      return request<import("./core/schema.gen.js").AkbGraphNeighborsEnvelope>(`/graph?${params}`);
    },
    overview(options: AkbGraphOverviewOptions = {}) {
      const vault = resolveGraphVault(defaultVault, "overview");
      const params = new URLSearchParams({ vault });
      appendOptional(params, "top_k", options.topK);
      return request<import("./core/schema.gen.js").AkbGraphOverviewEnvelope>(`/graph/overview?${params}`);
    },
    health(options: AkbGraphHealthOptions = {}) {
      const vault = resolveGraphVault(defaultVault, "health");
      const params = new URLSearchParams({ vault });
      appendOptional(params, "hub_threshold", options.hubThreshold);
      appendOptional(params, "limit", options.limit);
      return request<import("./core/schema.gen.js").AkbGraphHealthEnvelope>(`/graph/health?${params}`);
    },
    relations(uri: string, options: AkbGraphRelationsOptions = {}) {
      const params = new URLSearchParams({ uri });
      appendOptional(params, "direction", options.direction);
      appendOptional(params, "type", options.type);
      return request<import("./core/schema.gen.js").AkbRelationsEnvelope>(`/relations?${params}`);
    },
    link(input: AkbGraphLinkInput) {
      const body = {
        source: input.source,
        target: input.target,
        relation: input.relation,
        ...(input.metadata === undefined ? {} : { metadata: input.metadata }),
      };
      return request<import("./core/schema.gen.js").AkbRelationLinkEnvelope>("/relations", {
        method: "POST",
        body: JSON.stringify(body),
      });
    },
    unlink(input: AkbGraphUnlinkInput) {
      const params = new URLSearchParams({ source: input.source, target: input.target });
      appendOptional(params, "relation", input.relation);
      return request<import("./core/schema.gen.js").AkbRelationUnlinkEnvelope>(`/relations?${params}`, {
        method: "DELETE",
      });
    },
    provenance(uri: string) {
      const params = new URLSearchParams({ uri });
      return request<import("./core/schema.gen.js").AkbProvenanceEnvelope>(`/provenance?${params}`);
    },
  } satisfies AkbGraphFacade;
  return Object.freeze(facade);
}

function makeDocsFacade(
  request: AkbClient["request"],
  defaultVault: string | null,
): AkbDocsFacade {
  const rawRequest = <T = AkbSuccessEnvelope>(path: string | URL = "", init: RequestInit = {}) => {
    return request<T>(joinPath("/documents", path), init);
  };
  const facade = {
    name: "docs",
    request: rawRequest,
    createCollection(input: AkbCreateCollectionInput) {
      const vault = resolveDocumentVault(defaultVault);
      return request<import("./core/schema.gen.js").AkbCollectionCreateEnvelope>(
        `/collections/${encodePathSegment(vault)}`,
        {
          method: "POST",
          body: JSON.stringify(omitUndefined({ path: input.path, summary: input.summary })),
        },
      );
    },
    deleteCollection(path: string, options: AkbDeleteCollectionOptions = {}) {
      const vault = resolveDocumentVault(defaultVault);
      const query = options.recursive === true ? "?recursive=true" : "";
      return request<import("./core/schema.gen.js").AkbCollectionDeleteEnvelope>(
        `/collections/${encodePathSegment(vault)}/${encodeCollectionPath(path)}${query}`,
        { method: "DELETE" },
      );
    },
    get(docId: string, options: AkbDocumentGetOptions = {}) {
      const vault = resolveDocumentVault(options.vault ?? defaultVault);
      const params = new URLSearchParams();
      appendOptional(params, "version", options.version);
      const query = params.size > 0 ? `?${params}` : "";
      return request<import("./core/schema.gen.js").AkbDocumentEnvelope>(
        `/documents/${encodePathSegment(vault)}/${encodeDocumentPath(docId)}${query}`,
      );
    },
    history(docId: string, options: AkbDocumentHistoryOptions = {}) {
      const vault = resolveDocumentVault(options.vault ?? defaultVault);
      const params = new URLSearchParams();
      appendOptional(params, "limit", options.limit);
      const query = params.size > 0 ? `?${params}` : "";
      return request<import("./core/schema.gen.js").AkbDocumentHistoryEnvelope>(
        `/history/${encodePathSegment(vault)}/${encodeDocumentPath(docId)}${query}`,
      );
    },
    diff(docId: string, options: AkbDocumentDiffOptions) {
      const vault = resolveDocumentVault(options.vault ?? defaultVault);
      const params = new URLSearchParams({ commit: options.commit });
      return request<import("./core/schema.gen.js").AkbDocumentDiffEnvelope>(
        `/diff/${encodePathSegment(vault)}/${encodeDocumentPath(docId)}?${params}`,
      );
    },
    put(input: AkbDocumentPutInput) {
      const payload = documentPutPayload(input, defaultVault);
      return request<import("./core/schema.gen.js").AkbDocumentWriteEnvelope>("/documents", {
        method: "POST",
        body: JSON.stringify(payload),
      });
    },
    update(docId: string, input: AkbDocumentUpdateInput, options: AkbDocumentVaultOptions = {}) {
      const vault = resolveDocumentVault(options.vault ?? defaultVault);
      return request<import("./core/schema.gen.js").AkbDocumentWriteEnvelope>(
        `/documents/${encodePathSegment(vault)}/${encodeDocumentPath(docId)}`,
        {
          method: "PATCH",
          body: JSON.stringify(documentUpdatePayload(input)),
        },
      );
    },
    delete(docId: string, options: AkbDocumentVaultOptions = {}) {
      const vault = resolveDocumentVault(options.vault ?? defaultVault);
      return request<import("./core/schema.gen.js").AkbDocumentEnvelope>(
        `/documents/${encodePathSegment(vault)}/${encodeDocumentPath(docId)}`,
        { method: "DELETE" },
      );
    },
    browse(options: AkbDocumentBrowseOptions = {}) {
      const vault = resolveDocumentVault(options.vault ?? defaultVault);
      const params = new URLSearchParams();
      appendOptional(params, "collection", options.collection);
      appendOptional(params, "depth", options.depth);
      appendOptional(params, "include_hashes", options.includeHashes);
      appendOptional(params, "include_archived", options.includeArchived);
      const query = params.size > 0 ? `?${params}` : "";
      return request<import("./core/schema.gen.js").AkbDocumentEnvelope>(
        `/browse/${encodePathSegment(vault)}${query}`,
      );
    },
  } satisfies AkbDocsFacade;
  return Object.freeze(facade);
}

function makeActivityFacade(
  request: AkbClient["request"],
  defaultVault: string | null,
): AkbActivityFacade {
  const rawRequest = <T = AkbSuccessEnvelope>(path: string | URL = "", init: RequestInit = {}) => {
    return request<T>(joinPath("/activity", path), init);
  };
  const facade = {
    name: "activity",
    request: rawRequest,
    list(options: AkbActivityListOptions = {}) {
      const vault = resolveActivityVault(options.vault ?? defaultVault);
      const params = new URLSearchParams();
      appendOptional(params, "collection", options.collection);
      appendOptional(params, "author", options.author);
      appendOptional(params, "since", options.since);
      appendOptional(params, "limit", options.limit);
      const query = params.size > 0 ? `?${params}` : "";
      return request<import("./core/schema.gen.js").AkbActivityEnvelope>(
        `/activity/${encodePathSegment(vault)}${query}`,
      );
    },
    recent(options: AkbActivityRecentOptions = {}) {
      const params = new URLSearchParams();
      appendOptional(params, "vault", options.vault ?? defaultVault);
      appendOptional(params, "limit", options.limit);
      const query = params.size > 0 ? `?${params}` : "";
      return request<import("./core/schema.gen.js").AkbRecentChangesEnvelope>(`/recent${query}`);
    },
  } satisfies AkbActivityFacade;
  return Object.freeze(facade);
}

function makeStorageFacade(
  request: AkbClient["request"],
  defaultVault: string | null,
  fetchImpl: typeof fetch,
): AkbStorageFacade {
  const rawRequest = <T = AkbSuccessEnvelope>(path: string | URL = "", init: RequestInit = {}) => {
    return request<T>(joinPath("/files", path), init);
  };

  const presignUpload = (
    path: string,
    options: AkbStoragePresignUploadOptions = {},
  ): Promise<AkbResult<import("./core/schema.gen.js").AkbFileEnvelope>> => {
    const vault = resolveStorageVault(options.vault ?? defaultVault);
    const storagePath = splitStoragePath(path, options.collection);
    const params = new URLSearchParams({ filename: storagePath.filename });
    appendOptional(params, "collection", storagePath.collection);
    appendOptional(params, "description", options.description);
    appendOptional(params, "mime_type", options.mimeType ?? "application/octet-stream");
    appendOptional(params, "content_hash", options.contentHash);
    return request<import("./core/schema.gen.js").AkbFileEnvelope>(
      `/files/${encodePathSegment(vault)}/upload?${params}`,
      { method: "POST" },
    );
  };

  const confirm = async (
    fileRef: string,
    options: AkbStorageConfirmOptions = {},
  ): Promise<AkbResult<import("./core/schema.gen.js").AkbFileEnvelope>> => {
    const vault = resolveStorageVault(options.vault ?? defaultVault);
    const fileId = storageFileIdFromRef(fileRef);
    if (!fileId) return fileRefNotFound(fileRef);
    const params = new URLSearchParams();
    appendOptional(params, "content_hash", options.contentHash);
    appendOptional(params, "hash_algorithm", options.hashAlgorithm);
    const query = params.size > 0 ? `?${params}` : "";
    return request<import("./core/schema.gen.js").AkbFileEnvelope>(
      `/files/${encodePathSegment(vault)}/${encodePathSegment(fileId)}/confirm${query}`,
      { method: "POST" },
    );
  };

  function download(
    fileRef: string,
    options?: AkbStorageDownloadUrlOptions,
  ): Promise<AkbResult<import("./core/schema.gen.js").AkbFileEnvelope>>;
  function download(
    fileRef: string,
    options: AkbStorageDownloadBytesOptions,
  ): Promise<AkbResult<AkbStorageDownload>>;
  async function download(
    fileRef: string,
    options: AkbStorageDownloadUrlOptions | AkbStorageDownloadBytesOptions = {},
  ): Promise<AkbResult<import("./core/schema.gen.js").AkbFileEnvelope | AkbStorageDownload>> {
    const vault = resolveStorageVault(options.vault ?? defaultVault);
    const resolved = await resolveStorageFileRef(request, vault, fileRef, options.lookupLimit);
    if (resolved.error) return resolved.error;
    const file = await request<import("./core/schema.gen.js").AkbFileEnvelope>(
      `/files/${encodePathSegment(vault)}/${encodePathSegment(resolved.fileId)}/download`,
    );
    if (file.error || !options.bytes) return file;
    if (!file.data) {
      return errorResult<import("./core/schema.gen.js").AkbFileEnvelope | AkbStorageDownload>(
        "File metadata was not returned for the download.",
        "missing_file_metadata",
      );
    }
    const downloadUrl = file.data.download_url;
    if (!downloadUrl) {
      return errorResult<import("./core/schema.gen.js").AkbFileEnvelope | AkbStorageDownload>(
        "Download URL was not returned for the file.",
        "missing_download_url",
      );
    }
    const response = await fetchImpl(downloadUrl, options.fetchInit);
    if (!response.ok) {
      return makeResult<import("./core/schema.gen.js").AkbFileEnvelope | AkbStorageDownload>(
        null,
        new AkbError(
          { message: `Storage download failed: ${response.status} ${response.statusText}`, code: "storage_download_failed" },
          response,
        ),
        response,
      );
    }
    const bytes = await response.arrayBuffer();
    return makeResult({ kind: "file_download", file: file.data, bytes }, null, response);
  }

  const facade = {
    name: "storage",
    request: rawRequest,
    presignUpload,
    async upload(path: string, file: BodyInit, options: AkbStorageUploadOptions = {}) {
      const mimeType = storageMimeType(file, options.mimeType);
      const presigned = await presignUpload(path, { ...options, mimeType });
      if (presigned.error) return presigned;
      const uploadUrl = presigned.data?.upload_url;
      if (!uploadUrl) {
        return errorResult("Upload URL was not returned for the file.", "missing_upload_url");
      }
      const response = await fetchImpl(uploadUrl, {
        method: "PUT",
        body: file,
        headers: storageUploadHeaders(mimeType, options.headers),
      });
      if (!response.ok) {
        return makeResult(
          null,
          new AkbError(
            { message: `Storage upload failed: ${response.status} ${response.statusText}`, code: "storage_upload_failed" },
            response,
          ),
          response,
        );
      }
      if (options.confirm === false) return presigned;
      const fileId = storageFileIdFromRef(presigned.data?.uri ?? presigned.data?.id ?? "");
      if (!fileId) return errorResult("File id was not returned for the upload.", "missing_file_id");
      return confirm(fileId, {
        vault: options.vault,
        contentHash: options.contentHash,
        hashAlgorithm: options.hashAlgorithm,
      });
    },
    confirm,
    download,
    list(options: AkbStorageListOptions = {}) {
      const vault = resolveStorageVault(options.vault ?? defaultVault);
      const params = new URLSearchParams();
      appendOptional(params, "collection", options.collection);
      appendOptional(params, "limit", options.limit);
      const query = params.size > 0 ? `?${params}` : "";
      return request<import("./core/schema.gen.js").AkbFileEnvelope>(
        `/files/${encodePathSegment(vault)}${query}`,
      );
    },
    async delete(fileRef: string, options: AkbStorageRefOptions = {}) {
      const vault = resolveStorageVault(options.vault ?? defaultVault);
      const resolved = await resolveStorageFileRef(request, vault, fileRef, options.lookupLimit);
      if (resolved.error) return resolved.error;
      return request<import("./core/schema.gen.js").AkbFileEnvelope>(
        `/files/${encodePathSegment(vault)}/${encodePathSegment(resolved.fileId)}`,
        { method: "DELETE" },
      );
    },
  } as AkbStorageFacade;
  return Object.freeze(facade);
}

function makeSearchFacade(
  request: AkbClient["request"],
  defaultVault: string | null,
): AkbSearchFacade {
  const rawRequest = <T = AkbSuccessEnvelope>(path: string | URL = "", init: RequestInit = {}) => {
    return request(joinPath("/search", path), init);
  };
  const facade = (async function search(query: string, options: AkbSearchOptions = {}) {
    const params = new URLSearchParams({ q: query });
    appendSearchScope(params, options.vault ?? defaultVault);
    appendOptional(params, "mode", options.mode);
    appendOptional(params, "rerank", options.rerank);
    appendOptional(params, "collection", options.collection);
    appendOptional(params, "type", options.type);
    appendRepeated(params, "tags", options.tags);
    appendOptional(params, "limit", options.limit);
    appendOptional(params, "include_archived", options.includeArchived);
    appendRepeated(params, "source_uris", options.sourceUris);
    return request(`/search?${params}`);
  }) as AkbSearchFacade;

  Object.defineProperties(facade, {
    request: {
      value: rawRequest,
      enumerable: true,
    },
    drillDown: {
      value(uri: string, options: AkbDrillDownOptions = {}) {
        const params = new URLSearchParams({ uri });
        appendOptional(params, "section", options.section);
        return request(`/drill-down?${params}`);
      },
      enumerable: true,
    },
    grep: {
      value(pattern: string, options: AkbGrepOptions = {}) {
        const params = new URLSearchParams({ q: pattern });
        appendSearchScope(params, options.vault ?? defaultVault);
        appendOptional(params, "collection", options.collection);
        appendOptional(params, "regex", options.regex);
        appendOptional(params, "case_sensitive", options.caseSensitive);
        appendOptional(params, "limit", options.limit);
        appendOptional(params, "count_only", options.countOnly);
        appendOptional(params, "files_with_matches", options.filesWithMatches);
        return request(`/grep?${params}`);
      },
      enumerable: true,
    },
  });
  return Object.freeze(facade);
}

function appendSearchScope(
  params: URLSearchParams,
  vault: string | readonly string[] | null | undefined,
): void {
  appendRepeated(params, "vault", typeof vault === "string" ? [vault] : vault);
}

function appendRepeated(
  params: URLSearchParams,
  key: string,
  values: readonly string[] | null | undefined,
): void {
  for (const value of values ?? []) {
    if (value) params.append(key, value);
  }
}

function appendOptional(
  params: URLSearchParams,
  key: string,
  value: string | number | boolean | null | undefined,
): void {
  if (value === null || value === undefined) return;
  params.set(key, String(value));
}

function resolveDocumentVault(vault: string | null | undefined): string {
  if (typeof vault === "string" && vault.length > 0) return vault;
  throw new TypeError("Select a vault before using documents: client.vault(\"...\").docs.");
}

function resolveStorageVault(vault: string | null | undefined): string {
  if (typeof vault === "string" && vault.length > 0) return vault;
  throw new TypeError("Select a vault before using storage: client.vault(\"...\").storage.");
}

function resolveActivityVault(vault: string | null | undefined): string {
  if (typeof vault === "string" && vault.length > 0) return vault;
  throw new TypeError("Select a vault before listing activity: client.vault(\"...\").activity.list().");
}

function resolveGraphVault(vault: string | null | undefined, operation: "overview" | "health"): string {
  if (typeof vault === "string" && vault.length > 0) return vault;
  throw new TypeError(`Select a vault before using graph ${operation}: client.vault("...").graph.${operation}().`);
}

function encodePathSegment(value: string): string {
  return encodeURIComponent(value);
}

function encodeDocumentPath(path: string): string {
  const cleaned = path.replace(/^\/+/, "");
  if (!cleaned) {
    throw new TypeError("Document path must be a non-empty string.");
  }
  return cleaned.split("/").map(encodePathSegment).join("/");
}

function encodeCollectionPath(path: string): string {
  const segments = path.split("/");
  if (segments.some((segment) => segment === "." || segment === "..")) {
    throw new TypeError("Collection path must not contain URL dot segments ('.' or '..').");
  }
  return segments.map(encodePathSegment).join("/");
}

function documentPutPayload(
  input: AkbDocumentPutInput,
  defaultVault: string | null,
): Record<string, unknown> {
  const vault = resolveDocumentVault(input.vault ?? defaultVault);
  return omitUndefined({
    vault,
    collection: input.collection,
    title: input.title,
    content: input.content,
    type: input.type,
    status: input.status,
    tags: input.tags ? Array.from(input.tags) : undefined,
    domain: input.domain,
    summary: input.summary,
    depends_on: input.dependsOn ? Array.from(input.dependsOn) : undefined,
    related_to: input.relatedTo ? Array.from(input.relatedTo) : undefined,
    slug: input.slug,
  });
}

function documentUpdatePayload(input: AkbDocumentUpdateInput): Record<string, unknown> {
  return omitUndefined({
    content: input.content,
    title: input.title,
    type: input.type,
    status: input.status,
    tags: input.tags ? Array.from(input.tags) : input.tags,
    domain: input.domain,
    summary: input.summary,
    depends_on: input.dependsOn ? Array.from(input.dependsOn) : input.dependsOn,
    related_to: input.relatedTo ? Array.from(input.relatedTo) : input.relatedTo,
    message: input.message,
    expected_commit: input.expectedCommit,
    expected_content_hash: input.expectedContentHash,
  });
}

function omitUndefined(values: Record<string, unknown>): Record<string, unknown> {
  return Object.fromEntries(Object.entries(values).filter(([, value]) => value !== undefined));
}

function splitStoragePath(
  path: string,
  collectionOverride: string | null | undefined,
): { collection: string; filename: string } {
  const cleaned = path.replace(/^\/+|\/+$/g, "");
  if (!cleaned) throw new TypeError("Storage path must be a non-empty string.");
  const parts = cleaned.split("/").filter(Boolean);
  const filename = parts.pop();
  if (!filename) throw new TypeError("Storage path must include a filename.");
  const collection = collectionOverride === undefined ? parts.join("/") : (collectionOverride ?? "");
  return { collection, filename };
}

function storageMimeType(file: BodyInit, explicit: string | null | undefined): string {
  if (explicit) return explicit;
  if (typeof Blob !== "undefined" && file instanceof Blob && file.type) return file.type;
  return "application/octet-stream";
}

function storageUploadHeaders(mimeType: string, headers: HeadersInit | undefined): Headers {
  const out = new Headers(headers);
  if (!out.has("content-type")) out.set("content-type", mimeType);
  return out;
}

function storageFileIdFromRef(ref: string | null | undefined): string | null {
  if (!ref) return null;
  const fileUriMatch = ref.match(/^akb:\/\/[^/]+\/(?:coll\/.+\/)?file\/([^/?#]+)$/iu);
  const uriFileId = fileUriMatch?.[1] ? decodeURIComponent(fileUriMatch[1]) : null;
  if (uriFileId && isUuid(uriFileId)) return uriFileId;
  return isUuid(ref) ? ref : null;
}

function isUuid(value: string): boolean {
  return /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/iu.test(value);
}

async function resolveStorageFileRef(
  request: AkbClient["request"],
  vault: string,
  fileRef: string,
  lookupLimit: number | undefined,
): Promise<{ fileId: string; error: null } | { fileId: null; error: AkbResult<import("./core/schema.gen.js").AkbFileEnvelope> }> {
  const direct = storageFileIdFromRef(fileRef);
  if (direct) return { fileId: direct, error: null };

  const storagePath = splitStoragePath(fileRef, undefined);
  const params = new URLSearchParams();
  appendOptional(params, "collection", storagePath.collection);
  appendOptional(params, "limit", lookupLimit ?? 200);
  const listed = await request<import("./core/schema.gen.js").AkbFileEnvelope>(
    `/files/${encodePathSegment(vault)}?${params}`,
  );
  if (listed.error) return { fileId: null, error: listed };

  const matches = (listed.data?.items ?? []).filter((candidate) => {
    return stringField(candidate, "name") === storagePath.filename
      || stringField(candidate, "path") === fileRef
      || stringField(candidate, "uri") === fileRef;
  });
  if (matches.length > 1) {
    return {
      fileId: null,
      error: errorResult(
        `File reference is ambiguous: ${fileRef}`,
        "ambiguous_file_ref",
        { ref: fileRef, matches: matches.map((item) => stringField(item, "uri") ?? stringField(item, "id")) },
      ),
    };
  }
  const item = matches[0];
  const fileId = storageFileIdFromRef(stringField(item, "uri")) ?? stringField(item, "id");
  if (!fileId) {
    return { fileId: null, error: fileRefNotFound(fileRef) };
  }
  return { fileId, error: null };
}

function stringField(value: unknown, key: string): string | null {
  if (!value || typeof value !== "object") return null;
  const field = (value as Record<string, unknown>)[key];
  return typeof field === "string" && field.length > 0 ? field : null;
}

function fileRefNotFound(ref: string): AkbResult<import("./core/schema.gen.js").AkbFileEnvelope> {
  return errorResult(`File reference could not be resolved: ${ref}`, "file_not_found", { ref });
}

function errorResult<T>(
  message: string,
  code: string,
  details?: unknown,
): AkbResult<T> {
  return makeResult<T>(null, new AkbError({ message, code, details }), null);
}

function joinPath(prefix: string, path: string | URL): string {
  const suffix = String(path);
  if (!suffix) return prefix;
  return `${prefix}${suffix.startsWith("/") ? suffix : `/${suffix}`}`;
}
